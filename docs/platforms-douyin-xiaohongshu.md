# 抖音 / 小红书接入可行性调研（2026-10-19）

> 状态：**调研结论存档**，**未接入**（`core/platforms.py` 仍是预留框架）。
> 调研方式：只读联网检索（GitHub API + B站视频搜索 + 搜索引擎），
> 未注册账号、未调用平台 API、未发布任何内容。
>
> ⚠️ 多处**未核实**（尤其官方 API 权限矩阵），下文逐条标注，**不要把未核实当结论**。

---

## 0. 结论速览

| 维度 | 抖音 | 小红书 |
|---|---|---|
| 官方 API 是否开放内容发布/评论私信 | **未核实**（检索未取到文档正文） | **未核实** |
| 开源项目成熟度 | 中高（多为采集/客服/刷量） | 高（采集+发布+MCP 都有高星项目） |
| 主流实现手段 | Playwright/Patchright + cookie | 同左，且高频出现"逆向/纯协议" |
| 是否触碰本项目红线 | **是**（反检测、协议级刷量普遍） | **是**（"逆向"是主流表述） |
| "干饭不干事的小鲸鱼"是否已存在 | **公开搜索无法确认**（零命中） | 同上 |

**首要结论**：本轮查到的**所有"发布/评论/私信"实现路径**都依赖浏览器自动化或逆向协议；
**没有找到可用的官方开放 API 证据**。若坚持"不绕过风控、不代解验证码"的红线，
现实可行的只有 **A 方案（只读采集）**。

---

## 1. 开源项目（GitHub，sort=stars，检索于 2026-10-19）

### 抖音侧

| 仓库 | ⭐ | 语言 | 最近推送 | 能力 | License |
|---|---|---|---|---|---|
| [cs-lazy-tools/ChatGPT-On-CS](https://github.com/cs-lazy-tools/ChatGPT-On-CS) | 4490 | TypeScript | 2026-10-10 | 多平台 AI 客服自动回复（含私信/评论接待） | AGPL-3.0 |
| [3441293738/creatorhub](https://github.com/3441293738/creatorhub) | 2396 | Python | 2026-10-19 | 多平台监控·采集·搬运，FastAPI+Playwright，**仅采集** | **无** |
| [Useful-Scripts-Extension/useful-script](https://github.com/Useful-Scripts-Extension/useful-script) | 533 | JS | 2026-07-14 | Chrome 扩展含抖音下载器 | MIT |
| [DeekeScript/ad-deeke](https://github.com/DeekeScript/ad-deeke) | 416 | JS | 2026-08-17 | 自动评论/私信/点赞（引流类） | **无** |
| [baianjo/Douyin-Smart-Feed-Assistant](https://github.com/baianjo/Douyin-Smart-Feed-Assistant) | 194 | TS | 2026-06-02 | 推荐流调教 userscript（只读） | MIT |
| [tianxing-ovo/AutoSlide](https://github.com/tianxing-ovo/AutoSlide) | 111 | Kotlin | 2026-08-25 | 安卓无障碍自动滑动（不发布） | Apache-2.0 |
| [Laihiujin/Prism](https://github.com/Laihiujin/Prism) | 108 | HTML | 2026-10-19 | 多账号矩阵发布，topics 含 **`patchright`** | Apache-2.0 |

### 小红书侧

| 仓库 | ⭐ | 语言 | 最近推送 | 能力 | License |
|---|---|---|---|---|---|
| [Panniantong/Agent-Reach](https://github.com/Panniantong/Agent-Reach) | 99244（**疑似刷星**） | Python | 2026-10-15 | 一个 CLI 读/搜多平台，**只读** | MIT |
| [yikart/AiToEarn](https://github.com/yikart/AiToEarn) | 26627 | TS | 2026-09-18 | 多平台发布 | MIT |
| [putyy/res-downloader](https://github.com/putyy/res-downloader) | 20733 | Go | 2026-10-16 | 多平台资源下载 | Apache-2.0 |
| [xpzouying/xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp) | 16369 | Go | 2026-10-19 | **MCP server**：读取 + 发布 | Apache-2.0 |
| [dreammis/social-auto-upload](https://github.com/dreammis/social-auto-upload) | 15610 | **Python** | 2026-09-02 | 上传视频到抖音/小红书/视频号/… | MIT |
| [JoeanAmier/XHS-Downloader](https://github.com/JoeanAmier/XHS-Downloader) | 13050 | JS | 2026-10-18 | 采集，含 FastAPI + MCP | GPL-3.0 |
| [zhouxiaoka/autoclip](https://github.com/zhouxiaoka/autoclip) | 9534 | Python | 2026-10-19 | AI 剪短 + 文案（不发布） | MIT |
| [guozzz1/xiaohongshu-agent](https://github.com/guozzz1/xiaohongshu-agent) | 1 | Python | 未核实 | 采集→LLM 改写→**只预填发布页，人工点发布** | 未核实 |

**与我们形态最接近的三个**：

1. `dreammis/social-auto-upload`（Python，MIT）—— 唯一同时覆盖抖音+小红书发布的 Python 项目；
2. `xpzouying/xiaohongshu-mcp`（Go，Apache-2.0）—— 证明"小红书 MCP server"走得通（语言不同，只能搬运工具语义）；
3. `guozzz1/xiaohongshu-agent` —— "**预填表单、人工点发布**"，与我们的"受控发布"哲学一致，值得作为**设计**参考而非代码依赖。

---

## 2. B站实现视频（只取标题/简介，未逐条读全文）

**小红书方向**（注意标题里的用词）：

- 【2026全网首发】AI**逆向**开发小红书自动发布工具！一键批量发文 — [av116836951791780](http://www.bilibili.com/video/av116836951791780)
- 小红书全自动采集**曝光协议 V1.19** — [av1851573836](http://www.bilibili.com/video/av1851573836)
- 小红书流量系列，**纯协议** 全自动 — [av117349092956405](http://www.bilibili.com/video/av117349092956405)

**抖音方向**：

- 一键将视频发布全平台 批量发布 定时发布 — [av117421134385500](http://www.bilibili.com/video/av117421134385500)
- 最新抖音全自动获客工具，三天快速起号养号教学 — [av117422107397115](http://www.bilibili.com/video/av117422107397115)

**抖音开放平台方向**：

- 抖音开发过平台接口申请教程 — [av114533037249934](http://www.bilibili.com/video/av114533037249934)
- 获取字节跳动抖音平台的 API 接口三要素 — [av116028038256289](http://www.bilibili.com/video/av116028038256289)

> **观察**：小红书生态的标题普遍出现"逆向""纯协议""曝光协议 V1.19"这类
> **版本号迭代式命名** ⇒ 接口/签名频繁变更、攻防持续，逆向方案需长期跟进。
> 这正是与我们红线冲突的部分。

---

## 3. 平台上的"小鲸鱼"

对「干饭不干事的小鲸鱼」「小鲸鱼 干饭不干事」各检索一次，**两次均零命中**：
返回的全是"干饭"词条解释、平台首页等泛化内容。

**结论：公开搜索无法确认**该昵称在抖音/小红书的存在。
若需坐实，请主人**直接提供账号链接或 UID**，再定向核实（本轮不做任何推测）。

---

## 4. 官方 API 权限（**未核实**）

**抖音开放平台（open.douyin.com）与小红书开放平台（open.xiaohongshu.com）：
本轮未取到任何文档正文**，因此**不给权限矩阵**（凭记忆编表违反调研纪律）。

仅有两条**未经证实**的线索（来自 B站视频标题）：

- 「抖音平台的 API 接口三要素」⇒ 暗示走 client_key/secret 应用凭证体系；
- 「抖音小店开放平台」⇒ 抖音开放能力的一个明确方向是**电商/抖店**，而非通用内容发布。

**补做方法**（约 2 次抓取）：取 `https://open.douyin.com/platform/doc` 与
`https://open.xiaohongshu.com/` 的文档正文，再回答"个人/企业分别能申请什么、
发布/评论/私信是否开放、是否需要资质审核、有无配额"。

---

## 5. 代码框架（Python 优先）

- **Python 发布类**：`dreammis/social-auto-upload`（MIT，15610⭐）—— B/C 方案首要参照。
- **Python 采集类**：`3441293738/creatorhub`（Python+FastAPI+Playwright）——
  **无 License，法律上不可复用**，只能读思路。
- **架构参照**：`guozzz1/xiaohongshu-agent`（预填表单 + 人工发布）。
- **反检测风险源**：`Laihiujin/Prism` 的 topics 含 `patchright`（Playwright 反指纹分支）
  —— 引用其代码即等于引入红线风险。
- **MCP 形态已验证**：`xpzouying/xiaohongshu-mcp`（Go）证明这条路走得通。

**a_bogus / X-s / X-t 签名的具体开源实现库：未核实**（不提供库名，避免凭空生成）。

---

## 6. 风险与协议红线

1. **平台协议原文：未核实。** 未取到抖音/小红书用户协议与开发者协议正文，
   **不引述任何"原文条款"**。要引原文需抓取相应协议页。
2. **风控强度（有侧证）**：小红书生态标题高频出现"逆向""纯协议""V1.19"
   ⇒ 攻防持续、方案不稳定。
3. **封号风险**：自述"引流获客/模拟人工"的项目在平台规则中通常被定性为
   流量作弊/黑灰产，风险最高（本轮**未取得处罚条款原文**，故不量化）。
4. **是否需真机**：安卓无障碍方案与 `mobile-app` topics 表明**移动端真机**是该生态
   常见路径；agent 生态普遍依赖登录 cookie ⇒ 现实推论"大概率需真实账号登录态
   （可能含手机号验证）"，具体未核实。
5. **与本项目红线的冲突判定**：
   - **冲突（不做）**：反指纹/反检测（patchright）、签名逆向（a_bogus/X-s）、
     协议级自动化、批量自动评论/私信/点赞、验证码自动处理；
   - **不冲突（可做）**：只读页面采集、**预填发布表单 + 人工点发布**、
     官方 API（若存在且合规）。

---

## 7. 推荐落地选项

| 选项 | 能做什么 | 前提 | 主要风险 | 工作量 |
|---|---|---|---|---|
| **A. 只读采集**（推荐先做） | 按关键词发现抖音/小红书内容、抓标题/正文/评论数、送豆包预处理、产出候选 | 登录态（可复用 `ego_login_import`）；正常浏览器访问，**不做反检测** | 高频/无头访问可能被限流；页面结构变更；"自动化访问"条款（原文未核实） | **小～中**（1 个 platform adapter，复用 B站 discover/read 结构） |
| **B. 官方 API** | *无法评估* —— 权限矩阵未核实；抖音已知偏电商 | **先花 0.5–1 天核实文档**，并解决主体/资质 | 若仅对企业开放则个人小号申请不到 | 先核实，再定 |
| **C. 浏览器自动化**（风险最高） | 自动发布/评论/私信 | 真实登录态 + 大概率需反检测（**踩红线**） | **与红线直接冲突**；封号风险最高；维护成本随平台改动上升 | 大且不可控；**不建议**；若必须，只降级到"预填 + 人工确认发布" |

**建议路线**：先做 A（只读采集），同时用 0.5 天坐实 B；
C 只保留"预填 + 人工发布"的最低配形态。

---

## 8. 仍未核实的事项

1. `open.douyin.com` 与 `open.xiaohongshu.com` 的权限矩阵（主体要求/发布/评论/私信/配额）；
2. 抖音/小红书协议中关于自动化的**原文条款**；
3. `a_bogus` / `X-s` / `X-t` 的具体开源实现库；
4. "干饭不干事的小鲸鱼"在抖音/小红书的踪迹（零命中，无法确认）；
5. `Agent-Reach` 的 99244⭐ 是否刷星；
6. 采集行为本身是否构成协议违约；
7. 抖音/小红书账号是否需手机号实名才可登录。
