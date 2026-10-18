# HANDOFF —— 接手必读（2026-10-18 03:55 更新）

> 给**下一个对话**用。读完这一份就知道项目现在在哪、什么不能碰、下一步做什么。
> 详细背景按需查，**不要一上来就读全套 docs/**（20 份，会淹掉上下文）。

---

## 0. 30 秒速览

**项目**：`D:/DeepSeek/projects/dsh-bilibili-companion` —— B 站自主学习小号「干饭不干事的小鲸鱼」
**形态**：DSH 插件（设置页）+ Python MCP bridge（**25 个工具**）+ 常驻监听进程
**主人**：沐雨林心（`owner_mid=660490601`）；小号（`agent_mid=3747570769725629`）

```
视频入库 115 条 · 有证据 98 · 已打分 98 · 有提炼 37 · 合格(≥75) 37
schema v14 · dm_inbox 待回复 0
```

> ★ **2026-10-18 已完成 Codex 审查整改**（#2 字幕全量/分块、#3 整包预算、
> #4 发布读回 + `sent_pending_verify`、#5 写侧健康传播、#6 token 口径）。
> 生产闭环（#1）仍是**立项未实现**，见 [`docs/production-loop-plan.md`](docs/production-loop-plan.md)。

**当前红线（一个都没动）**：`mode=observe` · `dry_run=true` · `publish_enabled=false`
⇒ **任何公开写入（点赞/收藏/评论）都会被代码层拒绝**。这是设计，不是故障。

---

## 1. 开工先做这三件事

```bash
cd D:/DeepSeek/projects/dsh-bilibili-companion
export PYTHONIOENCODING=utf-8

# ① 看小号状态与配额
./.venv/Scripts/python.exe -m cli.app status

# ② 看监听进程活着没（它是后台常驻的）
curl -s http://127.0.0.1:8848/status | head -c 300

# ③ 看有没有待回复私信
./.venv/Scripts/python.exe -m cli.app dm-read
```

---

## 2. ★ 核心架构（这轮刚定型，务必理解）

### 2.1 两条路径，各管一段

```
主人发 B站私信
   │
   ├─ 监听进程（dm-watch，常驻，60 秒/轮）
   │    ├─ 发现新私信 → 写 dm_inbox(pending)
   │    ├─ 豆包带 19,268 字上下文 → 写一条回复（**这条是人设化的**）
   │    └─ 投递给「值班员」会话（agents.followup）
   │
   └─ 值班员会话（主人在 DSH 里手动开的专用会话）
        └─ 读到投递内容 → 用 DSH 的模型思考 → 调 dm_push 发私信
```

**分工（主人明确要求）**：
- **值班员（DSH 会话）**：做**实操** —— 读内容、写推荐、发私信
- **主对话（你）**：做**改代码、查问题、写文档**

### 2.2 视频推荐链路

```
① 搜索/推荐流 → 候选视频
② 豆包用**简介**初筛打分（快、便宜）
③ 高分（≥75）→ 补全字幕 + 热评 8 条（enrich_candidates.py）
④ 豆包**读全文**提炼（digest_candidates.py）
     → core_value / highlights / peers / human_bits / caveats
⑤ 按 2500 规则整理（deliver_to_duty.py）
⑥ 投递给值班员 → 它用人设写推荐 → dm_push 发私信
```

### 2.3 ★ 2500 规则（主人 2026-10-18 亲自定的，**不要改回一律提炼**）

> 主人原话：「字幕、评论 2500 以下全文发送，2500 以上豆包拣选有用的截断形成 2500 以下发送」

| 单项长度 | 处理 |
|---|---|
| **≤ 2500 字** | **全文原样**，**不调模型**，一个字节不动 |
| **> 2500 字** | 豆包挑有用部分（做法/踩坑/数字/同类项目），压到 2500 以下 |

实现在 `core/candidate_digest.condense_text()`。

---

## 3. ★ 主人的硬性原则（违反会被直接指出来）

| # | 原则 | 说明 |
|---|---|---|
| 1 | **主动发给主人的，必须是上层模型看过的** | 例外只有"监控豆包自动回复"那一条。不许把豆包的 `summary` 直接转手当推荐 |
| 2 | **实操交值班员，改代码交你** | 别让主对话去写推荐语 |
| 3 | **人设要贴合** | `docs/persona.md`（16832 字节，430 行）是权威。少女感、最多一个括号吐槽、**说清为什么值得看，不复述标题** |
| 4 | **展示：中文解释为主，英文代码作为小字放在下面** | 否则一堆英文看不懂是干啥的 |
| 5 | **模仿参考项目，不要自己造轮子** | 参考项目在 `D:/DeepSeek/workspace-temp/blb-src/`（bilibili_learning_bot） |

---

## 4. 环境与关键路径

| 项 | 值 |
|---|---|
| DSH_HOME | `C:\Users\yp-chenzhuoming\AppData\Roaming\dsh-desktop\harness` |
| profile | `web` |
| 插件配置 | `$DSH_HOME/bili-companion/config.json` |
| 数据库 | `.local-data/companion.db`（schema **v13**） |
| 监听面板 | http://127.0.0.1:8848/ |
| DSH_GUI | 当前 http://127.0.0.1:43129（**端口每次启动会变**） |

### 插件配置现状

```json
{
  "watchAutoStart": true,          // 随 DSH 自动启动监听（不是开机自启）
  "watchAutoReply": true,          // 豆包自动回复开着
  "watchInterval": 60,
  "watchSessionId": "session-d3af12f6-a4fb-4e61-a1e1-02eb94b2a1e9"   // 值班员会话
}
```

> ⚠️ **绝不要手改这个 JSON 的 `watchSessionId`** —— 插件 `apply` 里会用**内存里的 cfg**
> 写回磁盘；手改会被下一次写回清空。要改用 `POST /api/bili-companion/config`。

---

## 5. ★ 这轮踩过的坑（血泪，别再踩）

### 5.1 `agent.followup()` 必须自己带 `role`

```js
agent.followup({
  id: crypto.randomUUID(),
  role: 'user',                    // ★ 漏了这条 = 会话损坏
  content: [{ type: 'text', text }],
  source: { kind: 'user' },
})
```

官方 `createUserMessage()` 会补 `role`，绕过它就必须自己带。
**漏了会怎样**：消息写进会话 → 每轮 `messages[N]: missing field role` → DSH 判定会话 **corrupt**
→ **连删除都被拒绝**（删除前要 inspect）→ 主人既打不了字也删不掉。

### 5.2 会话 zstd 是**多帧**，第一帧必须只有 header 一行

修损坏会话时**不能整体重压成单帧**，否则报：
`first frame is not exactly one header line`。
必须逐帧（magic `0x28B52FFD`）处理，未改动的帧**保留原始字节**。
工具：`scripts/fix_session_role_v2.mjs`（v1 是错的，已废弃）

### 5.3 `{{model}}` 无值 = 会话从没选过模型

值班员会话报 `prompt variable "{{model}}" has no value` ⇒
该会话缺 `model/selection` 事件（主人在 DSH 界面选一次模型即可写入）。
排查：对比两个会话的事件类型计数，正常的必有 `model/selection`。

### 5.4 `62002` 是「稿件不可见」，不是风控

被 UP 删了/设为私密/地区限制。**已修**：分类为 `NOT_FOUND` + `blocks_all_requests=False`。
之前它不在码表里 ⇒ 判定平台故障 ⇒ **22 条补抓被一条已删视频全部停掉**。

### 5.5 ASR 必须用**本地模型目录**

`WhisperModel("small")` 走下载 ⇒ `Expecting value: line 1 column 1`（ModelScope 没元数据端点）。
模型早就在 `.local-data/.hf-cache/hub/models--Systran--faster-whisper-small/snapshots/modelscope/`。
`core/asr.py::_local_model_dir()` 会优先用本地。

### 5.6 `reply_to()` 必须同步 `dm_inbox.status`

否则状态永远 `pending` ⇒ 值班员每次被唤醒都当成新私信 ⇒ 被幂等规则挡掉（BLOCKED）。
**已修在 `core/dm_live.reply_to()` 里**（所有调用点受益）。

### 5.7 验证投递用 `scripts/verify_deliver.py`，别看 API 返回码

唯一可信信号：`turn/end` 的 `reason.kind == "completed"`。
`delivered: true` / 文件变大 **都不算成功**（被骗过两次）。
两个假阴性陷阱：会话边跑边写、agent 调工具可能跑几分钟 ⇒ **必须轮询**。

### 5.8 一次请求太多会被 B站 拦

连打 75 条 view 接口会触发异常判定。**节奏：10 秒/条、每 5 条 20 秒**（主人定的）。

### 5.9 ★ 发布后“查不到”是常态，`sent_pending_verify` 不是失败（#4）

刚发的评论经常因为审核/翻页在第一页看不到。现在拿到 rpid **先落
`sent_pending_verify`**（已确定发过、占额度、锁幂等键、不重发），
读回作者/正文/BV/root 全部匹配才升 `sent_verified`。

* 别把“没查到”写成“没发出去”，也别为了凑成功再 POST 一次；
* 过一会儿跑 `cli.app verify-pending` 只读重试；
* `publish()` 返回 `OK_PENDING_VERIFY` 是**预期状态**，不是 bug。

### 5.10 ★ bridge 的评论发布路径曾经没有 executor

`publish_draft`/`publish_reply` 以前构造 `PublishGate` 时没传 `executor`，
所以**即使开了 publish_enabled 也永远 `EXECUTOR_UNAVAILABLE`**。
2026-10-18 已接上 `_make_write_executor(runtime)`（门禁不变）。

### 5.11 长视频字幕：全量入库 + 模型分块（#2/#3）

* 字幕**全部**片段入库（旧实现只取前 40 段，后面的连证据都没有）；
* 豆包输入按 `CHUNK_MAX_CHARS=4000` 分块，最多 6 块，超出等距抽样并标
  `MODEL_INPUT_SAMPLED`；部分块失败标 `MODEL_CHUNK_FAILED_n` 并强制深审；
* DSH 返回的 `max_batch_chars` 现在按**完整 JSON** 计算，首卡也受限；
  截断数量在 `truncated` 字段里（`batch_chars` 等于最终序列化长度）。

---

## 6. 未完成的事（按优先级）

| # | 事项 | 说明 |
|---|---|---|
| 1 | **生产闭环（#1）** | 定时器、DSH 决策回收、主号汇报串成 `run_cycle`。**已立项未实现**，见 [`docs/production-loop-plan.md`](docs/production-loop-plan.md) |
| 2 | **真实评论读回样本（#4）** | 机制已实现（`sent_pending_verify` + `cli.app verify-pending`），但还没有真实 rpid 跑过 |
| 3 | **token 真实验收（#6）** | 当前只有架构估算；真实 DSH usage / ≥5 条互动 / 人工证据核对都未采集 |
| 4 | **评论区 @** | `core/mention.py::try_mention()` 写得很完整，但**生产代码零调用**（只在 tests 里）。主人说"代码做完了只是没开"，需确认开关在哪并接上 |
| 5 | **免打扰时段** | 参考项目有 `sleep_start/sleep_end`（02:00-08:00），我们没有 |
| 6 | **人设化推荐语未验证** | `scripts/generate_recommendations.py` 写好了但没跑过（后来改成投递给值班员写，这条可能不需要了） |

---

## 7. 常用脚本速查

```bash
# 评分
./.venv/Scripts/python.exe -u scripts/rescore_all.py --all                    # 全部重算
./.venv/Scripts/python.exe -u scripts/rescore_all.py --since "2026-10-16"    # 某时间起
./.venv/Scripts/python.exe -u scripts/rescore_all.py --last-run              # 仅最近一轮

# 内容补全与提炼
./.venv/Scripts/python.exe -u scripts/backfill_evidence.py --run             # 补简介
./.venv/Scripts/python.exe -u scripts/enrich_candidates.py --run             # 补字幕+热评
./.venv/Scripts/python.exe -u scripts/digest_candidates.py --run             # 豆包提炼
./.venv/Scripts/python.exe -u scripts/digest_candidates.py --show            # 看提炼结果

# 投递与推送
./.venv/Scripts/python.exe -u scripts/deliver_to_duty.py --send              # 投给值班员
./.venv/Scripts/python.exe -u scripts/push_all_scores.py --send              # 三档推送

# 验证与修复
./.venv/Scripts/python.exe -u scripts/verify_deliver.py --session <sid> --check-only
./.venv/Scripts/python.exe -m cli.app verify-pending                       # 只读重试待核验评论
node scripts/fix_session_role_v2.mjs <session.v4.jsonl.zstd>

# 自检
./.venv/Scripts/python.exe -m pytest tests/ -q
./.venv/Scripts/python.exe -m ruff check core bridge cli tests scripts/
./.venv/Scripts/python.exe -m mypy
cd plugin && node selfcheck.mjs
```

---

## 8. 插件安装（改 host 代码后必做）

```bash
export DSH_HOME="C:/Users/yp-chenzhuoming/AppData/Roaming/dsh-desktop/harness"
EXE="/d/DSH/DSH-file/DSH Desktop/DSH Desktop.exe"
ENTRY="D:/DSH/DSH-file/DSH Desktop/resources/app.asar/node_modules/@deepseek-ai/dsh/lib/bin.js"
P="$DSH_HOME/profiles/web"

# ① 备份
cp "$P/package.json" "$P/package.json.bak-$(date +%Y%m%d-%H%M%S)"
cp "$P/cordis.patch.yml" "$P/cordis.patch.yml.bak-$(date +%Y%m%d-%H%M%S)"

# ② 打包安装（**不要用 link:**，会进安全模式）
cd plugin && npm pack --silent
DSH_HOME="$DSH_HOME" ELECTRON_RUN_AS_NODE=1 "$EXE" --expose-internals "$ENTRY" \
  plugin --profile web add "file:<tgz 绝对路径>"

# ③ 三道体检
node "$DSH_HOME/tools/check-plugin-form.mjs"
# dump-config 验 __dshPluginOwner.packageName 计数 == 1
node "$DSH_HOME/tools/check-plugin-form.mjs" --dump <dump.txt>

# ④ 真实重启 DSH（改 host 代码只靠热重载不生效）
```

**铁律**：一个包的 `cordis.patch.yml` 只允许 **1 行**。MCP 行必须放 profile 层。

---

## 9. 性能现状（2026-10-18 实测）

DSH 启动时会等我们的 MCP：

```
[loader] still waiting for 1 plugin entries after 13s: @deepseek-ai/dsh-mcp-client
```

拆解：

| 部分 | 耗时 | 能否优化 |
|---|---|---|
| **mcp SDK**（`mcp.server.mcpserver`） | **3.6 秒** | ❌ 换任何入口都一样（lowlevel 3611ms / mcpserver 3851ms） |
| 我们的 `core.*` | 0.95 秒 → 已优化 | ✅ 已延迟 `core.runtime` |
| Python 解释器 | ~1 秒 | ❌ venv shim 会再启动一层 |
| **合计（空闲）** | 5~6 秒 | — |
| **合计（DSH 启动时负载下）** | **13 秒** | — |

**根治办法只有：用 Node 重写 MCP server**（Node 启动 ~100ms）。工作量不小，主人还没定。

**另外**：`hindsight-api.exe --idle-timeout 0` 常驻 **602 MB**（不是本项目的，但明显拖系统）。

---

## 10. 数据现状

```
视频总量        115
有证据           98   （简介 96 / 字幕 48 / 评论 36）
已打分           98   （分数范围 0-98）
已提炼           37
合格（≥75）      37
无证据           17   （简介为空且无字幕，物理上无法评分）
```

**评分机制**（新，2026-10-17 起）：

```
relevance_score = topic×0.80 + usefulness×0.10 + novelty×0.06 + evidence×0.04
```

topic 是绝对主导 ——「这条内容的主题，是不是主人会主动去搜的？」

> ⚠️ 打分时必须传 `focus`（`core/context_pack.build_focus()`）。
> 不传的话模型只知道标题和证据，不知道"对谁有用" ⇒ **这就是主人最早抱怨
> "没什么用却给高分"的根因**。缓存键含 `prompt_version`（现 v3），改 rubric 要 bump。

---

## 11. 接手第一件事建议

1. 跑一遍 `pytest` + `ruff` + `mypy` + `node selfcheck.mjs`，确认基线绿
2. 看 `$DSH_HOME/bili-companion/watch.log` 和 `curl 127.0.0.1:8848/status`，确认监听活着
3. 先读 [`docs/acceptance.md`](docs/acceptance.md) §6.5（审查整改状态）与
   [`docs/production-loop-plan.md`](docs/production-loop-plan.md)（#1 立项），
   再**问主人下一步做哪件**（第 6 节），别自己挑

---

## 12. 参考项目位置

| 项目 | 路径 | 看什么 |
|---|---|---|
| bilibili_learning_bot | `D:/DeepSeek/workspace-temp/blb-src/` | 设置页、配置项设计、内容读取口径（3 视频/各 2500 字/热评 8 条） |
| langup | `D:/DeepSeek/workspace-temp/langup-src/` | — |

值得模仿但还没做的：`sleep_start/end` 免打扰、`behavior` 随机回复延迟（4-18秒）、
细粒度权限（allow_like/coin/favorite）、`read_danmaku` 读弹幕。
