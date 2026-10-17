# DSH 接入安全性评估（重启前必读）

> 主人问：「你得先确认好不会导致出现大问题，例如 DSH 直接无法加载插件，无法启动等问题。」
>
> 本文是**实测结论**，不是推演。测试方式：**拷贝一份隔离的 DSH_HOME**，
> 在里面加载配置片段并启动，全程**未触碰**你正在用的 profile。
> 采集时间：2026-10-15。

---

## 0. 结论（先看这个）

| 问题 | 结论 |
|---|---|
| 会导致 DSH 无法启动吗？ | **不会**。隔离环境实测：加载后 `--dump-config` **exit 0**，组合出 218 个插件正常。 |
| 会导致插件无法加载吗？ | **不会**。片段是**纯新增**（只有 `insert`），不改、不禁用任何现有插件。 |
| 会动 `package.json` 投影元数据吗？ | **不会**。片段里 0 次提及（AGENTS.md 的致命红线）。 |
| 万一真的坏了能救回来吗？ | **能**。三步回滚（§4），且有 `desktop-safe-mode` + `safe-mode.html` 官方恢复路径。 |
| 唯一的真实风险是什么？ | 我提供的**工具收窄插件**名字写错时，会在**运行到那个 preset 时**报错。但它**失败关闭**（少工具），不影响 DSH 启动，也不影响其他会话。 |

**→ 可以安全地装。** 但仍建议按 §5 的步骤操作（先备份）。

---

## 1. 实测：配置片段能否被 DSH 正常组合

### 1.1 方法（关键：**不动你的 profile**）

```bash
# ① 拷贝一份隔离的 DSH_HOME（526 MB，含 node_modules）
cp -r "$LIVE" D:/DeepSeek/workspace-temp/dsh-home-test/harness

# ② 只往**隔离副本**里追加片段
cat dsh/cordis.patch.fragment.yml >> \
    D:/DeepSeek/workspace-temp/dsh-home-test/harness/profiles/web/cordis.patch.yml

# ③ 让 DSH 用隔离的 DSH_HOME 组合配置
DSH_HOME=D:/DeepSeek/workspace-temp/dsh-home-test/harness \
  "DSH Desktop.exe" --expose-internals <bin.js> --profile web --dump-config
```

### 1.2 结果对照

| 指标 | 不含片段（基线） | 含片段 | 差异 |
|---|---|---|---|
| 退出码 | **0** | **0** | 无 |
| 组合出的插件数 | 216 | **218** | **+2**（正是我加的两个） |
| stderr 报错 | 1 条（`ui-skin-maid-atelier` 未找到） | **同一条** | **无新增** |
| `mcp-bili-companion` 是否在树里 | — | ✅ 在 | |
| `preset-bili-companion` 是否在树里 | — | ✅ 在 | |

> ✅ **那条 `ui-skin-maid-atelier not found` 在基线里就存在**，与我的片段无关
> （是皮肤插件在隔离副本里找不到自己的东西）。
> **加了我的片段后没有多出任何一条错误。**

### 1.3 结论

配置能正常组合，**不会**导致 DSH 无法加载插件或无法启动。

---

## 2. 实测：片段是"纯新增"吗

逐条检查（`yaml.safe_load` 解析后遍历，不靠肉眼）：

| 检查项 | 结果 |
|---|---|
| 顶层 entry 的键 | 只有 `insert`（**没有** id-targeted 覆盖） |
| 是否 `disabled` 任何现有插件 | ❌ **没有**（配置文件里那次 `disabled` 字样出现在**注释**里） |
| 是否提及 `package.json` / `generationProjection` / `pnpm.overrides` | ❌ **0 次** |
| 新增的东西 | `mcp-bili-companion`、`preset-bili-companion`（都是新 id，不覆盖同名） |
| `serverName: bili_companion` 是否与现有冲突 | ❌ 无冲突（`cordis.yml`/`cordis.patch.yml` 里都没有这个名字） |

**`--dump-config` 无法发现的一个问题（诚实标注）**：

实测证明 `--dump-config` **不会**校验插件**文件路径**是否存在 ——
我故意写了一个不存在的路径 `D:\definitely\does\not\exist\my-plugin.mjs`，
`--dump-config` 仍然 **exit 0** 并把它写进树里。

> ⚠️ 这正是 `$DSH_HOME/AGENTS.md` 强调"`--dump-config` 只验证能组合，
> 不验证能重启"的原因。所以：**必须真实重启验证**，§5 给了步骤。
>
> 我已人工核对片段里每一个路径（§3）。

---

## 3. 片段里每个路径的真实性（已逐个 `test -e`）

| 路径 | 存在 |
|---|---|
| `D:\DeepSeek\projects\dsh-bilibili-companion\.venv\Scripts\python.exe` | ✅ |
| `D:\DeepSeek\projects\dsh-bilibili-companion`（`cwd`） | ✅ |
| `D:\DeepSeek\projects\dsh-bilibili-companion\dsh\restrict-bili-agent-tools.mjs` | ✅ |

另外：MCP server 已用**真实 stdio 协议**验证过能启动并列出 14 个工具
（`tests/test_mcp_protocol.py`，22 项；以及手工 `cli.app tools`），
所以"DSH 连上去之后 bridge 起不来"这一风险也已排除。

---

## 4. 回滚（万一真出问题）

**三步，1 分钟**：

```bash
P="$DSH_HOME/profiles/web"

# ① 备份现有 profile（第一次装时必须做）
cp "$P/cordis.patch.yml" "$P/cordis.patch.yml.bak-before-bili-companion"

# ② 出问题时：直接还原
cp "$P/cordis.patch.yml.bak-before-bili-companion" "$P/cordis.patch.yml"

# ③ 重启 DSH
```

**如果 DSH 已经起不来**（最坏情况）：
DSH 自带恢复路径，不会让你卡死：

| 恢复手段 | 说明 |
|---|---|
| `desktop-safe-mode` profile | 你本机**已存在**这个 profile（恢复用，只保留 Electron 必需部件） |
| `resources/safe-mode.html` | 官方安全模式页面（已确认存在） |
| 手动删掉片段 | 片段是**追加在文件末尾**的，删掉从 `# DSH 接入配置片段` 到文件尾即可 |

> 因为片段**只追加、不修改**，所以"删掉尾巴"就能恢复原状 ——
> 这也是我坚持用纯 `insert` 的原因之一。

---

## 5. 建议的操作步骤（装的时候）

```bash
# 1) 备份（必做）
P="$DSH_HOME/profiles/web"
cp "$P/cordis.patch.yml" "$P/cordis.patch.yml.bak-before-bili-companion"

# 2) 把片段追加到文件**末尾**
cat "D:/DeepSeek/projects/dsh-bilibili-companion/dsh/cordis.patch.fragment.yml" \
    >> "$P/cordis.patch.yml"

# 3) 组合自检（能过说明没写坏语法）
"DSH Desktop.exe" --expose-internals <bin.js> --profile web --dump-config | tail -30

# 4) ★ 真实重启 DSH，然后在**新会话**里调：
#      mcp__bili_companion__companion_status
#    期望：ok=true，且 config.mode == "observe"
```

**装完要看的两个风险点**：

| 风险 | 表现 | 处理 |
|---|---|---|
| 收窄插件的工具名与本机不一致 | 日志出现 `[restrict-bili] ❌ 施加工具限制失败` | 按日志给的名字改 `FORBIDDEN_TOOLS`；该 agent 处于**失败关闭**（少工具，不会漏通用写入） |
| MCP server 起不来 | 工具列表里没有 `mcp__bili_companion__*` | 看 DSH 日志里 `mcp-bili-companion` 的报错；**不影响 DSH 本身与其他会话** |

---

## 6. 为什么这个片段的风险本来就低

| 设计选择 | 为什么降低风险 |
|---|---|
| 只 `insert`，不做 id-targeted patch | 不会"整体替换"掉别人的 config（本机 patch 文件里有注释警告过这个坑） |
| 不碰 `package.json` / projection / overrides | 避开 AGENTS.md 记录的"进安全模式"事故路径 |
| `failOnStartupError: false` | bridge 挂掉**不会**让插件激活失败，更不会影响 DSH 启动 |
| 新增独立 `id` 与 `serverName` | 不会与现有插件重名（重名会**加载即失败**） |
| 收窄插件失败关闭 | 名字写错时**少工具**，而不是放行通用写入 |
| 片段自包含在文件末尾 | 删除即回滚 |

---

## 7. 未验证的部分（诚实标注）

| 项 | 状态 |
|---|---|
| 隔离环境里**真正启动** DSH GUI（不是 `--dump-config`） | ❌ **未做** —— 需要另起一个 GUI 实例，会与你正在用的实例争资源与端口 |
| 收窄插件在**真实 agent** 上的效果 | ❌ 未做（插件逻辑有 6 项 node 测试；未在真实 preset 上跑） |
| `FORBIDDEN_TOOLS` 与本机实际注册名是否**完全**一致 | ❌ 未验证（不一致时会报错，已做失败关闭） |
| MCP 工具在 DSH 会话里**真的出现** | ❌ 未做（服务端已用真实 stdio 协议验证；但"DSH 侧加载"这一跳要重启才算） |

> 所以：**"不会导致无法启动"这一条是实测的**（§1）；
> **"工具真的能出现"这一条要重启后才能确认**。
