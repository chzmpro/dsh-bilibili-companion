# 插件化施工方案（照着一步步做）

> **目标**：把现在"手动改 profile 配置文件"的接入方式，换成一个**能装的插件**
> （`dsh plugin --profile web add ...`），并在 DSH 的「设置」里多出一页「B站小号」，
> 让你能自己看状态、点按钮、改开关。
>
> **模仿对象**：[c1overzz/dsh-bili-scroll](https://github.com/c1overzz/dsh-bili-scroll)（见 [`competitor-review.md`](competitor-review.md)）。
> **不追求**功能对齐 —— 只做"骨架 + 设置页 + 跑一轮"这一条最小闭环，能跑通再往外加。
>
> **名词翻译**（怕看不懂的都在这里）：
> - **插件包** = 一个文件夹，里面放几个 `.js` 文件 + 一个 `package.json`，DSH 认识它。
> - **bundle** = DSH 启动时"把插件装进来"的那一步；改完必须**重启**才生效。
> - **slot（插槽）** = DSH 界面里预留的位置，比如「设置」左侧导航。插件往插槽里塞一页。
> - **overlay（临时配置）** = 只给"那一轮"用的额外配置，不改你平时的设置。
> - **家目录** = 插件放自己数据的地方：`%APPDATA%\dsh-desktop\harness\bili-companion\`。

---

## 0. 最终长这样

```
projects/dsh-bilibili-companion/
├── plugin/                     ← ★ 新增：插件包（就是"外壳"）
│   ├── package.json            声明"我是个 DSH 插件"
│   ├── cordis.patch.yml        把 Python MCP 挂进来（照抄现有片段）
│   ├── index.js                宿主端：配置 + 5 个按钮接口 + 定时器 + 起一轮
│   ├── client.js               设置页：开关、状态、按钮
│   ├── selfcheck.mjs           本地预检（不重启就能知道插件能不能加载）
│   └── lib/
│       ├── home.mjs            找家目录
│       ├── hostdeps.mjs        找 DSH 自带的小工具库（三级兜底）
│       ├── pyenv.mjs           建 Python 环境 + 装依赖
│       └── runner.mjs          起一轮（临时配置 + 启动子进程）
├── core/ bridge/ cli/           ← 不动（Python 内核已经是好的）
└── dsh/                         ← 旧的手改片段，插件跑通后退役
```

**关键取舍**：第一版**Python 代码不搬家**，插件通过配置指向项目里的 `.venv` 与 `bridge.server`
（就是现在片段里那套）。等外壳跑通了，再决定要不要把 Python 打包进去（那一步是为了"发给别人"）。

---

## 1. 十个步骤

每一步都写了 **做什么 / 关键代码 / 怎么算这步成了**。

### 步骤 1：建包骨架 + `package.json`

```jsonc
{
  "name": "dsh-bili-companion",          // ★ 这个名字要 client.js 里再写一遍，必须一致
  "version": "0.1.0",
  "type": "module",
  "main": "index.js",
  "exports": { ".": "./index.js", "./client": "./client.js" },
  "files": ["index.js", "client.js", "lib", "cordis.patch.yml"],
  "engines": { "dsh": ">=0.1.7-rc.1", "node": ">=22" },
  "peerDependencies": {
    "@deepseek-ai/schemastery": "*",
    "@deepseek-ai/dsh-tools": "*"
  },
  "peerDependenciesMeta": {                // ★ 必须标 optional，理由见步骤 4
    "@deepseek-ai/schemastery": { "optional": true },
    "@deepseek-ai/dsh-tools": { "optional": true }
  },
  "dependencies": { "@deepseek-ai/schemastery": "^3.18.1" },
  "dsh": {                                 // ★ 这三行决定"DSH 认不认这个插件"
    "bundle": { "patch": "./cordis.patch.yml" },
    "client": {
      "platform": "web",
      "inject": ["@deepseek-ai/dsh-client-runtime", "@deepseek-ai/dsh-client-ui-settings"]
    }
  }
}
```

**验证**：`node -e "const j=require('./package.json'); console.log(j.dsh)"` 能打印出 `bundle` 和 `client`。

### 步骤 2：`cordis.patch.yml`（把 Python 挂进来）

直接把现有的 `dsh/cordis.patch.fragment.yml` 里 **A 段**（`mcp-bili-companion`）搬过来，
只留 MCP 那条，**preset 段先不要**（后面用 overlay 方式做，见步骤 6）。
注意 `cwd` / `command` 用**绝对路径**。

**验证**：`dsh --profile web --dump-config | grep bili_companion` 能看到。

### 步骤 3：`lib/home.mjs`（找家目录）—— 抄这一条，能省一次"整插件不激活"

```js
import { join } from 'node:path'
import { mkdirSync } from 'node:fs'
import { homedir } from 'node:os'

export function stateDirOf(ctx) {
  let base = null
  try { const p = ctx.get?.('dshHomePath') } catch { /* 老版本没有 */ }
  const p = (() => { try { return ctx.get?.('dshHomePath') } catch { return null } })()
  // ★★ 坑：它可能是"函数"，不是字符串。当字符串用会把函数源码拼进路径 → 目录建不出来 → 整个插件不激活。
  if (typeof p === 'function') base = p('bili-companion')
  else if (typeof p === 'string' && p) base = join(p, 'bili-companion')
  else base = join(process.env.DSH_HOME || join(homedir(), '.dsh'), 'bili-companion')
  mkdirSync(base, { recursive: true })
  return base
}
```

**验证**：`node -e "import('./plugin/lib/home.mjs')"` 不报错；手动调一次能看到目录被建出来。

### 步骤 4：`lib/hostdeps.mjs`（拿 DSH 自带的小工具库）

照抄竞品 `lib/hostdeps.mjs` 的思路，顺序是：

1. 先从 **DSH 自己的目录**里找那两个库；
2. 找不到 → 用**我们包自己依赖里**那份（所以 `package.json` 的 `dependencies` 里留了 schemastery）；
3. 还找不到 → 用**本地自己写的替代**（只补一条校验），**插件照样加载，只是设置页降级**。

```js
export async function loadDefineTool(log) {
  for (const root of candidateRoots()) {
    try { return await import(pathToFileURL(entryOf(root, '@deepseek-ai/dsh-tools')).href) }
    catch { /* 换下一个 */ }
  }
  try { return await import('@deepseek-ai/dsh-tools') } catch { /* 继续降级 */ }
  log?.('用本地替代实现')
  return { defineTool: (o) => o }        // 最差情况：返回原样，功能不受影响
}
```

**为什么值得抄**：竞品在**桌面版**上就是死在这一步 —— 那两个库被标成"可选"，桌面版不装它们，
插件**整条不激活**（日志只有一句 `1 entry did not activate`）。桌面版就是你在用的这个。

**验证**：`node -e "import('./plugin/lib/hostdeps.mjs').then(m=>m.loadDefineTool(console.log))"`。

### 步骤 5：`lib/pyenv.mjs`（建 Python 环境 + 装依赖）

第一版**可以先不做**：直接在设置页让你填「项目目录」，插件用现成的 `.venv`。
真正要做时，抄竞品的 `lib/uvfetch.mjs`：**直连 GitHub → 国内加速代理 → 清华 PyPI**，三源轮着试，
下载下来的东西**必须核对官方 sha256**（对不上就换源，不降级）。

我们的依赖比它多，清单是：`mcp>=2,<3` `httpx>=0.27` `pydantic>=2` `pyyaml>=6` `typer>=0.12` `tzdata`。
⚠️ 两个本机已知坑（写进代码注释，别再踩）：`UV_CACHE_DIR` 要指到可写目录；`%LOCALAPPDATA%` 不可写。

**验证**：故意传一个不存在的项目目录，插件要给出**人话错误**，而不是崩。

### 步骤 6：`index.js` 宿主端（最核心的一个文件）

四块内容：

```js
import { defineTool } from './lib/hostdeps.mjs'
import { stateDirOf } from './lib/home.mjs'

// ① 配置（设置页读写的就是这些）
export const Config = z.object({
  projectDir: z.string().default('D:/DeepSeek/projects/dsh-bilibili-companion'),
  keywords: z.array(z.string()).default(['DSH插件', 'DeepSeek Harness']),
  dailyTop: z.natural().default(3),      // 每天最多挑几条
  valleyOnly: z.boolean().default(false),// 只在"便宜时段"跑
  autoComment: z.boolean().default(false),
  mentionName: z.string().default(''),
  reportLevel: z.string().default('full'),
}).extra?.('volatile', true) ?? undefined
// ★★ 每个字段都要 .extra('volatile', true)，否则保存时报 has no volatile fields

export function apply(ctx, config) {
  const cfg = normalize(config)              // ★ 抄竞品：逐字段白名单清洗，别信传进来的东西
  const home = stateDirOf(ctx)

  // ② 设置命名空间（名字必须等于插件在 patch 里的 id）
  ctx.settings.register('bili-companion', Config, { base: {}, applies: 'live' })

  // ③ HTTP 接口（设置页的按钮打的就是这些）
  //
  // ★★ 本机契约（从运行中的 dsh-our-free-model 挖出来的，很重要）：
  //     **不能在 apply 时直接读 webServer** —— 插件先加载、浏览器半身后发布，
  //     那时 ctx.get('webServer') 是 undefined，路由会永远注册不上，
  //     表现就是"设置页空着"。必须用 ctx.inject 等它就绪：
  ctx.inject(['webServer'], (scoped) => {
    scoped.effect(() => scoped.webServer.register({
      kind: 'prefix',
      path: '/api/bili-companion',
      handler: (req, res) => {
        //  GET  /status    → 跑没跑、环境就绪没、今天的日报在哪
        //  GET/POST /config → 读 / 改设置
        //  POST /run        → 现在跑一轮
        //  GET  /report     → 看今天的日报
      },
    }), 'bili-companion: api routes')
  })

  // ④ 定时器（每 60 秒看一眼该不该跑）
  const timer = setInterval(() => { tick().catch((e) => ctx.logger?.warn?.(String(e))) }, 60_000)
  timer.unref?.()
  ctx.effect(() => () => clearInterval(timer), 'bili-companion: 定时器')   // ★ 留个"收摊"动作
}
```

**两个必须抄的防守动作**：

```js
// ① 所有接口都包一层 try/catch —— 竞品的注释写着："2026-10-14 一个没接住的异常杀掉了整个宿主"
async function safe(fn) { try { return { ok: true, ...(await fn()) } } catch (e) { return { ok: false, error: String(e?.message ?? e) } } }

// ② 配置逐字段白名单清洗（它叫 normalize）：数组只收字符串、数字要过 Number.isFinite，其余丢弃
function normalize(src) { /* ... */ }
```

**怎么算这步成了**：重启 DSH 后，浏览器打开
`http://127.0.0.1:43129/api/bili-companion/status` 能看到一段 JSON。

### 步骤 7：`client.js` 设置页

**三条铁律**（抄竞品，都是它被用户当场抓出来的）：

```js
// ① 这个文件不是普通模块，是"自注册脚本"；id 必须等于 package.json 的 name
window.__ModuleLoader__.load({
  id: 'dsh-bili-companion',
  factory: (require) => {
    const React = require('react')
    // ② 进「设置」必须走插槽；★ 别自己糊标题栏按钮 + 全屏弹窗（竞品第一版就被一眼认出"跟别人不一样"）
    return {
      inject: ['slots'],
      apply(ctx) {
        const slots = (typeof ctx.get === 'function' ? ctx.get('slots') : undefined) || ctx.slots
        slots.inject('settings.section', () => slots.register(
          { name: 'settings.section', id: 'bili-companion', order: 30, label: 'B站小号' },
          () => React.createElement(Panel),
        ))
      },
    }
  },
})
```

**③ 输入框必须存"本地草稿"**：页面每 5 秒会去问一次状态，直接绑服务端的值会让
"你打字打到一半被冲掉"（竞品原话，是陌生人验收时抓到的）。

设置页先做 4 组就够：

| 组 | 内容 |
|---|---|
| 顶部徽章 | 今天已挑 N/上限 · 环境就绪没 · B站登录没 · 版本 | + 「**现在跑一轮**」按钮 |
| 什么时候跑 | 「只在便宜时段跑」开关 → 展开「中午 / 晚上」两个子开关 |
| 一轮挑几条 | 数字输入框（0 = 不自动跑）；关键词多行框 |
| 它会不会替你说话 | 「自动评论并 @ 你」开关（默认关）→ 展开「@ 谁」；「看今天的日报」按钮 |

**为什么"默认关"很重要**：会发东西出去的开关，竞品一律默认关，而且**总开关关着时子项不显示** ——
不给你"误开一半"的机会。我们照做。

### 步骤 8：`selfcheck.mjs`（不重启就知道行不行）

伪造一个假的 `ctx`（`get('dshHomePath')` 给个**函数**、`tools.register` 收集名字、
`settings/webServer/effect` 给空壳），真调一次 `apply()`。
家目录用系统临时目录，**跑完删掉，不碰真数据**。

**价值**：`failed to import` 这类"整插件不激活"的问题，能在重启前发现。

### 步骤 9：装 + 重启 + 验收

```bash
# 开发期用本地链接装（改完代码重启即生效）
dsh plugin --profile web add link:D:/DeepSeek/projects/dsh-bilibili-companion/plugin

# ⚠️ 必须真重启 DSH 桌面端（bundle 层要重启才组装）
```

然后逐条验收：
1. 设置里出现「**B站小号**」那一页；
2. 页面上徽章显示"环境就绪 / B站已登录"；
3. 点「现在跑一轮」→ 家目录里出现日报；
4. `http://127.0.0.1:43129/api/bili-companion/status` 有 JSON；
5. 平时对话里调 `mcp__bili_companion__companion_status` 仍正常（如果选了"平时也能用"）。

### 步骤 10：旧片段退役

插件跑通后，把 `profiles/web/cordis.patch.yml` 里手改的那段**删掉**（先备份），
只留插件自己的 `cordis.patch.yml`。这一步的意义：**以后升级不用再手改 profile**。

---

## 2. 必须照抄的 7 个坑（都是别人踩过的）

| # | 坑 | 症状 | 照抄的解法 |
|---|---|---|---|
| 1 | 桌面版不装"可选依赖" | 插件**整条不激活**，日志只一句 `did not activate` | 三级兜底：DSH 目录 → 自己依赖 → 本地替代 |
| 2 | 家目录 API 是**函数** | 目录建不出来 → 整插件不激活 | 函数/字符串两种都判断 |
| 3 | 沙箱只许写"启动前已存在"的目录 | 子进程写临时文件 `Permission denied` | 宿主**先建好**目录/文件，再启动子进程 |
| 4 | 设置字段没标 volatile | 保存时报 `has no volatile fields` | 每个字段都加 `.extra('volatile', true)` |
| 5 | 版本不满足 | **静默跳过**，装了跟没装一样 | `engines.dsh` 写清楚，设置页显示版本号 |
| 6 | 改完没重启 | 还是旧代码 | 设置页上写一行提示"装完要重启一次" |
| 7 | 临时文件重名 | 两轮同时跑互相覆盖 | 文件名带 BV 号/时间戳，不要固定名 |

---

## 3. 第一版**不做**的（免得一步太大）

- ❌ 把 Python 打包进插件（先指向项目目录）
- ❌ 扫码登录（先用现有的 `cli.app import-cookie`）
- ❌ 一键升级 / 检查更新
- ❌ 自动下载 uv（先要求机器上有 uv，我们这台已经有）
- ❌ 本地转写（那是另一个大件，单独排）

---

## 4. 需要你拍板的两件事（大白话）

### 问题一：B站工具要不要出现在你平时的对话里？

| 选项 | 意思 | 好处 | 代价 |
|---|---|---|---|
| **A. 平时也能用**（现在就是这样） | 你在任意对话里都能让我读B站 | 方便，随时能用 | 每个对话都会看到这些工具 |
| **B. 只有定时那一轮能用** | 平时对话干净，只有自动跑的时候才挂上 | 干净、安全 | 你想手动用得先切到专用对话 |
| **C. 两个都要，加个开关** | 默认平时不挂，你在设置页里勾一下才挂 | 灵活 | 多做一点点（约半天） |

> 我的建议：**C**。默认干净，你要用的时候在设置页打勾。

### 问题二：那些"记忆"要不要搬家？

现在"赞过谁、发过什么、看过哪些视频、用掉多少配额"存在**项目文件夹**里
（`D:\DeepSeek\projects\dsh-bilibili-companion\.local-data\`）。
按 DSH 的规范，插件的数据应该放在**它自己的家目录**（`...\harness\bili-companion\`）。

| 选项 | 意思 | 好处 | 代价 |
|---|---|---|---|
| **A. 先不搬** | 继续放项目文件夹 | 最省事、零风险 | 不太像"一个正经插件"；以后发别人用不了 |
| **B. 搬过去** | 写个搬家程序，第一次启动时把数据拷过去 | 规范、以后能发人 | 要做搬家 + 备份，有小风险 |

> 我的建议：**先 A 后 B**。第一版不搬（保证一次跑通），等插件能用了再搬。

---

**两个问题你只要回「问题一选 X，问题二选 Y」我就开工。**

---

## 5. 实操记录（2026-10-16 真机安装，含踩到的坑）

> 主人选了 **问题一 A（平时也能用）+ 问题二 A（先不搬）**，以下是实际执行过程。
> 这些是**真机验证过**的命令，下次直接用。

### 5.1 ★★ 调用官方 CLI 的正确方式（**必须带 `DSH_HOME`**）

```bash
EXE="/d/DSH/DSH-file/DSH Desktop/DSH Desktop.exe"
ENTRY="D:/DSH/DSH-file/DSH Desktop/resources/app.asar/node_modules/@deepseek-ai/dsh/lib/bin.js"
export DSH_HOME="C:/Users/yp-chenzhuoming/AppData/Roaming/dsh-desktop/harness"

# 查 profile 里的依赖（验证 CLI 指向的是"哪个" profile）
ELECTRON_RUN_AS_NODE=1 "$EXE" --expose-internals "$ENTRY" plugin --profile web list --depth 0

# 安装
ELECTRON_RUN_AS_NODE=1 "$EXE" --expose-internals "$ENTRY" plugin --profile web add "link:D:/DeepSeek/projects/dsh-bilibili-companion/plugin"

# 组合自检
ELECTRON_RUN_AS_NODE=1 "$EXE" --expose-internals "$ENTRY" --profile web --dump-config | grep -A4 "dsh-bili-companion"
```

**用法要点**：`dsh plugin --profile <name> <pnpm-args...>` —— `plugin` 在前面，
后面直接接 pnpm 的子命令（`add` / `remove` / `list` / `install`）。

> ⚠️ **不传 `DSH_HOME` 会装错地方**：CLI 会退回默认的 `~/.dsh`，
> pnpm 会**报告安装成功**，但真实 profile 一个字节都没变
> （真实桌面端的 `DSH_HOME` 是 `%APPDATA%\dsh-desktop\harness`）。
> 判断方法：`plugin --profile web list` 输出的路径对不对。

> ✅ **官方 CLI 会自己保护红线字段**：安装时它会打印
> `excluded 7 generation projection(s) from pnpm` … `restored 7 ... after pnpm` ——
> 临时把 `generationProjection` 挪开、跑完再放回。**所以必须走官方命令，别手改 package.json。**

### 5.2 安装后 `package.json` 的正确变化（就这两处）

```diff
   "dependencies": {
+    "dsh-bili-companion": "link:D:/DeepSeek/projects/dsh-bilibili-companion/plugin",
   "dsh": { "profile": { "bundles": [
+        "dsh-bili-companion"
```

`node_modules/dsh-bili-companion` → 指向 `plugin/` 的软链（自动建）。
**`generationProjection` 与 `pnpm.overrides` 不动**（动了就是事故）。

### 5.3 ★ 必须同时退役旧的手改片段

我们早先把 MCP 挂在 `profile/cordis.patch.yml` 里（`dsh/cordis.patch.fragment.yml` 那段）。
插件自带 MCP 之后，**同一个 `serverName` 出现两次 → "加载即失败"**。

处理：把该片段里的 `mcp-bili-companion` 那一块**删掉**（保留它下面的 `preset-bili-companion`，
工具收窄仍然有用）。验证：

```bash
grep -c 'id: mcp-bili-companion' dump.txt      # 期望 1（只剩插件带来的那份）
grep -c 'id: preset-bili-companion' dump.txt   # 期望 1
```

### 5.4 实测结果（真机）

| 项 | 结果 |
|---|---|
| `plugin add` | ✅ `+ dsh-bili-companion link:...`，红线字段装前装后一致 |
| `node_modules` 软链 | ✅ 建好 |
| `--dump-config` | ✅ **exit 0**，无新增报错 |
| 插件行 | ✅ `- id: bili-companion` / `name: dsh-bili-companion`，带 `__dshPluginOwner` 认领信息 |
| MCP 行 | ✅ `- id: mcp-bili-companion`，**恰好 1 次** |
| `preset-bili-companion` | ✅ 保留 |
| 唯一 stderr | `patch: entry "ui-skin-maid-atelier" not found` —— **装之前就有**，与本次无关 |

### 5.5 ⚠️ 还没做的一步：真实重启

`--dump-config` 只验证"能组合"，**不验证"能重启"**（`$DSH_HOME/AGENTS.md` 的硬要求，
也是上次出事故的诱因）。重启由主人做（重启会中断当前会话）。

**重启后的验收清单**：

| # | 验收 | 期望 |
|---|---|---|
| 1 | 设置里是否多出一页 | 「设置 → **B站小号**」 |
| 2 | 页面顶部徽章 | 「环境已就绪」+「今天还没跑/已有日报」 |
| 3 | 直接打接口 | `http://127.0.0.1:43129/api/bili-companion/status` 返回 JSON |
| 4 | MCP 工具仍在 | 新会话里 `mcp__bili_companion__companion_status` 能调 |
| 5 | 点「现在跑一轮」 | 家目录 `run.log` 有输出；约 3 分钟后日报出现在 `out/` 等价目录 |

### 5.6 回滚（1 分钟）

```bash
P="/c/Users/yp-chenzhuoming/AppData/Roaming/dsh-desktop/harness/profiles/web"
export DSH_HOME="C:/Users/yp-chenzhuoming/AppData/Roaming/dsh-desktop/harness"
EXE="/d/DSH/DSH-file/DSH Desktop/DSH Desktop.exe"
ENTRY="D:/DSH/DSH-file/DSH Desktop/resources/app.asar/node_modules/@deepseek-ai/dsh/lib/bin.js"

# ① 卸插件（官方命令，会同步删掉 bundles 里的那一行）
ELECTRON_RUN_AS_NODE=1 "$EXE" --expose-internals "$ENTRY" plugin --profile web remove dsh-bili-companion

# ② 还原手改片段（把它加回来，MCP 就又由旧片段提供了）
cp "$P/cordis.patch.yml.bak-before-bili-companion" "$P/cordis.patch.yml"

# ③ 重启 DSH
```

`plugin/package.json.bak-before-bili-companion` 也在同一目录，可作二次保险。

### 5.7 ⚠️ 事故记录（2026-10-16）：`link:` 装法让 DSH 进了安全模式

**现象**（`harness.log` 第 2903–2918 行）：

```
[renderer] Uncaught Error: client-modules: duplicate factory registration for "bili-companion"
           (bundle executed twice without invalidate?)
[renderer] Error: client-modules: dsh-bili-companion import failed
[desktop]  launch requested (safe mode)
[desktop]  safe mode: third-party web profile bundles are blocked
```

**根因**：用 `link:` 装 ⇒ `node_modules/dsh-bili-companion` 是**软链**。
软链让浏览器端 bundle 的**模块身份不唯一**（软链路径与真实路径被当成两个模块），
于是同一个 id 注册了两次 ⇒ DSH 判为致命错误 ⇒ 进安全模式。

> ⚠️ 这与 `$DSH_HOME/AGENTS.md` 早就写明的规则属同一类：
> **symlink 形态必须有 `generationProjection` + `pnpm.overrides`；真实目录形态必须两者都无。**
> 本次失误在于：只执行了"用官方命令"那一半，**漏了形态校验**；
> 而且 `--dump-config` 通过就宣布安全 —— 它**不检查形态**。

**恢复过程（已完成）**：

| 步骤 | 结果 |
|---|---|
| DSH 启动时自己清掉了不一致的插件 | `dependencies`/`bundles`/`node_modules`/投影里**全无**该包 |
| `cordis.patch.yml` 用备份还原 | `diff` 确认**完全一致** |
| 形态体检 | `$DSH_HOME\tools\check-plugin-form.mjs`（**全局工具，不在本项目里**）→ 12 个插件 **0 处错位** |
| 确认已退出安全模式 | 日志 `dsh web: http://127.0.0.1:43129` + `Harness is ready` |

### 5.8 ★ 正确的本地插件装法（下次照这个）

**不要用 `link:`。** 要装成**真实目录**，落进"直接形态"（无投影元数据 = 一致）：

```bash
cd D:/DeepSeek/projects/dsh-bilibili-companion/plugin
npm pack                                   # 生成 dsh-bili-companion-0.1.0.tgz

export DSH_HOME="C:/Users/yp-chenzhuoming/AppData/Roaming/dsh-desktop/harness"
EXE="/d/DSH/DSH-file/DSH Desktop/DSH Desktop.exe"
ENTRY="D:/DSH/DSH-file/DSH Desktop/resources/app.asar/node_modules/@deepseek-ai/dsh/lib/bin.js"

# tgz 会被 pnpm 解包成真实目录（不是软链）
ELECTRON_RUN_AS_NODE=1 "$EXE" --expose-internals "$ENTRY" \
  plugin --profile web add "file:D:/DeepSeek/projects/dsh-bilibili-companion/plugin/dsh-bili-companion-0.1.0.tgz"

# ★★ 装完必跑 —— **在重启之前**（脚本是全局工具，不在本项目里）
node "%APPDATA%\dsh-desktop\harness\tools\check-plugin-form.mjs"   # 期望：全部一致，exit 0
```

**顺序铁律**：`装 → 体检 → dump-config → 重启`。**体检不过就别重启**（重启就进安全模式）。

> 补充说明：`link:` 在开发期看起来最方便（改代码即时生效），
> 但代价是**形态与元数据不一致**。想让它合法，就得在 `generationProjection` 和
> `pnpm.overrides` 里都补条目 —— 而这两处是**绝不许手改**的红线。
> 所以结论只有一条：**老老实实打包安装**。

#### ✅ 2026-10-16 22:01 实测：tgz 装法通过

按上面的顺序重做了一遍，**每一步都留了证据**：

| 步骤 | 命令 | 结果 |
|---|---|---|
| ① 备份 | `cp package.json{,.bak-<时间戳>}` + 同上 `cordis.patch.yml` | ✅ |
| ② 打包 | `npm pack` | ✅ `dsh-bili-companion-0.1.0.tgz`（10.8 KB） |
| ③ 安装 | `plugin --profile web add file:<tgz>` | ✅ `+ dsh-bili-companion`，投影"装前装后一致" |
| ④ **形态体检** | `node $DSH_HOME/tools/check-plugin-form.mjs` | ✅ **exit 0**；`dsh-bili-companion` = **真实目录 / 无元数据 / ✅** |
| ⑤ 退役旧 MCP | 删 `cordis.patch.yml` 里 `mcp-bili-companion` 段（保留 preset） | ✅ |
| ⑥ 组合自检 | `--dump-config` | ✅ **exit 0**；`mcp-bili-companion` **恰好 1 次**、插件 1 次、preset 保留 |

**与失败那次的关键差别**（一眼可辨）：

```bash
ls -ld node_modules/dsh-bili-companion
# 失败那次：lrwxrwxrwx ... -> /d/DeepSeek/.../plugin        （软链 → 注册两次 → 安全模式）
# 这次：    drwxr-xr-x ... node_modules/dsh-bili-companion   （真实目录 ✅）
```

> ⚠️ **开发流程变化**：tgz 装法下，**改完项目代码要重新 `npm pack` + 重装**才生效
> （不再像 `link:` 那样即时）。重新安装前同样要跑体检。

---

## 6. 第一版验收记录（2026-10-16 完成）

### 6.1 五条验收：**全部通过**

| # | 验收 | 结果 |
|---|---|---|
| 1 | 设置里出现「B站小号」 | ✅ 主人确认可见 |
| 2 | 面板徽章/内容 | ✅ 重启后不再卡「读设置中」 |
| 3 | `/api/bili-companion/status` | ✅ HTTP 200，含 `config` |
| 4 | MCP 工具 | ✅ `mcp__bili_companion__companion_status` 正常 |
| 5 | 「现在跑一轮」 | ✅ 真起子进程并写出日报（`run_62550a909bff44f4`） |

### 6.2 ★ 途中修掉的两个**只有真机才会暴露**的 bug

| # | bug | 现象 | 根因 | 修法 |
|---|---|---|---|---|
| 1 | **MCP 行放进插件 patch** | `client-modules: duplicate factory registration` → **安全模式（两次）** | `client.js` 绑定在**该包的每一行**上（按 `__dshPluginOwner.packageName` 归属）；两行 → 挂载两次 | 插件 patch 只留自己一行；MCP 回归 profile |
| 2 | **`/status` 漏 `config` 字段** | 设置页**永久停在「读设置中」**，控制台**零报错** | 前端 `if (!cfg)` 判断，后端没返回该字段 | 补字段 + `selfcheck` 加 **17 项接口契约断言** |

> 第 2 个特别值得记：**接口 200、DOM 也渲染了、控制台干净** ——
> 看起来像"加载慢"，实际是两半之间的**字段契约**断了。

### 6.3 装法定型（每次都要走这四步）

```
① npm pack → plugin add file:<tgz>     （真实目录形态）
② node $DSH_HOME/tools/check-plugin-form.mjs            （形态一致性）
③ node $DSH_HOME/tools/check-plugin-form.mjs --dump …   （行归属唯一）
④ 真实重启
```

**改 host（`index.js`）必须重启**；只改 `client.js` 内容可走 HMR。

---

## 7. 第二轮：P0 谷时调度（2026-10-16 完成）

来自 [`competitor-review.md`](competitor-review.md) §2.1 的省钱项，**已落地并接进设置页**。

### 7.1 实现方式：按**官方口径反推**，不维护时段表

竞品把窗口写死成两段（`noon 12:00–14:00` / `night 18:00–09:00`）。
我们直接按官方口径反推 —— **工作日 9–12 / 14–18 是高峰，其余（含整个周末）都是谷时**：

```python
PEAK_WINDOWS = (((9, 0), (12, 0)), ((14, 0), (18, 0)))

def in_valley_window(current):        # 周末直接 True
def next_valley_start(current):       # 按分钟向前找，最多 24h
```

好处：**不用维护时段表**，也不用处理竞品那个"跨零点"特例；官方调价只改两个常量。

### 7.2 接线（三层都改了）

| 层 | 改动 |
|---|---|
| `core/schedule.py` | `in_valley_window()` / `next_valley_start()`；`evaluate_due(peak_valley=…)` |
| `core/config.py` | `schedule.peak_valley`（**默认 false**） |
| `bridge/server.py` | `companion_status.schedule` 多返回 `peak_valley` |
| `cli/app.py` | `schedule` 命令同步 |
| `config.yaml` / `config.example.yaml` | 新增该字段（含注释） |

**语义**：到期但处在峰时 → `due=false`，理由里给出**下一个谷时起点**。
**默认关** = 不改变主人"打开 DSH 就干活"的原有设定。

### 7.3 测试

`tests/test_schedule.py` 新增 **13 项**：工作日两段高峰、高峰前后的谷时、周末全天谷时、
`next_valley_start` 三个起点（峰中→12:00 / 15:00→18:00 / 已在谷时→自身）、
闸门关着照跑 / 开着被挡 / 谷时放行 / 不覆盖"今天已完成"、默认值必须是 false。

### 7.4 ★ 设置页开关现在是**真的**（含安全边界）

新增 [`plugin/lib/pyconfig.mjs`](../plugin/lib/pyconfig.mjs)：让设置页能改 `config.yaml`，
但**只碰白名单字段**：

```js
EDITABLE_KEYS = { 'schedule.peak_valley', 'discovery.keywords',
                  'discovery.published_within_days', 'interaction.max_*' }
```

**为什么需要它，以及它的边界**：

- 用**逐行替换**而不是"YAML 解析再写回" → **注释一行不丢**
  （我们 `config.yaml` 几乎每行都有注释，解释"为什么是这个值"）；
- ★ **授权字段（`mode` / `dry_run` / `publish_enabled`）一字不动** ——
  那是"改授权"，只能由人在终端改。这条与项目既有的「人用入口不进 MCP」是同一个原则；
- 非白名单字段**直接被拒绝**（不是静默忽略）。

**实测验证**（`plugin/scripts/try-pyconfig.mjs`，跑完删）：

| 检查 | 结果 |
|---|---|
| 授权行改前/改后 | ✅ **JSON 完全相同**（一字未动） |
| 注释行数 | ✅ 22 / 22，未丢 |
| 非白名单字段 | ✅ `written: []`，`interaction.mode` 仍是 `observe` |
| 块状列表（`keywords`） | ✅ 读写正确（3 项 → 2 项 → 复原） |

**双向同步**：`GET /status` 从 `config.yaml` **读回**权威值（不是插件缓存的），
所以不会出现"界面显示开着、实际没生效"。

> ⚠️ `selfcheck.mjs` 会真改 `config.yaml` —— 所以它在**沙箱里造一个假项目目录**，
> **绝不碰主人的真配置**（已实测：跑完无 `.bak-plugin`，真配置 `peak_valley` 仍 `false`）。

---

## 8. 第三轮：P1 + P2 全部完成（2026-10-16 深夜）

按 [`competitor-review.md`](competitor-review.md) §4 的优先级表**逐项做完**，
每一项都**照竞品源码移植**（不自创），并配了测试。

### 8.1 完成清单

| 优先级 | 事项 | 文件 | 测试 |
|---|---|---|---|
| **P1** | **占位日报** | `core/report.py` 的 `write_stub()` / `log_tail()` | `tests/test_stub_digest.py` **10 项** |
| **P1** | **评论可见性复核** | `core/verify.py`（619 行） | `tests/test_verify.py` **33 项** |
| **P1** | **本地 ASR 兜底** | `core/asr.py`（810 行）+ `scripts/asr_transcribe.py` | `tests/test_asr.py` **42 项** + `tests/test_asr_pipeline.py` **8 项** |
| **P2** | **扫码登录** | `core/login.py` + `cli.app login-qr` | 真机验证（见下） |
| **P2** | **判重表导出** | `cli.app seen-export` | 真机验证 |
| **P2** | **uv 自举** | `core/uvfetch.py` | `tests/test_uvfetch.py` **17 项** |

**外加接线**：`companion_status` 新增 `asr` / `comment_verifications` 字段；
`run_once.py` 跑完自动追加复核结果；`pipeline.read_video` 在字幕缺失时按开关走 ASR。

### 8.2 ★ 从竞品照抄的**实测细节**（这些是省下来的摸索）

| # | 细节 | 出处 |
|---|---|---|
| 1 | **凭证在轮询的 `Set-Cookie` 里，不在 `data.url` 里**（那个是别的域的游戏票）⇒ 必须 `redirect: manual` | `lib/login.mjs` |
| 2 | 扫码状态码：`86101` 未扫 / `86090` 已扫待确认 / **`0` 成功** / `86038` 过期 | 同上 |
| 3 | **查不到只写「没查到」**，绝不写「没发出去」（翻页/热度排序都会漏） | `lib/verify.mjs` |
| 4 | **`/x/v2/reply/detail` 走不通（-400）** —— 别无谓地换它 | 同上 |
| 5 | 顶层用 `mode=2`（**按时间**）；楼中楼按 `root` 直拉 | 同上 |
| 6 | 已确认可见的 **7 天内不重复查**（账本 TTL） | 同上 |
| 7 | **空转写 = 没有语音**（不是故障）⇒ 记「跳过（无语音）」，**不重试** | `lib/asr.py` + duty.md |
| 8 | HF 三件套必须**在 `import faster_whisper` 之前**：`HF_ENDPOINT` / `HF_HOME` / **`HF_HUB_DISABLE_XET=1`**（Xet 国内镜像不代理 ⇒ 401） | `lib/asr.py` |
| 9 | yt-dlp **沙箱三连**：`paths`（别写 `%TEMP%`）/ `nopart`（新建+改名会被拒）/ `cachedir` | 同上 |
| 10 | 临时音频**用 BV 号命名**（写死 `audio.m4a` 会让两轮互相覆盖） | 同上 |
| 11 | **PyPI 的 `urls` 顺序不保证最新在前** ⇒ 必须自己按版本号排序 | `lib/uvfetch.mjs` |
| 12 | **"下载下来是要被执行的"** ⇒ sha256 对不上**只换源，绝不降级** | 同上 |

### 8.3 我们的**差异决定**（比竞品严的地方）

| 项 | 竞品 | 我们 |
|---|---|---|
| 占位日报的日志尾 | 直接写进报告 | **必须过 `core.redact.redact()`** —— 凭据不进日报 |
| 复核措辞 | 「（不等于没发出去）」 | 「（**观测不到 ≠ 不存在**）」—— 连那个说法都不写，避免误读 |
| `mid` 兜底匹配 | 命中即可能算可见 | **只用于定位、不用于核销**（否则会误核销另一条评论） |
| ASR 依赖 | 装完 ~1 GB | **默认关**；缺依赖只记 `ASR_NO_DEPENDENCY`，不让读取失败 |
| ASR 执行方式 | 线程 | **子进程**（线程杀不掉，超时形同虚设） |
| `HF_HOME` | 插件脚本目录 | `paths.data_dir()/.hf-cache`（数据目录是**探测**出来的） |
| 界面改配置 | 改自己的 config.json | 改 `config.yaml`，但**只碰白名单**；授权字段一字不动 |

### 8.4 自检结果（2026-10-17 00:18）

| 项 | 结果 |
|---|---|
| `pytest tests/` | ✅ **622 passed** |
| `ruff check core bridge cli tests scripts/` | ✅ All checks passed |
| `mypy`（strict） | ✅ 34 source files clean |
| 插件 `selfcheck.mjs` | ✅ 全部通过（含接口契约 + 谷时双向同步 + 真配置保护） |
| 三道体检（孤儿行/形态/行归属） | ✅ 全过 |
| MCP 工具（真实 stdio） | ✅ 人用入口未出现在工具列表里 |
| **真机接口**（`companion_status`） | ✅ `asr` / `comment_verifications` / `schedule.peak_valley` 三个新字段正确 |
| 安全模式复发 | ✅ 无新增（最后一次仍是 14:27Z 的历史记录） |
| 真配置授权字段 | ✅ `observe` / `dry_run=true` / `publish_enabled=false` 未动 |

### 8.5 ⚠️ 未验收项（**如实标注**）

| 项 | 为什么没验 |
|---|---|
| **真实 ASR 转写** | 需 ~1 GB 依赖（`faster-whisper` + `yt-dlp` + Whisper `small` 538 MB）。**本机未装、一次都没联网下模型**。已验证的只有逻辑/参数/错误码/子进程管道（42+8 项测试，含**变异验证**：把"空输出"改判成失败 → 3 个测试立刻挂） |
| **真实扫码登录** | 已验到"**能生成二维码**"（拿到真实 URL）；**未真的用手机扫**（需要主人操作） |
| **真实评论复核** | 已验到"如实报告还没有已确认发出的评论"（`observe` 模式下这是常态）；**未对真实评论跑过**（我们没有 `sent_verified` 的评论） |
| **uv 真实下载** | 逻辑与 sha256 校验已测（17 项）；**未真跑一次下载**（本机已有 uv） |

> 建议：想验 ASR 时先手工预热一次 ——
> `pip install faster-whisper yt-dlp` 然后 `scripts/asr_transcribe.py BV1xx411c7mD`。

---

## 9. 第四轮：真实 ASR 验证 + 镜像源重大修正（2026-10-17 凌晨）

主人授权下载依赖后，做了一次**真实验证**。结论有一个**重要修正**。

### 9.1 ★★ 镜像源修正：`hf-mirror.com` → **ModelScope**

**实测发现竞品用的 `hf-mirror.com` 在本机不可用**：

| 现象 | 证据 |
|---|---|
| 下到 **32.6 MB / 75 MB 后彻底停住** | `*.incomplete` 文件 **10 分钟不再增长**（不是慢） |
| **网络层已断** | `Get-NetTCPConnection` 查不到该进程的**任何 TCP 连接** |
| 两次尝试都一样 | 分别卡在 12.6 MB、32.6 MB |

**三源实测对比**（同一个 `model.bin`，各给 12 秒）：

| 源 | 结果 |
|---|---|
| `hf-mirror.com` | ❌ 超时 / 中途断流 |
| `huggingface.co` | ❌ 不通（本机已知） |
| **`modelscope.cn`** | ✅ **75.5 MB，8.9 MB/s** |

**最终实测**（`scripts/fetch_model_modelscope.py --model tiny`）：

```
✅ model.bin: 72.0 MB（5.9s，12.2 MB/s）
✅ tokenizer.json: 2.1 MB
✅ config.json / vocabulary.txt
合计 74.6 MB，耗时 8.8s
```

**快了约 100 倍**（8.8 秒 vs 10 分钟还没下完）。

**改动**：
- `core/asr.py` 的 `HF_ENDPOINT_MIRROR` 默认改为 **`https://modelscope.cn/models`**
  （`hf-mirror` 保留为 `HF_ENDPOINT_FALLBACK` 记录）；
- 新增 [`scripts/fetch_model_modelscope.py`](../scripts/fetch_model_modelscope.py) ——
  因为 ModelScope **只实现 `/resolve/main/` 文件路径，没有 `/api/models/...` 元数据端点**，
  所以 `snapshot_download` 会 JSON 解析失败 ⇒ 改成**自己按文件列表下，再摆成 HF 缓存的标准结构**。

> ⚠️ 这是**竞品没有踩到、但我们踩到了**的坑：
> 竞品把 `hf-mirror.com` 写死，它没遇到过"中途断流"。
> 我们**实测**后换源，并保留了备选记录。

### 9.2 ★ 另一个本机限制：**不支持 symlink 缓存**

`huggingface_hub` 默认用 symlink 组织缓存，而**本机 Windows 未开开发者模式**：

```
UserWarning: huggingface_hub cache-system uses symlinks by default ...
your machine does not support them ... Caching files will still work
but in a degraded version that might require more space
```

**处理**（不是简单静音）：
- 新增 `core.asr.supports_symlink_cache()` —— **真建一个软链探测**，实测本机返回 `False`；
- 把它如实报进 `companion_status.asr.symlink_cache`，**让人知道这台机器会多占一份磁盘**；
- 静音那段会淹掉日志的长警告（`HF_HUB_DISABLE_SYMLINKS_WARNING=1`），
  但**事实不被掩盖**（由 status 字段如实报出）。

### 9.3 真实链路验证结果（逐步）

| 环节 | 结果 |
|---|---|
| 装依赖（清华镜像） | ✅ `faster-whisper==1.2.1` + `yt-dlp==2026.8.19` |
| `is_available()` | ✅ `(True, 'OK')` |
| **yt-dlp 抓 B 站音频** | ✅ **实测成功**（34.6 MB `BV1xx411c7mD.m4a`） |
| **模型下载** | ✅ **8.8 秒 / 74.6 MB**（ModelScope） |
| 模型加载 | ✅ 不再重复下载（读本地缓存） |
| **CPU 转写** | ⏳ 进行中（tiny 模型，音频较长，需数分钟） |

### 9.4 踩到的两个"真机才有"的坑

| # | 坑 | 现象 | 处理 |
|---|---|---|---|
| 1 | **`HTTP 416 Range Not Satisfiable`** | yt-dlp 断点续传撞上残留 `.part` 状态 | 清空 `tmp/asr/` 后正常。**错误被如实报成 `DOWNLOAD_FAILED`**（没假装成功） |
| 2 | **孤儿进程反复下载** | 我用 `Start-Process` 起的转写进程脱离 shell，60 秒超时**杀不到**它，一直后台重下模型并**锁住 `.incomplete` 文件**（`Device or resource busy`） | 排查出 4 个残留 PID 并清理。**教训：长任务要记 PID，结束时核对** |

### 9.5 ⚠️ 仍未验收

| 项 | 状态 |
|---|---|
| 真实扫码登录 | 仍未真的用手机扫（需主人操作） |
| 真实评论复核 | 仍无 `sent_verified` 记录可复核 |
| uv 真实下载 | 仍未真跑（本机已有 uv） |

---

## 10. ★★ 真实 ASR 转写：**跑通了**（2026-10-17）

### 10.1 端到端实测结果

```bash
./.venv/Scripts/python.exe scripts/asr_transcribe.py BV1t3JX65Exc --model tiny
# real 0m51.9s
```

**转写输出**（节选，真实内容）：

> 游戏素材大师0.7.0来了 生涂厚端可以换成自己的服务 设置里多了一个生涂
> …支持三种协议 OpenAI金融接口 ConfigUI还有Stable的Fusion WebUI…
> 地图编辑器转正 不再是实验性功能…新增标记层…0.7.0已经在MPM上…

**与真实字幕要点对照**（我们早先从字幕读到过同一期）：

| 转写里的信息 | 字幕里的要点 | 一致？ |
|---|---|---|
| 游戏素材大师 0.7.0 来了 | DSH插件游戏素材大师0.7.0版本已发布 | ✅ |
| 生图后端可换成自己的服务 | 该版本生图后端可以换成用户自己的服务 | ✅ |
| 支持三种协议 OpenAI / ComfyUI / SD WebUI | 支持三种协议（OpenAI兼容/ComfyUI/SD WebUI） | ✅ |
| 地图编辑器转正、新增标记层 | 地图编辑器已转正、新增标记层 | ✅ |
| 0.7.0 已经在 NPM 上 | 0.7.0版本已经在NPM上 | ✅ |

> **内容正确**，只有同音错别字（`生涂`=生图、`金融`=兼容、`ConfigUI`=ComfyUI）——
> 这是 `tiny` 模型的正常表现。**正式用建议 `small`**。

**至此"真实 ASR 转写"从未验收 → ✅ 已验证。**

### 10.2 ★★ 踩到并修掉的两个**依赖版本坑**（竞品没记）

`faster-whisper` 的依赖声明**过宽**，裸装最新版**直接跑不起来**：

| 包 | 装到 | 症状 |
|---|---|---|
| **`av`** | **19.0.1** | `open() got an unexpected keyword argument 'metadata_errors'` —— `faster_whisper/audio.py:46` 仍在传它，**新版 PyAV 已移除** ⇒ 转写**必失败** |
| **`huggingface_hub`** | **2.2.0** | 同类问题（`metadata_errors` 被移除） |

**报错完全看不出是版本问题** —— 这是最坑的地方（我第一次就误判成 HF 缓存没命中）。

**实测跑通的组合**（已写进 `core/asr.py` 的 `ASR_REQUIREMENTS`）：

```
faster-whisper==1.2.1
av==15.1.0          （av<16）
huggingface-hub==1.33.0  （huggingface_hub<2）
yt-dlp==2026.8.19
```

**代码改动**：新增 `check_dependency_versions()`，把版本不兼容**提前报进
`companion_status.asr`**（新原因码 `BAD_VERSION`），而不是等转写时才炸。

### 10.3 另一类坑：**缓存目录布局**

`huggingface_hub` 的缓存在 **`HF_HOME/hub/models--<org>--<name>/`** 下，
且 snapshot 目录名是**真实 commit hash**（`d90ca5fe…`），不是 `main`。

我第一版脚本把模型下到了 `HF_HOME/models--…/snapshots/modelscope/`
（**少了 `hub/` 层、commit 名也不对**）⇒ HF 库不认，**自己又重下一份**，
于是出现"两边各下一份、互不认"，表现为"模型明明下了却还在联网"。

**修法**：`scripts/fetch_model_modelscope.py` 按 HF 标准布局落盘；
验证方式是 **`HF_HUB_OFFLINE=1` 下能加载**（实测 2.5 秒成功）。

### 10.4 本轮修的第三个真 bug：**HTTP 416**

见 §9.4 第 1 条。已修（下载前清残留 + 416 时清干净重试一次），
并补了 **3 条回归测试**（含"非 416 不重试"，避免把确定性失败变成反复请求）。

### 10.5 最终自检（2026-10-17）

| 项 | 结果 |
|---|---|
| `pytest tests/` | ✅ **629 passed, 1 skipped** |
| `ruff check` | ✅ All checks passed |
| `mypy`（strict） | ✅ 34 source files clean |
| **真实转写** | ✅ **跑通**（51.9s，内容与字幕要点吻合） |
| yt-dlp 抓 B 站 | ✅ 实测（1.53 MB 音频） |
| 模型下载 | ✅ ModelScope **6 秒 / 75.5 MB** |
| 离线加载 | ✅ `HF_HUB_OFFLINE=1` 2.5s 成功 |

---

## 11. 第五轮：两种搜索方式 + 多平台预留框架（2026-10-17）

主人定了两件事：

> 「搜索方式按照你的来」
> 「抖音和小红书平台的不用做刷推荐流且走正规渠道，但是这两个你先预留框架，
> 待 B 站基本完成后再对它们进行完善」

### 11.1 搜索方式 ①：模糊意图 → 关键词

新增 [`core/intent.py`](../core/intent.py) + MCP 工具 **`expand_intent`**。

**做法**：把主人的一句模糊意图交给豆包展开成一组关键词，再走**原有搜索链路**。

**真机实测**（2026-10-17，豆包**真的调用了**）：

```
输入：想看看有没有人做 DSH 的记忆插件
输出：['DSH记忆插件', 'DSH学习插件', 'DSH记忆工具', 'DSH备考辅助工具']
     used_worker: True  error_code: None
```

**三条设计原则**：

1. **意图展开是"建议"不是"授权"** —— 只产出关键词，不碰发布模式/配额/UID；
2. **可控性优先于"聪明"** —— 模型输出要过**确定性清洗**
   （非字符串/超长/重复/含换行 一律丢），**不信任它的形状**；
3. **失败必须能降级** —— 豆包不可用时**退回"意图本身当关键词"**，
   还拆出 `DSH` 这类有效片段，**不让整轮停摆**。

### 11.2 搜索方式 ②：推荐流**只当词库扩展器**

新增 [`core/trends.py`](../core/trends.py) + MCP 工具 **`trends_suggest`**。

**为什么必须克制**（实测印证）：拉了一屏 8 条真实推荐 ——

| 推荐 | 与我们相关？ |
|---|---|
| DSH插件下载榜Top10 | ✅ |
| 什么？deepseek v4.1，kimi k3免费了 | ✅ |
| 让百万人尖叫的鬼屋 | ❌ |
| 100%神匹配度！当逐月节遇上《琵琶曲》 | ❌ |
| 越贵的AI越菜？ | ⚠️ 勉强 |

**⇒ 推荐流是算法喂的**。若让它直接决定"看什么、评什么"，
**小号的兴趣画像就被算法带跑了**，与项目"按主人关键词、只看 48 小时"的可控姿态相反。

**所以设计成**：

```
推荐流（只读、限额 8 条）→ 提取候选词 → 候选池（**不自动生效**）
                                        ↓
                        人用 CLI 过一眼 → --accept 才写进 config.yaml
```

**真机实测**：

```
fetched: 8 | new_in_pool: 8
  DeepSeek   hits=4    ← 来自 BV1VLJQ6XEZi
  Harness    hits=3
  DSH        hits=3
  桌面端      hits=2
```

**两个技术要点**（都实测踩过）：

| # | 问题 | 解法 |
|---|---|---|
| 1 | **中文没空格**，整段当词会得到"让百万人尖叫的鬼屋"（没法搜） | 中文切 **2~4 字 n-gram** |
| 2 | n-gram 产出跨词边界碎片（"需安装一"） | **`min_hits=2`** —— 只留"在 ≥2 条视频里出现"的词。实测把 12 个噪音收敛到 3 个真话题 |

### 11.3 ★ 候选词**人审**才生效（与"改授权"的边界）

新增 [`core/yamlkeywords.py`](../core/yamlkeywords.py) + CLI **`cli.app keywords`**。

**为什么不让候选词自动生效**：谁能改"我的关注点"必须由人决定 ——
这与项目既有的「授权类改动只能由人做」是同一条原则。

**实测验证**（用副本，**不碰真配置**）：

| 检查 | 结果 |
|---|---|
| 授权字段（mode/dry_run/publish_enabled） | ✅ **逐行完全相同**（一字未动） |
| 注释行数 | ✅ **25 / 25**（没丢 —— 逐行替换，不是 YAML 解析再写回） |
| 去重 | ✅ 大小写不敏感（`DSH插件` vs `dsh插件` 判为同一个） |
| 写前备份 | ✅ 生成 `.bak-keywords` |

### 11.4 多平台：**预留框架**（抖音/小红书，走正规渠道）

新增 [`core/platforms.py`](../core/platforms.py) + CLI **`cli.app platforms`**。

**★★ 最重要的设计：未实现的平台必须"明确拒绝"**

```
❌ 返回空列表  → 上层会理解成"有这能力，只是这次没内容"
              → 走进完全错误的降级路径（重试 / 记 NO_CONTENT）
✅ 抛 CapabilityNotImplemented → 上层立刻知道"这平台还没接"
```

**实测**：

```
$ cli.app platforms
bilibili       已接入     （web_readonly）
    能力：search, detail, subtitles, comments, ..., recommendation_feed
douyin         未接入     （official_api）
    为什么没做：…需要三件套签名（a_bogus + timestamp + x-secsdk-web-signature，
    其中 secsdk 是 177KB 混淆运行时），逆向签名属于绕过平台技术保护措施，本项目不做
xiaohongshu    未接入     （official_api）
    为什么没做：…已有生效判决认定「绕开技术保护措施抓取」违法
    （常州天宁法院刑事案、上海知产法院判赔 110 万），本项目只走官方授权通道
```

**红线检查**（有测试钉住）：抽象层源码里**不该出现**
`proxy` / `fingerprint` / `captcha` / `bypass` / `rotate_ip` 这些词 ——
一旦留了口子，接新平台时就会有人顺手用上。

### 11.5 最终自检（2026-10-17）

| 项 | 结果 |
|---|---|
| `pytest tests/` | ✅ **693 passed, 1 skipped** |
| `ruff check` | ✅ All checks passed |
| `mypy`（strict） | ✅ 38 source files clean |
| 插件预检 | ✅ 全部通过 |
| 三道体检 | ✅ 孤儿行 / 形态 / 行归属 全过 |
| MCP 工具 | ✅ **19 个**（新增 `expand_intent` / `trends_suggest`） |
| **真机 `expand_intent`** | ✅ 豆包真调用，产出 4 个可用关键词 |
| **真机 `trends_suggest`** | ✅ 拉到 8 条推荐 → 8 个候选词进池 |
| **候选池审阅** | ✅ 12 个候选带来源 BV，授权字段未被动 |

---

## 12. 补：把「严格限额」做成真的（2026-10-17）

### 12.1 ★ 自查发现的缺口：限额原本是**假的**

写文档时发现：`core/trends.py` 的 docstring 写着
「**限额**：一天最多 ``max_rounds_per_day`` 轮」，
但 `DEFAULT_MAX_ROUNDS_PER_DAY` 这个常量**只被定义，从未被检查**。

⇒ 也就是说：**agent 想跑几轮就跑几轮**，"限额"只是一句注释。

这不是小事 —— 推荐流是**算法喂的**（实测一屏 8 条只有 2~3 条相关），
跑得越多画像越被带跑。**限额是这个设计里唯一防画像漂移的闸门**，
闸门是假的等于没有。

### 12.2 修法：独立轮次账本 + 跑前检查

新增迁移 **v7** → `trends_rounds` 表；`core/trends.py` 新增：

| 函数 | 作用 |
|---|---|
| `quota_check(db)` | **跑之前**查今天的轮次，满了就拒绝（附人话理由） |
| `record_round(...)` | 跑完记一轮（**失败也要记**） |
| `rounds_today` / `rounds_remaining` / `rounds_summary` | 计数与展示 |

**真机实测**（真实库，连跑 4 次）：

```
第1轮：✅ fetched=6 new=1 剩余=1
第2轮：✅ fetched=6 new=0 剩余=0
第3轮：⛔ 被拒绝 code=RATE_LIMITED
       理由：今天已经跑过 2 轮推荐流（上限 2）。推荐流是算法喂的，
             跑太多会把小号的兴趣画像带跑 —— **这是设计，不是故障**，明天再来。
```

### 12.3 ★★ 两个容易踩错的地方（都有测试钉住）

**① 轮次**绝不能**写进 `runs` 表**

`runs` 是**主流程轮次**的账本，`schedule.is_due()` 会读它判断
"今天跑完没有"。若把推荐流也写进 `runs`：

> **刷一次推荐流 ⇒ 主流程误以为"今天已经干过活了" ⇒ 当天不再抓取/评论。**

刷推荐流把整天主流程废掉 —— 这个耦合绝不能有。
测试 `test_record_round_writes_to_own_ledger_not_runs` 专门钉这一点。

**② 「今天从几点算起」只有一份实现**

`trends._start_of_today()` **刻意复用** `storage._start_of_today()`，
不自己再写一遍。两份实现早晚会一份本地时区、一份 UTC
⇒ **配额在半夜附近对不上，而且白天测不出来**。
测试 `test_start_of_today_is_shared_with_storage` 断言两者相等。

**③ 失败也要记轮次**

否则"每次都被拦"会变成**无限重试** —— 限额的意义正在于
"今天试过了就别再试"。所有失败分支（`blocked` / `rate_limited` /
`error` / `no_content`）都调 `record_round`。

### 12.4 `companion_status` 新增可见字段

限额**看得见**才叫限额。status 现在含：

```json
"trends": {
  "suggestions": {"total": 13},
  "rounds": {"used_today": 2, "max_per_day": 2, "remaining_today": 0, "last_at": ...},
  "note": "候选词只进池子、不自动改关键词；采纳由主人跑 cli.app keywords --accept。"
},
"platforms": [
  {"platform": "bilibili",    "implemented": true,  "access_mode": "web_readonly"},
  {"platform": "douyin",      "implemented": false, "access_mode": "official_api"},
  {"platform": "xiaohongshu", "implemented": false, "access_mode": "official_api"}
]
```

### 12.5 回填路径真机验证（`--accept`）

```
改前关键词：['DSH插件', 'DeepSeek Harness', 'DSH 插件']
$ cli.app keywords --accept "本地模型"
已采纳「本地模型」：已写进 config.yaml（共 4 个关键词）

改后关键词：[..., '本地模型']
授权字段（mode/dry_run/publish_enabled）：✅ 一字不动
注释行数：25 / 25（没丢）
```

> 验证完已把真配置**还原**（那是验证性测试，不是真的要采纳这个词）。

### 12.6 最终自检（2026-10-17 第二轮）

| 项 | 结果 |
|---|---|
| `pytest tests/` | ✅ **720 passed, 1 skipped** |
| `ruff check` | ✅ All checks passed |
| `mypy`（strict） | ✅ 39 source files clean |
| 插件预检 | ✅ 全部通过 |
| 三道体检 | ✅ 孤儿行 / 形态 / 行归属 全过 |
| **真机限额** | ✅ 跑满 2 轮后第 3 轮被 `RATE_LIMITED` 拒绝 |
| **轮次账本** | ✅ 落库 2 条，`runs` 表**未被污染**（仍 4 条历史） |
| **回填路径** | ✅ `--accept` 写进 keywords，授权字段/注释未动 |

### 12.7 自动回填开关（默认关，给"不想手动审"留口子）

目标原文是「发现的新词**回填**关键词」。默认实现是**人审后回填**
（候选池 → `--accept`）。为了不丢原文那层意思，加了显式开关：

```yaml
discovery:
  auto_accept_suggestions: false   # 默认；true = 直接写进 keywords
```

**★ 关键边界（有测试钉住）**：开关**只决定"词怎么进 keywords"**，
**不决定"能跑几轮"** —— 每日轮次上限与单次条数上限**始终生效**。
否则打开自动回填就等于**同时解除了防画像漂移的闸门**。

测试 `test_quota_applies_regardless_of_auto_backfill` 专门断言这一点。










