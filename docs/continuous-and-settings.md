# 持续跑 + 设置页全量配置（2026-10-17）

对应主人两条要求：

> 「我觉得每天跑一轮太少了，而且跑一轮速度太快了，
> 你看看能不能出一个让其一直跑的开关？」
>
> 「我看我们插件侧的各种配置比较少，我是很希望能够像 DSH 一样
> 所有的能够进行修改，基本上大部分配置都能够让我在设置页进行配置，
> 同时防止一页太多，可以将相关的折叠展开进行配置」

---

## 一、持续跑（`schedule.mode = "continuous"`）

### 为什么**不做**真正的无限循环

主人原话是"一直跑"，但我**刻意没有**做成无限循环 —— 理由要说清：

- 本项目红线是「**不加大重试强度、不绕过风控**」；
- 无限循环会持续发请求，**风控风险随轮次线性上升**，而收益递减
  （一轮已经读 3 个候选 + 完整读 5 条）；
- 更重要的是：**没人盯着时失控的代价由主人承担**（封号）。

⇒ 改成 **"每 N 分钟一轮 + 每天上限 + 到点自动停"**：

```yaml
schedule:
  mode: "continuous"        # on_host_start | daily | continuous
  interval_minutes: 30      # 两轮间隔（≥5）
  max_runs_per_day: 8       # 跑满自动停
```

### 三条判定（实测）

| 情形 | 结果 |
|---|---|
| 间隔已满 | ✅ 跑（"今天第 3/8 轮"） |
| 距上轮不足 30 分钟 | ⛔ 不跑（告诉下一轮几点） |
| 今天已跑满 8 轮 | ⛔ **自动停**（"明天继续"） |

**与 `on_host_start` 的关键差别**：持续模式**不看"今天做过没有"** ——
它的语义就是"今天还要接着做"。防重入靠 `interval_minutes` + 任务锁。

### ★★ 两个容易踩错的地方（都有测试钉住）

**① 跑满判定必须**优先于**间隔判定**

顺序反了的话，跑满后还会因为"间隔够"而继续跑 —— 上限形同虚设。
测试 `test_continuous_cap_takes_priority_over_interval` 专门钉这一点。

**② "今天"只有一份实现**

`runs_today()` 走 `storage._start_of_today()`（与配额同一个函数）。
两份实现早晚一份本地时区、一份 UTC ⇒ **配额在半夜附近对不上，
而且白天测不出来**。

### 仍然走全部既有门禁

持续跑**不是**旁路：配额、平台健康、幂等键、发布门禁、谷时闸门
**一个都不少**。它只是"更勤地问该不该跑"，不是"绕过该不该跑"。

`companion_status.schedule` 现在会报：

```json
{ "mode": "continuous", "interval_minutes": 30,
  "max_runs_per_day": 8, "runs_today": 3, "due": true, ... }
```

---

## 二、设置页：45 个字段 + 折叠分区

### 从 8 个字段扩到 45 个

之前只有 8 个可改字段，现在覆盖：

| 分组 | 字段数 | 默认展开 |
|---|---|---|
| 什么时候自己跑（唤醒/频率） | 6 | ✅ |
| 挑什么、挑多少（搜索筛选） | 5 | ✅ |
| 每天最多做多少（配额） | 8 | 收起 |
| 人设 | 15 | 收起 |
| 豆包（文字处理） | 10 | 收起 |
| **只能去终端改的（授权类）** | 6 | 收起 |

**折叠设计**：只把最常用的两组默认展开；折叠状态存在 React state 里
（刷新回到默认），避免"上次不小心全展开、这次一屏看不到底"。

### ★★ 三个字段**永远不进**设置页（红线）

| 字段 | 为什么 |
|---|---|
| `interaction.mode` | 决定**能不能真的发东西**到公开平台 |
| `interaction.dry_run` | 同上 |
| `interaction.publish_enabled` | 同上 |
| `interaction.pause` | 安全停机 —— **恢复必须由人做** |
| `interaction.dm_reply_enabled` | 一开就能真发私信 |
| `text_worker.api_key_env` | 凭据相关 |

理由：**界面上点一下就生效 = 绕过"人审"这道闸门**。
这与项目既有的「人用入口不进 MCP」是同一个原则。

⇒ 设置页会**明确列出**这 6 个并说明"去终端改"，
而不是让人以为"找不到就是没有"。

### 单一字段表（前后端不各维护一份）

- 白名单只在 [`plugin/lib/pyconfig.mjs`](../plugin/lib/pyconfig.mjs) 的 `EDITABLE_KEYS`；
- 后端 `buildStatus()` 把**值**（`fields`）与**元数据**（`schema`）一起给前端；
- 前端按 `schema[path].type` 渲染对应控件（bool→开关 / choice→下拉 / int,float→数字框 / 其余→文本框）。

**两份字段表早晚会不一致，而症状是"界面改了没生效"** —— 很难查。所以只留一份。

### 安全机制

| 机制 | 做法 |
|---|---|
| 白名单外的字段 | `writeEditable` **跳过**，不写进 `config.yaml` |
| 跳过要**说出来** | 返回 `skipped`，界面提示"有 N 项被拒绝"，**不静默吞掉** |
| 非法 `choice` 值 | `renderValue` **先校验再写**（否则会写坏 config，Python 侧启动才报错） |
| 注释 | 逐行替换，**注释与格式全保留** |
| 备份 | 写前存 `.bak-plugin` |

### 接口契约（D19 教训）

`selfcheck.mjs` 新增断言（**真调接口、逐字段验**）：

- `status` 必须含 `fields` / `schema` / `neverEditable`（缺了界面渲染空面板且不报错）；
- `schema` 每项都有 `type`，`choice` 必须带 `options`；
- **授权字段不得出现在 schema 里**（泄漏即失败）；
- `neverEditable` 要列全；
- 传 `interaction.publish_enabled` / `interaction.mode` 必须**被拒绝**且出现在 `skipped`；
- 非法 `choice` 值不写进 config。

---

## 四、★ 渲染冒烟测试（`plugin/render-smoke.mjs`）

### 为什么必须有它

`selfcheck.mjs` 验的是**后端接口契约**（字段在不在），**没验前端能不能渲染**。
而这两类失败性质完全不同：

| 失败类型 | 后果 |
|---|---|
| 后端少字段 | 界面停在"读设置中"（难看，但 **shell 活着**） |
| **前端渲染抛异常** | **整个 DSH 桌面端起不来**（AGENTS.md A4，issue #869） |

本轮设置页从 8 个字段扩到 45 个、引入折叠分区与通用 `Field` 组件 ——
**渲染路径变复杂了**，所以必须真跑一次渲染，而不是"看着没毛病"。

### 怎么做

用**假的** `window.__ModuleLoader__` + **真的** React（`renderToStaticMarkup`）
把 `client.js` 加载起来，然后用**真实形态的 status 载荷**渲染 `Panel`：

```bash
# ① 先解出 DSH 自带的 react（本机没有独立的 react）
bash plugin/extract-react-for-smoke.sh
# ② 跑渲染冒烟
NODE_PATH=/d/DeepSeek/workspace-temp/react-probe/node_modules node plugin/render-smoke.mjs
```

### 覆盖的 5 种载荷

| 载荷 | 为什么测它 |
|---|---|
| 正常（45 字段 schema） | 基线 |
| `fields` 全空 | `config.yaml` 读不到时**不能崩** |
| `schema` 全空 | 后端没给元数据时**不能崩** |
| `neverEditable` 缺失 | 老后端兼容 |
| 字段值类型异常（数字给成字符串） | 界面值不一定符合类型 |

外加：**折叠分区标题必须真的出现在 HTML 里**（否则等于没渲染出来）。

### ★ 幽灵字段检查（最有价值的一条）

`FIELD_GROUPS` 在 `client.js`，白名单在 `pyconfig.mjs` —— **两处引用同一批字符串**。
写错一个字母，那一项就在界面上**静默消失**（渲染时 `.filter(p => schema[p] !== undefined)`
会把它滤掉）：**不报错、不崩溃，只是"少了一项"**，最难发现。

所以加了**双向**断言：

- 正向：设置页引用的字段**都在**白名单里；
- 反向：白名单字段**都在**设置页有控件（`discovery.keywords` 除外，它用专门的多行框）。

这条同时进了 `selfcheck.mjs`（不依赖 react，任何环境都能跑）。

### 变异验证（证明测试不是摆设）

| 变异 | 结果 |
|---|---|
| 把 `'schedule.max_runs_per_day'` 写错一个字母 | ✅ **抓到**（3 项失败，点名那个字段） |
| 让 `Field` 组件抛异常 | ✅ **抓到**（5 种载荷全报"渲染抛异常"） |
| 还原 | ✅ `diff` 逐字节确认 `client.js` 完好，全部通过 |

> ⚠️ **第一版冒烟测试自己有个 bug**：我手写了一个"精简 schema"当测试数据，
> 结果 `interaction.*` 那一组因为缺字段被正确过滤掉，**测试误报成代码 bug**。
> 修法：**用真实白名单**（`import EDITABLE_KEYS`）造测试数据，
> 不手写子集 —— 手写的数据和真实数据早晚会漂移。

### 本机没有 react 怎么办

DSH 的 react 打在 `app.asar` 里（`react@18.3.1`）。
`extract-react-for-smoke.sh` 把它解到 `workspace-temp/react-probe`（幂等、可从零重建）。

> ⚠️ `asar extract-file` 的两个坑：① 路径必须用**反斜杠**；
> ② 它把文件解到 **cwd**（不是目标目录），要自己 `mv`。

---

## 五、本轮自检

| 项 | 结果 |
|---|---|
| `pytest tests/` | ✅ **733 passed, 1 skipped** |
| `ruff check` | ✅ All checks passed |
| `mypy`（strict） | ✅ 39 source files clean |
| 插件 `selfcheck.mjs` | ✅ 全部通过（含授权字段拒绝、契约、幽灵字段） |
| **渲染冒烟**（真 React） | ✅ **全部通过**（20 项断言） |
| **变异验证** | ✅ 写错字段名 / 渲染抛异常 **都能抓到** |
| 三道体检 | ✅ 孤儿行 / 形态 / 行归属 全过 |
| 真配置完整性 | ✅ 授权字段一字未动、注释保留、自检改动的值已复原 |
| 已装副本 vs 源码 | ✅ 三个运行时文件 `diff` 一致 |
