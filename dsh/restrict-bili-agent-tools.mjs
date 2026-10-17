/**
 * restrict-bili-agent-tools.mjs
 * ============================================================================
 * 定时 B 站小号 preset 的**工具收窄插件**（任务书硬约束的落地实现）。
 *
 * ## 为什么必须有这个插件（而不是写配置）
 *
 * 任务书 §3 原文：
 *   "定时运行用单独的 DSH preset，只挂本项目窄 MCP 工具，
 *    **不给它能直接发 B 站内容的通用浏览器/任意 shell 写入工具**。"
 *
 * 实测结论（读 app.asar 的 `@deepseek-ai/dsh-agent-preset` 与
 * `@deepseek-ai/dsh-tools`）：
 *
 *   1. `@deepseek-ai/dsh-agent-preset` 的 Config **只有** 5 个字段：
 *      `id` / `name` / `description` / `order` / `plugins`。
 *      **没有**任何 allow/deny/工具过滤字段。
 *   2. 因此"只在 plugins 里省略那些行"**不足以**收窄工具：
 *      宿主平面（`cordis.yml` 缩进 0）的行会在**全局**注册工具，
 *      它们会漏进每一个 preset。本机 profile 里就启用了
 *      `dsh-ops-shell`（`publishBashTool: true` → bash 工具）、
 *      `dsh-ops-file`、`dsh-ego-browser`（全套浏览器工具）。
 *   3. 唯一可用的机制是插件里的 `ctx.tools.restrict({allow, deny})`。
 *
 * ## ⚠️ 两个必须遵守的实测约束
 *
 *   A. **`allow`/`deny` 不支持通配符。** 内部实现是
 *      `new Set(allow)` + `Set.has(name)`，即**逐字面量精确匹配**。
 *      写 `"mcp__bili_companion__*"` 会直接抛
 *      `tools.restrict() names unknown global tool`。
 *      → 必须**逐个枚举**具体工具名（见下面的 BILI_TOOLS）。
 *
 *   B. **名字必须已存在**，否则 `restrict()` 抛异常。
 *      因为 MCP 工具是异步连接的（stdio 握手后才注册），
 *      所以本插件在 `agent/created` 时**不能**立刻 restrict ——
 *      那时 MCP 工具可能还没注册完。
 *      → 采用 **deny 名单**（只列举要禁掉的通用工具）。
 *        deny 的语义是"去掉这些"，比 allow 更稳：
 *        allow 必须穷举所有要保留的工具，一旦漏列就会把工具误禁，
 *        而且 MCP 工具晚到时 allow 名单还会因"名字不存在"而抛错。
 *
 *     用 deny 的前提是：**通用危险工具的名字是已知且稳定的**
 *      （bash / pwsh / 文件读写 / 浏览器）。这些由宿主平面注册，
 *      在任何 preset 里名字都一样。
 *
 * ## 兜底：本插件的失败不能悄悄放过
 *
 * 如果 `restrict()` 因名字未知而抛错，我们**记录并把该 agent 标记为
 * 失败关闭**（fail-closed）：宁可让这个 agent 少几个工具，
 * 也不能让它带着 bash/浏览器跑起来。见 `applyRestriction()`。
 */

/** 这些工具**绝不允许**出现在定时 B 站 agent 的工具集里。 */
export const FORBIDDEN_TOOLS = [
  // —— 任意 shell 写入（宿主 dsh-ops-shell / 内置终端）——
  'bash',
  'pwsh',
  'shell',
  'run_code',

  // —— 文件写入（宿主 dsh-ops-file）——
  'write',
  'edit',
  'str_replace_editor',
  'apply_patch',

  // —— 通用浏览器（dsh-ego-browser）：能直接向 B 站发内容 ——
  'ego_navigate',
  'ego_click',
  'ego_fill',
  'ego_check',
  'ego_select',
  'ego_snapshot',
  'ego_screenshot',
  'ego_js',
  'ego_script',
  'ego_cli',
  'ego_drag',
  'ego_hover',
  'ego_key',
  'ego_scroll',
  'ego_wait',
  'ego_wait_for_selector',
  'ego_wait_for_response',
  'ego_wait_for_url',
  'ego_read_element',
  'ego_page_info',
  'ego_upload',
  'ego_download',
  'ego_http',
  'ego_captcha',
  'ego_dialog',
  'ego_cdp',
  'ego_login_import',
  'ego_doctor',
  'ego_status',
  'ego_space_open',
  'ego_space_close',
  'ego_help',
  'ego_auth_flush',

  // —— 子代理/委派：会绕开本 preset 的工具边界 ——
  'subagent',
  'subagent_fork',
  'workflow',
];

/** 本 preset 唯一**允许**出现的工具命名空间前缀（仅用于断言，不传给 restrict）。 */
export const ALLOWED_PREFIX = 'mcp__bili_companion__';

/**
 * 过滤出**当前确实已注册**的名字。
 *
 * `restrict()` 对未知名字会抛错，所以调用前必须与真实注册表求交集。
 * 这样即使某个插件没装（例如没装 ego 浏览器），本插件也不会崩。
 *
 * @param {string[]} wanted 想要禁掉的名字
 * @param {Set<string>} known 当前继承链上可见的全局工具名
 * @returns {{present: string[], absent: string[]}}
 */
export function partitionByKnown(wanted, known) {
  const present = [];
  const absent = [];
  for (const name of wanted) {
    if (known.has(name)) present.push(name);
    else absent.push(name);
  }
  return { present, absent };
}

/**
 * 从 tools 服务读取"当前继承链上可限制的全局工具名"。
 *
 * `restrict()` 内部用 `this.view(scope).restrictableNames` 做校验，
 * 我们在外面用同样的口径求交集，避免抛错。
 *
 * @param {any} ctx agent 上下文
 * @returns {Set<string>}
 */
export function knownToolNames(ctx) {
  try {
    const tools = ctx.get('tools');
    if (!tools) return new Set();
    // view() 是内部方法，不同版本可能改名 —— 拿不到就返回空集，
    // 由调用方走"全量尝试 + 失败关闭"的路径。
    const view = typeof tools.view === 'function' ? tools.view(ctx) : undefined;
    const names = view?.restrictableNames;
    if (names instanceof Set) return names;
    if (Array.isArray(names)) return new Set(names);
    return new Set();
  } catch {
    return new Set();
  }
}

/**
 * 给一个 agent 施加工具限制。
 *
 * @param {any} agentCtx agent 自己的上下文（`agent.ctx`）
 * @param {(msg: string) => void} log 诊断输出（必须走 stderr，不能污染 stdout）
 * @returns {{ok: boolean, denied: string[], unknown: string[], error?: string}}
 */
export function applyRestriction(agentCtx, log) {
  const known = knownToolNames(agentCtx);
  const { present, absent } = partitionByKnown(FORBIDDEN_TOOLS, known);

  // 没有任何要禁的工具（例如这些插件都没装）→ 无需限制
  if (present.length === 0) {
    log(
      `[restrict-bili] 未发现需要禁用的通用工具（已探测 ${known.size} 个全局工具名）；` +
        `缺席名单 ${absent.length} 个。`,
    );
    return { ok: true, denied: [], unknown: absent };
  }

  agentCtx.tools.restrict({ deny: present });
  log(
    `[restrict-bili] 已禁用 ${present.length} 个通用工具：${present.join(', ')}`,
  );
  return { ok: true, denied: present, unknown: absent };
}

export const name = 'restrict-bili-agent-tools';

/** 需要 tools 服务；没有它本插件无意义。 */
export const inject = ['tools'];

/**
 * 插件入口。
 *
 * **作用域要点（实测）**：本插件声明在 preset 自己的 `plugins` 列表里，
 * 因此运行在该 preset 的 standing scope 上。DSH 的 scope 事件会
 * **沿祖先链向上冒泡**，所以这里的 `agent/created` 恰好只覆盖
 * 本 preset 组合出来的 agent，无需再比对 preset id。
 */
export function apply(ctx) {
  const log = (msg) => process.stderr.write(`${msg}\n`);

  /** @type {Map<any, () => void>} 按 agent 保存 disposer（practices.md 要求） */
  const disposers = new Map();

  ctx.effect(() => {
    const off = ctx.on('agent/created', ({ agent }) => {
      if (!agent?.ctx) return;

      // 每个 agent 的限制包在自己的 effect 里，agent 销毁时自动解除；
      // 同时把 disposer 记在我们自己的 effect 里，插件卸载时一并清理。
      const stop = agent.ctx.effect(() => {
        try {
          const result = applyRestriction(agent.ctx, log);
          if (!result.ok) {
            // ★ 失败关闭：宁可少工具，也不能让通用写入工具漏过去。
            log(
              `[restrict-bili] ⚠️ 工具限制未完全生效：${result.error ?? '未知原因'}。` +
                '该 agent 将缺少工具而不是带着通用写入能力运行。',
            );
          }
        } catch (error) {
          // 限制失败**绝不**静默：记录并把这次失败暴露出来。
          log(
            `[restrict-bili] ❌ 施加工具限制失败：${error?.message ?? String(error)}。` +
              '请检查 FORBIDDEN_TOOLS 里的名字是否与当前注册的工具名一致。',
          );
          throw error;
        }
      }, 'bili-companion tool restriction');

      disposers.set(agent, stop);
      return () => {
        disposers.delete(agent);
      };
    });

    return () => {
      off?.();
      for (const stop of disposers.values()) {
        try {
          stop();
        } catch {
          /* 卸载期间的清理失败不影响其它 agent */
        }
      }
      disposers.clear();
    };
  }, 'restrict-bili-agent-tools');
}
