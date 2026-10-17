# dsh-raw-html 精读与**未安装**结论（2026-10-17）

> - 视频：主人自己刷到的 `dsh-raw-html`
> - 仓库：<https://github.com/plolpl789/dsh-raw-html>（⭐79，MIT，v0.6.2）
> - 它是什么：**VCP 视觉通感协议插件** —— 让 agent 输出的 HTML 被**真正渲染**成
>   UI（卡片 / KaTeX 公式 / Mermaid 图表 / 内置书法字体 / 零 JS 交互）

## 结论：**只取了设计资产，没装插件**

主人的决定：*「先不装，只把它的设计规范/字体拿来用」*。

**为什么装不了**（不是"我不想装"，是**装不上**）：它的渲染能力**靠给 DSH
自己的前端 bundle 打补丁**，而那个补丁**是为 DSH 0.1.1-rc.x 写的**，
本机是 **0.2.0-rc.2** —— **四个锚点全部不命中**。

已提取的资产见 [`design/`](../design/)（设计规范 + 16 风格 + 25 款字体）。

## ★ 精读要点：它到底怎么工作

它**不是普通插件**，是"**插件 + 给宿主打补丁**"两半：

| 半身 | 做什么 | 载体 |
|---|---|---|
| **补丁** | 给 DSH 前端 bundle 注入 HTML 渲染能力（`case"html"` 分支 + 属性过滤 + SVG/Mermaid/KaTeX） | `patch/install-v6.cjs` **直接改写** `dsh-web-frontend/dist/assets/index-*.js` |
| **插件** | `</>` 开关按钮 + `input()` 交互桥 + 可信模式徽章 | `lib/client.js`（正常插件机制） |
| **host** | 开关开启时往 systemPrompt 注入 VCP 协议说明 | `lib/index.js` |

> ★ 关键设计取舍（它的 CHANGELOG 0.6.2 自己写的）：
> **可信模式已从补丁迁到插件层** —— 因为补丁改的是 bundle，停用插件也撤不掉；
> 迁到插件层后"停用插件 = 徽章消失、放行全部回收"。**这是个好设计**，
> 值得记：**能让插件机制管的事，就不要焊进宿主 bundle**。

## ★ 为什么装不上：锚点全面失配（实测）

我把目标 bundle 从 `app.asar` 提取出来（633,282 字节），逐个比对补丁锚点：

| 补丁期望（0.1.1-rc.x） | 本机实际（**0.2.0-rc.2**） | 命中 |
|---|---|---|
| `case"html":return n.value;` | `case"html":return e.value` | **0** |
| `for(const c of i.attributes)c.name==="class"?…` | `for(const u of o.attributes)u.name==="class"?…` | **0** |
| `}function vc(n,r){` | `function vc(e,n)` | **0** |
| `localName==="script"‖i.localName==="iframe"` | **完全不存在** | **0** |

**根因两层**：

1. **压缩变量名变了** —— 打包器把 `i/n/c` 换成了 `o/e/u`（同名代码不同压缩结果）；
2. **更根本：0.2.0 的渲染层结构变了** —— 补丁要找的 `script/iframe/object/embed`
   硬过滤那段**在新版里根本不存在**（渲染层被重写过）。

⇒ 就算把变量名改对，**第 4 条也无处可打**（目标代码没了）。

## ✅ 它的安全设计**有效**（实测确认）

我干跑了一次补丁，**它安全中止、没写坏任何文件**：

```
[install-v6] bundle: D:/.../index-5SrrfWpU.js
[install-v6] 备份: ...bak-installv6-2026-10-17T00-31-48-917Z
[install-v6] ✗ case"html" 分支：既非原始态也非已补丁增强态，无法识别。
[install-v6]   可能是 dsh 版本过新导致压缩代码变化，已安全中止（备份已保留，未写入修改）。
```

原文件仍是 **633,282 字节**（未变）。**这一点值得表扬**：
它按"锚点不匹配 → 中止"设计，而不是"尽力替换"。

> ⚠️ 但注意它**仍然写了备份文件**（`.bak-installv6-<时间戳>`）——
> 中止前的备份动作会留下垃圾。我已清理自己那次产生的备份。

## ★ 还有一个更根本的障碍：目标在 `app.asar` 里

补丁的探测路径**全是磁盘目录**：

```js
addDir(path.join(APPDATA, 'npm/node_modules/@deepseek-ai/dsh-web-frontend'))
addDir(path.join(USERPROFILE, '.dsh/profiles/web/node_modules/@deepseek-ai/dsh-web-frontend'))
```

但本机 `dsh-web-frontend` **只存在于 `app.asar` 内部**（281 MB 只读归档），
`app.asar.unpacked` 里没有它，profile 的 `node_modules` 里也没有。

⇒ 即使锚点匹配，也要**先把文件从 asar 提取出来、打补丁、再放回去**
（或改用 `asar` 的 unpack 机制）—— **这已经超出插件安装的范畴，
是在改 DSH 自己的安装产物**，DSH 一升级就被覆盖。

## 我们从中**学到**什么（不只是"装不上"）

| # | 学到的东西 | 用在哪 |
|---|---|---|
| 1 | **能让插件机制管的事，不要焊进宿主 bundle** | 我们的插件本来就这么做（设置页走 `slots`，不改宿主） |
| 2 | **补丁必须"锚点不匹配就中止"** | 与我们的"接口契约断言"同源：**宁可失败得响亮，不要静默改坏** |
| 3 | **依赖必须显式声明**（它 CHANGELOG 记了"2026-08-19 崩溃事件"） | 我们已清掉 `peerDependencies` 里的内部包（AGENTS.md D10） |
| 4 | **vcp-root 内禁止空行** —— Markdown 的 HTML 块遇 `\n\n` 就结束，卡片会被解析成多个节点（症状：背景只包顶部一条） | 写卡片 HTML 时 `\n\n` 必须为 0 |
| 5 | **正文禁止字面标签名** —— 提到 script 要写 `&lt;script&gt;`，否则扫描器当成未闭合标签、**正文从该处起全部消失** | 写含 HTML 的正文时注意 |
| 6 | **字体入库前要目视确认"名实相符"**（它的"爱情手写"根本不是手写体） | 我们用它的字体时，先看效果再定 |

## 如果以后想装（前提条件）

需要**上游先适配 0.2.0**，或由我们自己：

1. 确认 0.2.0 的渲染层还有没有"HTML 分支"可改（**目前看结构已变**）；
2. 解决 `app.asar` 目标问题（提取→改→回填，且升级即失效）；
3. **接受"DSH 每次升级都要重打补丁"**的维护成本。

> 我的建议：**别走补丁路线**。要么等上游适配，要么把"渲染 HTML"做成
> **我们自己的插件半身**（走正常插件机制），而不是改宿主 bundle。

## 已提取的资产

```
design/
├── DESIGN.md      视觉设计系统（字体资产表 / 中文排版 / 安全铁律 / 色板）
├── EDITORIAL.md   审美层（编辑感 / 四色系 / 视觉词汇库）
├── FRAMING.md     构图取景
├── BREATH.md      留白与节奏
├── styles/        16 个风格定义
├── fonts/         25 款 woff2 子集字体（55 MB）
└── LICENSE-dsh-raw-html
```

⚠️ **字体授权不做担保** —— 上游声明为开源（OFL）+ 自备字库子集，
但商用前请自行确认（上游 `DESIGN.md` §0.1 也这么写）。
