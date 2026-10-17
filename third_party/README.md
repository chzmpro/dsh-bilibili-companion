# third_party —— 本目录**故意为空**（附"看 GPL 代码再自己重写"的准确边界）

任务书 §2 建议"将上游仓库作为固定 commit 的只读依赖目录或 Git submodule"，
并把 `xiaoyaya191/bilibili_learning_bot`（下称 **BiliLearn**）列为第二参考。
**本项目没有把它放进来，这是主人确认过的决定。**

---

## 1. 为什么不 vendor

| 项 | 实测事实 |
|---|---|
| BiliLearn 自身许可 | **MIT**（`Copyright (c) 2025 XingYe contributors`）—— 本身没问题 |
| 它唯一的 B 站依赖 | **`bilibili-api-python==17.4.1`** → **GPL-3.0-or-later** |
| 它的 B 站读写实现 | **全部** import 了 `bilibili_api`（`api/client.py`、`api/auth.py`、`xingye_bot/bilibili_ops.py`、`agent/tools.py` 等） |

→ **一旦 import 或 vendor 它的读写模块，GPL-3.0 就会传染到本项目**，
本项目就不能再以 MIT 分发。

**决定**：不 vendor、不 import。
本项目的 B 站客户端（`core/bilibili.py` + `core/wbi.py`）是**自建 httpx**，
依赖只有 `mcp / httpx / pydantic / pyyaml / typer / tzdata` —— **零 GPL**。

---

## 2. ★ "看 GPL 代码然后自己重写"——准确边界（主人问过这条）

主人的意思是：**读别人的实现、学到东西，然后写自己的版本，这样不就没风险了吗？**
大方向对，但**准确说法要分两类东西**，否则容易踩坑：

| 类别 | 例子 | 能否自由使用 |
|---|---|---|
| **协议事实 / 接口契约** | 参数名叫 `at_name_to_mid`；接口是 `POST /x/v2/reply/add`；WBI 签名的混音表；某个错误码 `-352` 的含义 | ✅ **可以**。这类是**事实**，不是表达；版权不保护事实与接口规格 |
| **代码表达** | 具体怎么组织函数、变量名、注释、控制流、内部数据结构 | ❌ **不可以**照搬。这受版权保护 |

所以**正确做法是**：

```
读 GPL 代码 / 官方文档  →  只提取【协议事实】（参数名、端点、格式、约束）
                        →  用我们自己的结构、命名、错误处理【独立实现】
                        →  不复制任何代码行、注释、内部设计
```

### ⚠️ 一个必须诚实说明的注意点

严格意义的"**净室实现**（clean room）"要求
**写规格的人**与**写代码的人**分离，避免实现者脑子里带着别人的表达。
我们做的是**弱化版**：同一个人既看又写，只保证"只取事实、不抄表达"。

**这在实践中是可接受的**，但对**高价值/高争议**的模块要更谨慎。
本项目的现状：

| 模块 | 来源 | 现在的做法 |
|---|---|---|
| `core/wbi.py`（WBI 签名） | 同工作区的 **curator**（MIT，自建实现） | 从 **MIT** 项目移植 → 无 GPL 问题 |
| `core/bilibili.py`（读取层） | 自建 | 只按接口事实写 |
| `core/publish.py`（门禁） | 自建 | 参考 BiliLearn 的**开关清单**（纯逻辑模块，无 GPL） |
| `core/mention.py`（`@`） | 待定 | 只取协议事实，自己实现 |

> ✅ **我们从来没有 import 过 `bilibili_api`** —— 可用下面命令复核：
> ```bash
> grep -rl "bilibili_api" core bridge cli tests scripts | wc -l    # 期望 0
> ```

---

## 3. 实际借鉴了什么（可追溯）

| 借鉴对象 | 拿到什么 | 本项目落点 |
|---|---|---|
| `core/platform_actions.py`（纯逻辑，**无 GPL**） | 17 个发布开关的**清单** | `core/publish.py` 的 9 道门禁 |
| `agent/tools.py`（只看**签名形状**） | `dry_run` / `allow_write` 的形态 | `config.interaction` 三件套 |
| `tests/` 的 **125 个测试文件名** | 一份免费的边界清单 | `docs/compatibility.md` §3.6 |
| `api/throttle.py`、`utils/storage.py`、`security/guard.py` | 限流 / 原子写 / 注入过滤的**设计思路** | `core/ratelimit.py`、`core/storage.py`、`core/redact.py` |
| `SocialSisterYi/bilibili-API-collect`（**文档仓库**，非代码） | 接口参数事实 | 用于 `@` 与评论接口 |

> 全部为**重写实现**，未复制代码。审核方法与结论见
> [`docs/compatibility.md`](../docs/compatibility.md) §3。

---

## 4. 将来若确实需要 vendor

必须**先**满足：

1. 主人明确同意接受 GPL-3.0 传染（或换一个 MIT 的上游）；
2. 只 vendor **不含 `bilibili_api` import** 的纯逻辑模块
   （`core/platform_actions.py` / `api/throttle.py` / `utils/storage.py` /
   `xingye_bot/state.py` / `security/guard.py` / `api/subtitles.py` /
   `services/deep_dive.py` / `core/config.py`）；
3. 保留 MIT 许可与归属，并补针对性测试。

**在此之前请保持本目录为空。**
（`.gitignore` 写的是 `third_party/*` + `!third_party/README.md`，
即：除本说明外，放进来任何上游代码都不会被误提交。）
