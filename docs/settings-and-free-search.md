# 设置页扩容 + 免费联网搜索（2026-10-17）

## 主人两条反馈

> 「blb 我记得他的设置项有很多可以自己设置的，你怎么没有学习」

> 「这个搜索不是我想要的免费互联网搜索那种」

---

## 一、设置页：46 → 61 个可配置项

### 1.1 现状核对（先承认问题）

参考项目 `bilibili_learning_bot` 有 **391 个配置项 / 54 个段**。
我们当时只有 **46 / 70** —— 而且剩下 18 个**既没开放、代码里也没说明为什么锁着**。

| 类别 | 数量 |
|---|---|
| 配置叶子字段总数 | **70** |
| 设置页可编辑（改前） | 46 |
| 设置页可编辑（改后） | **61** |
| 明确锁定（授权） | 6 |
| 只读身份 | 3 |

**61 + 6 + 3 = 70** —— 现在每一个字段都有归属，没有"隐形字段"。

### 1.2 新增开放了什么

| 分组 | 字段 | 为什么开放 |
|---|---|---|
| **网络与限速**（新组） | `bilibili_requests.*` 8 项 | 调的是**快慢/容忍度**，最坏是变慢，不会变成乱发 |
| **日报与通知**（新组） | `report.*` 5 项 | 决定推什么、什么值得打扰你 |
| 什么时候自己跑 | `schedule.timezone` | 时区影响 `daily` 模式的时刻解释 |
| 豆包（文字处理） | `text_worker.provider` | 换接口类型（默认 `volcengine_ark`） |

### 1.3 ★ 为什么**不**全开放（和 blb 的取舍不同）

blb 是**给陌生人用的产品**，所以它有邮箱找回、Docker、Android、35 个面板分区。
我们是**你一个人用的工具**，所以原则是：

> **只锁"授权"，开放"调参"。**

| 锁着的 | 为什么 |
|---|---|
| `interaction.mode` / `dry_run` / `publish_enabled` / `pause` / `dm_reply_enabled` | **授权**。界面上点一下就能发布 = 绕过人审闸门 |
| `text_worker.api_key_env` | 密钥环境变量名，改错会让 worker 静默失效 |
| `account.agent_mid` / `owner_mid` / `owner_name_hint` | **身份**。改错小号直接工作不了 |

### 1.4 结构性防线（新增测试）

`tests/test_settings_coverage.py` 8 项，钉死三条：

1. **不许有幽灵字段** —— 可编辑清单里的字段必须真在 `Config` 里；
2. **不许有隐形字段** —— 每个 `Config` 叶子必须归入「可编辑 / 锁定 / 只读身份」之一；
3. **授权字段永不泄漏** —— 6 项绝不能出现在可编辑清单里。

外加**覆盖度下限 80%** —— 防止以后又退回"只开放少数几项"。

### 1.5 顺带修的一个自检 bug

`selfcheck.mjs` 原来把段名前缀**硬编码**成
`(?:schedule|discovery|interaction|persona|text_worker)`，
所以新增 `bilibili_requests.*` / `report.*` 两个段时，
**即使 `client.js` 里真的加了控件也会误报"未展示"**。

⇒ 改成从 `EDITABLE_KEYS` **自动推导**段名，以后加新段不会再漏。

---

## 二、免费联网搜索：装 `modsearch`

### 2.1 先说我上次推荐错了

上次我推荐的是 `@sugarforever/dsh-zvec-grep` —— 那是**工作区语义搜索**
（在你自己的代码/文档里按意思找），**不是联网搜索**。

### 2.2 找到的正确答案

`liustack/modsearch`（⭐722，MIT）

> *"In the chat app your model can search the web. On the API it cannot.
> ModSearch puts the web back: web search, X search, page fetch.
> **Free, no signup, no API key.**"*

引擎：**Firecrawl**（keyless，默认）· **Keenable**（keyless）· Antigravity CLI · Tavily · Exa · Grok(X) · local，自动故障切换。

### 2.3 ★ 为什么**不是**装成插件（重要）

我先按插件装了 `@liustack/modsearch`，三道体检里**行归属检查报警**：

```
'@liustack/modsearch'   2    行 · client=未知（跳过）
```

原因是它的 `cordis.patch.yml` 有**两行**：
一行 patch 掉 core `web` 行的 `searchProvider`，一行 insert 自己。
而 `__dshPluginOwner.packageName` 因此把**两行**都归到了它名下 ——
它又**声明了 `dsh.client`**，这正是 `AGENTS.md` **A1 铁律**描述的安全模式场景
（浏览器半身会被挂载两次）。

**已立即卸载回滚**（`searchProvider` 恢复 `deepseek-official`、0 残留、体检全过），
绝不拿"能不能开机"去赌。

### 2.4 改为装成 **skill**（零 safe-mode 风险）

modsearch 自带 `skills/modsearch/`，skill 不挂插件、不参与 client bundle 组合。

```bash
# 已装到
~/.agents/skills/modsearch/
  ├── SKILL.md
  ├── references/（cli / configure / output-schema / runtime）
  └── scripts/run.ps1（Windows）· run.sh（POSIX）
```

它靠 `npx @liustack/modsearch@5.10.7` 跑已发布的 npm 包，
**不需要本地构建**。前置：Node ≥ 22.13（本机 v24.19.0 ✅）。

### 2.5 真机验证（这是关键证据）

```
查询：DeepSeek Harness 最新版本
结果：engine=firecrawl  status=ok  durationSeconds=1.76  credits=2
      返回真实条目（36氪 / Medium / 菜鸟教程）
```

**1.76 秒、无 API key、无注册。** DSH 的 skill 目录已自动识别到 `modsearch`。

### 2.6 用法

```
modsearch -q "<查询>"              # 搜网页
modsearch -q "<查询>" --source x   # 搜 X
modsearch -u "<url>"               # 读一页
```

### 2.7 已知限制

| 限制 | 说明 |
|---|---|
| X 搜索需要 Grok Build | 没有就会**降级**为普通网页搜索，并在 `warnings` 里写明 |
| Firecrawl keyless 额度 | 1000 credits/月，用完会自动切到 Keenable |
| 网页抓取的 `local` 引擎 | 不执行 JS，JS 渲染的页面会说明"内容很少" |

---

## 三、自检

| 项 | 结果 |
|---|---|
| `pytest tests/` | ✅ **911 passed** |
| 新增测试 | `test_settings_coverage.py` 8 项 |
| `ruff` / `mypy` | ✅ 全过 |
| 插件 `selfcheck.mjs` | ✅ 全部通过（含幽灵字段双向检查） |
| 三道体检 | ✅ 孤儿行 / 形态 / 行归属全过 |
| 设置页字段 | 46 → **61**，覆盖度 61+6+3 = 70 ✅ |
| 免费联网搜索 | ✅ `firecrawl` 1.76s 真实返回 |
| modsearch 插件形态 | ❌ 已回滚（2 行 = A1 风险），改走 skill ✅ |
