# 借鉴同类项目：读源码、避坑、落地（2026-10-17）

## 主人要求

> 「你在做我之前的代码时，你好歹得找点相近项目做参考进行模仿与修改，
> 或者复制粘贴，而不是自己一昧的造轮子抱着别人现成的不用」
>
> 「你进行实践时也要看看他出现过的问题不要再犯，
> 同时找找其他相似的项目看看它们的实现方式，对比这个参考项目有没有优势」

这份文档就是**照这个做的**：先读源码、先看踩坑、先横向对比，**然后才写代码**。

---

## 一、对比了哪些项目

| 项目 | Stars | 规模 | 定位 | 与我们的关系 |
|---|---|---|---|---|
| **`xiaoyaya191/bilibili_learning_bot`** | 121 | 8.5 万行 / 300 文件 / 125 测试 | 让 AI 替你管 B站（45+ 功能） | **主要参考**（功能最全） |
| **`jiran214/langup-ai`** | 732 | 2519 行 / 46 文件 | B站直播/私信/@ 回复 bot | **横向对比**（结构更清晰） |
| `Nemo2011/bilibili-api` | 4167 | — | B站 API 库 | 两个项目都用了它 |

> ⚠️ 说明：`bilibili_learning_bot` 仓库 API 报 `size: 310126 KB`（303 MB），
> 但**实际 zip 只有 43 MB**（含 git 历史）。源码树 ~50 MB / 588 条目。

---

## 二、★ 两个项目的**对比**（谁更好）

| 维度 | `bilibili_learning_bot` | `langup-ai` | 我们抄谁 |
|---|---|---|---|
| **结构** | 单文件很大（`monitor.py` 764 行） | **四层解耦**（listener/reaction/brain/uploader） | 抄 `langup-ai` 的分层思路 |
| **监听异常保护** | ✅ `try` + `asyncio.gather(return_exceptions=True)` | ❌ `while 1` **无 try**，一次异常整个线程死 | **抄 blb** |
| **单实例锁** | ✅ `_acquire_bot_lock()` | ❌ 无 | **抄 blb** |
| **限流** | ✅ 分频道间隔 + `-509` 退避 + `-799` 全局冷却 + 抖动 | 只有 `listener_sleep` | **抄 blb** |
| **自我进化** | ✅ 3 层 + 提案制 + 4 道硬闸 | ❌ 无 | **抄 blb** |
| **代码量** | 8.5 万行（**过度膨胀**） | 2519 行（**精炼**） | 抄 `langup-ai` 的克制 |

**结论**：**blb 的机制更好，langup-ai 的结构更干净**。
我们抄 blb 的**机制**，抄 langup-ai 的**分层**，同时**避开 blb 的膨胀**（它 45+ 功能里
有一半是我们不需要的：邮箱找回、Docker、Android、35 分区 Web 面板）。

---

## 三、★ 它踩过的坑（我照抄时避开的）

来源：`CHANGELOG.md`（30 条修复记录）+ 源码注释。

| # | 坑 | 出处 | 我们怎么避 |
|---|---|---|---|
| **1** | **`buvid3` 格式不对 → B站永久 `-799`** | `api/client.py:67` | `check_buvid3_format()` 发请求前校验 |
| **2** | **`-799` 是**全站级**限流，继续请求任何接口都加重** | `api/throttle.py` | 全局冷却 90~180s，**所有频道一起停** |
| **3** | **固定间隔像爬虫** | `api/throttle.py` 的 `random.uniform` | 间隔加**抖动** |
| **4** | **监听循环一次异常整个线程死** | `langup-ai` 的 `while 1` | 每轮 `gather(return_exceptions=True)` |
| **5** | **`_safe_task_callback` 未定义崩溃** | `CHANGELOG:394` | 回调必须定义后再注册（我们不用裸回调） |
| **6** | **`player/wbi/v2` 带 cookie 返回 412 风控** | `CHANGELOG:425` | 字幕取不到就降级，**不硬刚** |
| **7** | **`12006 没有该评论` 被当可重试 → 无意义重复发送** | `CHANGELOG:192` | 明确区分"不可重试"错误码 |
| **8** | **AI 处理超时要跳过并继续轮询** | `CHANGELOG:186` | 单条超时**跳过**，不阻塞整轮 |
| **9** | **备份默认排除 Cookie/密钥，避免泄露** | `CHANGELOG:178` | 我们已有 `core.redact` |
| **10** | **自我进化必须 `auto_apply = False` 写死** | `evolution_settings.py` | 照抄，**连配置项都不暴露** |

---

## 四、落地的三项（照抄，不自造）

### ① 分频道限速 + `-509`/`-799` 退避 → [`core/throttle.py`](../core/throttle.py)

**抄自**：`blb/brain/monitor.py`（分频道间隔）+ `blb/api/throttle.py`（全局冷却 + 抖动）

| 机制 | 实现 |
|---|---|
| 分频道间隔 | 评论 120s / 私信 60s / @通知 300s / metadata 3s |
| `-509` | **该频道**退避 10 秒 |
| `-799` | **全局**冷却 90~180 秒（全站级） |
| 抖动 | 间隔 + `random(0, 1.0)` |
| `buvid3` 校验 | UUID+infoc 形态，格式错**拒绝** |

**★ 我们比它严的地方**：它评论/私信间隔是 **5 秒**，我们用**分钟级**——
本项目红线是"不加大请求强度"。

**已接进**：`core/ratelimit.py` 的 `get_json()`（唯一读取出口），
按 `kind` 当频道名，命中限流码时自动冷却。

### ② 私信闭环最后一环 → [`core/repolink.py`](../core/repolink.py) + MCP 工具 `find_repo` / `read_source`

**主人纠正的流程**：

```
私信线索 → B站搜索/推荐流 → 读视频 → 顺视频找仓库 → 读源码 → 回复
                                        ↑ 之前缺这一环
```

**★ 诚实记录**：`blb` 与 `langup-ai` **都没有**这个能力
（它们的 agent 工具只有 B站操作 + 知识库）。

而且 `blb` 把「**不修改源码或执行生成代码**」写进了 `hard_boundaries` ——
这条我们**照抄**：只读文本，绝不执行仓库代码。

**两个工具**：

| 工具 | 作用 |
|---|---|
| `find_repo(bvid)` | 从视频简介/字幕/标题抽仓库地址（简介优先，**不猜**） |
| `read_source(repo, path?)` | 读源码：不给 path 则列根目录 + README；给 path 读单文件 |

**★★ 真机踩到的坑（第一版写错，已修）**：

第一版 `read_source` 为了"列根目录"直接下**整个 zip** ——
一个 13 MB 的仓库要下 13 MB，而且实测**卡住不返回**（超过 240 秒）。

⇒ 改用 **`api.github.com/contents`**：只返回目录元数据（几 KB），快几十倍。
新增 `fetch_repo_contents()`，并加测试钉住"**列目录不许下整包**"。

**真机验证（完整闭环）**：

```
① find_repo(BV1DkJ869EEn)  → Ebony-Vinyl/dsh-our-free-model
② read_source(repo)        → 12 个根文件 + README（jsdelivr）
③ read_source(repo, path)  → 真读到 PACKAGING.md（2815 字）
```

**实际用这条闭环回了主人的私信**（"找点好用的插件"）：

```
搜 B站 3 个关键词 → 读 3 个视频 → 抽出 5 个仓库
→ 读源码判断 → 推荐 @sugarforever/dsh-zvec-grep
   （底层 zvec-grep ⭐4104 / Apache-2.0，有 DSH 封装）
```

### ③ 自我进化安全框架 → [`core/evolution.py`](../core/evolution.py)

**抄自**：`blb/services/evolution_settings.py`

| 闸 | 它的做法 | 我们的实现 |
|---|---|---|
| `auto_apply` | 代码里强制 `= False` | `AUTO_APPLY_FORBIDDEN = False`，**配置项都不暴露** |
| 参数步长 | `max_parameter_step: 0.05` | `MAX_PARAMETER_STEP = 0.05` |
| 参数硬边界 | `PARAMETERS = {...: (min, max, default)}` | `EVOLVABLE_PARAMETERS`（3 个参数） |
| 每日上限 | `daily_ai_limit: 2` | `DAILY_PROPOSAL_LIMIT = 2` |
| 禁改项 | `hard_boundaries` | `FORBIDDEN_TARGETS`（8 项，含前缀防绕过） |

**★ 一个我们做得更细的地方**：步长按参数类型分两种——

* **比率型**（0~1，如打分权重）：绝对 0.05；
* **天数型**（如 `published_within_days` 1~90）：**相对 5%**（30 天 → 最多动 1.5 天）。
  否则"0.05 天"对 30 天毫无意义。

**越界是拒绝，不是夹紧** —— 夹紧会让 AI 以为"我改成功了"，后续推理全错。

---

## 五、我们**没抄**的（及理由）

| 功能 | 为什么不抄 |
|---|---|
| **好感度系统** | 面向陌生人的运营；我们只服务主人一个 |
| **AI 心情系统** | 让行为不可复现，与可审计原则冲突 |
| **随机模式**（`core/variety.py`） | 主人刚抱怨打分不准，这时引入随机抖动是反方向 |
| **邮箱找回 / Web 35 分区 / Docker / Android** | 它是给陌生人用的产品，我们是你一个人的工具 |
| **3 层进化**（knowledge/strategy） | 我们还没有"策略说明书"，做了是空壳；先把 parameters 层做扎实 |
| **`bilibili-api` 库** | 我们已有自研 `core/bilibili.py`（23 函数 + WBI 签名 + 缓存），换库是**大改**且收益不明。**但**：如果将来要接更多平台，值得重新评估 |

---

## 六、自检

| 项 | 结果 |
|---|---|
| `pytest tests/` | ✅ **883 passed, 1 skipped** |
| `ruff check` | ✅ All checks passed |
| `mypy`（strict） | ✅ 43 source files clean |
| 插件 `selfcheck.mjs` | ✅ 全部通过 |
| **新增测试** | **90 项**（throttle 32 + repolink 26 + evolution 32） |
| **真机验证** | ✅ `find_repo` 从 `BV1DkJ869EEn` 抽出 `Ebony-Vinyl/dsh-our-free-model` |

**MCP 工具**：19 → **20**（新增 `find_repo`）。
