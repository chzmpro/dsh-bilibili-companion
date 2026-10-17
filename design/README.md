# design/ —— 设计规范与字体（**第三方资产，非本项目代码**）

> ⚠️ **这不是本项目的代码**，是从 [plolpl789/dsh-raw-html](https://github.com/plolpl789/dsh-raw-html)
> 提取的**设计资产**（2026-10-17）。

## 为什么放这里（而不是装那个插件）

`dsh-raw-html` 的**插件本体没装** —— 原因见
[`docs/dsh-raw-html-review.md`](../docs/dsh-raw-html-review.md)：
它的渲染能力靠**给 DSH 自己的前端 bundle 打补丁**实现，
而它的补丁锚点是为 **DSH 0.1.1-rc.x** 写的，本机是 **0.2.0-rc.2**
（压缩变量名从 `i/n/c` 变成 `o/e/u`，且 0.2.0 的渲染层结构已变）
⇒ **四个锚点全部不命中**（实测补丁会安全中止、不写坏文件）。

所以按主人的决定：**只把它的设计规范与字体拿来用**。

## 内容

| 文件/目录 | 是什么 |
|---|---|
| [`DESIGN.md`](DESIGN.md) | 视觉设计系统：字体资产表、中文排版、**安全铁律**、色板速查 |
| [`EDITORIAL.md`](EDITORIAL.md) | 审美层：编辑感、四色系、视觉词汇库 |
| [`FRAMING.md`](FRAMING.md) | 构图/取景规范 |
| [`BREATH.md`](BREATH.md) | "呼吸法"——留白与节奏 |
| [`styles/`](styles/) | 16 个风格定义（brutalism / cyberpunk-neon / wabi-sabi / maiden-diary …） |
| [`fonts/`](fonts/) | **25 款 woff2 子集字体**（GB2312 一级 3755 字 + 标点），共 55 MB |
| [`LICENSE-dsh-raw-html`](LICENSE-dsh-raw-html) | 上游 MIT 许可证 |

## ★ 值得注意的两条（上游踩坑记录）

1. **`vcp-root` 内禁止空行**（`DESIGN.md` §4）——
   Markdown 的 HTML 块遇到 `\n\n` 就结束，卡片会被解析成多个节点，
   症状是"背景只包顶部一条，下面内容溢出"。**写卡片 HTML 时 `\n\n` 必须为 0**。
2. **正文禁止出现字面标签名**（CHANGELOG 0.6.3）——
   提到 script 标签时要写实体 `&lt;script&gt;` 或中文描述，
   否则会被扫描器当成未闭合标签、**正文从该处起全部消失**。

## 字体授权

- 上游声明：内置字体为**开源授权**（OFL）+ 作者自备字库的子集。
- ⚠️ **本项目不对字体授权做任何担保** —— 商用前请自行确认
  （上游 `DESIGN.md` §0.1 也写明"其中可能含商业字库，授权由用户自行确认"）。

## 用途

这些资产**不参与本项目任何运行逻辑**（不会被打进插件包，
`plugin/package.json` 的 `files` 只含 `index.js`/`client.js`/`lib`/`cordis.patch.yml`）。
它们的用途是：

- 给 **Multiplane** 做视频时提供字体与设计规范（见
  [`docs/multiplane-setup.md`](../docs/multiplane-setup.md)）；
- 需要产出**视觉化内容**（卡片、封面、图文）时当参考。

## ★ 已经真用上了一次（验证）

[`multiplane/demo.html`](multiplane/demo.html) —— 用这里的 **Lanxi-HeiTiBold**（标题）
与 **Lanxi-WenKai**（正文）真渲染了一条 2 秒 960×540 的视频（48 帧，H.264），
抽帧确认**中文字体真的加载了**（不是系统兜底）：

![demo 抽帧](multiplane/frame.png)

> ⚠️ 由此实测出 Multiplane 的两条硬规则（详见
> [`docs/multiplane-setup.md`](../docs/multiplane-setup.md) §6/§7）：
> 资源必须在**资源根之下**（`../` 越出报 `resource_missing`，哪怕文件存在）；
> 自定义字体必须在 `Shot.define` 的 `fonts` 里**显式声明**（否则 `font_not_awaited`）。
> 所以 [`multiplane/`](multiplane/) 里拷了两份用到的字体。
