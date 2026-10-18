# 用户操作手册（人用）

> 面向**主人**（人）：怎么装、每天怎么用、草稿怎么批、出问题怎么查。
> 模型的行为约束（不是给人看的）见 [`AGENTS.md`](../AGENTS.md)；
> 技术细节与验收证据见 [`README.md`](../README.md) 与 [`docs/acceptance.md`](acceptance.md)。
>
> 版本：2026-10-18 · 适用 schema v15 · MCP 工具 26 个

---

## 目录

1. [一页速查](#1-一页速查)
2. [它是什么 / 不做什么](#2-它是什么--不做什么)
3. [准备工作](#3-准备工作)
4. [安装（复制命令即可）](#4-安装复制命令即可)
5. [接进 DSH](#5-接进-dsh)
6. [三分钟验收清单](#6-三分钟验收清单)
7. [第一次运行](#7-第一次运行)
8. [日常操作](#8-日常操作)
9. [从草稿到发布](#9-从草稿到发布)
10. [设置页](#10-设置页)
11. [安全与合规](#11-安全与合规)
12. [故障排查](#12-故障排查)
13. [数据、备份与迁移](#13-数据备份与迁移)
14. [升级与回滚](#14-升级与回滚)
15. [已知限制](#15-已知限制)
16. [命令与文档速查](#16-命令与文档速查)

---

## 1. 一页速查

| 我想… | 怎么做 |
|---|---|
| **打开 DSH 就自动跑** | 默认就会（插件 5 秒后起 dm-watch，60 秒后检查是否该跑；见 §8.0） |
| 看今天该不该跑 | 对 DSH 说“调 `companion_status` 看今天该不该跑”，或跑 `cli.app status` / `cli.app schedule` |
| 手动跑一轮 | 对 DSH 说“`prepare_batch` 跑一轮，给我最多 3 个短包” |
| 看今天推荐了什么 | `<数据目录>/digests/digest-YYYYMMDD.md` |
| 批准一条评论草稿 | `cli.app drafts` → `cli.app approve <draft_id>` |
| 让评论真正发出去 | 先改 `config.yaml` 三重开关 → 在 DSH 里 `publish_draft` |
| 核对发出去的评论 | 看返回码：`OK` / `OK_PENDING_VERIFY`；后者等一会儿跑 `cli.app verify-pending` |
| 看私信 | 对 DSH 说“`dm_inbox`”，或跑 `cli.app dm-read` |
| 停一切公开动作 | 对 DSH 说“`pause`”；恢复用 `cli.app resume` |
| 出事了 | 先看 [故障排查](#12-故障排查)，再看 `cli.app status` 的健康状态 |

**三条默认红线**（装好就是这样，不是故障）：

- `interaction.mode = observe`
- `interaction.dry_run = true`
- `interaction.publish_enabled = false`

⇒ 任何公开写入（评论 / 点赞 / 收藏）都会被**代码层**拒绝，而不是靠提示词劝住。

---

## 2. 它是什么 / 不做什么

**是什么**：一个 B 站小号的学习与互动助手。

- 定时/手动发现候选视频 → 读简介/字幕/评论 → 豆包做廉价预处理；
- 原文**留在本机 bridge**，只把最多 3 个短候选包交给 DSH；
- DSH 是**唯一决策者**：最终判断、写评论草稿、决定是否推荐；
- 受控发布：9 道门禁 + 幂等 + 读回核验；
- 给主人发私信 / 写日报。

**不做什么**：

- 不轮换 IP/账号、不伪造客户端、不代解验证码、不绕过风控；
- 不把豆包的草稿直接当结论发出去；
- 不允许模型自己批准草稿 / 恢复停机（那些人用入口不在 MCP 工具里）；
- ✅ **打开 DSH 会自动跑一轮**（插件起 dm-watch，后者按 `schedule` 判定是否到期）；
- 但**公开写入（评论/点赞/收藏）仍默认关闭**，推荐只走私信。

---

## 3. 准备工作

| 项 | 要求 |
|---|---|
| 系统 | Windows 10/11（本项目只在 Windows 实测过） |
| Shell | Git Bash（命令示例都按它写） |
| Python | 3.12（用 `uv` 建 venv） |
| DSH | 桌面版（插件方式接入） |
| B 站 | 一个**专用小号**（不要用主号） |
| 豆包 | 火山方舟的模型 ID + API Key |
| 可选 | ASR：`faster-whisper` + `yt-dlp`（无字幕视频才用，约 1 GB） |

> ⚠️ **B 站协议 §5.6(3) 禁止未经书面许可用自动程序获取内容**。
> 本项目**没有**拿到平台许可；风险由使用者评估并承担。详见 [安全与合规](#11-安全与合规)。

---

## 4. 安装（复制命令即可）

```bash
export PYTHONIOENCODING=utf-8
cd D:/DeepSeek/projects/dsh-bilibili-companion

# 1) 建虚拟环境
uv venv --python 3.12 .venv

# 2) 装依赖（本机直连 pypi 不通，走清华镜像）
export UV_DEFAULT_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
uv pip install --python .venv/Scripts/python.exe \
    'mcp>=2,<3' 'httpx>=0.27' 'pydantic>=2' 'pyyaml>=6' 'typer>=0.12' \
    'pytest>=8' 'pytest-asyncio>=0.23' ruff mypy types-PyYAML tzdata

# 3) 配置
cp config.example.yaml config.yaml

# 4) 自检
./.venv/Scripts/python.exe -m cli.app status
```

> `uv` 报 `Failed to initialize cache` / 权限拒绝时：
> ```bash
> export UV_CACHE_DIR="D:/DeepSeek/workspace-temp/uv-cache-companion"
> ```

### 4.1 填配置（必须由人完成）

编辑 `config.yaml`：

| 项 | 说明 |
|---|---|
| `account.agent_mid` | **小号** UID；0 = 未配置（只读功能仍可用） |
| `account.owner_mid` | **主号** UID；0 = 未配置 → `@` 汇报不可验收 |
| `discovery.keywords` | 关注关键词，至少一项 |
| `text_worker.model` | 豆包模型 / 接入点 ID；空着 → `prepare_batch` 拒绝运行 |
| `text_worker.api_key_env` | **密钥所在环境变量名**（不是密钥本身） |

### 4.2 配豆包密钥（别写进 config.yaml）

密钥按「环境变量 → `HKCU\Environment`」解析。MCP 子进程会清洗 `*_KEY*` 环境变量，
所以**注册表兜底**是必须的。

1. Windows「系统属性 → 环境变量」新建变量（名字与 `api_key_env` 一致），值填 Key；
2. 自检：

```bash
./.venv/Scripts/python.exe -m cli.app status | grep text_worker
# 期望 text_worker_key_source 不是 missing，fingerprint 形如 "ARK_API_KEY:registry:len=46"
```

### 4.3 登录小号

```bash
# ① 在官方 App/网页用小号登录，导出 cookie 头到本机临时文件
#    （形如 SESSDATA=...; bili_jct=...; buvid3=...）
# ② 写入本机数据目录（值不会回显）
./.venv/Scripts/python.exe -m cli.app import-cookie /path/to/cookie.txt --yes

# 或扫码登录（终端出二维码）
./.venv/Scripts/python.exe -m cli.app login-qr

# ③ 核对
./.venv/Scripts/python.exe -m cli.app status | grep -A5 credentials
```

> **两套登录态互相独立**：本项目的 cookie ≠ agent 浏览器（ego）的 profile，各自登录一次。
> **永远不要把 cookie 贴进对话**。

---

## 5. 接进 DSH

插件方式（推荐）：装完设置里会多一页「B站小号」。

```bash
cd plugin && npm pack

export DSH_HOME="C:/Users/<你>/AppData/Roaming/dsh-desktop/harness"
EXE="/d/DSH/DSH-file/DSH Desktop/DSH Desktop.exe"
ENTRY="D:/DSH/DSH-file/DSH Desktop/resources/app.asar/node_modules/@deepseek-ai/dsh/lib/bin.js"

ELECTRON_RUN_AS_NODE=1 "$EXE" --expose-internals "$ENTRY" \
  plugin --profile web add "file:<tgz 绝对路径>"

# ★ 装完必跑两道体检，不过就别重启
node "$DSH_HOME/tools/check-plugin-form.mjs"
ELECTRON_RUN_AS_NODE=1 "$EXE" --expose-internals "$ENTRY" --profile web --dump-config > dump.txt
node "$DSH_HOME/tools/check-plugin-form.mjs" --dump dump.txt
```

顺序铁律：**装 → 形态体检 → 行归属检查 → 真实重启**。

> - 本地插件**不要用 `link:`**，必须 tgz（软链会让浏览器端注册两次 → 安全模式）。
> - 插件包的 `cordis.patch.yml` **只能有一行**（它自己）；MCP 行放 profile 层。
> - 完整踩坑见 [`docs/plugin-plan.md`](plugin-plan.md)。

重启后在 DSH 新会话里让模型调用 `mcp__bili_companion__companion_status`，
能看到状态就说明接入成功。

**不依赖 DSH 的自检**：

```bash
./.venv/Scripts/python.exe -m cli.app tools
# 期望：26 个工具 + "✅ 人用入口未出现在 MCP 工具列表里"
```

---

## 6. 三分钟验收清单

装完后逐条打勾（全绿才算装好）：

- [ ] `cli.app status` 能显示数据目录 / 健康 / 配额，且 `text_worker_key_source` 不是 missing
- [ ] `cli.app tools` 列出 26 个工具，且没有 `approve` / `resume` / `import-cookie`
- [ ] DSH 里 `companion_status` 能返回 `mode=observe` / `dry_run=true` / `publish_enabled=false`
- [ ] `cli.app schedule` 能说出“该不该跑 / 下一次”
- [ ] `scripts/run_once.py --keywords "..."` 能产出一份日报
- [ ] 设置页「B站小号」能打开、能改一个关键词并保存
- [ ] 监听面板 http://127.0.0.1:8848/ 能打开（如果开了 dm-watch / 插件自动监听）

---

## 7. 第一次运行

### 7.1 不通过 DSH 的只读试跑

```bash
./.venv/Scripts/python.exe -m cli.app schedule                 # 今天该不该跑
./.venv/Scripts/python.exe scripts/run_once.py --keywords "DeepSeek Harness"
```

产出：`<数据目录>/digests/digest-YYYYMMDD.md`（搜索词、候选数、完整阅读数、
推荐列表、证据链接、失败/待处理项）。

### 7.2 通过 DSH（推荐）

在 DSH 新会话里这样说：

```text
1) 调 companion_status，看今天该不该跑、配额与健康状态
2) 如果 due=true，调 prepare_batch 跑一轮（最多 3 个短包）
3) 把短包逐条讲给我：是什么 / 有什么用 / 为什么给我
4) 我挑中的，用 read_video 或 read_evidence_excerpt 核实后 save_analysis
```

> MCP 工具（别的会话）看不到原文全文；**自动调度**会把
> 「3 个短包 + 2500 规则整理后的完整内容」投递给**值班员会话**，
> 那个会话里的 DSH 才能看到完整内容。这是两条不同的通道。

---

## 8. 日常操作

### 8.0 打开 DSH 后自动跑（R1）

默认行为（插件配置 `watchAutoCycle=true`）：

```
DSH 启动
  → 5 秒后插件起 dm-watch（私信监听 + 学习轮调度）
  → 60 秒后检查 schedule：到期就自动跑一轮
  → 发现/阅读/豆包打分 → 补评论 → 短包+完整内容投给值班员会话
  → DSH 写推荐，用 dm_push 发 1~3 条私信
  → 轮询会话 turn/end，跑完才记 ok；超时记 delivered_unverified
```

要点：

- **不是开机自启**：DSH 退出，dm-watch 一起停；
- 先满足 `schedule`：暂停 / 平台阻断 / 未结束的上一轮 / 未到期 → **不跑**；
- 值班员会话不在线 → 只写日报，不发私信；
- 想关掉：插件配置 `watchAutoCycle=false`（或在设置页关；需重启 dm-watch）。

### 8.1 看状态

| 想知道 | 做法 |
|---|---|
| 今天该不该跑 / 跑了几轮 | `cli.app schedule` 或 `companion_status.schedule` |
| 健康状态 / 配额 / 凭据诊断 | `cli.app status` |
| @ 主号投递状态 | `cli.app mention-status` |
| 监听进程活着没 | 打开 http://127.0.0.1:8848/ 或看 `$DSH_HOME/bili-companion/watch.log` |
| 自动学习轮状态 | 看 `runs` 表最近一轮的 `conclusion`（`ok`/`delivered_unverified`/`delivery_failed`…） |

### 8.2 跑一轮

- **自动**：打开 DSH 后按 `schedule` 自己跑（§8.0）；
- DSH：`prepare_batch`（手动再跑一轮）；
- 命令行：`scripts/run_once.py --keywords "..."`；
- 插件设置页：「现在跑一轮」。

> `continuous` 模式是“每 30 分钟一轮 + 每天上限 8 轮”；
> 想“打开就跑一次”，把 `schedule.mode` 改成 `on_host_start`。

### 8.3 看日报

`<数据目录>/digests/digest-YYYYMMDD.md`。数据目录用 `cli.app status | head -5` 查。

### 8.4 私信

| 场景 | 做法 |
|---|---|
| 看主人发来的私信 | DSH：`dm_inbox`；命令行：`cli.app dm-read` |
| 回复某条私信 | DSH：`dm_reply(msg_seqno, text)` |
| 主动给主人发推荐 | DSH：`dm_push(text)`（接收者硬编码为主人） |
| 人给主人发一条 | `cli.app dm-send --text "..." --yes` |
| 私信里的分享视频入库 | `cli.app dm-ingest` |

> 监听进程的自动回复开关是 `dm-watch --reply` / 插件配置 `watchAutoReply`，
> 与视频评论的 `publish_enabled` **互相独立**。
> 豆包那条只是**先回占位**（不占 DSH 的正式回复），DSH 之后仍可用
> `dm_reply` 给正式回答；`dm_inbox` 里 `acked=true` 就表示豆包已先回过。

### 8.5 调整关键词 / 偏好

- 关键词：`config.yaml` 的 `discovery.keywords`，或设置页；
- 推荐流候选词：`cli.app keywords` 审阅后 `--accept`（**模型不能自己改关注词**）；
- 偏好（只影响排序）：让 DSH 调 `owner_preferences`，或 `cli.app prefs`。

### 8.6 兴趣学习与表达学习

**兴趣学习（R4，自动）**：

- DSH 在主人表达喜欢/不喜欢时调 `feedback(bvid, verdict)`；
- 系统按“净反馈”自动聚合成 `liked_ups / disliked_ups / liked_keywords /
  disliked_keywords`，进入打分 `focus`；
- 喜欢的 UP/关键词 → topic 至少 80；不喜欢的 → topic 最多 35；
- 想看聚合结果：`cli.app prefs`（偏好）或 `db.feedback_signals()`。

**表达学习（R6，人用触发）**：

```bash
./.venv/Scripts/python.exe -m cli.app learn-style
```

从**高分视频评论区**提炼 3~5 条表达技巧，写进 `owner_preferences`
的「表达风格（自动学习）」注记；之后豆包回复会自动带上。**不改 persona.md**。

### 8.7 暂停 / 恢复

| 动作 | 谁做 | 命令 |
|---|---|---|
| 暂停新公开动作 | DSH 可以 | `pause` |
| 恢复 | **只能人** | `cli.app resume --reason "..."` |
| 风控/验证码后恢复 | **只能人** | 先在官方 App 完成验证，再 `resume` |

### 8.8 监听面板

```bash
./.venv/Scripts/python.exe -m cli.app dm-watch            # 只记录，不发送
./.venv/Scripts/python.exe -m cli.app dm-watch --reply    # 豆包先回占位
./.venv/Scripts/python.exe -m cli.app dm-watch --cycle    # 随 DSH 自动跑学习轮
```

面板：http://127.0.0.1:8848/（状态 / 开始停止 / 事件流）。
插件配置在 `$DSH_HOME/bili-companion/config.json`；**不要手改 `watchSessionId`**，
会被插件内存里的 cfg 写回覆盖，要改用设置页或 `POST /api/bili-companion/config`。

---

## 9. 从草稿到发布

### 9.1 默认（review）流程

```text
DSH save_analysis → 生成 pending 草稿
        ↓
人：cli.app drafts --status pending      # 看内容与证据
        ↓
人：cli.app approve <draft_id>           # 批准
        ↓
DSH：publish_draft(draft_id) / publish_reply(draft_id)
        ↓
返回 OK            → 已读回核验，状态 sent_verified
返回 OK_PENDING_VERIFY → 已发出但还没核验到（审核延迟/翻页），状态 sent_pending_verify
        ↓
过一会儿：cli.app verify-pending         # 只读重试核验，匹配才升 sent_verified
```

**关键概念**：

- `sent_pending_verify` **不是失败**：rpid 已拿到，确定发过；只是还没读回作者/正文/BV。
- 待核验动作**占额度、锁幂等键、不会自动重发**。
- 查不到 ≠ 没发出（翻页/审核都会看不到）；**绝不**为了“凑成功”再发一次。

### 9.2 打开真实发布（谨慎）

只有**人**改 `config.yaml` 这三项，缺一不可：

```yaml
interaction:
  mode: "review"          # 或 auto
  dry_run: false
  publish_enabled: true
```

然后重启 DSH / 重连 MCP。发布前每次都会：读实际登录态比对 `agent_mid`、
检查平台健康、配额、幂等键、证据可反查。

### 9.3 点赞 / 收藏

DSH 调 `like_video` / `favorite_video`，与评论同一套门禁。
已赞/已藏会直接拒绝（`ALREADY_LIKED` / `ALREADY_FAVORITED`）。

---

## 10. 设置页

设置页有 **45 个可改字段**，按折叠分区：唤醒/频率、搜索筛选、配额、人设、
豆包、以及“只能去终端改的授权类”。

**永远不在设置页的 6 个字段**（防止点一下就绕过人审）：

| 字段 | 为什么 |
|---|---|
| `interaction.mode` | 决定能不能真的发东西 |
| `interaction.dry_run` | 同上 |
| `interaction.publish_enabled` | 同上 |
| `interaction.pause` | 安全停机，恢复必须由人做 |
| `interaction.dm_reply_enabled` | 一开就能真发私信 |
| `text_worker.api_key_env` | 凭据相关 |

白名单外的字段会被**明确拒绝**并在界面提示（`skipped`），不会静默吞掉。
完整字段说明见 [`docs/continuous-and-settings.md`](continuous-and-settings.md)。

---

## 11. 安全与合规

**代码层保证**（不靠提示词）：

- 原文不出 bridge；单次摘录 ≤500 字；每批 ≤3 个短候选包；
- 证据必须能反查（不存在的 ID / 跨 BV / 编造引文 / 时间戳不符 → 拒绝）；
- 发布只能引用已存草稿，不接受任意文本/URL；
- 身份每次从登录态读；`reserved` 点击前落库；`uncertain` 冻结不重发；
- 验证码 / 登录失效 / 平台故障 → 停一切新请求，**只能人工恢复**；
- 模拟与真实分开计数；日志与 MCP 返回过脱敏；
- 人用入口（approve/resume/import-cookie）不在 MCP 工具里。

**合规边界**：

- B 站用户协议 §5.6(3) 禁止未经书面许可用自动程序获取平台内容；
- 开放平台**没有**“发评论/回复”能力 ⇒ 评论互动没有合规授权通道；
- 限速/缓存/退避**不能保证**不触发风控；
- 明令不做：轮换 IP/账号、伪造客户端、改指纹、代解验证码、转包打码。

---

## 12. 故障排查

先跑 `cli.app status` 看健康状态与凭据诊断，再对号入座：

| 现象 | 原因与处理 |
|---|---|
| `TEXT_WORKER_NOT_CONFIGURED` | `text_worker.model` 为空 → 填模型 ID |
| `TEXT_WORKER_KEY_MISSING` | 密钥没设/名字不对 → 看 `text_worker_key_source`，设到用户环境变量 |
| 字幕总是 `description_only` | 未登录 → `import-cookie` / `login-qr` 后重试 |
| `verification_required` 后什么都干不了 | **设计如此**。人工完成验证 → `cli.app resume` |
| `-412` / `blocked` | 疑似风控 → 停机调查，**不要**加大重试；`resume` 前先确认原因 |
| `DRY_RUN` / `MODE_OBSERVE` 拒绝发布 | 正常，真实发布要人改三重开关 |
| `TARGET_FROZEN_UNCERTAIN` | 有结果未知的动作 → 到官方页面人工核对，不会自动重发 |
| 自动轮没跑 | 依次查：暂停？平台阻断？上一轮未结束？未到期？值班员会话不在线？看 `runs` 最近一条 conclusion |
| `conclusion=delivery_failed` | 值班员会话没配/没激活 → 在 DSH 里打开该会话并选一次模型 |
| `conclusion=delivered_unverified` | 已投递但没等到 turn/end 完成 → 看会话是否报错，或调大 `DEFAULT_WAIT_SECONDS` |
| `conclusion=no_candidates` | 本轮没有新的候选（可能都读过/分数不够），不是故障 |
| 豆包先回占位后 DSH 回不了 | 老版本会这样；v15 起占位不占 `dm_replies`，DSH 仍可 `dm_reply` |
| 返回 `OK_PENDING_VERIFY` | **正常**：已发出、待读回。等一会儿 `cli.app verify-pending` |
| `MODEL_INPUT_CHUNKS_n` | 这条视频的证据被分成 n 块送豆包（长视频）——这是正常限制标记 |
| `MODEL_CHUNK_FAILED_n` | 有 n 块模型调用失败；候选会标深审，不会把部分覆盖当全文 |
| `MODEL_INPUT_SAMPLED_x_OF_y` | 证据块数超过上限被等距抽样；该候选**强制深审** |
| `truncated` 里有候选/证据计数 | 触发了整包字符预算；被裁掉多少都写在里面 |
| MCP 工具在 DSH 里不出现 | 看日志里 `mcp-bili-companion`；跑两道插件体检；确认已真实重启 |
| `schedule` 报无法解析时区 | 缺 `tzdata` → `uv pip install tzdata` |
| `mention-status` 全是 `unsupported` | 正常：`@` 投递通道未接，或 `owner_mid=0` |
| 数据库打不开 / 迁移失败 | 先用备份恢复（[§13](#13-数据备份与迁移)），再查是否被旧进程占用 |
| 设置页打不开 | 插件没加载 → 两道体检 + 重启；看 DSH 日志里的 client 报错 |

---

## 13. 数据、备份与迁移

**数据目录**（按优先级探测可写）：

1. `COMPANION_DATA_DIR`（显式指定，不回退）
2. `%LOCALAPPDATA%\dsh-bilibili-companion\`
3. `%APPDATA%\dsh-bilibili-companion\`
4. `<项目根>/.local-data/` ← 本机实际落点
5. 系统临时目录（兜底）

查看：

```bash
./.venv/Scripts/python.exe -m cli.app status | head -5
```

里面通常有：`companion.db`（SQLite，**唯一真相源**）、`digests/`（日报）、
`npm-cache/`、`.hf-cache/`（ASR 模型）、cookie（凭据，勿拷给别人）。

**备份**（停掉监听/DSH 后做）：

```bash
DATA="D:/DeepSeek/projects/dsh-bilibili-companion/.local-data"
tar -czf "backup-$(date +%Y%m%d).tgz" "$DATA/companion.db" \
    "$DATA"/digests "D:/DeepSeek/projects/dsh-bilibili-companion/config.yaml"
```

**恢复**：停进程 → 覆盖回 `companion.db`（并删掉旧的 `companion.db-wal` / `-shm`）
与 `config.yaml` → 启动（schema 会自动迁移）。

**schema 迁移**：每次升级自动执行，只能追加、不会改历史版本。
2026-10-18 已到 **v15**（发布读回 + 豆包占位与 DSH 正式回复分离）。升级前**先备份**。

---

## 14. 升级与回滚

```bash
cd D:/DeepSeek/projects/dsh-bilibili-companion

# 1) 备份（至少 config.yaml + 数据目录）
# 2) 拉代码
git pull origin main

# 3) 依赖有变化时，按安装一节第 2 步重跑（幂等，可整段复制）

# 4) 质量门（可选，开发者）
./.venv/Scripts/python.exe -m pytest tests/ -q

# 5) 插件有改动时重新打包安装（见 §5）
# 6) 真实重启 DSH（改 host 代码只热重载不生效）
```

**回滚**：`git checkout <上一个 commit>` → 重新装插件（若插件改过）→ 重启。
**数据库不会降级**：回滚代码前先用备份恢复数据库，或接受新表被旧代码忽略。

---

## 15. 已知限制

| # | 限制 | 影响 |
|---|---|---|
| 1 | **自动轮已实现，但依赖值班员会话在线** | 会话不在线时只写日报、不发私信；自动轮不会替你打开 DSH 会话 |
| 2 | **token 只有架构估算（#6）** | 83.7% 是估算，不是真实 DSH usage；真实费用未验收 |
| 3 | **真实评论读回无样本（#4）** | 机制已实现并测试，但还没有真实 rpid 跑过 |
| 4 | **`@` 投递通道未接** | `mention-status` 默认 `unsupported`，不假称已通知 |
| 5 | **兴趣学习是规则聚合，不是大模型总结（R4）** | 只看“UP / 发现关键词”的净反馈；更细的兴趣归纳还没做 |
| 6 | **表达学习是人用触发（R6）** | `cli.app learn-style` 才会更新；不会每轮自动改人设 |
| 7 | **多平台只预留框架** | 抖音 / 小红书未接入 |
| 8 | **ASR 依赖重（约 1 GB）** | 默认关；缺依赖只降级，不崩 |
| 9 | **只在 Windows 实测** | 浏览器探测/注册表/creationflags 都是 Windows 假设 |
| 10 | **无 CI** | 质量门目前靠本机命令，不靠 GitHub Actions |

---

## 16. 命令与文档速查

### 人用 CLI（**不在**模型工具里）

```bash
./.venv/Scripts/python.exe -m cli.app status            # 状态/健康/配额/凭据诊断
./.venv/Scripts/python.exe -m cli.app schedule          # 该不该跑
./.venv/Scripts/python.exe -m cli.app drafts            # 草稿
./.venv/Scripts/python.exe -m cli.app approve <id>      # 批准
./.venv/Scripts/python.exe -m cli.app reject <id>       # 拒绝
./.venv/Scripts/python.exe -m cli.app resume --reason "…"  # 恢复暂停/风控
./.venv/Scripts/python.exe -m cli.app import-cookie <file> --yes
./.venv/Scripts/python.exe -m cli.app login-qr
./.venv/Scripts/python.exe -m cli.app verify-pending    # 只读重试待核验评论
./.venv/Scripts/python.exe -m cli.app verify-recent     # 复核公开可见性
./.venv/Scripts/python.exe -m cli.app digest            # 生成日报
./.venv/Scripts/python.exe -m cli.app keywords          # 候选关键词池
./.venv/Scripts/python.exe -m cli.app prefs             # 主人偏好
./.venv/Scripts/python.exe -m cli.app learn-style       # 从评论学表达技巧（R6）
./.venv/Scripts/python.exe -m cli.app dm-read
./.venv/Scripts/python.exe -m cli.app dm-send --text "…" --yes
./.venv/Scripts/python.exe -m cli.app mention-status
./.venv/Scripts/python.exe -m cli.app audit-persona
./.venv/Scripts/python.exe -m cli.app tools             # 26 个模型工具（对照）
```

### 模型工具（26 个，只通过 DSH 调）

`companion_status` `expand_intent` `trends_suggest` `discover_videos` `prepare_batch`
`read_video` `read_evidence_excerpt` `get_discussion` `save_analysis` `search_notes`
`list_drafts` `publish_draft` `check_interactions` `prepare_reply` `publish_reply`
`dm_inbox` `dm_reply` `dm_push` `find_repo` `read_source` `web_search` `like_video`
`favorite_video` `owner_preferences` `feedback` `pause`

### 文档地图

| 想知道 | 看 |
|---|---|
| 装/配置/技术细节 | [`README.md`](../README.md) |
| 接手：现在在哪、下一步 | [`HANDOFF.md`](../HANDOFF.md) |
| 已验收 / 未验收 | [`docs/acceptance.md`](acceptance.md) |
| 生产闭环（R1）说明 | [`docs/production-loop-plan.md`](production-loop-plan.md) |
| 设置页与持续跑 | [`docs/continuous-and-settings.md`](continuous-and-settings.md) |
| 定时接线 | [`docs/schedule-wiring.md`](schedule-wiring.md) |
| 人设与红线 | [`docs/persona.md`](persona.md) |
| 插件安装踩坑 | [`docs/plugin-plan.md`](plugin-plan.md) |
| 私信监听面板 | [`docs/dm-watch-panel.md`](dm-watch-panel.md) |
| 环境与许可边界 | [`docs/compatibility.md`](compatibility.md) |
