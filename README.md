# dsh-bilibili-companion

**给 DSH 一个专用 B 站小号**：定时自己找视频、读字幕、判断值不值得推荐、
写评论草稿、受控发布、回复讨论，并把值得看的视频 `@` 汇报给主号。

按 [`D:/DeepSeek/docs/DSH_EXECUTION_BRIEF.md`](../../docs/DSH_EXECUTION_BRIEF.md)（310 行权威任务书）实现。

---

## ⚠️ 先读这三条

| # | 事实 |
|---|---|
| 1 | **本方案未获 B 站书面许可。** 用户协议 §5.6(3) 明确禁止未经书面许可用自动程序获取平台内容。「能运行」**不等于**「已许可」。风险由使用者评估并承担。 |
| 2 | **默认姿态是"什么都不发"**：`observe` + `dry_run: true` + `publish_enabled: false`。真实发布需要主人显式改配置**并**提供小号登录与主号 UID。 |
| 3 | **当前状态：真实读取与真实发布都未验收**（本机未提供 `agent_mid` / `owner_mid` / 小号登录）。详见 [`docs/acceptance.md`](docs/acceptance.md)。 |

---

## 它长什么样

```
DSH Agent（唯一决策者：选题、复核证据、写最终文字、日程唤醒）
   │  官方 @deepseek-ai/dsh-mcp-client（stdio）
   ▼
本项目 Python MCP bridge（25 个窄工具，命名空间 bili_companion）
   ├── 读取适配器      自建 httpx（零 GPL）+ WBI 签名
   ├── 豆包文字 worker  分块提取 → **证据反查** → 短候选包
   ├── 受控发布         9 道门禁 + 幂等键 + reserved/uncertain 状态机
   └── 状态             SQLite（WAL）+ 日报 Markdown
```

**核心是 token 架构**：原始字幕/评论**留在 bridge 内**，
DSH 只拿 ≤3 个短候选包，必要时按 `evidence_id` 取 ≤500 字摘录。
估算对照见 [`docs/token-experiment.md`](docs/token-experiment.md)：DSH 侧**估算**降低 **83.7%**
（架构对照，`measurement=estimated`，**不是**真实 token/费用验收）。

---

## 安装

前置：Windows、Git Bash、`uv`（[安装](https://docs.astral.sh/uv/)）。

```bash
export PYTHONIOENCODING=utf-8
cd D:/DeepSeek/projects/dsh-bilibili-companion

# 1) 建虚拟环境（Python 3.12）
uv venv --python 3.12 .venv

# 2) 装依赖（本机直连 pypi.org 不通，走清华镜像）
export UV_DEFAULT_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
uv pip install --python .venv/Scripts/python.exe \
    'mcp>=2,<3' 'httpx>=0.27' 'pydantic>=2' 'pyyaml>=6' 'typer>=0.12' \
    'pytest>=8' 'pytest-asyncio>=0.23' ruff mypy types-PyYAML tzdata

# 3) 配置
cp config.example.yaml config.yaml
#    然后编辑 config.yaml（下一步细说）

# 4) 自检
./.venv/Scripts/python.exe -m cli.app status
```

> ⚠️ **本机实测**：`uv` 的默认缓存在 `%LOCALAPPDATA%\uv\cache` 会**权限拒绝**。
> 若报 `Failed to initialize cache`，加上：
> ```bash
> export UV_CACHE_DIR="D:/DeepSeek/workspace-temp/uv-cache-companion"
> ```

### 数据放哪（重要）

本项目**不假定** `%LOCALAPPDATA%` 可写——本机实测它**不可写**。
数据目录按优先级**探测可写性**决定：

1. `COMPANION_DATA_DIR`（显式指定，**不回退**）
2. `%LOCALAPPDATA%\dsh-bilibili-companion\`
3. `%APPDATA%\dsh-bilibili-companion\`
4. `<项目根>/.local-data/` ← **本机实际落点**
5. 系统临时目录（兜底）

**查看实际位置**：

```bash
./.venv/Scripts/python.exe -m cli.app status | head -5
```

---

## 配置

```bash
cp config.example.yaml config.yaml   # config.yaml 已 gitignore
```

必须由人填写的项：

| 项 | 说明 |
|---|---|
| `account.agent_mid` | **小号** UID。0 = 未配置（只读功能仍可用）。 |
| `account.owner_mid` | **主号** UID。0 = 未配置 → **`@` 汇报不可验收**。 |
| `discovery.keywords` | 兴趣关键词，至少一项。 |
| `text_worker.model` | 豆包模型/接入点 ID。**空着 → 批处理拒绝运行**（见下）。 |
| `text_worker.api_key_env` | **密钥所在的环境变量名**（不是密钥本身）。 |

### 豆包密钥怎么配

**密钥绝不写进配置文件。** 本项目按「环境变量 → `HKCU\Environment`」顺序解析
（`core/secrets.py`）。

为什么必须读注册表（两条实测原因）：

1. **MCP stdio 子进程的环境会被清洗**：官方客户端会删除匹配
   `/KEY|PASSWORD|SECRET|TOKEN/i` 的名称与所有 `DSH_*` 名称。
   所以 `ARK_API_KEY` 这类名字**传不进** bridge。
2. **DSH 由 `explorer.exe` 启动**，继承的是旧环境块；
   在「系统属性 → 环境变量」新设的变量，**重启 DSH 也读不到**。

→ 读 `HKCU\Environment` 同时化解这两条。设好后自检：

```bash
./.venv/Scripts/python.exe -m cli.app status | grep text_worker
# 期望 text_worker_key_source 不是 "missing"，且 fingerprint 形如
# "ARK_API_KEY:registry:len=46"（只报来源与长度，永不回显值）
```

> `text_worker.enabled: true` 但模型 ID/密钥缺失时，`companion_status` 报
> `TEXT_WORKER_NOT_CONFIGURED`，且 `prepare_batch` **拒绝运行**——
> 这是刻意的：任务书禁止在豆包不可用时偷偷把全文灌给 DSH。

### 登录小号（人用入口）

凭据只在 bridge 进程内使用，**永不**进日志/上下文/git/MCP 返回。

```bash
# ① 在官方 App 或页面用小号登录，导出 cookie 头到一个本机临时文件
#    （形如 "SESSDATA=...; bili_jct=...; buvid3=..."）
# ② 写入本机数据目录（值不会在终端回显）
./.venv/Scripts/python.exe -m cli.app import-cookie /path/to/cookie.txt --yes
# ③ 核对
./.venv/Scripts/python.exe -m cli.app status | grep -A5 credentials
```

> ⚠️ **两套登录态互相独立**：本项目的 `cookie.txt` ≠ agent 浏览器（ego）的 profile。
> **各自登录一次。**

---

## 在 DSH 里接入（**插件方式**，推荐）

已经做成一个**能装的 DSH 插件**：装完设置里会多出一页「**B站小号**」，
可以看状态、改关键词、点「现在跑一轮」、看日报。

```bash
cd plugin
npm pack                                    # 打成 tgz

export DSH_HOME="C:/Users/<你>/AppData/Roaming/dsh-desktop/harness"
EXE="/d/DSH/DSH-file/DSH Desktop/DSH Desktop.exe"
ENTRY="D:/DSH/DSH-file/DSH Desktop/resources/app.asar/node_modules/@deepseek-ai/dsh/lib/bin.js"

ELECTRON_RUN_AS_NODE=1 "$EXE" --expose-internals "$ENTRY" \
  plugin --profile web add "file:<tgz 绝对路径>"

# ★★ 装完必跑这两道体检 —— 不过就别重启
node "$DSH_HOME/tools/check-plugin-form.mjs"
#（形态一致 + bundles 无孤儿行）
ELECTRON_RUN_AS_NODE=1 "$EXE" --expose-internals "$ENTRY" --profile web --dump-config > dump.txt
node "$DSH_HOME/tools/check-plugin-form.mjs" --dump dump.txt
#（声明 dsh.client 的包必须恰好 1 行）
```

**顺序铁律：`装 → 形态体检 → 行归属检查 → 真实重启`。**

> ⚠️ **本地插件不要用 `link:`** —— 软链形态会让浏览器端 bundle **注册两次**，
> 直接进安全模式。必须 `npm pack` 成 tgz（pnpm 解包为**真实目录**）。
>
> ⚠️ **插件包里的 `cordis.patch.yml` 只允许一行**（它自己）。
> 额外行（如 MCP 客户端）要放 **profile** 的 patch ——
> 否则两行都归属同一个包，浏览器端挂载两次 → 同样进安全模式。

完整施工记录、踩坑与自检清单见 [`docs/plugin-plan.md`](docs/plugin-plan.md)。

### 备选：手工合并片段

不想装插件时，仍可把 [`dsh/cordis.patch.fragment.yml`](dsh/cordis.patch.fragment.yml)
合并进 `%APPDATA%\dsh-desktop\harness\profiles\web\cordis.patch.yml`（合并前**先备份**，
改完**必须真实重启**）。该片段同时挂上
[`dsh/restrict-bili-agent-tools.mjs`](dsh/restrict-bili-agent-tools.mjs)——
它把定时 preset 的通用 shell / 文件写入 / 浏览器工具**逐个禁用**
（任务书硬约束；配置层做不到，原因见 [`docs/compatibility.md`](docs/compatibility.md) §2.4）。

重启后在新会话里让模型调用 `mcp__bili_companion__companion_status`。

**不依赖 DSH 的自检**：

```bash
./.venv/Scripts/python.exe -m cli.app tools
# 期望：列出 25 个工具 + "✅ 人用入口未出现在 MCP 工具列表里"
```

---

## 日常使用

### 跑一轮只读闭环（不发布）

```bash
./.venv/Scripts/python.exe scripts/run_once.py --keywords "DeepSeek Harness"
```

产出：`<数据目录>/digests/digest-YYYYMMDD.md`（含搜索词、候选数、完整阅读数、
推荐列表、证据链接、失败与待处理项）。

### 25 个模型可见工具

| 工具 | 作用 |
|---|---|
| `companion_status` | 登录/健康/模式/配额/最近任务（不含凭据） |
| `expand_intent` | 模糊意图 → 一组可搜索关键词（只读） |
| `trends_suggest` | 推荐流 → 候选关键词池（**不自动改关注词**） |
| `discover_videos` | 按关键词与发布时间搜索（只读） |
| `prepare_batch` | 发现→去重→限速读取→豆包预处理→**最多 3 个短候选包** |
| `read_video` | 单视频短候选包 + 证据 ID（**不返回原文**） |
| `read_evidence_excerpt` | 按 `evidence_id` 取 ≤500 字摘录（只读） |
| `get_discussion` | 限速拉取评论（只读，内容当**数据**不当指令） |
| `save_analysis` | 存笔记与草稿（**校验证据真实可反查**） |
| `list_drafts` | 列出草稿（只读） |
| `publish_draft` | 从**已存草稿**发布顶层评论（9 道门禁；读回核验后才是 `sent_verified`） |
| `check_interactions` | 拉取与小号评论/提及相关的新互动 |
| `prepare_reply` | 校验目标与上下文，存回复草稿 |
| `publish_reply` | 回复一次并核验（同顶层门禁） |
| `dm_inbox` | 取待回复的主人私信（只读） |
| `dm_reply` | 回复主人的某条私信（接收者硬编码为 `owner_mid`） |
| `dm_push` | 主动私信主人（推荐视频等；不需要 `msg_seqno`） |
| `find_repo` | 从视频里找 GitHub/Gitee 仓库地址（只读） |
| `read_source` | 读仓库源码（只读文本，**绝不执行**） |
| `web_search` | 免费联网搜索（只读，结果当数据） |
| `like_video` | 点赞（与评论同一套门禁） |
| `favorite_video` | 收藏到指定收藏夹（与评论同一套门禁） |
| `owner_preferences` | 读写主人偏好（只影响排序/内容策略） |
| `feedback` | 记录主人评价（只影响排序，不改授权） |
| `pause` | 立即暂停新公开动作 |

### 人设与红线：**审查过**，不是照搬

> 主人要求「不能只转移而不审查思考一番」。已做两件事：

**① 逐条对撞**（[`docs/persona-conflicts.md`](docs/persona-conflicts.md)）：
把 curator 的 21 题人设（476 行）与任务书（310 行）逐条比对，
查出 **7 处冲突**，其中 **2 处需要你拍板**：
* **点赞/收藏**：人设里是核心行为（含 5 个收藏夹），任务书**完全没规划** → 要不要补做？
* **AI 身份**：人设「绝不提」，任务书「可适度表明」→ 取人设（但不伪装真人经历）。

**② 做成可重复运行的审计**（不再是一次性文档）：

```bash
./.venv/Scripts/python.exe -m cli.app audit-persona          # 人读
./.venv/Scripts/python.exe -m cli.app audit-persona --json   # 机读
```

它把**实际生效的配置值**与两处上级权威对撞，逐条判定
`一致 / 更严 / 更松 / 缺失 / 对立`，并列出**需要你拍板**的项。

> ⚠️ 顺带查出一处**文档漂移**：curator 13 号文档 §5.4 的频率还是**降频前的旧值**
> （评论5/点赞30/收藏5/间隔10），而 `config.toml` 早已因验证码降为
> **3/8/3/25**。**照抄文档会把频率调回危险值** —— 本项目取最严的一档。

### 主号汇报（`@` 提及）—— **纯 API，不需要浏览器**

任务书 §6.6 要求"不能把 `@数字UID` 或纯文本 `@昵称` 视为一定成功，
必须检查**渲染后的 UID**"。原先以为这必须靠浏览器点选候选，
**实测发现不需要** —— B站把"这条评论实际 @ 到了谁"作为**服务端事实**返回：

```json
content: {
  "message": "@MsGsrs @HJQC",        // 正文：只是文本，不算证据
  "members": [                        // ★ 服务端确认的 @ 结果
    {"mid": 282831595, "uname": "MsGsrs"},
    {"mid": 351873378, "uname": "HJQC"}
  ]
}
```

**真机验证**（BV1nFp46qECm）：

| 核验场景 | 结果 |
|---|---|
| 核验**真被 @ 的人** | `verified` ✅ |
| 核验**没被 @ 的主号** | `failed`，并报出实际 @ 到谁 ✅ |

**四态如实记录**：

| 状态 | 含义 |
|---|---|
| `verified` | `members` 里确有 `owner_mid` → B站确认提醒已建立 |
| `uncertain` | 读不回 / 评论还没发 / `members` 为空（正文的 `@` 只是文字） |
| `failed` | @ 到了**别人**（同名不同 UID） |
| `unsupported` | 缺 `owner_mid`，或**缺昵称**（B站按昵称匹配，数字 UID 不产生提醒） |

**两道独立防线**：`try_mention()` 写库前判定；存储层
`settle_owner_notification(status='verified')` 再断言 `observed_mid == owner_mid`。

> ✅ **`owner_name_hint` 现在是必需项**（不是可选提示）——
> B站按昵称匹配提及。已在 `config.yaml` 里填好（沐雨林心）。

```bash
./.venv/Scripts/python.exe -m cli.app mention-status          # 投递记录与统计
./.venv/Scripts/python.exe -m cli.app mention-check BV1xxxx    # 按"双峰"规则判定
```

### 本地 ASR 兜底（无字幕视频，**默认关**）

字幕不可用的视频，可以让它**在本机转写一遍**（**不花 API 钱**）。
开关：`text_worker.asr_fallback: true`。

**装依赖（⚠️ 版本必须钉住）**：

```bash
export UV_CACHE_DIR="D:/DeepSeek/workspace-temp/uv-cache-companion"
uv pip install --python .venv/Scripts/python.exe \
  faster-whisper yt-dlp 'av<16' 'huggingface_hub<2'
```

> 🔴 **为什么必须写 `av<16` / `huggingface_hub<2`**（真机实测踩到）：
> `faster-whisper` 的依赖声明**过宽**，裸装最新版会**直接跑不起来**：
>
> ```
> 转写失败：open() got an unexpected keyword argument 'metadata_errors'
> ```
>
> 原因是 `faster_whisper/audio.py` 仍在传 `metadata_errors`，
> 而 **PyAV ≥16 / huggingface_hub ≥2 已移除它**。
> **这个报错完全看不出是版本问题** —— 我第一次就误判成"缓存没命中"。
>
> 实测跑通的组合：`faster-whisper==1.2.1` + `av==15.1.0` + `huggingface-hub==1.33.0`。
> `companion_status` 的 `asr.versions_ok` 会**提前**把这类不兼容报出来。

**预下模型**（可选，但强烈建议 —— 免得第一次转写干等）：

```bash
./.venv/Scripts/python.exe scripts/fetch_model_modelscope.py --model tiny
```

> ★ **镜像源实测**：竞品默认的 `hf-mirror.com` 在本机**下到 32.6MB 就断流**
> （10 分钟不再增长）；**ModelScope（魔搭）6 秒下完 75.5 MB**（12 MB/s，快约 100 倍）。
> 所以 `HF_ENDPOINT` 默认已改为 `https://modelscope.cn/models`。

**跑一次**：

```bash
./.venv/Scripts/python.exe scripts/asr_transcribe.py BV1t3JX65Exc --model small
```

> ⚠️ **"空"是一种结果，不是坏了**：转写返回空 ⇒ **这条视频没有语音**
> （纯音乐 / 纯录屏 / 无声演示）⇒ 按「跳过（无语音）」处理，
> **不要重试、不要写调试脚本**（有项目实测因此烧掉 27 次工具调用）。

### 两种搜索方式

| 方式 | 用途 | 入口 |
|---|---|---|
| **① 模糊意图 → 关键词** | "我大概想看什么" | MCP 工具 `expand_intent` |
| **② 推荐流 → 候选词** | 发现**你想不到的**词 | MCP 工具 `trends_suggest` + `cli.app keywords` |

**① 怎么用**：直接跟 agent 说意图，它会调 `expand_intent` 拿到关键词，再搜。

**② 为什么需要你过一眼**（设计决定，不是麻烦）：

> B 站推荐流是**个性化算法**喂的。实测一屏 8 条里只有 2~3 条与我们相关，
> 其余是鬼屋 / 原神 / 影视飓风这类高互动内容。
> 若候选词**自动**写进关注关键词，**小号的兴趣画像就被算法带跑了**。

所以：推荐流只**攒候选**，采纳与否由你决定：

```bash
./.venv/Scripts/python.exe -m cli.app keywords              # 看候选池（带来源 BV，可审计）
./.venv/Scripts/python.exe -m cli.app keywords --accept "桌面端"   # 采纳 → 写进 config.yaml
./.venv/Scripts/python.exe -m cli.app keywords --drop "面端"       # 丢弃
```

> ★ 采纳时**只动 `discovery.keywords`**；授权字段
> （`mode` / `dry_run` / `publish_enabled`）**一字不动**，注释也不会丢。

**严格限额**（不只是文档里写写）：

| 限额 | 值 | 说明 |
|---|---|---|
| 一次最多看几条推荐 | **8** | 它只用来"发现新词"，不是刷视频 |
| **一天最多几轮** | **2** | 跑满后 `trends_suggest` 直接返回 `RATE_LIMITED` |

轮次记在**独立账本** `trends_rounds` 里 —— 刻意**不写进 `runs`**，
否则"刷一次推荐流"会让调度器误以为**主流程今天已经干过活了**，
当天不再抓取/评论。当前用量见 `companion_status.trends.rounds`。

**想让它自动回填**（不用手动审）：

```yaml
discovery:
  auto_accept_suggestions: true   # 默认 false
```

> ⚠️ 代价说清：打开后候选词**直接**写进 `keywords`，
> 等于让**算法间接决定小号关注什么**（推荐流是算法喂的）。
> 换来的是"不用手动过目"。
>
> ★ 但**无论开关如何**，每日轮次上限（2）与单次条数上限（8）**始终生效** ——
> 开关只决定"词怎么进 keywords"，**不决定"能跑几轮"**。

### 多平台（抖音 / 小红书：**只预留框架**）

```bash
./.venv/Scripts/python.exe -m cli.app platforms
```

| 平台 | 状态 | 接入方式 |
|---|---|---|
| **B 站** | ✅ 已接入 | 网页只读（含 WBI 签名） |
| **抖音** | ⏳ 未接入 | 规划走**官方授权通道** |
| **小红书** | ⏳ 未接入 | 规划走**官方授权通道** |

**为什么抖音/小红书没做**（如实说明）：

- **抖音**：web 接口需要**三件套签名**（`a_bogus` + `timestamp` +
  `x-secsdk-web-signature`，其中 secsdk 是 177KB 混淆运行时）。
  **逆向签名属于绕过平台技术保护措施** —— 本项目不做。
- **小红书**：没有公开的只读 API，且**已有生效判决**认定
  「绕开技术保护措施抓取」违法（常州天宁法院**刑事案**、上海知产法院**判赔 110 万**）。

**框架已就位**：新增 `core/platforms.py`（`PlatformAdapter` 协议 +
能力声明 + 占位 adapter）。将来某平台开了正规 API，
**接进来只是加一个 adapter**，不用改 pipeline / 门禁 / 日报那条链。

> ★ 未实现的平台调用时**明确抛错**（`CapabilityNotImplemented`），
> **绝不返回空列表** —— 空列表会被上层误解成"有这能力但这次没内容"，
> 从而走进完全错误的降级路径。

### 取源码的通道（GitHub 部分主机不通）

```bash
./.venv/Scripts/python.exe -m cli.app sources                          # 看通道一览
./.venv/Scripts/python.exe -m cli.app sources --repo psf/requests      # 现场测一个仓库
```

> ⚠️ **不是"GitHub 不通"，而是"部分主机不通"** —— 这**也不是 DSH 沙箱限制**
> （沙箱拒绝会打印 `[sandbox: file access denied]`，而这里是 DNS/连接层失败）。

| 用途 | 可用通道 | 实测 |
|---|---|---|
| 下整个仓库 zip | `codeload.github.com` | ✅ |
| 仓库元数据 | `api.github.com` | ✅ |
| **读单个文件** | `cdn.jsdelivr.net/gh/...` | ✅ **首选** |
| 读单个文件（兜底） | `gh-proxy.com` / `ghproxy.net` | ✅ |
| 国内镜像项目 | `gitee.com`（⚠️ 探测用 GET，HEAD 返 405） | ✅ |
| 网页 / raw 直连 | ❌ shell 不通 → 用 jsdelivr 或**浏览器** | ❌ |

**已固化成** [`core/sourcefetch.py`](core/sourcefetch.py)：
多源自动兜底、**每个源留痕可审计**、zip 解压**防路径穿越**。

> ★ 另外：**浏览器能访问 shell 不通的主机**（走另一条网络路径）。
> 实测浏览器打开 `raw.githubusercontent.com` 正常 —— 这是最后的人工兜底。

### 持续跑（想让它更勤）

默认是"打开 DSH 跑一轮"。想让它**一直跑**：

```yaml
schedule:
  mode: "continuous"      # on_host_start | daily | continuous
  interval_minutes: 30    # 两轮间隔（≥5）
  max_runs_per_day: 8     # 跑满自动停
```

> ⚠️ **刻意不做无限循环** —— 那会明显增加请求量与风控风险。
> 改成"每 N 分钟一轮 + 每天上限 + **到点自动停**"，且仍走**全部**既有门禁
> （配额 / 健康 / 幂等 / 发布门禁 / 谷时）。
>
> 状态可见：`companion_status.schedule` 会报 `runs_today` / `max_runs_per_day`。

### 设置页能改 45 个字段（折叠分区）

设置 → B站小号：按主题分成 5 组折叠面板（常用两组默认展开）。

| 分组 | 内容 |
|---|---|
| 什么时候自己跑 | 唤醒方式、谷时、持续跑间隔与上限 |
| 挑什么、挑多少 | 只看几天、候选数、完整读数、分数线 |
| 每天最多做多少 | 评论/回复/点赞/收藏配额、动作间隔 |
| 人设 | 自称、小名、点赞收藏阈值、各动作开关 |
| 豆包 | 模型、超时、字数上限、ASR |

> ★ **授权类字段刻意不在界面上**（`mode` / `dry_run` / `publish_enabled` /
> `pause` / `dm_reply_enabled` / `api_key_env`）—— 界面上点一下就生效
> 等于绕过了"人审"。设置页会**明确列出这 6 个**并告诉你去终端改。
>
> 详见 [`docs/continuous-and-settings.md`](docs/continuous-and-settings.md)。

### 做视频（Multiplane）

给 agent 用的 HTML 视频渲染器，已装好并**真跑通**：

```bash
multiplane doctor                              # 体检
multiplane lint shot.html                      # 查确定性
multiplane render shot.html --out v.mp4 --draft --from 0 --to 3
```

> 它是**独立 CLI，不是 DSH 插件**。安装踩坑与 shot 契约见
> [`docs/multiplane-setup.md`](docs/multiplane-setup.md)。
> 配套字体与设计规范在 [`design/`](design/)（25 款字体 + 16 风格）。

### 定时运行怎么接线

任务书 §7 任务 7 引用官方 Schedule 的**三条语义**，本项目据此分工：

| 关注点 | 由谁负责 |
|---|---|
| "什么时候唤醒" | DSH Schedule（宿主级） |
| "这一轮该不该跑" | 本项目 `is_due()`（读 `runs` 表判断今天跑完没有） |
| "有没有重复发布" | 本项目幂等键 + `uncertain` 冻结（**不依赖**调度器） |
| "任务做成没有" | 本项目 `runs`/`actions` 表（**最终事实来源**，不看投递记录） |

查看调度状态（人用，只读）：

```bash
./.venv/Scripts/python.exe -m cli.app schedule
# daily_time = 10:00  timezone = Asia/Shanghai
# 上次运行 = （从未运行）
# 下一次   = 2026-10-15T12:09:03+08:00  （已过点且今天尚未完成 → 立即补跑）
# 现在是否到期 = 是（补跑）
```

**两个"上次"承担不同职责**（这是抗"不保证恰好投递一次"的关键）：

* **已完成** → 判断"今天这轮的活干完了吗"（崩溃的轮次不算数）；
* **已尝试** → 配合最小间隔（默认 120 分钟）挡住**并发窗口**：
  两次唤醒只隔几秒、第一次还没结束时，第二次不该再跑。

详见 [`docs/schedule-wiring.md`](docs/schedule-wiring.md)。

> ⚠️ **别把唤醒时间同时写在 DSH 和系统计划任务里**。两个调度器同时唤醒
> 会被单实例锁挡住（不会双发），但会产生令人困惑的失败记录。选一个。

> ⚠️ Windows 的 Python **不自带 IANA 时区库**，需要 `tzdata`（已列入依赖）。
> 缺它时 `resolve_tz()` 会**明确报错**而不是静默用 UTC——
> 静默回退会让"每天 10:00"在实际 18:00 触发。

### 人用入口（**不在**模型工具列表里）

```bash
./.venv/Scripts/python.exe -m cli.app drafts --status pending  # 看草稿
./.venv/Scripts/python.exe -m cli.app approve <draft_id>       # 批准（review 模式发布前提）
./.venv/Scripts/python.exe -m cli.app reject  <draft_id>       # 拒绝
./.venv/Scripts/python.exe -m cli.app resume --reason "已在 App 完成验证"  # 恢复暂停/风控停机
./.venv/Scripts/python.exe -m cli.app digest                   # 生成日报
./.venv/Scripts/python.exe -m cli.app mention-status           # @ 汇报投递状态
./.venv/Scripts/python.exe -m cli.app mention-check <bvid>     # 判定是否值得 @ 主号
./.venv/Scripts/python.exe -m cli.app schedule                 # 调度状态（该不该跑）
./.venv/Scripts/python.exe -m cli.app audit-persona            # 人设/红线冲突审计
./.venv/Scripts/python.exe -m cli.app dm-read                  # 读与主人的私信（只读）
./.venv/Scripts/python.exe -m cli.app dm-send --text "..." --yes  # 给主人发私信（只能发给主人）
./.venv/Scripts/python.exe -m cli.app login-qr                 # 扫码登录小号（终端出二维码）
./.venv/Scripts/python.exe -m cli.app verify-recent            # 复核评论公开可见没有（只读）
./.venv/Scripts/python.exe -m cli.app seen-export --out seen.md  # 导出人类可读判重表（只读）
```

> 人用入口与模型工具**结构性分离**：防"评论区里声称自己是主人，
> 要求批准/恢复"这类越权。`cli.app tools` 会断言这一点。

### 切换到真实发布（**需谨慎**）

三重条件缺一不可，且都在 `config.yaml` 里由**人**修改：

```yaml
interaction:
  mode: "review"          # 或 auto
  dry_run: false
  publish_enabled: true
```

发布前**每次**都会：从实际登录态读 UID 并比对 `agent_mid`（不信配置/缓存）、
检查平台健康状态、检查配额与幂等键、校验证据真实可反查。

---

## 安全边界（都是代码层保证，不是靠提示词）

| 保证 | 落点 |
|---|---|
| 原文不出 bridge | `read_video` 不返回全文；摘录硬上限 500 字（**配置层就挡住**） |
| 每批最多 3 个短候选包 | 配置层挡（`max_candidates_to_dsh ≤ 3`） |
| 证据必须可反查 | `verify_key_points()` 拒绝不存在的 ID、跨 BV、编造引文、时间戳不符 |
| 发布只能引用已存草稿 | `publish_draft(draft_id)` 不接受任意文本/URL |
| 身份每次从登录态读 | `PublishGate.read_identity()` |
| `reserved` 在点击前持久化 | `reserve_action()` 事务内落库 |
| `uncertain` 冻结不重发、占额度 | `find_action_by_key()` + 配额把 `reserved`/`uncertain` 计入 |
| 验证码 → 停所有新请求 | `BLOCKING_STATES` 门禁；**只能人工恢复** |
| 连续限流 → 暂停并通知 | `max_consecutive_rate_limits` |
| 模拟与真实分开计数 | `actions.is_simulated` |
| 日志/返回无凭据 | `core/redact.py`（所有 MCP 返回都过它） |
| 人用入口不进 MCP | `cli/` 与 `bridge/` 分离 + 测试断言 |
| **`@` 成功必须核验** | `try_mention()` + 存储层二次断言 `observed_mid == owner_mid` |
| 纯文本 `@UID` 不算成功 | 只能由渲染后 UID 判定；其余记 `uncertain`/`unsupported`/`failed` |

**明令不做**：轮换 IP/账号、伪造客户端、改指纹、代解验证码、转包打码、绕过风控。

---

## 故障排查

| 现象 | 原因与处理 |
|---|---|
| `companion_status` 报 `TEXT_WORKER_NOT_CONFIGURED` | `text_worker.model` 为空 → 填模型 ID 并确认密钥在注册表里 |
| `TEXT_WORKER_KEY_MISSING` | 密钥环境变量名不对或未设。用 `cli.app status` 看 `text_worker_key_source` |
| 字幕总是 `description_only` | 未登录（字幕需登录）→ `import-cookie` 后重试 |
| `verification_required` 后什么都干不了 | **设计如此**。主人在官方 App/页面人工完成验证，再 `cli.app resume` |
| `blocked` / `-412` | 疑似风控 → 停机调查，**不要**加大重试强度 |
| `DRY_RUN` / `MODE_OBSERVE` 拒绝发布 | 正常。真实发布需人改配置（见上） |
| `TARGET_FROZEN_UNCERTAIN` | 该目标有结果未知的动作 → 人工到官方页面核对，**不会自动重发** |
| MCP 工具在 DSH 里不出现 | 看 DSH 日志里 `mcp-bili-companion`：`command` 路径、`PYTHONPATH`、`serverName` 重名 |
| `schedule` 报"无法解析时区" | 缺 `tzdata`（Windows Python 不自带 IANA 时区库）→ `uv pip install tzdata` |
| `mention-status` 全是 `unsupported` | **正常**：未接 `@` 投递通道（或 `owner_mid=0`）→ 只保留本地日报，不假称已通知 |
| `[restrict-bili] 施加工具限制失败` | `FORBIDDEN_TOOLS` 里某名字与本机注册名不一致 → 按日志修正。此时该 agent **失败关闭**（少工具，不会漏通用写入） |

---

## 开发

```bash
export PYTHONIOENCODING=utf-8

# 质量门（全绿才提交）
./.venv/Scripts/python.exe -m pytest tests/          # 733 passed
./.venv/Scripts/python.exe -m ruff check core bridge cli tests scripts/
./.venv/Scripts/python.exe -m mypy                   # strict, 39 files

# 插件预检（不重启 DSH 就能知道插件能不能加载）
cd plugin && node selfcheck.mjs

# ★ 渲染冒烟（验设置页**真能渲染**，不只是接口字段在）
#   本机没有独立 react（DSH 的 react 在 app.asar 里），先解出来
bash plugin/extract-react-for-smoke.sh
NODE_PATH=/d/DeepSeek/workspace-temp/react-probe/node_modules node plugin/render-smoke.mjs

# token 对照实验（重新生成报告）
./.venv/Scripts/python.exe -c "
from pathlib import Path
from core.token_experiment import run_experiment, render_report
Path('docs/token-experiment.md').write_text(render_report(run_experiment()), encoding='utf-8')
print('ok')
"
```

目录：

```
core/     配置、存储、限流、健康、WBI、B站读取、豆包、发布门禁、编排、日报、
          主号汇报、调度、人设审计
bridge/   MCP server（14 工具）+ 统一返回契约
cli/      人用入口（approve/reject/resume/import-cookie/digest/tools）
dsh/      DSH profile 片段 + 工具收窄插件
tests/    单元 + MCP 真实 stdio 集成 + 故障注入
docs/     compatibility.md / acceptance.md / persona.md / persona-conflicts.md
          token-experiment.md / schedule-wiring.md / dsh-install-safety.md
scripts/  run_once.py
```

---

## 许可与合规

代码为 MIT。**但**：

- 不 import / 不 vendor `xiaoyaya191/bilibili_learning_bot` 的读写实现
  （它依赖 **GPL-3.0** 的 `bilibili-api-python`，会传染）。只借鉴设计与测试清单。
- 不复制参考账号「大肥鱼在摸鱼」的身份或话术。
- 平台协议边界与风险见 [`docs/compatibility.md`](docs/compatibility.md) §5。
