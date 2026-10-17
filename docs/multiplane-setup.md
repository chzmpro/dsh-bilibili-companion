# Multiplane 安装与踩坑（2026-10-17）

> Multiplane = **给 AI Agent 用的 HTML 视频渲染器**。
> 镜头就是普通网页，画面只由时间 `t` 决定，写出来的东西**可检查、可复现**。
>
> - 视频：`BV1g8Jy6FEpD`「【完全开源】实测比 HyperFrames 更快的 AI Agent 视频渲染器」
> - 仓库：<https://github.com/LBEILC/Multiplane>
> - npm：`multiplane`（**独立 CLI，不是 DSH 插件**）

## ★ 先澄清一件事：它不是 DSH 插件

它**不挂在 DSH 里**，是一个**命令行工具**（`multiplane <command> <shot.html>`）。
所以"安装"= `npm install -g` + 一个能用的 ffmpeg，**与 DSH 插件体系无关**
（不碰 `cordis.patch.yml`、不进 profile、不影响安全模式）。

## 安装结果（本机实测）

```bash
npm install -g multiplane                    # 0.3.8
npm install -g --allow-scripts=multiplane multiplane   # 放行 postinstall（下载 Chromium）
```

`doctor` 全绿（**9/9**，含可选组件）：

| 检查 | 结果 |
|---|---|
| `home` | ✅ `C:\Users\...\.multiplane\` |
| `node` | ✅ v24.19.0（要求 ≥24） |
| `browser` | ✅ Chromium 153.0.8010.12 |
| `gpu` | ✅ ANGLE / D3D11 |
| `ffmpeg` | ✅ 6.1.1 |
| `libx264` | ✅ |
| `libvpx-vp9` / `prores_ks` | ✅（可选，透明输出用） |
| `three` | ✅（可选，3D 场景用；`npm install -g three`） |

## ★ 踩坑点（逐条，都实测过）

### 1. `postinstall` 被 npm 默认拦下 → Chromium 不会装

```
npm warn allow-scripts 1 package has install scripts not yet covered by allowScripts:
npm warn allow-scripts   multiplane@0.3.8 (postinstall: node tools/install-browser.mjs)
```

**症状**：`multiplane` 命令能用、`--version` 正常，但一渲染就报找不到浏览器。

**解法**：显式放行一次 ——
`npm install -g --allow-scripts=multiplane multiplane`。

> ⚠️ 这是 npm 的**供应链保护**（默认不跑依赖的安装脚本），不是 bug。
> 放行前值得知道：这个脚本做的事就是**下载 Playwright 的 Chromium**。

### 2. Playwright 自带的 ffmpeg **不能用**

Playwright 缓存里有个 `ffmpeg-1011`，看着像"已经有 ffmpeg 了"，但它是**精简版**：

```
--disable-everything --enable-encoder=libvpx_vp8 --enable-muxer=webm ...
```

**只支持 VP8/WebM，没有 libx264** ⇒ Multiplane 要 H.264，用不了。

**解法**：另装完整版。本机没有 winget/scoop，改用**国内镜像直接下**：

```bash
# npmmirror（实测 28MB 秒下；比 GitHub Releases 稳）
curl -sL -o ffmpeg.exe.gz \
  "https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.1.1/ffmpeg-win32-x64.gz"
gunzip -f ffmpeg.exe.gz
multiplane config set ffmpeg "D:/DeepSeek/tools/ffmpeg/ffmpeg.exe"
```

> ★ `multiplane doctor` 会说 `ffmpeg_not_found` 并提示三种解法
> （`--ffmpeg` / `MULTIPLANE_FFMPEG` / PATH）。用 `config set` 存一次最省事。

### 3. shot 文件有**硬性契约**，不符合直接 lint 失败

第一版我写了个普通 HTML，报：

```
shot_missing:  Call Shot.define({...}) or assign window.SHOT.
stage_missing: Add an element with id="stage".
```

**正确形态**（照它 `examples/alpha.html`）：

```html
<link rel="stylesheet" href="/multiplane/runtime/shot.css">
<div id="stage">…</div>
<script src="/multiplane/runtime/shot.js"></script>
<script>
Shot.define({ duration: 1, fps: 24, render(t) { /* 只由 t 决定 */ } })
</script>
```

三个硬要求：**`id="stage"`**、**引 `shot.js`**、**调 `Shot.define`**。

> `/multiplane/runtime/...` 是**虚拟路径**，由它自己的 server 提供，不用你放文件。

### 4. 第一个参数是**文件**不是目录

```
multiplane render <shot.html> [options]      # ✅
multiplane render .                          # ❌ shot_not_found
```

### 5. `--from/--to` 是**秒**，且建议先 `--draft`

本机实测 1 秒 / 640×360 / 24fps / draft ≈ **33 秒**（4 workers）。
正式质量会明显更慢 —— 调样式时**一直用 `--draft`**，最后再渲一次正式的。

### 6. ★ 资源必须在**资源根之下**（`../` 会报 `resource_missing`）

实测：shot 里写 `src:url('../fonts/X.woff2')`，**文件明明存在**，却报：

```
resource_missing (error): A local resource is missing or outside the resource root.
```

**根因**：Multiplane 把 shot 所在目录当**资源根**，`../` 越出根就被拒
（这是**安全设计**，防 shot 引用任意本地文件）。

**解法**：把资源**拷进 shot 同目录（或其子目录）**，用 `./fonts/X.woff2`。

### 7. ★ 自定义字体必须**显式声明加载**

只写 `@font-face` 会得到警告（不致命，但字体可能没加载完就渲染）：

```
font_not_awaited (warning): Custom fonts have no explicit loading declaration.
hint: Add fonts to Shot.define or await document.fonts.load in setup.
```

**解法**：在 `Shot.define` 的 `fonts` 数组里列出**用到的每个组合**：

```js
Shot.define({
  duration: 2, fps: 24,
  fonts: [['64px Lanxi-HeiTiBold', '小肥鱼做视频'],   // [CSS font 简写, 要预载的文本]
          ['30px Lanxi-WenKai', '字体与设计规范来自']],
  render(t) { ... },
});
```

### 8. 排错手法：**逐特征二分**

`shot_load_failed` 这个错误**不带任何细节**（连 JSON details 都没有），
很难直接定位。有效做法：把 shot 拆成最小可用版本，**每次只加一个特征**
（中文字符 / @font-face / fonts 数组…），哪个变体挂了就是哪个特征的问题。
本项目就是这么在 4 个变体里定位到"字体路径越根"的。

## 本机验证（真跑通，不是"应该能跑"）

```bash
multiplane lint shot.html
# {"errors":0,"warnings":0,"suppressed":0}

multiplane render shot.html --out out.mp4 --from 0 --to 1 --draft --overwrite
# real 0m32.8s

ffmpeg -i out.mp4
# Duration: 00:00:01.00
# Stream #0:0: Video: h264 (High) (avc1), yuv420p, 640x360, 24 fps
```

✅ 产物是**真 H.264 MP4**（文件头 `ftypisom`）。

## 它能做什么 / 不做什么

| 能做 | 不做 |
|---|---|
| 把网页**逐帧渲染**成视频 | ❌ 不内置动画框架 |
| `lint` 查出**会破坏确定性**的写法 | ❌ 不做动画库适配 |
| `verify` 乱序/重载/多进程逐帧比对 | ❌ 没有教 agent 做动画的 skill |
| 每个命令输出**机器可读事件流** | |
| **托管渲染**：会话关了照样渲完，`multiplane wait` 接上 | |

> ★ 定位是"**只做渲染**"—— 动画怎么写是你（或 agent）的事。
> 配合 [`design/`](../design/) 里的设计规范与字体一起用。

## 常用命令

```bash
multiplane doctor                      # 体检（缺什么一目了然）
multiplane lint shot.html              # 渲染前查确定性
multiplane render shot.html --out v.mp4 --draft --from 0 --to 3
multiplane stills shot.html --out f.png --at 1.5   # 出单帧（调样式最快）
multiplane verify shot.html            # 逐帧比对（防不确定性）
multiplane skill install --host claude # 装成 Claude Code 的 skill（可选）
```
