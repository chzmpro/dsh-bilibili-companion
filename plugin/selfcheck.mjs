/**
 * selfcheck.mjs —— **不重启 DSH** 就能知道"这个插件能不能加载"
 *
 * ## 为什么值得写
 *
 * DSH 对插件加载失败的处理是**静默的**：日志里只有一句
 * `1 entry did not activate … failed to import`，装的人只会看到"设置页里没这一项"。
 * 这个脚本用假的 ctx 把两个半身**真跑一遍**（家目录用系统临时目录，跑完删掉，不碰真数据），
 * 于是"整插件不激活"这类问题在**重启之前**就能发现。
 *
 * 用法：
 *     cd <项目>/plugin
 *     node selfcheck.mjs        # 退出码 0 = 通过，1 = 有问题
 */

import {
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { EDITABLE_KEYS } from './lib/pyconfig.mjs'

/** 本文件所在目录（读 client.js 用）。 */
const here = dirname(fileURLToPath(import.meta.url))

const sandbox = mkdtempSync(join(tmpdir(), 'bili-companion-selfcheck-'))
// 让 lib/home.mjs 的兜底路径落在沙箱里（fakeCtx 会先命中 dshHomePath）
process.env.DSH_HOME = sandbox

// ★★ 沙箱里造一个**假项目目录**：自检会真的改 `config.yaml`
//    （谷时开关要写进去再读回来），**绝不能碰主人的真配置**。
const fakeProject = join(sandbox, 'fake-project')
mkdirSync(fakeProject, { recursive: true })
// 始终使用当前 checkout 的完整示例，不依赖作者的安装目录。
// 示例缺失时应让自检失败，而不是使用缺字段的模板制造假故障。
copyFileSync(join(here, '..', 'config.example.yaml'), join(fakeProject, 'config.yaml'))

const failures = []
function ok(condition, label) {
  if (!condition) failures.push(label)
  console.log(`${condition ? '✅' : '❌'} ${label}`)
}

// ---------------------------------------------------------------------------
// Host 半身
// ---------------------------------------------------------------------------

const routes = []
const effects = []
const logs = []

const fakeCtx = {
  logger: {
    info: (m) => logs.push(String(m)),
    warn: (m) => logs.push(String(m)),
  },
  // ★ 故意给"函数"形态：字符串/函数搞混会让整个插件不激活（竞品踩过）
  get: (key) => (key === 'dshHomePath' ? (...segments) => join(sandbox, ...segments) : undefined),
  // 真实 cordis 是"等服务就绪再回调"；这里立即回调，等价于服务已就绪
  inject: (deps, callback) => {
    callback({
      webServer: {
        register: (route) => {
          routes.push(route)
          return () => {}
        },
      },
      // ★ cordis 的 effect 语义是「立即执行、返回值当收摊函数」——
      //   路由就是在 effect 里注册的，所以这里必须真跑一次。
      effect: (fn) => {
        effects.push(fn())
      },
    })
  },
  effect: (fn) => {
    effects.push(fn())
  },
}

const host = await import('./index.js')
const pkg = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8'))

// ★★ 命名规则（对着本机长期可用的 `dsh-our-free-model` 核实的）：
//
//   index.js 的 `name`（短名）  = cordis.patch.yml 的行 id   → 例如 'our-free-model'
//   package.json 的 `name`（全称）= 包名                     → 例如 'dsh-our-free-model'
//   client.js 的 `id`           = **包名全称**               → 例如 'dsh-our-free-model'
//
//   ⚠️ 最容易错的就是最后一条：写成短名会报
//      `loaded without registering "<包名>" via ModuleLoader.load`（讨论区 #5235）。
ok(host.name === 'bili-companion', `Host 半身 name 是短名（实际 ${String(host.name)}）`)
ok(typeof host.apply === 'function', 'Host 半身导出 apply 函数')
ok(Array.isArray(host.inject), 'Host 半身导出 inject 数组')

host.apply(fakeCtx, { projectDir: fakeProject })

ok(routes.length === 1, `注册了 1 条路由（实际 ${routes.length}）`)
ok(routes[0]?.path === '/api/bili-companion', `路由前缀 = /api/bili-companion（实际 ${String(routes[0]?.path)}）`)
ok(routes[0]?.kind === 'prefix', `路由 kind = prefix（实际 ${String(routes[0]?.kind)}）`)

const configFile = join(sandbox, 'bili-companion', 'config.json')
ok(existsSync(configFile), `家目录里的 config.json 已落盘（${configFile}）`)
ok(effects.length > 0, `注册了收摊动作 ctx.effect（${effects.length} 个）`)

// ---------------------------------------------------------------------------
// ★★ 接口契约检查：前端依赖的字段**必须真的在响应里**
// ---------------------------------------------------------------------------
//
// 为什么必须有这一段（2026-10-16 真机事故）：
//   `client.js` 用 `if (!cfg) return '读设置中…'` 判断"拿到配置没有"，
//   而 `index.js` 的 `buildStatus()` **漏掉了 `config` 字段** ——
//   结果设置页**永久停在「读设置中」**，且**浏览器控制台一个错都不报**。
//   这类"契约缺失"单靠看代码很难发现，必须**真调一次接口、断言字段存在**。
//
// 做法：用假的 req/res 调真实的 route handler，解析 JSON 后逐字段断言。

/** 调一次路由，返回解析后的 JSON。 */
async function callRoute(method, path, body) {
  const handler = routes[0].handler
  const chunks = body === undefined ? [] : [Buffer.from(JSON.stringify(body))]
  const req = {
    method,
    url: `/api/bili-companion${path}`,
    async *[Symbol.asyncIterator]() {
      for (const chunk of chunks) yield chunk
    },
  }
  let status = 0
  let payload = ''
  const res = {
    writeHead(code) {
      status = code
    },
    end(text) {
      payload = text
    },
  }
  await handler(req, res)
  return { status, json: payload ? JSON.parse(payload) : null }
}

const statusRoutes = await callRoute('GET', '/status')
const statusJson = statusRoutes.json

ok(statusRoutes.status === 200, `GET /status 返回 200（实际 ${statusRoutes.status}）`)
ok(statusJson?.ok === true, 'GET /status 的 ok=true')

// ★ 这两条就是这次事故的直接防线
for (const field of [
  'config',
  'projectDir',
  'pythonFound',
  'reportPath',
  'reportExists',
  'today',
  'home',
  // ★ 2026-10-17 新增：设置页靠这三个渲染全部折叠分区
  //   （缺了它们界面会渲染出空面板，同样"不报错但没用"）
  'fields',
  'schema',
  'neverEditable',
]) {
  ok(
    statusJson !== null && Object.prototype.hasOwnProperty.call(statusJson, field),
    `GET /status 含 ${field} 字段（前端依赖它）`,
  )
}
// 前端读的是 config.keywords / config.dailyTop
for (const field of ['keywords', 'dailyTop', 'projectDir', 'valleyOnly']) {
  ok(
    statusJson?.config !== null &&
      statusJson?.config !== undefined &&
      Object.prototype.hasOwnProperty.call(statusJson.config, field),
    `GET /status 的 config 含 ${field} 字段`,
  )
}

// ★ 契约：私信监听接口（2026-10-17）
//   前端靠 `watch.running / watch.port / watch.url` 决定按钮文案和链接。
//   少了任一字段，界面会显示"已停止"但永远点不动 —— 又是 D19 那类静默卡住。
const watchRoute = await callRoute('GET', '/watch')
ok(watchRoute.status === 200, `GET /watch 返回 200（实际 ${watchRoute.status}）`)
for (const field of ['running', 'port', 'url']) {
  ok(
    watchRoute.json !== null && Object.prototype.hasOwnProperty.call(watchRoute.json, field),
    `GET /watch 含 ${field} 字段（前端依赖它）`,
  )
}
ok(
  typeof watchRoute.json?.port === 'number' && watchRoute.json.port >= 1024,
  'GET /watch 的 port 是合法端口号',
)
ok(
  typeof watchRoute.json?.url === 'string' && watchRoute.json.url.startsWith('http://127.0.0.1:'),
  'GET /watch 的 url 指向本机（面板不对外暴露）',
)
// 未启动时必须 running=false（不能一上来就骗人说在跑）
ok(watchRoute.json?.running === false, 'GET /watch 初始 running=false')

// ★ 契约：服务探测接口（2026-10-17）
//   验证"插件能否拿到 DSH 的 agents 服务"——这是"进程内驱动思考"的前提。
const probeRoute = await callRoute('GET', '/probe')
ok(probeRoute.status === 200, `GET /probe 返回 200（实际 ${probeRoute.status}）`)
ok(
  probeRoute.json !== null &&
    Object.prototype.hasOwnProperty.call(probeRoute.json, 'agentsService'),
  'GET /probe 含 agentsService 字段',
)
ok(
  Array.isArray(probeRoute.json?.agentsMethods),
  'GET /probe 的 agentsMethods 是数组',
)

const configRoute = await callRoute('GET', '/config')
ok(configRoute.json?.config !== undefined, 'GET /config 返回 config 对象')

const reportRoute = await callRoute('GET', '/report')
for (const field of ['exists', 'path']) {
  ok(
    reportRoute.json !== null && Object.prototype.hasOwnProperty.call(reportRoute.json, field),
    `GET /report 含 ${field} 字段`,
  )
}

// 改配置要真的落盘并反映到 status
const patched = await callRoute('POST', '/config', { dailyTop: 7, keywords: ['自检关键词'] })
ok(patched.json?.config?.dailyTop === 7, 'POST /config 能改 dailyTop')
ok(Array.isArray(patched.json?.yaml?.written), 'POST /config 返回 yaml 写入结果')

// ★ 契约：改完之后 GET /status 必须反映出来（D19 那次就是少了这类断言）
const afterDaily = await callRoute('GET', '/status')
ok(afterDaily.json?.config?.dailyTop === 7, 'GET /status 反映改后的 dailyTop')

// ★ 谷时开关：双向同步（写进 Python 的 config.yaml，再读回来一致）
const toggleOn = await callRoute('POST', '/config', { valleyOnly: true })
const yamlWrote = (toggleOn.json?.yaml?.written ?? []).includes('schedule.peak_valley')
ok(yamlWrote, 'POST /config 把谷时开关写进了 config.yaml（schedule.peak_valley）')

const afterValley = await callRoute('GET', '/status')
ok(
  afterValley.json?.config?.valleyOnly === true,
  'GET /status 从 config.yaml 读回 valleyOnly=true（权威值在 yaml）',
)

// 改回去，别把主人的真配置留在改动状态
await callRoute('POST', '/config', { valleyOnly: false })
const restored = await callRoute('GET', '/status')
ok(restored.json?.config?.valleyOnly === false, '谷时开关能改回 false（复原）')

// ---------------------------------------------------------------------------
// ★ 通用字段通道（2026-10-17）—— 设置页靠它渲染/保存全部折叠分区
// ---------------------------------------------------------------------------

const schema = statusJson?.schema ?? {}
const fieldCount = Object.keys(schema).length
ok(fieldCount >= 30, `白名单字段数够多（实际 ${fieldCount}，设置页靠它渲染）`)

// schema 的每一项都要有 type，否则前端渲染不出控件
const missingType = Object.entries(schema).filter(([, s]) => !s || !s.type)
ok(missingType.length === 0, `schema 每项都有 type（缺 ${missingType.length} 项）`)

// choice 类型必须带 options，否则下拉框是空的
const choiceNoOptions = Object.entries(schema).filter(
  ([, s]) => s?.type === 'choice' && !Array.isArray(s.options),
)
ok(choiceNoOptions.length === 0, `choice 字段都带 options（缺 ${choiceNoOptions.length} 项）`)

// ★★ 红线：授权字段**绝不在** schema 里（界面上点一下就打开发布 = 绕过人审）
const FORBIDDEN_IN_SCHEMA = [
  'interaction.mode',
  'interaction.dry_run',
  'interaction.publish_enabled',
  'interaction.pause',
  'interaction.dm_reply_enabled',
]
const leaked = FORBIDDEN_IN_SCHEMA.filter((k) => k in schema)
ok(leaked.length === 0, `授权/安全字段不在可编辑白名单里（泄漏：${leaked.join('、') || '无'}）`)

// neverEditable 要如实列出这些字段，界面才能告诉人"去终端改"
const never = statusJson?.neverEditable ?? []
const notListed = FORBIDDEN_IN_SCHEMA.filter((k) => !never.includes(k))
ok(notListed.length === 0, `neverEditable 列全了（缺：${notListed.join('、') || '无'}）`)

// ★ 通用写入：传点分路径，要真的写进 config.yaml
const genWrite = await callRoute('POST', '/config', {
  fields: { 'discovery.published_within_days': 3 },
})
ok(
  (genWrite.json?.yaml?.written ?? []).includes('discovery.published_within_days'),
  'POST /config 的 fields 通道能写点分路径',
)
const afterGen = await callRoute('GET', '/status')
ok(
  afterGen.json?.fields?.['discovery.published_within_days'] === 3,
  'GET /status 的 fields 反映刚写入的值',
)

// ★★ 关键：传授权字段必须**被拒绝**（不是静默接受）
const authAttempt = await callRoute('POST', '/config', {
  fields: { 'interaction.publish_enabled': true, 'interaction.mode': 'auto' },
})
const authWritten = authAttempt.json?.yaml?.written ?? []
ok(
  !authWritten.includes('interaction.publish_enabled'),
  '★★ 界面传 publish_enabled 会被拒绝（不进 written）',
)
ok(!authWritten.includes('interaction.mode'), '★★ 界面传 interaction.mode 会被拒绝')
const authSkipped = authAttempt.json?.yaml?.skipped ?? []
ok(
  authSkipped.includes('interaction.publish_enabled'),
  '被拒绝的字段要出现在 skipped 里（不能静默吞掉）',
)

// ★ 非法 choice 值必须报错（否则会把 config.yaml 写坏）
const badChoice = await callRoute('POST', '/config', {
  fields: { 'schedule.mode': '这不是合法模式' },
})
ok(
  badChoice.json?.yaml?.error !== undefined || (badChoice.json?.yaml?.written ?? []).length === 0,
  '非法 choice 值不会写进 config.yaml',
)

// 复原成 2（主人的原值）
await callRoute('POST', '/config', { fields: { 'discovery.published_within_days': 2 } })
const restoredGen = await callRoute('GET', '/status')
ok(
  restoredGen.json?.fields?.['discovery.published_within_days'] === 2,
  '通用字段能复原（published_within_days 回到 2）',
)

// ---------------------------------------------------------------------------
// 浏览器半身
// ---------------------------------------------------------------------------

let captured = null
// 浏览器半身会往 document.head 插一个 <style>，这里给个最小替身
globalThis.document = {
  createElement: () => ({
    style: {},
    setAttribute() {},
    remove() {},
  }),
  head: { appendChild() {} },
}
globalThis.window = {
  __ModuleLoader__: {
    load: (definition) => {
      captured = definition
    },
  },
}

await import('./client.js')

ok(captured !== null, '浏览器半身调用了 window.__ModuleLoader__.load')
// ★★ 这是最要命的一条：id 必须**逐字**等于包名，否则加载器认为"加载了没注册"
ok(
  captured?.id === pkg.name,
  `浏览器半身 id 逐字等于包名（${String(captured?.id)} vs ${pkg.name}）`,
)
ok(typeof captured?.factory === 'function', '浏览器半身提供了 factory 函数')

// 用"假 React"把 factory 真跑一遍，确认它不炸
const fakeReact = {
  createElement: (type, props, ...children) => ({ type, props, children }),
  useState: (initial) => [initial, () => {}],
  useEffect: () => {},
}
const slots = []
let styleAdded = false
const clientHalf = captured?.factory((request) => (request === 'react' ? fakeReact : undefined))
clientHalf.apply({
  get: () => undefined,
  slots: {
    inject: (name, register) => {
      slots.push(name)
      register()
    },
    register: (spec) => {
      slots.push(spec.id)
      return () => {}
    },
  },
  effect: () => {},
})

ok(slots.includes('settings.section'), '浏览器半身往 settings.section 插槽注册了设置页')

// ---------------------------------------------------------------------------
// ★ 渲染冒烟（2026-10-17）
// ---------------------------------------------------------------------------
//
// 上面那些验的是**后端接口契约**；这一段验**前端能不能渲染**。
// 两类失败性质完全不同：
//   · 后端少字段 → 界面停在"读设置中"（难看，但 shell 活着）；
//   · **前端渲染抛异常 → 整个 DSH 桌面端起不来**（AGENTS.md A4）。
//
// ⚠️ 本机没有独立 react（DSH 的 react 在 app.asar 里），
//    所以这里**只跑不依赖 react 的那部分**（幽灵字段检查），
//    完整渲染用 `render-smoke.mjs`（先跑 `extract-react-for-smoke.sh`）。
{
  const clientSrc = readFileSync(join(here, 'client.js'), 'utf8')
  // ★ 前缀**从白名单自动推导**，不要硬编码。
  //
  //   2026-10-17 踩到：这里原本写死
  //   `(?:schedule|discovery|interaction|persona|text_worker)`，
  //   于是新增 `bilibili_requests.*` / `report.*` 两组时，
  //   即使 client.js 里**真的**加了控件，这个正则也匹配不到 ⇒ 误报"未展示"。
  //   ⇒ 改成从 EDITABLE_KEYS 取顶层段名，以后加新段不会再漏。
  const sections = [
    ...new Set(Object.keys(EDITABLE_KEYS).map((key) => key.split('.')[0])),
  ].sort()
  const referenced = [
    ...new Set(
      [
        ...clientSrc.matchAll(
          new RegExp(`'((?:${sections.join('|')})\\.[A-Za-z0-9_]+)'`, 'g'),
        ),
      ].map((m) => m[1]),
    ),
  ]
  const ghost = referenced.filter((p) => !(p in EDITABLE_KEYS))
  ok(
    ghost.length === 0,
    `设置页引用的字段都在白名单里（幽灵字段：${ghost.join('、') || '无'}）`,
  )
  const notShown = Object.keys(EDITABLE_KEYS).filter(
    (p) => !referenced.includes(p) && p !== 'discovery.keywords',
  )
  ok(
    notShown.length === 0,
    `白名单字段在设置页都有控件（未展示：${notShown.join('、') || '无'}）`,
  )
}

// ---------------------------------------------------------------------------
// 收摊
// ---------------------------------------------------------------------------

rmSync(sandbox, { recursive: true, force: true })

if (failures.length > 0) {
  console.error(`\n${failures.length} 项不通过：`)
  for (const item of failures) console.error(`  · ${item}`)
  process.exit(1)
}
console.log('\n全部通过。')
