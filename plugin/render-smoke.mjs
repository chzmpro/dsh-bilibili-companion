#!/usr/bin/env node
/**
 * 渲染冒烟测试（2026-10-17）—— 验证 `client.js` 的 `Panel` **真的能渲染**。
 *
 * ## 为什么必须有这个测试
 *
 * `selfcheck.mjs` 验的是**后端接口契约**（字段在不在），但**没验前端能不能渲染**。
 * 而这两类失败的性质完全不同：
 *
 * * 后端少字段 → 界面停在"读设置中"（难看，但 shell 活着）；
 * * **前端渲染时抛异常 → 整个 DSH 桌面端起不来**（AGENTS.md A4，issue #869）。
 *
 * 本轮设置页从 8 个字段扩到 45 个、还引入了折叠分区与通用 Field 组件，
 * **渲染路径变复杂了** —— 必须真跑一次渲染，而不是"看着没毛病"。
 *
 * ## 做法
 *
 * 用**假的** `window.__ModuleLoader__` 与**真的** React（服务端渲染 `renderToStaticMarkup`）
 * 把 `client.js` 加载起来，然后用**真实形态的 status 载荷**渲染 `Panel`：
 *
 * 1. 正常载荷 → 必须渲染出内容且不抛；
 * 2. **字段全空**（config.yaml 读不到）→ 也必须不抛（这是最容易被忽略的路径）；
 * 3. 折叠分组的路径**必须都在 schema 里**（否则界面上是幽灵字段）。
 */

import { createRequire } from 'node:module'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const require = createRequire(import.meta.url)

let failures = 0
function ok(cond, label) {
  if (cond) {
    console.log(`✅ ${label}`)
  } else {
    failures += 1
    console.log(`❌ ${label}`)
  }
}

// ---------------------------------------------------------------------------
// ① 把 client.js 在假 ModuleLoader 下加载起来
// ---------------------------------------------------------------------------

// ★ 先拿**真实白名单**（与后端同一份来源，避免测试自己编一套）
const pyconfig = await import('./lib/pyconfig.mjs')
const REAL_SCHEMA = {}
const REAL_FIELDS = {}
for (const [path, spec] of Object.entries(pyconfig.EDITABLE_KEYS)) {
  REAL_SCHEMA[path] = {
    type: spec.type,
    note: spec.note || '',
    options: spec.options || null,
  }
  // 造一个"形态正确"的样例值（只为渲染，不参与写盘）
  if (spec.type === 'bool') REAL_FIELDS[path] = true
  else if (spec.type === 'int') REAL_FIELDS[path] = 3
  else if (spec.type === 'float') REAL_FIELDS[path] = 8.5
  else if (spec.type === 'choice') REAL_FIELDS[path] = (spec.options || [''])[0]
  else if (spec.type === 'inline-list') REAL_FIELDS[path] = ['甲', '乙']
  else if (spec.type === 'block-list') REAL_FIELDS[path] = ['甲', '乙']
  else REAL_FIELDS[path] = '样例'
}
const REAL_NEVER = [...pyconfig.NEVER_EDITABLE]

const source = readFileSync(join(here, 'client.js'), 'utf8')

let registered = null
globalThis.window = {
  __ModuleLoader__: {
    load(spec) {
      registered = spec
    },
  },
}
// client.js 里用到 localStorage（可信模式那块），给个假的
globalThis.localStorage = {
  _d: {},
  getItem(k) {
    return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null
  },
  setItem(k, v) {
    this._d[k] = String(v)
  },
  removeItem(k) {
    delete this._d[k]
  },
}
// 它还会挂 DOM（样式、徽章），给最小的假 document
const fakeHead = { appendChild() {}, removeChild() {} }
const fakeEl = () => ({
  setAttribute() {},
  appendChild() {},
  remove() {},
  style: {},
  querySelector: () => null,
  addEventListener() {},
})
globalThis.document = {
  head: fakeHead,
  body: fakeEl(),
  createElement: fakeEl,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
}

// 直接 eval（client.js 是自注册脚本，不是 ESM）
// eslint-disable-next-line no-eval
new Function(source)()

ok(registered !== null, 'client.js 调用了 window.__ModuleLoader__.load')
ok(registered?.id === 'dsh-bili-companion', `load 的 id 逐字等于包名（${registered?.id}）`)
ok(typeof registered?.factory === 'function', 'factory 是函数')

// ---------------------------------------------------------------------------
// ② 用真 React 的 renderToStaticMarkup 渲染 Panel
// ---------------------------------------------------------------------------

let React = null
let renderToStaticMarkup = null
try {
  React = require('react')
  renderToStaticMarkup = require('react-dom/server').renderToStaticMarkup
} catch (error) {
  // ★ 本机没有独立 react（DSH 的 react 打在 app.asar 里）。
  //   这时**跳过**渲染部分，但**仍然**跑幽灵字段检查 —— 那部分不依赖 react。
  console.log(`⚠️ 本机没有 react / react-dom（${error.code || error.message}），跳过渲染部分`)
  console.log('   要跑完整渲染：把 DSH 的 react 解出来，然后')
  console.log('   NODE_PATH=<解出来的 node_modules> node render-smoke.mjs')
  console.log('   （提取方法见 docs/continuous-and-settings.md §四）')
  runGhostFieldCheck()
  finish()
}

/**
 * 幽灵字段检查（不依赖 react）。
 *
 * 为什么单独查：`FIELD_GROUPS` 在 client.js 里，白名单在 pyconfig.mjs 里 ——
 * **两处引用同一个字符串**。写错一个字母，那一项就在界面上**静默消失**
 * （渲染时 `.filter(p => schema[p] !== undefined)` 会把它滤掉）。
 * 这类 bug 不报错、不崩溃，只是"少了一项"，最难发现。
 */
function runGhostFieldCheck() {
  const clientSrc = readFileSync(join(here, 'client.js'), 'utf8')
  const referenced = [
    ...new Set(
      [
        ...clientSrc.matchAll(
          /'((?:schedule|discovery|interaction|persona|text_worker)\.[A-Za-z0-9_]+)'/g,
        ),
      ].map((m) => m[1]),
    ),
  ]
  const ghost = referenced.filter((p) => !(p in pyconfig.EDITABLE_KEYS))
  ok(
    ghost.length === 0,
    `FIELD_GROUPS 引用的字段都在白名单里（幽灵字段：${ghost.join('、') || '无'}）`,
  )

  // 反向：白名单里除 keywords（用专门的多行框）外，其余都该在界面上有
  const notShown = Object.keys(pyconfig.EDITABLE_KEYS).filter(
    (p) => !referenced.includes(p) && p !== 'discovery.keywords',
  )
  ok(
    notShown.length === 0,
    `白名单字段都在界面上有控件（未展示：${notShown.join('、') || '无'}）`,
  )
}

function finish() {
  console.log('')
  if (failures > 0) {
    console.log(`❌ 渲染冒烟失败 ${failures} 项`)
    process.exit(1)
  }
  console.log('全部通过（渲染冒烟）。')
  process.exit(0)
}

// factory 要一个 `require`：只放行 react，其余一律报错（这样能发现意外依赖）
const fakeRequire = (name) => {
  if (name === 'react') return React
  throw new Error(`client.js 意外依赖了 ${name}（不该有）`)
}
const mod = registered.factory(fakeRequire)
ok(typeof mod?.apply === 'function', 'factory 返回的对象有 apply')

// 把 Panel 抠出来：它没被导出，只能通过 apply 时的 register 拿到
let panelFactory = null
const fakeSlots = {
  inject(_name, fn) {
    fn()
  },
  register(_spec, factoryFn) {
    panelFactory = factoryFn
    return () => {}
  },
}
const fakeCtx = {
  get: (k) => (k === 'slots' ? fakeSlots : undefined),
  slots: fakeSlots,
  effect: () => () => {},
}
try {
  mod.apply(fakeCtx)
} catch (error) {
  ok(false, `apply 不该抛异常（实际：${error.message}）`)
}
ok(typeof panelFactory === 'function', 'apply 通过 slots 注册了设置页')

// ---------------------------------------------------------------------------
// ③ 渲染三种载荷
// ---------------------------------------------------------------------------

function makeStatus(overrides = {}) {
  return {
    ok: true,
    home: 'C:/Users/x/.dsh/bili-companion',
    projectDir: 'D:/DeepSeek/projects/dsh-bilibili-companion',
    pythonFound: true,
    reportPath: 'D:/x/digest.md',
    reportExists: true,
    today: '2026-10-17',
    config: {
      projectDir: 'D:/DeepSeek/projects/dsh-bilibili-companion',
      keywords: ['DSH插件'],
      dailyTop: 3,
      valleyOnly: true,
    },
    // ★ 用**真实的白名单**，不手写子集 ——
    //   手写子集会让"某分组因为缺字段而被过滤掉"看起来像 bug
    //  （第一版就是这么误报的）。这里直接 import pyconfig 的 EDITABLE_KEYS。
    fields: REAL_FIELDS,
    schema: REAL_SCHEMA,
    neverEditable: REAL_NEVER,
    asr: { enabled: false, model: 'small', note: '' },
    ...overrides,
  }
}

/**
 * 渲染一次。
 *
 * ⚠️ 难点：`Panel` 用 `useEffect` 去 fetch `/status`，而 `renderToStaticMarkup`
 * **不跑 effect** ⇒ 首屏一定是"读设置中…"。所以这里**直接测两条路径**：
 *
 * 1. `renderToStaticMarkup` —— 验证"没有 config 时的首屏"不抛（真实首屏路径）；
 * 2. 直接调 `Panel` 的**渲染函数**并注入 `st`（用 React 的 hooks 打桩），
 *    验证"拿到 config 后的完整渲染"不抛。
 */
function renderPanel() {
  const el = panelFactory()
  return renderToStaticMarkup(el)
}

// 路径 1：首屏（st=null → 走 "读设置中…"）
try {
  const html = renderPanel()
  ok(html.includes('读设置中'), '首屏渲染出"读设置中…"且不抛')
} catch (error) {
  ok(false, `首屏渲染抛异常：${error.message}`)
}

// 路径 2：注入 st（打桩 useState/useEffect，让 Panel 走到完整渲染）
function renderWithStatus(status) {
  const RealUseState = React.useState
  const RealUseEffect = React.useEffect
  const realCreate = React.createElement

  // 用一个"受控"的 useState：第一次调用返回 [status, noop]，
  // 其余调用返回真实的默认值（各字段草稿等）
  let stateCalls = 0
  React.useState = (initial) => {
    stateCalls += 1
    if (stateCalls === 1) return [status, () => {}]
    return [typeof initial === 'function' ? initial() : initial, () => {}]
  }
  React.useEffect = () => {}
  // createElement 原样（保持 React 内部一致）
  React.createElement = realCreate
  try {
    return renderPanel()
  } finally {
    React.useState = RealUseState
    React.useEffect = RealUseEffect
  }
}

const CASES = [
  ['正常载荷（45 字段的 schema）', makeStatus()],
  ['fields 全空（config.yaml 读不到）', makeStatus({ fields: {} })],
  ['schema 全空（后端没给元数据）', makeStatus({ schema: {} })],
  ['neverEditable 缺失', makeStatus({ neverEditable: [] })],
  [
    '字段值类型异常（数字给成字符串）',
    makeStatus({ fields: { 'schedule.interval_minutes': '30', 'persona.like_min_score': '8' } }),
  ],
]

for (const [label, payload] of CASES) {
  try {
    const html = renderWithStatus(payload)
    ok(html.length > 100, `渲染不抛且产出内容：${label}（${html.length} 字符）`)
  } catch (error) {
    ok(false, `渲染抛异常：${label} → ${error.message}`)
  }
}

// ★ 折叠分区标题必须真的出现在 HTML 里（否则等于没渲染出来）
try {
  const html = renderWithStatus(makeStatus())
  for (const title of ['什么时候自己跑', '挑什么、挑多少', '每天最多做多少', '人设']) {
    ok(html.includes(title), `折叠分区渲染出来了：${title}`)
  }
  ok(html.includes('只能去终端改的'), '授权类字段分区渲染出来了')
} catch (error) {
  ok(false, `折叠分区渲染失败：${error.message}`)
}

// ---------------------------------------------------------------------------
// ④ ★ 幽灵字段检查（不依赖 react）
// ---------------------------------------------------------------------------

runGhostFieldCheck()

finish()
