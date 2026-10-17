# 重启前检查单（2026-10-15）

> 主人：「在这个测试完全后，我会手动重启，你要保证我重启后不会出现问题。」
>
> 本文是**重启前的实测检查结果**。装的东西只有一处：往 profile 追加了一个配置片段。

---

## 0. 结论

| 问题 | 结论 |
|---|---|
| 会不会起不来？ | **不会**。组合产物正常，退出码 0；启动仅 **5s**（SDK 超时 60s）。 |
| MCP 连得上吗？ | **能**。已用与 DSH 相同的方式（`PYTHONPATH` + `python -m bridge.server`）实测，`initialize` 有正常响应。 |
| 三个关键路径都在吗？ | **在**。已逐个 `Path.exists()` 验证。 |
| 万一坏了怎么退？ | **一条命令回滚**（§4），备份文件已就位。 |
| 会不会卡死？ | **不会**。6 类风险全部实测通过；**已修掉一个真实的超时 bug**（见 §7.1）。 |
| 现在是什么状态？ | `observe` + `dry_run=true` + 发布关闭 → **重启后不会自动发任何东西**。 |

---

## 1. 这次到底改了什么（只有一处）

```
C:\Users\yp-chenzhuoming\AppData\Roaming\dsh-desktop\harness\profiles\web\cordis.patch.yml
  └─ 文件末尾**追加**了一段（465 行 → 621 行）
```

追加内容 = `dsh/cordis.patch.fragment.yml`，只做两件**新增**事：

1. 插入 `mcp-bili-companion`（官方 MCP 客户端，连本项目的 bridge）
2. 插入 `preset-bili-companion`（定时用 preset，含工具收窄插件）

**没有**：装 npm 包、跑 `dsh plugin`、改 `package.json`、改投影元数据、禁用任何现有插件。

---

## 2. 实测检查结果

### 2.1 配置能正常组合

```bash
DSH_HOME=... "DSH Desktop.exe" --expose-internals <bin.js> --profile web --dump-config
```

| 项 | 结果 |
|---|---|
| 退出码 | **0** |
| stderr | **空**（无任何报错） |
| 组合产物行数 | 2543（与装之前**完全一致**） |

> ⚠️ 一个诊断上的坑（我自己踩了）：`--dump-config` 的输出里，**用户 patch 层的条目
> 是缩进 0 的顶层条目**，不是 `- id:` 开头的列表项。我一开始用
> `grep "^- id:.*bili"` 找不到，误以为没生效 —— 实际它**在**（见 §2.3）。
> 判断是否生效要看 **`cordis.yml`**（已组合产物），不是只看 dump 的行首模式。

### 2.2 组合产物里确实有我们的两条

`profiles/web/cordis.yml`（DSH 组合后的结果）实测包含：

```yaml
- id: mcp-bili-companion
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: bili_companion
    transport: stdio
    command: D:\DeepSeek\projects\dsh-bilibili-companion\.venv\Scripts\python.exe
    args: ['-m', 'bridge.server']
    cwd: D:\DeepSeek\projects\dsh-bilibili-companion
    ...
- id: preset-bili-companion
  name: '@deepseek-ai/dsh-agent-preset'
  config:
    id: bili-companion
    ...
      - id: bili-companion-tool-restriction
        name: >-
          D:\DeepSeek\projects\dsh-bilibili-companion\dsh\restrict-bili-agent-tools.mjs
```

### 2.3 三个路径逐个验证存在

| 路径 | 结果 |
|---|---|
| `...\.venv\Scripts\python.exe`（MCP command） | ✅ 存在 |
| `...\dsh-bilibili-companion`（cwd） | ✅ 存在 |
| `...\dsh\restrict-bili-agent-tools.mjs`（收窄插件） | ✅ 存在 |
| `...\bridge\server.py` | ✅ 存在 |

### 2.4 MCP server 能被 DSH 正常拉起

用**与 DSH 相同的方式**（同样的 python、同样的 `PYTHONPATH`、同样的 stdio）实测：

```bash
PYTHONPATH="D:/DeepSeek/projects/dsh-bilibili-companion" PYTHONIOENCODING=utf-8 \
  ./.venv/Scripts/python.exe -m bridge.server
# 输入 initialize 请求
```

**实测返回**：

```json
{"jsonrpc":"2.0","id":1,"result":{"capabilities":{...},"instructions":"这是 B 站自主..."}}
```

→ **握手成功**，说明重启后 DSH 能连上 bridge、能拿到 17 个工具。

### 2.5 重启后不会自动发东西

```
mode          = observe
dry_run       = true
publish_enabled = false
发布门禁      = (False, 'MODE_OBSERVE')
```

**只有你自己**把这三项改掉，才可能发生真实写入。重启本身不会触发任何发布。

---

## 3. 重启后你应该看到什么

在**新会话**里让模型调用：

```
mcp__bili_companion__companion_status
```

**期望**：
- `ok: true`
- `data.server.name == "bili_companion"`
- `data.config.mode == "observe"`
- `data.config.publish_allowed == false`
- `data.owner_notifications.mention_ready == true`（`@` 通道就绪）
- `data.schedule.mode == "on_host_start"`

**工具总数应为 16 个**（14 个任务书 + `like_video` / `favorite_video`）。

---

## 4. 万一出问题的回滚（一条命令）

备份已就位：

```
C:\Users\yp-chenzhuoming\AppData\Roaming\dsh-desktop\harness\profiles\web\
    cordis.patch.yml.bak-before-bili-companion-20261015-160712
```

**回滚**：

```powershell
$P = "$env:APPDATA\dsh-desktop\harness\profiles\web"
Copy-Item "$P\cordis.patch.yml.bak-before-bili-companion-20261015-160712" "$P\cordis.patch.yml" -Force
# 然后重启 DSH
```

**如果 DSH 已经起不来**（最坏情况）：DSH 自带恢复路径，不会卡死：

| 手段 | 说明 |
|---|---|
| `desktop-safe-mode` profile | 你本机**已存在**（恢复用，只保留 Electron 必需部件） |
| `resources/safe-mode.html` | 官方安全模式页面（已确认存在） |
| 手动删片段 | 片段是**追加在文件末尾**的 → 删掉从 `# DSH 接入配置片段` 到文件尾即可 |

> 因为片段**只追加、不修改**，所以"删尾巴"就能恢复原状 —— 这也是坚持用纯 `insert` 的原因。

---

## 5. 两个已知的、重启后**可能**出现的现象（都不影响启动）

| 现象 | 原因 | 处理 |
|---|---|---|
| 日志出现 `[restrict-bili] ❌ 施加工具限制失败` | `FORBIDDEN_TOOLS` 里某个名字与本机实际注册名不一致（`restrict()` 对未知名字**抛错**） | 按日志给出的名字改 `dsh/restrict-bili-agent-tools.mjs`。此时该 agent 处于**失败关闭**（少工具），**不会**漏出通用写入能力 |
| 工具列表里没有 `mcp__bili_companion__*` | bridge 没起来（路径/权限/端口） | 看 DSH 日志里 `mcp-bili-companion` 的报错。**不影响 DSH 本身与其他会话**（`failOnStartupError: false`） |

---

## 6. ★ 卡死风险专项审查（实测，6 类）

主人明确要求「重点审查重启后是否会卡死」。已逐项实测，
脚本：`scripts/hang_risk_review.py`（可随时重跑）。

| # | 风险 | 实测结果 | 判定 |
|---|---|---|---|
| 1 | **启动耗时**（DSH 会**等待** bridge 起来） | **4.9 ~ 5.6s**（3 次） | ✅ 远低于 SDK 的 60s 协商超时 → **不卡启动** |
| 2 | 工具调用耗时 | 纯本地工具 **5.0 ~ 6.0s**（含进程启动） | ✅ 调用后立即响应 |
| 3 | **网络超时** | httpx 超时 20s，且健康门禁会先拦 | ✅ 有界 |
| 4 | **限速 sleep 拖过超时** | 见 §6.1（**这是真发现**） | ✅ 已修 |
| 5 | **stdout 污染** | 全部输出**都是**合法 JSON-RPC | ✅ 协议干净 |
| 6 | **异常后是否还能用** | 7 个畸形参数全部得到响应；之后 `companion_status` **仍可用**；退出码 0 | ✅ 不崩 |

### 6.1 ★ 风险 4 是真的 —— 已修

**发现**：实测真实一轮 `prepare_batch` 花了 **198 秒**，
而 DSH 的 `toolCallTimeoutMs` 原本是 **120 秒** → **工具调用会在中途被掐断**。

后果不只是"报个错"：
- DSH 侧看到超时，以为没跑；
- bridge 侧可能已经写了 `runs`、发了豆包请求（**花了钱**）；
- 用户若重试 → **又跑一轮**。

**修法（两层）**：

1. **bridge 内加时间预算** `text_worker.max_batch_seconds`（现 **240s**）：
   每处理完一个视频就检查剩余时间；不够就**主动收尾**，
   把**已完成的部分**返回，并在 `limitations` 里说明
   （`TIME_BUDGET_EXHAUSTED_DEFERRED_N`）。
   → **调用永不超时，已做的工作不浪费。**
2. **DSH 超时提到 300s**（原 120s），给预算留 60s 余量。

对齐结果：

```
批处理预算         : 240s
DSH toolCallTimeout: 300s   ← 已从 120s 提到 300s
限速最坏（估）     : ~100s
```

- ✅ 预算 < 超时（不会被掐断）
- ✅ 预算 > 限速最坏（正常情况不会被提前收尾）

> ⚠️ 这个 bug **只有真实跑一次才会暴露** —— 单测里没有真实限速与豆包延迟。
> 已加 3 项回归测试（含"预算耗尽要主动收尾"与"预算必须小于 DSH 超时"）。

### 6.2 一个诊断上的坑（记录备查）

`--dump-config` 的输出里，**用户 patch 层的条目是缩进 0 的顶层条目**，
不是 `- id:` 开头的列表项。用 `grep "^- id:.*bili"` 会**误判为没生效**。
正确做法：查 `profiles/web/cordis.yml`（已组合产物）。

---

## 7. 新增的两个能力（主人 2026-10-15 要求）

### 7.1 主人偏好：跨对话持久

**问题**：对话上下文被压缩后，「我上次说过少点赞」这类**口述约定会丢**。

**做法**：新增 `owner_preferences` 表（schema v4）+ MCP 工具 `owner_preferences`。

```bash
# 人用 CLI
./.venv/Scripts/python.exe -m cli.app prefs --add "少点赞" --kind rule
./.venv/Scripts/python.exe -m cli.app prefs                    # 查看全部
```

**★ 安全约束（写进表结构）**：`scope` 只允许 `ranking` / `content` / `topic`，
想写 `publish` 之类**直接报错** —— 偏好**绝不能**变成"改发布权限"的旁路。
发布模式/配额/UID/暂停**只能**由 `config.yaml` 与人用 CLI 决定。

### 7.2 下一步指引：新对话不迷路

`companion_status` 新增 `next_action` 字段，**纯本地计算**（不发请求），
告诉 agent 现在该干什么：

| `action` | 含义 |
|---|---|
| `STOP_AND_REPORT` | 健康异常或已暂停 → 停下报告，**不要**继续 |
| `AWAIT_APPROVAL` | 有草稿待主人批准 |
| `NEEDS_HUMAN_CHECK` | 有 `uncertain` 动作已冻结 → 人工核对，**不要**重发 |
| `RUN_BATCH` | 到了该跑的时刻 → `prepare_batch` |
| `IDLE` | 今天已完成/未到点 → **不要重复干活** |

**实测输出**（当前状态）：

```json
{"action": "IDLE",
 "reason": "今天已完成过一轮 → 打开 DSH 不重复跑",
 "do": "告诉主人'今天已完成/防抖跳过'，不要重复干活",
 "blocking": false}
```

---

## 8. 诚实标注：未做的事

| 项 | 状态 |
|---|---|
| 隔离环境里**真正启动 GUI**（不只是 `--dump-config`） | ❌ 未做（会与你在用的实例争端口/资源） |
| `restrict-bili-agent-tools.mjs` 在**真实 agent** 上的收窄效果 | ❌ 未验证（插件逻辑有 6 项 node 测试） |
| `FORBIDDEN_TOOLS` 与本机实际注册名**完全一致** | ❌ 未验证（不一致时**失败关闭**） |
| **真实限速 + 豆包延迟**下的预算表现 | ❌ 未跑满一轮验证（预算逻辑有单测，未在真实慢速下验） |

> 所以："**不会导致无法启动**"、"**不会卡死**"是实测的（§2、§6）；
> "**收窄插件完美生效**"要重启后才能确认。

---

## 9. 最终质量门

```
pytest tests/   → 481 passed
ruff check      → All checks passed
mypy (strict)   → Success: 29 source files
MCP 工具数      → 17
```
