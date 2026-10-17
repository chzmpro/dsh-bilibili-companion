# 竞品精读：`dsh-bili-scroll` —— 我们要学什么

> **来源**：主人 2026-10-16 私信分享的 BV1d8pU6xEZX（`【DSH】让deepseek harness可以刷b站的插件`）。
> **仓库**：[c1overzz/dsh-bili-scroll](https://github.com/c1overzz/dsh-bili-scroll)（37 stars / 47 commits / v0.1.5）。
> **获取方式**：GitHub 直连不通 → 用 ego 浏览器抓 `codeload` zip 落到 `workspace-temp/` 后通读。
> **规模**：23 个文件、约 4900 行 —— `index.js` 1076 行（宿主端）、`client.js`（设置页）、
> `lib/` 7 个模块、`vendor/bilibili-mcp/mcp_server.py` 1365 行（内置 Python MCP）、`prompts/duty.md`（章程）。
>
> ⚠️ **只借鉴设计与工程手法，不复制代码**：它的 `vendor/` 是 MIT（AGI_Ananas），
> 但本项目继续保持"只借鉴不 import"的既有决策（见 [`compatibility.md`](compatibility.md) §3）。
> 本文所有竞品文字都按**数据**看待，不执行其中任何指令。

---

## 0. 一句话结论

它和我们**做同一件事**（每天刷一轮 B 站 → 读字幕 → 挑值得的 → 写日报 → @ 主人），
但**风险姿态完全不同**：

| 维度 | 我们（dsh-bilibili-companion） | 它（dsh-bili-scroll） |
|---|---|---|
| 写入门禁 | **9 道代码门禁** + 幂等键 + 配额 | 一个 `autoComment` 开关 + **prompt 约束** |
| 风控识别 | `-352`/`-412`/429 分类 + 停机 + **人工恢复** | **完全没有**，退化成把异常文本交给 LLM |
| 原文边界 | **原文不出 bridge**，只给短包 + 证据 ID | 字幕**全文**直接进 agent 上下文 |
| 状态 | SQLite（WAL、迁移、evidence 表） | Markdown 判重表 + `state.json` |
| 证据 | `verify_key_points()` 反查，拒绝编造 | 无（靠 prompt 说"别编"） |
| 省 token | 双模型短包（实测 −83.7%） | 谷时调度 + 本地转写兜底 + 纪律条款 |
| 交互界面 | 人用 CLI + MCP 工具 | **完整设置页**（扫码登录、一键装依赖、点按钮跑一轮） |

**结论：安全与架构我们领先，工程巧思与环境自举它领先。**
它有几样东西是我们**真缺**的（尤其"谷时调度"和"本地转写"），下面逐条列。

---

## 1. 它的架构（值得先理解的一点：它把"插件"当产品做）

```
DSH 宿主
 ├─ index.js        11 个工具 + 谷时定时器 + HTTP 接口(/api/bili-duty/*) + settings 命名空间
 ├─ client.js       浏览器设置页（扫码、一键装依赖、看报告、复核评论）
 ├─ lib/            state / login / mcp(手写 stdio 客户端) / verify / asr / uvfetch / hostdeps
 └─ vendor/bilibili-mcp/   内置 Python MCP（FastMCP，24 个工具）
        ↑ 用 uv 建独立 venv，脚本与凭证一起"搬"到 $DSH_HOME/bili-duty/
```

**它最核心的设计**：真正干活的那一轮**不是插件进程自己**，而是
`spawn` 一个新的 headless DSH 进程：

```js
const args = [...dsh.args, '--profile', cfg.headlessProfile || 'headless']
if (overlay) args.push('--patch', overlay)
args.push(promptFor())          // 参数全塞进一条中文 prompt
spawn(dsh.command, args, { cwd: stateDir, env: pyEnv({ BILI_DUTY_HOME: stateDir }) })
```

`overlay` 是一个自动生成的 `headless-overlay.yml`，往那一轮里插入
`@deepseek-ai/dsh-mcp-client` 的 `bili-duty` 命名空间 —— **不改用户 profile 也能让那一轮拿到 B 站工具**。
这一招对我们将来做"定时 preset"很有参考价值。

---

## 2. ★ 我们真缺的（按价值排序）

### 2.1 ★★★ 谷时窗口调度 —— 直接省钱，我们完全没有

它把"什么时候跑"钉在 **DeepSeek 官方峰谷口径**上：

```js
WINDOWS = { noon: { label:'中午谷时', start:'12:00', end:'14:00' },
            night:{ label:'晚上/夜间谷时', start:'18:00', end:'09:00' } }
```

- 工作日 9–12 / 14–18 是**高峰**，其余（含周末）都是谷时；
- `inWindow()` 支持**跨零点**（`start<=end ? 在区间 : cur>=start||cur<end`）；
- 防重复：`state.windows['2026-10-14:noon']=true`，**一个窗口只跑一轮**；
- 跨天重置：`if (st.day!==today){ st.day=today; st.count=0; st.windows={} }`。

**我们的现状**：`schedule.mode = on_host_start`，由主人打开 DSH 触发；**没有任何"谷时"概念**。
我们一轮真实花销（含豆包）不小，而 `prepare_batch` 实测 198–223 秒 / 20 次请求。

**建议**：给 [`core/schedule.py`](../core/schedule.py) 增加 `peak_valley` 档位 ——
`due=true` 且当前处于谷时窗口才真跑，否则返回 `WAIT_FOR_VALLEY`（并在 `companion_status` 里说明）。
**改动小、收益直接。**

### 2.2 ★★★ 本地 ASR 转写 —— 补我们最大的功能缺口

我们现在的降级链是"无字幕 ⇒ `description_only` ⇒ 降分 ⇒ 不编造"（诚实，但**等于放弃**）。
它多了一条兜底：**yt-dlp 抽音频 → faster-whisper 本地转写**。

| 细节 | 值 |
|---|---|
| 取音频 | `yt_dlp` + `format='bestaudio/best'`，**不用 ffmpeg**（B 站 DASH 已音视频分离） |
| 转写 | `WhisperModel(model, device='cpu', compute_type='int8')`，默认 `small`，`vad_filter=True` |
| 模型可换 | `tiny|base|small|medium`，`BILI_DUTY_ASR_MODEL` 可覆盖；默认**关**（省空间/时间） |
| 国内可用 | `HF_ENDPOINT=hf-mirror.com`、`HF_HOME=<家>/.hf-cache`、`HF_HUB_DISABLE_XET=1`（**Xet 国内镜像不代理 ⇒ 401**）——且必须在 `import faster_whisper` **之前** |
| 体积 | 不装转写 ~84 MB；装了 ~1 GB（Whisper `small` 538 MB） |

**我们的现状**：`docs/acceptance.md` §7.2 明确写着"**无真实 ASR**"、"**B 站 ASR 未调研**"。
`docs/compatibility.md` §7.2 也记着 `HF_ENDPOINT` 镜像（Hindsight 用），说明这条链路我们验证过。

**建议**：作为**可选降级**接入 `Pipeline.read_video` —— 字幕不可用时（且配置开启）调本地转写，
登记为 `evidence_level = asr`（这个常量我们**已经有了**：`LEVEL_ASR`），并同样走证据反查。
默认关，避免 1 GB 依赖强加。

### 2.3 ★★★ 两条"省钱纪律"（零代码，写进章程即可）

它的章程里有两条极其具体的纪律，**实测省近一半 token**：

> ⚠️ **看够了就下结论**：不要反复权衡同一条该不该收 —— 拿不准就按"跳过"处理，别来回改主意。
> ⚠️ **工具失败两次就记为"跳过"**：不要反复重试同一个命令、也不要为了绕过它写一堆临时脚本。
> （实测里"纠结"那一轮比干净的一轮多花了**近一倍** token。）

还有一条更狠的教训（0.1.5 专门修）：

> ⭐⭐ **"空"是一种结果，不是坏了**：字幕/转写拿回来是**空的、0 字节的、或只有一行「字幕byXX」署名**
> ⇒ 那是**这条视频没有语音**（纯音乐/纯录屏/无声演示）⇒ 按"跳过（无语音）"记一笔、继续下一条。
> ⚠️ **不许**因此去写 `debug.py` / 临时脚本，也不许反复重跑
> （**有用户实测 27 次工具调用全耗在这上面**，最后报告空、判重表没建、评论也没发）。

**我们的现状**：[`AGENTS.md`](../AGENTS.md) 有三条硬约束，但没有这两条"止损纪律"；
我们的 `docs/persona.md` 也没有。而**我这次就差点踩进去**（`dm-ingest --force` 报错后，
如果不去查根因而是反复重试，就会重复烧豆包 token）。

**建议**：把这四条写进 [`AGENTS.md`](../AGENTS.md) §0 与 `docs/persona.md`。
**零代码成本，立刻生效。**

### 2.4 ★★ 保底产物：不管这一轮发生什么，"发生了什么"必须留痕

`stubReportIfMissing(code)` 在子进程 `exit` 时触发：

- 报告档位是 `off` → 返回；文件已存在 → 返回；
- 否则读 `duty.log` 末尾 1500 字符，写一份**占位报告**：
  `# 刷B站 · <日期> —— 这一轮没写出报告` + `> ⚠️ …（退出码 ${code}）` + 日志尾代码块。

理由（它的注释）：`out/` 是空的、判重表不存在 ⇒ **使用者完全看不到这一轮发生了什么**，
也不知道这一轮白烧了钱。

**我们的现状**：`scripts/run_once.py` 崩了就是崩了；`runs` 表有记录，但**没有日报**。
主人只能看到"最近运行 conclusion=ok/failed"，看不到**为什么**。

**建议**：`core/report.py` 增加 `write_stub(reason, log_tail)`，在 CLI/定时收尾的 `finally` 里调用。
与它的差异：我们的"日志尾"应该走 `core/redact.py`（它没有脱敏这一步）。

### 2.5 ★★ "发送成功 ≠ 公开可见" —— 二次复核 + **弱断言措辞**

它把"接口回 success"和"评论真的公开可见"拆成**两个事实**：

- 插件在跑完一轮 **90 秒后**自动复核一次（躲"还在审核"的误判），设置页也有手动按钮；
- 复核只读：先 `view?bvid=` 取 aid，再 `/x/v2/reply/main?type=1&oid&mode=2&next=0&ps=20`（**按时间**）
  或楼中楼 `/x/v2/reply/reply?…&root=&ps=20&pn=1`；
  ⚠️ **`/x/v2/reply/detail` 走不通（回 -400）** —— 这是省我们一次试错的接口情报；
- 账本 `verify-state.json`：`visible===true && now-at < 7*864e5` 则跳过复查（7 天 TTL）；
- **措辞纪律**：查不到只写「**没查到**」，绝不写「没发出去」——
  因为翻页和热度排序都会漏，**观测不到不能反推不存在**。

**我们的现状**：我们有 `uncertain` 状态（已经比它严谨），但
① 核验是**发布时同步做**的，没有"过一会儿再核一次"；② 报告措辞未强制弱断言。

**建议**：把"90 秒后自动复核"做成 `scripts/verify_recent.py` + 日报追加一节
`## 评论复核（宿主自动跑的，不是那一轮自己说的）`。**注意**：它的 `root` 列设计
（楼中楼要额外记根评论 rpid）我们**已经有** `root` 字段，直接可用。

### 2.6 ★★ 人类可读判重表 + **宽进严出**的解析

它不存数据库，判重表就是 `seen.md`：

```markdown
| BV | rpid | root（楼中楼才填） | 时间 | 正文前 20 字 |
## 看过的视频
```

解析器**容错**：逐行 `BV[0-9A-Za-z]{10}` 抓 BV、`\b(\d{9,})\b` 抓长数字，
**第一个当 rpid、第二个当 root**，容忍 Markdown 表格/列表/随手写的行。
配合 prompt 侧"**严出**"的格式约定 —— **宽进严出**。

而且它有一条重要的**故障纪律**：

> 判重表**读不出来**（不存在/打不开）时，**不要当成"没记录过"** ——
> 那会让同一批视频反复推送、重复打扰一批人。
> 正确做法：报告里明写"判重表读不到"，且**这一轮不发评论**（宁可少发）。

**我们的现状**：我们有 SQLite + `videos.status`，判重更可靠。
但**没有"人类可读的判重表"**——主人想手工看看"它到底看过什么"，得写 SQL。

**建议**：加 `cli.app seen-export`（导出 Markdown），**只读导出**，不改变事实来源。
同时把它的"读不到就不发评论"契约写进我们的 `PublishGate` 文档（我们的 SQLite 不会读不到，
但 `db` 损坏时应有同等纪律）。

### 2.7 ★★ 扫码登录（我们仍是手工导 cookie）

它把扫码登录做在插件里：`passport.bilibili.com/x/passport-login/web/qrcode/generate`
→ 轮询 `.../qrcode/poll`，**凭证在 `Set-Cookie` 里**（不在成功响应那个 `data.url` 里），
所以要用 `redirect:'manual'` + `res.headers.getSetCookie()`。

状态机五态显式化：`0→ok / 86101→waiting / 86090→scanned / 86038→expired / 其它→unknown`；
缺 `buvid3` 时回落 `api.bilibili.com/x/frontend/finger/spi` 的 `data.b_3`。

**我们的现状**：`cli.app import-cookie <file> --yes` —— 需要主人**手工**从浏览器导出。
`docs/acceptance.md` §6 把"小号扫码登录"列为**主人待办**。

**建议**：这是**减少主人手工步骤**的高价值项，可做成 `cli.app login-qr`（人用入口，
**不进 MCP**，保持结构性隔离）。

### 2.8 ★ 环境自举三件套（uv 自举 / hostdeps 降级 / 沙箱适配）

| 手法 | 它怎么做 | 对我们的意义 |
|---|---|---|
| **uv 自举** | GitHub 直连 → `gh-proxy.com` → `ghproxy.net` → 清华 PyPI wheel（**自己按版本号排序取最新**，因为 urls 不保证有序）；"下载即执行"的产物**强制 sha256**，校验失败**只换源不降级** | 我们 README 目前要求主人先装 `uv`；我们有**完全一样**的网络约束（`pypi.org` 直连不通 → 清华镜像，见 `compatibility.md` §7.2） |
| **optional peer 三级降级** | 宿主树 → 自己 dependencies → 本地等价实现 `localDefineTool`；`schemastery` 拿不到就 `z=null`，**插件照常加载、只降级设置页** | 它踩到的是**桌面版真 bug**：`autoInstallPeers:false` ⇒ optional peer 永不安装 ⇒ `1 entry did not activate … failed to import`。**我们将来若做 DSH 插件，这是必踩的坑** |
| **沙箱适配三件套** | ① 宿主**预建** `tmp/asr` 与 `seen.md`（"沙箱只允许写子进程启动前就存在的目录"）；② `tempfile.tempdir=` 钉死 + **不用 `mkdtemp`**；③ yt-dlp `nopart:True`（绕开"新建+改名"被拒） | 我们踩过一模一样的坑（`%LOCALAPPDATA%` 不可写 → 探测可写性，见 `compatibility.md` §7.5）。它的解法更彻底 |

### 2.9 ★ 插件工程硬知识（若我们将来做成 DSH 插件，逐条都是坑）

- **设置命名空间必须等于 loader entry id**；每个字段要 `.extra('volatile', true)`，
  否则 `settings.update` 报 `has no volatile fields`；
- **client bundle 不是 ESM**，是自注册脚本：`window.__ModuleLoader__.load({ id, factory })`，
  **`id` 必须等于 `package.json.name`**；
- **`ctx.get('dshHomePath')` 是函数**（`dshHomePath(...segments)`）—— 当成字符串用
  会把函数源码拼进路径 ⇒ `mkdir ENOENT` ⇒ **整个插件不激活**（它有踩坑记录）；
- **`defineTool` 的 object schema 必须写 `additionalProperties`**（宿主的校验）；
- `safe(v)` 包所有工具返回值，规避宿主 `value is not lossless JSON`；
- `normalize(src, base)` 逐字段白名单清洗配置，`tick` 整体 try/catch —— **插件再错不杀宿主**
  （它的注释：2026-10-14 一个未捕获异常杀掉过整个宿主）；
- `pyEnv()` 统一钉 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`（Windows GBK）；
- **`selfcheck.mjs`**：本地造 `fakeCtx`（`dshHomePath` 给**函数**）真调 `apply()`，
  用 `tmpdir()` 不碰真家目录 —— **重启前**就能暴露"整插件不激活"；
- `tools/register.mjs` 的第 2 参是 **parentURL**，写成 `pathToFileURL(import.meta.url)` 会拼出
  `file:///…/file:/…` 的怪路径。

---

## 3. 我们**比它强**的地方（不要盲目照抄）

| # | 它的问题 | 我们的做法 |
|---|---|---|
| 1 | **"同一视频不留第二条评论"只靠 prompt 约束**，代码层无唯一性校验 | `idempotency_key` + `find_action_by_key()`，DB 层 UNIQUE |
| 2 | **完全没有风控/验证码识别**：读路径大量 `except: break/pass`，风控退化为"把异常文本交给 LLM" | `core/health.py` 五态分类（`-352`/`-412`/429 不混同）+ `BLOCKING_STATES` + **只能人工恢复** |
| 3 | **字幕全文直接进 agent 上下文** | 原文不出 bridge；`read_video` 只给短包，摘录 ≤500 字 |
| 4 | 无证据校验，靠 prompt"别编" | `verify_key_points()` 拒绝不存在的 ID / 跨 BV / 伪造引文 / 时间戳不符 |
| 5 | 凭据是明文 JSON、无权限位 | `cookie.txt`（gitignore）+ `core/redact.py`（所有 MCP 返回过它） |
| 6 | 人用入口与模型工具无结构性隔离 | `cli/` 与 `bridge/` 分离 + `cli.app tools` 断言 + 测试固化 |
| 7 | 无配额（只有 `perDay` 条数） | 评论/回复/点赞/收藏四类日配额 + `reserved`/`uncertain` **占额度** |

**它自己也有一处反例值得记**：`tools/_check-1016.mjs` 把一次性架构约束固化成 **51 条断言**
（含"旧错误文案必须已删除"的**否定断言**）——思路很好，但它**硬编码**了
`const P='D:/deepseek/plugins/bili-duty/'`，**换机即失效**。抄思路，别抄路径。

---

## 4. 落地建议（优先级 + 工作量）

| 优先级 | 事项 | 位置 | 预估 |
|---|---|---|---|
| **P0** | 谷时窗口调度（`peak_valley` 档位，非谷时返回 `WAIT_FOR_VALLEY`） | `core/schedule.py` + `config` | 小 |
| **P0** | 四条止损纪律写进章程（"空是结果"、"失败两次即跳过"、"看够就下结论"、"读不到判重就不发"） | `AGENTS.md` + `docs/persona.md` | **零代码** |
| **P1** | 占位日报（`write_stub(reason, log_tail)`，日志尾过 redact） | `core/report.py` | 小 |
| **P1** | 评论可见性**延迟复核** + 弱断言措辞（"没查到"≠"没发出去"） | `core/publish.py` / 新 `scripts/verify_recent.py` | 中 |
| **P1** | 本地 ASR 兜底（可选、默认关；登记为 `LEVEL_ASR`；HF 三环境变量前置） | `core/` 新模块 + `Pipeline.read_video` | 中大（含 ~1 GB 依赖） |
| **P2** | 扫码登录（`cli.app login-qr`，人用入口） | `cli/` | 中 |
| **P2** | 判重表导出为人类可读 Markdown（只读导出） | `cli/` | 小 |
| **P2** | uv 自举（三源 + sha256），让 README 的"前置"从主人手工变成自动 | `cli/` | 中 |

---

## 5. 本次已经顺手落地的

| # | 事项 | 证据 |
|---|---|---|
| 1 | **私信分享视频接入双模型管道**：`cli.app dm-ingest` —— 读私信 → 抽卡片 BV → 走 **同一个** `Pipeline.read_video` → 只给短包 + 证据 ID；默认按 BV 判重跳过已读 | `dm-ingest` 实测：2 个 BV 全部判为已读并跳过（**不重复烧 token**）；`--force` 跑通完整管道 |
| 2 | **修掉一个真 bug**：`text_jobs` 的部分唯一索引（`WHERE status='ok'`）与"缓存命中再写 ok"冲突 ⇒ **任何重读同一视频都会抛 `IntegrityError`** | [`core/storage.py`](../core/storage.py) 改 ok 走 upsert；回归测试 [`tests/test_text_job_dedup.py`](../tests/test_text_job_dedup.py) 4 项 |
| 3 | 私信通道（读 + 只发给主人 + 独立开关） | [`core/dm.py`](../core/dm.py)、[`tests/test_dm.py`](../tests/test_dm.py) 16 项；`acceptance.md` §2.5c |

> 私信箱里那条 `--force` 的报错，正好实证了 §2.3 那条纪律：
> **工具失败时先查根因，不要反复重试** —— 如果当时重试三次，就是三次白烧豆包 token。
