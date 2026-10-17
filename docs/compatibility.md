# 兼容性核查报告（任务 1 验收物）

> 依据 [`D:/DeepSeek/docs/DSH_EXECUTION_BRIEF.md`](../../../docs/DSH_EXECUTION_BRIEF.md) §7 任务 1 编写。
> **所有"实测"结论均为 2026-10-15 在本机采集**，采集命令随文给出；未实测的一律标注「未验证」。

---

## 0. 一句话结论

**可以按任务书路线开工**：DSH 官方 MCP 客户端在本机可用（但尚未激活）、Python 环境可干净复现、
B 站只读接口在本机可用。**但有两处必须先说清的限制**：

1. **BiliLearn 不能 import**——它自身 MIT，但 B 站读写实现依赖 GPL-3.0 的 `bilibili-api-python`（§3）。
2. **B 站用户协议 §5.6(3) 明确禁止**未经书面许可用自动程序获取平台内容（§5）。
   本任务书的方案**不因此获得平台许可**，实际启用由主人评估并承担风险。

---

## 1. 实际版本（实测）

| 项 | 值 | 采集方式 |
|---|---|---|
| 操作系统 | Windows 10.0.19044.7725 | `cmd /c ver` |
| DSH 内核 | **0.2.0-rc.2** | `DSH Desktop.exe --expose-internals …/dsh/lib/bin.js --profile web --version` |
| DSH 桌面端 | **0.11.1** | `(Get-Item 'DSH Desktop.exe').VersionInfo.FileVersion` |
| DSH_HOME | `C:\Users\yp-chenzhuoming\AppData\Roaming\dsh-desktop\harness` | 会话注入 |
| DSH_PROFILE | `web` | 同上 |
| 项目 venv Python | **3.12.15**（uv 托管） | `uv venv --python 3.12` → `.venv/Scripts/python.exe -V` |
| 系统其他 Python | **无** `python` / `py` 在 PATH（`where.exe python.exe` 无结果） | `where.exe python.exe` |
| uv | 0.12.23 | `uv --version` |
| Node / pnpm | v24.19.0 / 11.22.0 | `node -v` / `pnpm -v` |
| git | 2.55.0.windows.3 | `git --version` |
| Web GUI | `http://127.0.0.1:43129` | 运行时注入 |

> ⚠️ **`python` 不在 PATH**。本项目一律用 `.venv/Scripts/python.exe`，
> 与 curator 的环境完全隔离（见 §6）。
> ⚠️ `resources/app.asar` 是归档：`Test-Path`/`ls` **看不到内部路径**，
> 必须走 CLI 或 asar 头部解析。这正是我定位 MCP 客户端的方法（§2）。

---

## 2. DSH 的 MCP 客户端可用性（任务 2 的前置条件）

### 2.1 结论：**包在，但尚未激活**

| 核查项 | 结果 |
|---|---|
| 包是否随 DSH 分发 | ✅ **在**。`app.asar` 内 21223 个条目中存在 `node_modules/@deepseek-ai/dsh-mcp-client/{package.json,lib/index.js,README.zh.md,LICENSE}` |
| 是否已在 profile 激活 | ❌ **未激活**。`--dump-config` 里只有 `mcp-resources`，**没有** `dsh-mcp-client` 条目 |
| profile 的 `cordis.patch.yml` | 全文 grep `mcp` **零命中** |
| `@deepseek-ai` 在 profile node_modules | 只有 `cosmokit` / `schemastery`（即 MCP 客户端**不从 profile 装**，用内核自带的） |

**采集命令**（asar 头部解析，因为 `ls` 看不进去）：

```bash
node -e '…解析 app.asar 头部 JSON，列出 node_modules/@deepseek-ai/dsh-mcp*…'
# → /node_modules/@deepseek-ai/dsh-mcp-client/{LICENSE,README.i18n.yaml,README.zh.md,lib/index.js,package.json}
# → /node_modules/@deepseek-ai/dsh-mcp-resources/{…}
ELECTRON_RUN_AS_NODE=1 "DSH Desktop.exe" --expose-internals <bin.js> --profile web --dump-config | grep -i mcp
# → 只有 "- id: mcp-resources / name: '@deepseek-ai/dsh-mcp-resources'"
```

### 2.2 官方配置契约（摘自包内 `README.zh.md`，非我猜测）

```yaml
- id: mcp-bili-companion
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: bili_companion      # 工具命名空间，[A-Za-z0-9_-]{1,32}
    transport: stdio
    command: <本项目 .venv 的 python.exe>
    args: ['-m', 'bridge.server']
    cwd: <项目根>
    toolCallTimeoutMs: 60000        # 默认值
    failOnStartupError: false
```

| 字段 | 默认 | 对本项目的意义 |
|---|---|---|
| `serverName` | 必填 | 固定为 **`bili_companion`** → 工具名 `mcp__bili_companion__companion_status`（与任务书 §4 一致） |
| `transport` | 必填 | `stdio` |
| `toolCallTimeoutMs` | 60000 | 豆包 `timeout_seconds: 60` 与之相同，**长时间批处理要留意** |
| `failOnStartupError` | false | 保持 false：bridge 挂了不影响 harness 启动 |

### 2.3 ★ 两个必须写进设计的实测约束

**① stdio 子进程环境会被清洗**（README「环境清洗（stdio）」原文）：

> 子进程环境以 `scrubbedParentEnv()` 为基座——删除匹配 `/KEY|PASSWORD|SECRET|TOKEN/i`
> 的环境名称与所有 `DSH_*` 名称——再在其上合并配置的 `env`。

本机实测该正则的后果：

| 环境变量名 | 会被剔除？ |
|---|---|
| `doubao-seed-2-0-lite-260428-API-KEY`（本机实际使用，见 §7） | ❌ **被剔除**（含 `KEY`） |
| `ARK_API_KEY`（任务书 §3 默认名） | ❌ **被剔除** |
| `EGO_LINUX_HEADLESS` / `HF_ENDPOINT` | ✅ 保留 |

→ **设计结论**：bridge **不能依赖进程环境变量拿豆包密钥**。
必须走「配置文件指向的凭据来源」，或在本项目 `config.yaml` 的 `env:` 块里显式传入（显式覆盖优先）。
本项目的默认实现读 **`HKCU\Environment`**（见 §7 的环境继承坑），不读 `os.environ` 兜底。

**② 工具名是稳定约定，HMR/重启后不变。** 两个服务器可同名工具共存；
同一 `serverName` 重复配置会**加载即失败**。→ 本项目不要重复注册 `bili_companion`。

### 2.4 ★ preset 无法用配置收窄工具（实测，直接影响任务书硬约束）

任务书要求"定时 preset **只挂窄 MCP 工具**，不给通用浏览器/任意 shell"。
读 `app.asar` 实测后确认：**这件事做不到"只改配置"**。

| 实测结论 | 证据 |
|---|---|
| `@deepseek-ai/dsh-agent-preset` 的 Config **只有 5 个字段**：`id` / `plugins` / `name` / `description` / `order` | `dsh-agent-preset/lib/index.js` 的 Schemastery schema |
| 它**没有**任何 allow/deny/工具过滤字段 | 同上；全 asar 搜 `allowedTools\|toolAllowlist\|toolPolicy\|restrictTools` → **0 命中** |
| `@deepseek-ai/dsh-mcp-client` 也**没有**工具过滤字段 | 其 README 的字段表（§2.2） |
| **宿主平面（`cordis.yml` 缩进 0）的行会全局注册工具，会漏进每个 preset** | 本机 profile 里 `dsh-ops-shell`(`publishBashTool: true`)、`dsh-ops-file`、`dsh-ego-browser` 都在宿主平面启用 |
| ⚠️ `ctx.tools.restrict({allow, deny})` **不支持通配符** | 内部是 `new Set(allow)` + `Set.has(name)` 精确匹配；写 `mcp__bili_companion__*` 会抛 `names unknown global tool` |
| `restrict()` 必须跑在 `agent.ctx` 上；名字**必须已注册**否则抛错 | `dsh-tools/lib/index.js` 的 `restrict()` 实现 |

**→ 设计决定**：本项目提供 `dsh/restrict-bili-agent-tools.mjs`，
在 preset 自己的 `plugins` 里挂上它，通过 `agent/created` +
`agent.ctx.tools.restrict({ deny: [...] })` **逐个字面量**禁用
shell / 文件写入 / 浏览器 / 子代理工具（44 个名字，见该文件 `FORBIDDEN_TOOLS`）。

用 `deny` 而非 `allow` 的理由：`allow` 必须穷举所有**要保留**的工具，
一旦 MCP 工具晚注册就会因"名字不存在"抛错，且漏列即误禁。
`deny` 只需列出**已知且稳定**的通用工具名。

**作用域要点**：插件声明在 preset 的 `plugins` 里 → 运行在该 preset 的
standing scope；DSH 的 scope 事件沿祖先链冒泡，因此 `agent/created`
恰好只覆盖本 preset 的组合 agent，无需比对 preset id。

> ⚠️ 该插件**未经真实重启验证**（见 §2.5）。它本身有测试覆盖
> （`tests/test_dsh_integration.py`）：无通配符、只用 deny、
> 只禁已注册名字、不误禁本项目 MCP 工具、在工具缺席时不崩。

### 2.5 ★ 真实平台读取：✅ **已验证通过**（2026-10-15）

本轮用主人已有的小号登录态做了**首次真实端到端验证**：

| 项 | 实测结果 |
|---|---|
| 登录态 | `isLogin=true`，`mid=3747570769725629`，`uname=干饭不干事的小鲸鱼` |
| 身份核对 | `mid_matches = true`（实际登录态与配置一致） |
| 关键词发现 | 3 个关键词 → **新发现 28 条 + 已见 32 条**，0 错误 |
| 字幕读取 | ✅ 成功（41 个字幕片段登记为证据，带时间戳） |
| 证据登记 | **238 条** evidence |
| 豆包预处理 | ✅ 真实调用成功（8 个任务，prompt 12,264 / completion 11,473 tokens） |
| 短候选包 | 786 / 1308 / 2357 字符（**原文留在 bridge**） |
| 限流 | 18 次请求，0 次限流、0 次重试 |
| **真实公开发布** | **0 次**（observe + dry_run） |

**真实读取验收：✅ 通过。**

### 2.6 尚未验证的部分（诚实标注）

- ❌ **未验证**：在 profile 里真正激活 `dsh-mcp-client` 后，
  `mcp__bili_companion__*` 是否如期出现在模型工具列表。
  → **已用真实 stdio 协议 + 官方 MCP 客户端库验证过服务端**（22 个集成测试，
  14 个工具全部可枚举、`companion_status` 可调用、错误码明确、
  stdout 只含协议帧），且**配置片段已在隔离 DSH_HOME 里验证能被正常组合**
  （见 [`dsh-install-safety.md`](dsh-install-safety.md)），
  但"真实重启 DSH 后工具出现"这一跳**未做**。
- ❌ **未验证**：`restrict-bili-agent-tools.mjs` 在真实 agent 上的收窄效果。
- ❌ **未验证**：`FORBIDDEN_TOOLS` 里的名字与本机实际注册名是否**完全**一致
  （不一致时 `restrict()` 抛错；插件已做失败关闭，见该文件注释）。
- ⚠️ profile 的 `cordis.patch.yml` 属**结构类改动**：按 `$DSH_HOME/AGENTS.md`，
  改完必须**真实重启 DSH 验证**，不能只 dump-config。

---

## 3. 上游 BiliLearn 审核（`xiaoyaya191/bilibili_learning_bot`）

### 3.1 ⚠️ 先纠一处事实：commit 与任务书**一致**

| 项 | 任务书 | 实测 | 判定 |
|---|---|---|---|
| 钉的 commit | `0ea9a817e38ea6d5fa0229552da0f86497be42bb` | **`main` HEAD 就是它**（2026-10-10T19:23:52Z，`fix: preserve legacy panel accounts and verify permission saves`） | ✅ **一致** |
| 版本标签 | — | 依赖清单首行写 `v3.1.6` | ℹ️ 只是**依赖文件里的版本注释**，不是 commit 漂移 |

> 采集：`GET /repos/xiaoyaya191/bilibili_learning_bot/commits/main` 与 `/commits/0ea9a81…` → 两者 sha 相同。
> 交接包 `docs/handoff/06` 曾记「不一致（当前 main 是 v3.1.6）」——**该记录指的是 release 标签，易被误读为 commit 漂移，此处更正**。

### 3.2 规模与许可（实测）

| 项 | 实测值 |
|---|---|
| 文件 / Python 文件 | **496 / 300** |
| Python 体量（按目录，KB） | `services` 807 · `brain` 649 · `cli` 415 · `tests` 514 · `xingye_bot` 229 · `core` 143 · `knowledge` 117 · `api` 88 · `utils` 66 · `ob_bridge` 46 · `agent` 39 · `mcp_server` 31 · `persona` 79 · `security` 11 · root 786 |
| 测试文件数 | **125** |
| LICENSE | **MIT License**（`Copyright (c) 2025 XingYe contributors`） |
| 唯一 GPL 依赖 | **`bilibili-api-python==17.4.1`**（GPL-3.0-or-later），`requirements.txt` 第 3 行 |

### 3.3 ★ 许可红线：逐个查 import 的结果（实测）

用「只提取 `from|import` 行并过滤 `bilibili_api`」的方式定性，结果与交接包一致：

| 文件 | 是否 import `bilibili_api`（GPL） | 结论 |
|---|---|---|
| `api/client.py` | ⚠️ **是**（`Credential, user, homepage, comment, video, Danmaku` 等 6 行） | ❌ 不可借 |
| `api/auth.py` | ⚠️ **是**（`Credential`, `login_v2`） | ❌ 不可借 |
| `xingye_bot/bilibili_ops.py` | ⚠️ **是**（9 行） | ❌ 不可借 |
| `agent/tools.py` | ⚠️ **是**（`from bilibili_api import favorite_list`） | ❌ 不可借 |
| `core/platform_actions.py` | ✅ **NONE** | ✅ 可借鉴 |
| `api/throttle.py` | ✅ NONE | ✅ 可借鉴 |
| `utils/storage.py` | ✅ NONE | ✅ 可借鉴 |
| `xingye_bot/state.py` | ✅ NONE | ✅ 可借鉴 |
| `security/guard.py` | ✅ NONE | ✅ 可借鉴 |
| `api/subtitles.py` | ✅ NONE | ✅ 可借鉴 |
| `services/deep_dive.py` | ✅ NONE | ✅ 可借鉴 |
| `core/config.py` | ✅ NONE | ✅ 可借鉴 |
| `mcp_server/bili.py` | ✅ NONE（但 `import api.client` → **间接传染**） | ⚠️ 间接 |

> **`agent/tools.py` 的证据修正**：交接包只标"是"。实测它**只引用了 `favorite_list` 一处**，
> 因此正确的做法不是整文件丢弃，而是**照抄签名、自行实现**（见 §3.5）。

**结论（与主人已确认的决策一致）：只借鉴设计与测试清单，不 import、不 vendor。**

### 3.4 「无需复用的模块」清单

`cli/`（415KB）、`brain/`、`services/learning_agent.py`、`persona/`、`ob_bridge/`、
`knowledge/`、以及全部 Web 面板 / 视频转 PPT / 测验 / 私信机器人能力
—— 任务书 §1「不属于本项目」已排除，且大多踩 GPL。

### 3.5 可借鉴的接口形状（实测签名，自行实现）

`core/platform_actions.py` 的**发布开关清单**（本任务书"发布门禁"的直接来源）：

```
public_commenting_enabled()  at_mention_replies_enabled()  video_liking_enabled()
_interaction_switch(key, default=True)
commenting_enabled()  reply_comment_enabled()  reply_dm_enabled()  liking_enabled()
coining_enabled()  favoriting_enabled()  following_enabled()  watch_later_enabled()
active_dm_enabled()  owner_share_enabled()  dynamic_draft_enabled()  dynamic_publish_enabled()
platform_action_enabled(action) -> bool
```

`agent/tools.py` 的**门禁参数形状**（★ 值得照抄的正是 `dry_run` / `allow_write`）：

```
search_videos(keyword, limit=8)            get_video_info(bvid)
get_video_comments(bvid, limit=10)         get_video_danmakus(bvid, limit=40)
get_up_info(uid)                           get_up_videos(uid, limit=10)
get_recommendations(limit=10)              get_my_status()      check_mentions(limit=20)
video_interact(bvid, action, text="", dry_run=False, allow_write=True)
reply_comment(oid, root, parent, text, allow_write=True)
manage_platform_action(action, payload)    manage_platform_favorites(operation, payload)
list_platform_favorites(media_id=0, page=1)
kb_search(keyword, limit=5)  kb_add(title, content)  memory_search(...)  memory_write(...)
panel_config_get(section="")  panel_config_update(section, patch)  finish(summary, outcome="done")
```

### 3.6 测试用例名清单（免费边界清单，只读文件名）

125 个测试文件名中最相关的一批：

```
test_platform_action_safety.py   test_subtitle_verification.py   test_permission_management.py
test_web_qr_login.py             test_web_login_rate_limit.py    test_comment_reply_duplicates.py
test_comment_reply_routing.py    test_reply_safety.py            test_log_redaction.py
test_time_policy.py              test_candidate_log_compaction.py test_video_decision.py
test_owner_share.py              test_direct_video.py            test_runtime_config_and_monitor_backoff.py
test_contact_filter.py           test_security_defaults.py       test_political_safety_preset.py
test_account_isolation.py        test_token_observability.py     test_config_masking.py
```

> 用途：任务书 §8「自动化必须证明」的每一条，几乎都能在这里找到对应的**用例意图**。
> **只读文件名**即可，不读内容——这是省 token 的关键（本次审核总计约 4 次浏览器调用）。

### 3.7 任务书对上游的其他要求（照做）

- ❌ 不启动它的自主 bot/monitor 主循环（避免两个 AI 同时控制同一小号）。
- ❌ 不把 5 万行项目翻译成 TypeScript。
- ❌ 不把它的 UI/导出功能作为运行依赖。
- ✅ 项目数据放**本项目专属目录**，**不要**放进 BiliLearn 的 `Data/`。
- ✅ 若必须补上游缺口：小补丁 + 理由 + 保留 MIT 归属 + 针对性测试。

---

## 4. 数据目录、登录方式、凭据

| 项 | 决定 |
|---|---|
| 数据目录 | **`%LOCALAPPDATA%\dsh-bilibili-companion\`**（本项目专属，独立于 BiliLearn 的 `Data/` 与本仓库） |
| 数据库 | `…/companion.db`，SQLite **WAL** + 单实例任务锁 + 可重复迁移 |
| 登录方式 | **本机人用扫码入口**（主人本人在独立浏览器 profile 扫码一次）；凭据只在 bridge 进程内 |
| 凭据存放 | 项目数据目录下的 `cookie.txt`（**gitignored**），或 Windows 凭据库 |
| 凭据红线 | **永不**进 LLM 上下文 / 日志 / git / MCP 返回 / 素材包 / 回执；所有出站文本过 `redact` |
| 主号 UID | 主人原话「**真实实现后再给**」→ 未提供，`owner_mid=0`。`@` 汇报的**机制已实现**（三态状态机 + 双重断言），但**投递通道未接** → 默认 `unsupported`，**真实 @ 未验收** |

> ⚠️ **两套登录态互相独立**：本项目自己的 cookie 文件 ≠ agent 浏览器（ego）的 profile
> （`%LOCALAPPDATA%\ego-lite-linux\profile`）。**各自登录一次**。

---

## 5. B 站协议边界与风险（**必须写清的"限制"**）

### 5.1 用户使用协议原文（实测抓取，2025-04-30 版 / 2025-05-07 生效）

抓取：`https://www.bilibili.com/blackboard/protocal/licence.html`（页面 JS 渲染，需浏览器上下文）。
页面顶部版本选择器实测为：`2021.02.25 / 2021.08.21 / 2022.03.23 / 2022.05.13 / 2023.09.05 / 2025.04.30`。

**§5.6 哔哩哔哩禁止用户在使用哔哩哔哩软件及相关服务时从事以下违规行为：**

> (3) **未经哔哩哔哩事先明确书面许可，以任何方式（包括但不限于机器人软件、蜘蛛软件、爬虫软件等任何自动程序、脚本、软件）和任何理由自行或委托他人、协助他人获取平台的服务、内容、数据**；

**另一条相关**（§1.2 附近）：

> 哔哩哔哩所提供的服务仅供用户**非商业性**使用，用户在未经哔哩哔哩事先书面同意的情况下，不得以商业性目的使用哔哩哔哩所提供的服务。

### 5.2 开放平台（`https://open.bilibili.com/doc`，实测）

平台提供：账号授权、用户管理、**视频管理（发布/删除/查询）**、数据开放、专栏管理、
直播能力、Webhooks、SDK。**身份认证需先填写身份信息并审核通过才能创建应用**。

> ★ **关键限制**：开放平台**没有"发表评论 / 回复评论"的公开能力**，
> 其视频管理面向**自己稿件**的分发。→ 本任务书要的"评论互动"**无法**通过开放平台合规获授权。
> 这不意味着可以做，而是意味着：**该能力不存在合规授权通道**，风险由主人评估。

### 5.3 风险结论（写进 README 与 acceptance）

1. 本任务书描述的自动检索/读取/评论，**未获平台书面许可**；
   「能运行」**不等于**「获许可」。
2. 风控是现实存在的（见 §7）：本项目采用**降频 + 缓存 + 有限重试 + 健康状态停机**，
   这些是**负载控制**，**不构成免风控保证**。
3. 明令不做（任务书 §5 + §1）：**不轮换 IP/账号、不伪造客户端、不改指纹、不代解验证码、不转包打码**。
4. 默认姿态：`interaction.mode: observe`、`dry_run: true`、`publish_enabled: false`。

---

## 6. 与现有项目的关系

| 项目 | 处置 | 理由 |
|---|---|---|
| `projects/dsh-bili-curator/` | **原地保留，作为移植来源** | 它的 `platform/bilibili/` 是**自建 httpx 客户端，零 GPL**（BiliLearn 同类模块踩 GPL），是最有价值的干净资产；368 个测试全绿 |
| `projects/dsh-our-free-model/` | 不涉及 | 与本任务无关 |

**本项目对 curator 的依赖方向**：只**复制/移植**代码，**不 import curator 包**——
两套环境隔离（curator 是 Python 3.11.5 venv，本项目是 3.12.15 venv），
且避免"两个 AI 同时控制同一账号"的耦合。

---

## 7. 环境事实与已踩的坑（本机实测）

### 7.1 ★ 环境变量继承坑 → 直接影响豆包密钥读取

| 事实 | 证据 |
|---|---|
| 豆包 key **只在 `HKCU\Environment`**，名为 `doubao-seed-2-0-lite-260428-API-KEY` | `(Get-Item 'HKCU:\Environment').Property` |
| `ARK_API_KEY`（任务书 §3 默认名）**不存在于** bash / pwsh / `HKCU\Environment` | 三处均查无 |
| DSH 从 `explorer.exe` 继承旧环境块 → 设了变量重启 DSH 也读不到 | 交接包记录 + 本次复核 |

**双重结论**：
1. bridge 必须**读注册表兜底**（`resolve_secret()`），不能只读 `os.environ`；
2. 即便读了注册表，MCP stdio 的**环境清洗**（§2.3①）也不影响它——
   因为我们读的是注册表，不是被子进程环境传递的值。**两条约束在这里正好互相化解。**

### 7.2 网络可达性（实测）

| 目标 | 直连（bash/python） | 浏览器（ego） | 本次用法 |
|---|---|---|---|
| `pypi.org` | ❌ **不通**（`Failed to connect … :443`，21.7s 超时） | — | **改用清华镜像** |
| `pypi.tuna.tsinghua.edu.cn` | ✅ **通**（HTTP 200，10.8s） | — | `UV_DEFAULT_INDEX` |
| `github.com` / `raw.githubusercontent.com` / `api.github.com` | ❌ 不通 | ✅ **通** | 用 `ego_js` 的 `fetch` 取上游 |
| `www.bilibili.com` | ✅ 通 | ✅ 通 | 直连 |
| `open.bilibili.com` | ⚠️ 页面 fetch 需浏览器上下文 | ✅ 通 | 浏览器 |

> ⚠️ `ego_script`（Node 侧）**没有 `fetch`**；`ego_js` / `ego_http`（页面上下文）**才有**。

### 7.3 验证码（来自交接包实测，本报告采信并保留检测手段）

- B 站用**极验 geetest** 点选，`z-index: 2147483647` **全屏遮罩**，会吞掉所有点击。
- ⚠️ 插件的 `ego_captcha` **漏报极验**（实测 `detected:false` 但有全屏遮罩）。
- 铁律：**检测到 → 立即停止一切写操作 → 程序不代解、不转包打码、不绕过 → 只能人工恢复。**

### 7.4 沙箱写盘（交接包实测，本报告采信）

`ops_bash` 里的 Python **不能写 workspace 之外**（`PermissionError`），
其派生的 PowerShell 继承同一限制；`pwsh` 工具**不受限**。

### 7.5 ★★ 数据目录：`%LOCALAPPDATA%` 实测**不可写**（本轮新发现）

交接包建议数据放 `%LOCALAPPDATA%\dsh-bilibili-companion\`。**本轮实测该位置不可写**：

| 试探 | 结果 |
|---|---|
| Python `mkdir` `%LOCALAPPDATA%\dsh-bilibili-companion` | ❌ `PermissionError [WinError 5]` |
| `pwsh New-Item` 同一路径 | ❌ `UnauthorizedAccessException` |
| `pwsh` 在 `%LOCALAPPDATA%` 下建**任意新随机目录** | ❌ 同样拒绝 |
| `pwsh` 在 `%LOCALAPPDATA%` 下建**任意新文件** | ❌ 同样拒绝 |
| `%APPDATA%\dsh-bilibili-companion` | ❌ 同样拒绝 |
| `<项目根>/.local-data` | ✅ **可写** |

注：`%LOCALAPPDATA%` 的 ACL 显示当前用户是 `FullControl`，
所以这不是 ACL 问题，而是工具侧/进程侧的限制。

**→ 设计决定**（`core/paths.py`）：数据目录**不写死**，按优先级**探测可写性**：

1. `COMPANION_DATA_DIR`（显式指定即视为权威，**不回退**——避免静默忽略使用者的选择）
2. `%LOCALAPPDATA%\dsh-bilibili-companion\`
3. `%APPDATA%\dsh-bilibili-companion\`
4. `<项目根>/.local-data/`（已 gitignore）
5. 系统临时目录（最后兜底，避免崩溃）

并把**实际选中的目录与来源**通过 `companion_status.data_dir` 报告出来，
避免"数据写到了你不知道的地方"。

✅ **本机实测落点**：`D:\DeepSeek\projects\dsh-bilibili-companion\.local-data`（来源 `project-local`）。

> ⚠️ 若将来把项目移到别处，或换了机器，`data_dir` 会跟着变。
> 用 `companion_status` 或 `companion status` 查看实际位置，不要假定路径。

---

## 8. 依赖清单（本项目，实测可装）

`uv venv --python 3.12` + 清华镜像，实测装成功：

| 包 | 版本 | 用途 |
|---|---|---|
| `mcp` | **2.3.0** | MCP server（**注意：2.x 把 `FastMCP` 改名为 `MCPServer`**，见下） |
| `pydantic` | 2.14.1 | 配置校验 |
| `httpx` | 0.28.1 | B 站客户端（**`trust_env=False`**） |
| `pyyaml` | 6.0.3 | `config.yaml` |
| `typer` | 0.27.3 | **人用 CLI**（`companion approve/resume`，不进 MCP） |
| `pytest` / `pytest-asyncio` | 9.1.1 | 测试 |
| `ruff` / `mypy` | — | 静态检查 |

### 8.1 ⚠️ MCP SDK 2.x 的破坏性改名（实测踩到）

```
>>> from mcp.server.fastmcp import FastMCP
ModuleNotFoundError: No module named 'mcp.server.fastmcp'.
This is mcp 2.x, where FastMCP was renamed to MCPServer
(from mcp.server.mcpserver import MCPServer) …
```

实测的正确入口与签名：

```python
from mcp.server.mcpserver import MCPServer
MCPServer(name=…, version=…, instructions=…)
server.tool(name=…, description=…)     # 装饰器
server.run(transport='stdio')
server.run_stdio_async()
```

→ **设计决定**：本项目**锁 `mcp>=2,<3`** 并按 2.x 的 `MCPServer` 编写，
不使用已废弃的 `FastMCP` 别名。

---

## 9. 已确认的限制（汇总）

| # | 限制 | 影响 |
|---|---|---|
| 1 | B 站协议 §5.6(3) 禁止自动程序获取平台内容 | **方案未获平台许可**；「能运行」≠「已许可」 |
| 2 | 开放平台无"发表/回复评论"能力 | 评论互动**无合规授权通道** |
| 3 | `bilibili-api-python` 是 GPL-3.0 | **不 import / 不 vendor BiliLearn 读写实现** |
| 4 | MCP stdio 清洗 `KEY/SECRET/TOKEN` 环境变量 | 豆包密钥**不能**靠进程环境传递 |
| 5 | `python` 不在 PATH | 一切命令用 `.venv/Scripts/python.exe` |
| 6 | pypi.org 直连不通 | 必须走清华镜像 |
| 7 | github.com 直连不通 | 只能用浏览器上下文取上游 |
| 8 | 极验验证码会全屏遮罩且插件漏报 | 检测到即**全线停机，人工恢复** |
| 9 | 沙箱 Python 不能写 workspace 外 | 改插件/系统文件用 `pwsh` |
| 10 | DSH 继承 explorer 旧环境块 | 密钥读注册表兜底 |
| 11 | 任务书 §4：原文不出 bridge | `read_video` 不返回全文；摘录 ≤500 字 |
| 12 | 任务书 §3：定时 preset 只挂窄 MCP | 配置层**做不到**，必须用 `ctx.tools.restrict()`（§2.4） |
| 13 | 主号 UID 未提供 + `@` 投递通道未接 | `@` 机制已实现；真实提及**未验收**，实测落 `unsupported` |
| 14 | **`%LOCALAPPDATA%` 与 `%APPDATA%` 都不可写** | 数据目录改为**探测可写性**，落到 `<项目根>/.local-data/`（§7.5） |
| 15 | `ctx.tools.restrict()` **不支持通配符** | 必须逐个枚举工具名（§2.4） |

---

## 10. 未验证 / 待办（**不虚报**）

| 项 | 状态 |
|---|---|
| profile 里激活 `dsh-mcp-client` 后工具是否如期出现 | ❌ **未验证**（需重启 DSH；服务端已验，片段已在隔离环境验能组合） |
| preset 的工具收窄在真实 agent 上的效果 | ❌ **未验证**（插件逻辑有测试，未在真实 preset 上跑） |
| `FORBIDDEN_TOOLS` 与本机实际注册名是否完全一致 | ❌ 未验证（不一致时 `restrict()` 抛错，插件已做失败关闭） |
| MCP 2.3.0 与 DSH 侧协议版本协商 | ❌ 未验证（本机服务端与官方客户端库互通已验） |
| B 站 ASR 能力 | ❌ 未调研 |
| 收藏夹实际名称 / 首次真实收藏 | ❌ 未做（curator 时只做过一次真实点赞） |
| 真实扫码登录 + UID 核对 | ✅ **已完成**（复用 curator 登录态；`mid_matches=true`） |
| 主号昵称 / UID | ❌ 主人说"真实实现后再给" |
| 真实发布验收 | ❌ **不做**（需主人显式开启，属任务 5） |
| token 对照实验（≥60%） | ❌ 未做（属任务 8） |
| 跨平台（Linux/macOS） | ❌ 未验证（本机全为 Windows 假设） |
| BiliLearn 本地副本 | ❌ **未拉取**（按已确认决策：只借鉴、不 vendor） |

---

## 11. 任务 1 验收对账

任务书 §7 任务 1 的验收标准：

| 验收项 | 状态 |
|---|---|
| 检查 DSH 实际版本及 MCP 客户端是否可用，记录 Python/Windows 版本 | ✅ §1、§2 |
| 查看本地 `AGENTS.md` 和现有目录 | ✅ 已读（`$DSH_HOME/AGENTS.md`、工作区、curator、handoff 包） |
| 拉取 BiliLearn 到依赖目录并锁定 commit（不可获取则记录实际 commit） | ✅ **已核实**：`main` HEAD **就是**任务书钉的 `0ea9a81…`；按已确认决策**不 vendor**（避免 GPL） |
| 读 LICENSE / README / `mcp_server/README.md` / `agent/tools.py` / `xingye_bot/bilibili_ops.py` / `api/` / `core/platform_actions.py` / 相关 tests | ✅ §3.3–§3.6（用**签名 + import 行 + 测试文件名**方式，未读函数体） |
| 一页 `docs/compatibility.md` | ✅ **本文件** |
| 核对用户协议 + 开放平台文档，说明使用限制与风险 | ✅ §5 |
| **不在这里停止并只交研究报告；研究结束继续任务 2** | ⏭ **继续任务 2** |

> **未验证声明**：本报告中所有「✅ 实测」均有上表采集命令支撑；
> 凡未实测者一律标注「未验证 / 未做」，**不以"应该能跑"充当结论**。

---

*生成时间：2026-10-15 · 采集人：DSH 会话（接手会话）*
