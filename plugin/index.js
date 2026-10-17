/**
 * dsh-bili-companion —— DSH 插件（Host 半身）
 * ============================================================================
 * 它做什么：把本仓库自研的 Python MCP bridge 挂进 DSH（见 `cordis.patch.yml`），
 * 并给「设置 → B站小号」提供数据与动作接口。
 *
 * ## 本文件遵守的三条本机契约（0.2.0-rc2，从运行中的插件挖出来的）
 *
 * 1. **`webServer` 不能在 apply 时直接读。** 插件先加载、浏览器半身后发布，
 *    所以在 apply 里 `ctx.get('webServer')` 得到的是 `undefined` ——
 *    路由永远不注册，设置页没有数据源。
 *    必须用 `ctx.inject(['webServer'], scoped => ...)`：它会在服务就绪时跑，
 *    服务被替换时会重跑。
 * 2. **清理动作要挂在 `ctx.effect(() => () => {...})` 上**，热重载才收得干净。
 * 3. **配置逐字段白名单清洗**（下面 `normalizeConfig`）—— 不信传进来的东西。
 *
 * ## 第一版刻意不做的（见 docs/plugin-plan.md §3）
 *
 * · 不搬数据目录（主人决定：业务数据仍留在 `<项目>/.local-data/`）
 * · 不在设置页改「发布授权」（publish_enabled / 模式）—— 那是改授权，
 *   仍走终端 `cli.app`；设置页只**显示**当前值。
 * · 不做扫码登录、不做一键升级、不做本地转写。
 */

import { spawn } from 'node:child_process'
import {
  closeSync,
  existsSync,
  openSync,
  readFileSync,
  writeFileSync,
} from 'node:fs'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { resolvePluginHome } from './lib/home.mjs'
import { EDITABLE_KEYS, NEVER_EDITABLE, readEditable, writeEditable } from './lib/pyconfig.mjs'

/** 插件 id。必须与 `cordis.patch.yml` 的 id、client.js 的 id 一致。 */
export const name = 'bili-companion'

/**
 * 不声明任何硬依赖：没有 `webServer` 时插件照样加载（只是设置页没数据），
 * 这比"整条插件不激活"好得多。
 */
export const inject = []

const PKG_DIR = fileURLToPath(new URL('./', import.meta.url))
/** 默认项目根：插件目录的上一级（`<项目>/plugin/` → `<项目>/`）。 */
const DEFAULT_PROJECT_DIR = resolve(PKG_DIR, '..')
const API_PREFIX = '/api/bili-companion'

const CONFIG_DEFAULTS = Object.freeze({
  projectDir: DEFAULT_PROJECT_DIR,
  keywords: ['DSH插件', 'DeepSeek Harness', 'DSH 插件'],
  dailyTop: 3,
  valleyOnly: false,
  // 私信监听面板（2026-10-17）
  watchPort: 8848,
  watchInterval: 60,
  watchAutoReply: false,
  // ★ 值班员会话 id（用户手动开的那个会话；投递只允许投给它）
  watchSessionId: '',
})

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------

function readJson(file, fallback) {
  try {
    return JSON.parse(readFileSync(file, 'utf8'))
  } catch {
    return fallback
  }
}

function writeJson(file, value) {
  try {
    writeFileSync(file, JSON.stringify(value, null, 2), 'utf8')
  } catch {
    // 家目录不可写不该让插件崩：下次还会再试
  }
}

/**
 * 逐字段白名单清洗。数组只收非空字符串、数字必须有限并钳到区间、
 * 布尔必须是布尔 —— 其余字段一律丢弃。
 */
function normalizeConfig(src, base) {
  const out = { ...base }
  if (typeof src !== 'object' || src === null) return out

  if (typeof src.projectDir === 'string' && src.projectDir.trim() !== '') {
    out.projectDir = src.projectDir.trim()
  }
  // 私信监听面板（2026-10-17）
  const watchPort = Number(src.watchPort)
  if (Number.isInteger(watchPort) && watchPort >= 1024 && watchPort <= 65535) {
    out.watchPort = watchPort
  }
  const watchInterval = Number(src.watchInterval)
  if (Number.isInteger(watchInterval) && watchInterval >= 15 && watchInterval <= 3600) {
    out.watchInterval = watchInterval
  }
  if (typeof src.watchAutoReply === 'boolean') out.watchAutoReply = src.watchAutoReply
  // 值班员会话 id（投递目标，白名单）
  if (typeof src.watchSessionId === 'string') out.watchSessionId = src.watchSessionId.trim()

  if (Array.isArray(src.keywords)) {
    out.keywords = src.keywords
      .filter((k) => typeof k === 'string' && k.trim() !== '')
      .map((k) => k.trim())
  }
  const top = Number(src.dailyTop)
  if (Number.isFinite(top)) out.dailyTop = Math.max(0, Math.min(20, Math.trunc(top)))
  if (typeof src.valleyOnly === 'boolean') out.valleyOnly = src.valleyOnly
  return out
}

/**
 * 白名单元数据（给前端渲染控件用）。
 *
 * ★ 单一来源：`lib/pyconfig.mjs` 的 `EDITABLE_KEYS` ——
 * 后端**不另维护一份字段表**（两份早晚不一致，症状是"界面改了没生效"）。
 */
function editableSchema() {
  const out = {}
  for (const [path, spec] of Object.entries(EDITABLE_KEYS)) {
    out[path] = {
      type: spec.type,
      note: spec.note || '',
      options: spec.options || null,
    }
  }
  return out
}

/** 永远不许从界面改的字段（前端会明确列出来，告诉人要去终端改）。 */
function neverEditable() {
  return [...NEVER_EDITABLE]
}

/** 读白名单字段的当前值；**读不到就返回空对象**（不让 status 整体失败）。 */
function readEditableSafe(cfg) {
  try {
    return readEditable(cfg.projectDir)
  } catch {
    return {}
  }
}

function todayKey(now = new Date()) {
  const pad = (n) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

/**
 * 读 ASR 配置状态（**只读 config.yaml，不启动 Python 子进程**）。
 *
 * 为什么不去调 Python 侧那份诊断：`/status` 是设置页每 5 秒轮询一次的接口，
 * 在里面起子进程会把界面拖慢。这里只回答"**开关开没开**"；
 * "依赖装没装"由主人点「测一下」或在终端跑 `cli.app status` 时看。
 */
function readAsrStatus(projectDir) {
  try {
    const fromYaml = readEditable(projectDir)
    return {
      enabled: fromYaml['text_worker.asr_fallback'] === true,
      model: fromYaml['text_worker.asr_model'] || '',
      note: '开着时：字幕不可用的视频会在本机转写一遍（不花 API 钱）。返回空 = 这条视频没有语音，不是故障。',
    }
  } catch {
    return { enabled: false, model: '', note: '' }
  }
}

function sendJson(res, code, body) {
  const text = JSON.stringify(body, null, 2)
  res.writeHead(code, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
  })
  res.end(text)
}

/** 读请求体（带上限，避免一个坏请求把内存吃满）。 */
async function readBody(req) {
  const chunks = []
  let size = 0
  for await (const chunk of req) {
    size += chunk.length
    if (size > 1_000_000) throw new Error('请求体过大')
    chunks.push(chunk)
  }
  const text = Buffer.concat(chunks).toString('utf8').trim()
  if (text === '') return {}
  const parsed = JSON.parse(text)
  return typeof parsed === 'object' && parsed !== null ? parsed : {}
}

// ---------------------------------------------------------------------------
// apply
// ---------------------------------------------------------------------------

/**
 * 插件入口。
 *
 * ★★ **整体包 try/catch**（2026-10-16 从 `app-boot` 源码级分析得到的教训）：
 *
 *   DSH 的启动审计 `assertEntriesActivated` 是 **fail-loud**：
 *   插件在**激活阶段**抛错（而不是加载阶段解析失败）时，
 *   boot 会 **dispose 整个插件树并抛错退出** —— 连 `disabled: true` 豁免也救不了
 *   （豁免只覆盖 load 阶段）。**用户会被锁在门外，只能重装。**
 *
 *   所以：本插件的任何失败都**只能影响自己**，绝不允许把宿主拖下水。
 *   最坏情况 = 设置页不可用 + 日志里一行原因，DSH 照常启动。
 *
 * @param {any} ctx cordis 上下文
 * @param {any} config 来自 patch 的 config（本插件用不到，保留签名）
 */
export function apply(ctx, config) {
  try {
    applyInner(ctx, config)
  } catch (error) {
    // 连 logger 都可能不在（ctx 本身异常），逐层兜底
    try {
      const logger = ctx?.logger ?? console
      logger.error?.('[bili-companion] apply 失败（插件已跳过，宿主不受影响）', error)
    } catch {
      /* 什么都做不了就只能放弃 */
    }
  }
}

function applyInner(ctx, config) {
  const logger = ctx.logger ?? console
  const home = resolvePluginHome(ctx)
  const configPath = join(home, 'config.json')

  // patch 里的 config 先落底，磁盘上的用户改动盖在上面
  let cfg = normalizeConfig(
    { ...(typeof config === 'object' && config !== null ? config : {}), ...readJson(configPath, {}) },
    CONFIG_DEFAULTS,
  )
  writeJson(configPath, cfg)

  const pythonPath = () => join(cfg.projectDir, '.venv', 'Scripts', 'python.exe')
  const dataDir = () => join(cfg.projectDir, '.local-data')
  const reportPath = () => join(dataDir(), 'digests', `digest-${todayKey().replace(/-/g, '')}.md`)

  function buildStatus() {
    const report = reportPath()
    // ★ 谷时开关的**权威值在项目的 config.yaml 里**（Python 侧真正读的那个）。
    //   插件自己那份 config.json 只是缓存；这里以 yaml 为准回填，
    //   避免"界面显示开着、实际没生效"或反之。
    let effective = cfg
    // ★ 2026-10-17：字段变多以后**不再逐个手写映射** ——
    //   直接把白名单里读到的值原样带出来（`fields`），
    //   前端按 `schema` 的元数据渲染，**两边不用各维护一份字段表**
    //  （两份表早晚会不一致，而症状是"界面改了没生效"，很难查）。
    let fields = {}
    try {
      const fromYaml = readEditable(cfg.projectDir)
      fields = fromYaml
      if (typeof fromYaml['schedule.peak_valley'] === 'boolean') {
        effective = { ...cfg, valleyOnly: fromYaml['schedule.peak_valley'] }
      }
      if (Array.isArray(fromYaml['discovery.keywords'])) {
        effective = { ...effective, keywords: fromYaml['discovery.keywords'] }
      }
    } catch {
      // 读不到就用插件自己那份（不因此报错）
    }
    return {
      home,
      configPath,
      projectDir: cfg.projectDir,
      pythonPath: pythonPath(),
      pythonFound: existsSync(pythonPath()),
      dataDir: dataDir(),
      reportPath: report,
      reportExists: existsSync(report),
      today: todayKey(),
      // ★★ 必须有：前端用 `if (!cfg) return '读设置中…'` 判断是否拿到配置。
      //    漏掉这个字段的后果是**永久停在"读设置中"**（2026-10-16 真机踩到）。
      //    ⇒ 状态接口与配置接口返回同一份 `config`，前端两边都能拿到。
      config: effective,
      // ★ 白名单里所有字段的**当前值**（键是点分路径）
      fields,
      // ★ 白名单**元数据**（type / note / options）—— 前端据此渲染控件
      schema: editableSchema(),
      // ★ 永远不许从界面改的字段（让界面能明确显示"这几个要去终端改"）
      neverEditable: neverEditable(),
      // ★ 让前端能显示"为什么没走本地转写"（开关没开 / 依赖没装）。
      //   只读 Python 侧那份诊断，不在这里重复判断。
      asr: readAsrStatus(cfg.projectDir),
    }
  }

  /**
   * 起一轮只读闭环（`scripts/run_once.py`）。
   *
   * 刻意**不等待**它跑完：一轮实测要 200 秒上下，接口立刻返回，
   * 进度写进家目录的 `run.log`。
   */
  function startRun() {
    const py = pythonPath()
    if (!existsSync(py)) {
      return { started: false, hint: `找不到 Python：${py} —— 先在终端跑一次安装（见 README）` }
    }
    const args = ['scripts/run_once.py']
    if (cfg.keywords.length > 0) args.push('--keywords', cfg.keywords.join(','))

    const logFile = join(home, 'run.log')
    let fd
    try {
      fd = openSync(logFile, 'a')
    } catch (error) {
      return { started: false, hint: `日志打不开：${String(error)}` }
    }
    try {
      const child = spawn(py, args, {
        cwd: cfg.projectDir,
        // ★ 把 Windows 的 GBK 差异钉死在这里（中文字幕/表情会炸管道）
        env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
        windowsHide: true,
        stdio: ['ignore', fd, fd],
        detached: false,
      })
      child.on('error', (error) => logger.warn?.(`bili-companion: 起一轮失败 ${String(error)}`))
      child.on('exit', (code) => logger.info?.(`bili-companion: 一轮结束 exit=${String(code)}`))
      child.unref()
      return { started: true, pid: child.pid, log: logFile }
    } finally {
      // spawn 已经 dup 了 fd，这里必须关掉自己那份
      closeSync(fd)
    }
  }

  /** 服务探测状态（只读，给 /probe 用）。 */
  const serviceProbe = {
    agentsService: false,
    agentsMethods: [],
    agentCount: null,
    agentShape: null,
    listError: '',
    error: '',
  }

  // ---- 私信监听面板（2026-10-17）------------------------------------------
  //
  // 主人：「没有在插件里启动的方式吗」
  //
  // 之前只能在终端跑 `cli.app dm-watch`。这里让插件**代起**：
  //   POST /watch?action=start  → spawn 一个 dm-watch，记住 pid
  //   POST /watch?action=stop   → 杀掉它
  //   GET  /watch               → 现在跑没跑、端口多少
  //
  // ★ 为什么不 spawn 面板自己的常驻监听，而是 spawn 整个 dm-watch：
  //   面板 + 监听是一个进程（`http.server` 主线程 + asyncio 后台线程），
  //   杀掉这个进程两边一起停 —— 比管两个进程简单可靠。
  let watchChild = null
  let watchStartedAt = 0

  function watchInfo() {
    const alive = watchChild !== null && watchChild.exitCode === null
    return {
      running: alive,
      pid: alive ? watchChild.pid : null,
      port: cfg.watchPort || 8848,
      url: `http://127.0.0.1:${cfg.watchPort || 8848}/`,
      since: alive && watchStartedAt ? new Date(watchStartedAt).toISOString() : null,
    }
  }

  function startWatch() {
    if (watchChild !== null && watchChild.exitCode === null) {
      return { started: false, hint: '已经在跑了', ...watchInfo() }
    }
    const py = pythonPath()
    if (!existsSync(py)) {
      return { started: false, hint: `找不到 Python：${py}` }
    }
    const port = Number(cfg.watchPort) || 8848
    const args = ['-m', 'cli.app', 'dm-watch', '--port', String(port)]
    if (cfg.watchInterval) args.push('--interval', String(cfg.watchInterval))
    if (cfg.watchAutoReply) args.push('--reply')

    const logFile = join(home, 'watch.log')
    let fd
    try {
      fd = openSync(logFile, 'a')
    } catch (error) {
      return { started: false, hint: `日志打不开：${String(error)}` }
    }
    try {
      // ⚠️ 这个**不能被 unref**：我们要能 kill 它、要知道它是否还活着。
      watchChild = spawn(py, args, {
        cwd: cfg.projectDir,
        env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
        windowsHide: true,
        stdio: ['ignore', fd, fd],
        detached: false,
      })
      watchStartedAt = Date.now()
      watchChild.on('error', (error) =>
        logger.warn?.(`bili-companion: 起监听失败 ${String(error)}`),
      )
      watchChild.on('exit', (code) => {
        logger.info?.(`bili-companion: 监听退出 exit=${String(code)}`)
        watchChild = null
      })
      return { started: true, ...watchInfo(), log: logFile }
    } catch (error) {
      return { started: false, hint: String(error?.message ?? error) }
    } finally {
      closeSync(fd)
    }
  }

  function stopWatch() {
    if (watchChild === null || watchChild.exitCode !== null) {
      return { stopped: false, hint: '本来就没在跑', ...watchInfo() }
    }
    const pid = watchChild.pid
    try {
      // Windows 上 kill() 对 npm 包起来的进程不一定彻底，先温和后强杀
      watchChild.kill()
      setTimeout(() => {
        try {
          if (watchChild !== null && watchChild.exitCode === null) watchChild.kill('SIGKILL')
        } catch {
          /* 已经死了就算了 */
        }
      }, 3000)
    } catch (error) {
      return { stopped: false, hint: String(error?.message ?? error) }
    }
    watchChild = null
    watchStartedAt = 0
    return { stopped: true, pid, ...watchInfo() }
  }

  async function handler(req, res) {
    const url = new URL(req.url ?? '/', 'http://127.0.0.1')
    const route = url.pathname.slice(API_PREFIX.length) || '/'
    const method = (req.method ?? 'GET').toUpperCase()
    try {
      if (method === 'GET' && route === '/status') {
        return sendJson(res, 200, { ok: true, ...buildStatus() })
      }
      if (method === 'GET' && route === '/config') {
        return sendJson(res, 200, {
          ok: true,
          config: cfg,
          fields: readEditableSafe(cfg),
          schema: editableSchema(),
          neverEditable: neverEditable(),
        })
      }
      if (method === 'POST' && route === '/config') {
        const patch = await readBody(req)
        cfg = normalizeConfig(patch, cfg)
        writeJson(configPath, cfg)

        // ★ 把"可由界面改的字段"同步写进项目的 config.yaml。
        //   `pyconfig.mjs` 只碰白名单字段，授权字段（mode/dry_run/publish_enabled）
        //   **一字不动** —— 那是"改授权"，只能由人在终端改。
        let yamlResult = { written: [], skipped: [] }
        try {
          const patchForYaml = {}
          if (Object.prototype.hasOwnProperty.call(patch, 'valleyOnly')) {
            patchForYaml['schedule.peak_valley'] = Boolean(cfg.valleyOnly)
          }
          if (Array.isArray(patch.keywords)) {
            patchForYaml['discovery.keywords'] = cfg.keywords
          }
          // ★ 2026-10-17 新的通用形态：点分路径直接传。
          //   安全性由 `pyconfig` 的白名单保证 —— 传了不在白名单里的路径
          //   （例如 `interaction.publish_enabled`）会被**跳过**，绝不写进 config.yaml。
          if (patch.fields && typeof patch.fields === 'object') {
            for (const [path, value] of Object.entries(patch.fields)) {
              patchForYaml[path] = value
            }
          }
          if (Object.keys(patchForYaml).length > 0) {
            yamlResult = writeEditable(cfg.projectDir, patchForYaml)
          }
        } catch (error) {
          // 写不进 config.yaml 不该让保存失败：插件自己那份已存好了
          logger.warn?.(`bili-companion: 写 config.yaml 失败 ${String(error)}`)
          yamlResult = { written: [], skipped: ['<写失败>'], error: String(error) }
        }
        return sendJson(res, 200, {
          ok: true,
          config: cfg,
          yaml: yamlResult,
          fields: readEditableSafe(cfg),
        })
      }
      // ---- 把一条消息**投递**给已存在的会话（2026-10-17）----------------
      //
      // 主人：「难道不能我自己专门开个会话给你用吗，非得你自己调用呢」
      //
      // ★ 这是比 createAgent 更好的路：
      //   用户手动开一个"值班员"会话，它的 agent 一直活着；
      //   我们只调 `agent.followup(...)` 把私信投进去 —— **零冷启动**，
      //   而且那个会话带着完整上下文，用户还能看见它在思考。
      //
      // ⚠️ 安全：只允许投给**配置里白名单**的会话，绝不投给别的
      //   （投错会把消息插进用户正在用的对话）。
      if (method === 'POST' && route === '/deliver') {
        const body = await readBody(req)
        const sessionId = String(body?.sessionId || cfg.watchSessionId || '')
        const text = String(body?.text || '').trim()
        if (!text) return sendJson(res, 400, { ok: false, error: 'text 不能为空' })
        if (!sessionId) {
          return sendJson(res, 400, {
            ok: false,
            error: '没有配置 watchSessionId（值班员会话 id），拒绝投递',
          })
        }
        const allow = cfg.watchSessionId
        if (allow && sessionId !== allow) {
          return sendJson(res, 403, {
            ok: false,
            error: `拒绝投递：只允许 ${allow}，收到 ${sessionId}`,
          })
        }
        const agents = serviceProbe._agents
        if (!agents) {
          return sendJson(res, 503, { ok: false, error: 'agents 服务还没就绪' })
        }
        try {
          // ★ 先找活的；找不到就**从磁盘恢复**（2026-10-17 真机踩到）
          //
          // DSH 重启后只有「当前打开的会话」是活 agent，其余都在磁盘上。
          // `agents.get()` 只查内存 ⇒ 直接 404。而 `agents.resume()`
          // 能按 `resumeSessionId` 把持久化会话拉回来。
          let agent = agents.get(sessionId)
          let resumed = false
          if (!agent) {
            try {
              agent = await agents.resume({ resumeSessionId: sessionId })
              resumed = Boolean(agent)
              logger.info?.(
                `bili-companion: 会话 ${sessionId.slice(-8)} 已从磁盘恢复 (${resumed})`,
              )
            } catch (error) {
              return sendJson(res, 500, {
                ok: false,
                error: `恢复会话失败：${String(error?.message ?? error)}`,
              })
            }
          }
          if (!agent) {
            return sendJson(res, 404, {
              ok: false,
              error: `找不到会话 ${sessionId}（可能被删了）`,
            })
          }
          // ★★★ 必须带 role（2026-10-17 真机踩到）
          //
          // 官方 `createUserMessage()` 的实现是：
          //     createMessage({ ...input, role: "user" })
          // 也就是说 **role 是它补上的**。我们绕过它直接调 followup，
          // 就必须自己带上，否则 agent 组装请求时那条消息缺 role，
          // 模型 API 会报：
          //     Failed to deserialize the JSON body: messages[N]: missing field `role`
          // 现象是「投递成功但会话每次都运行失败」—— 极难查。
          //   `createMessage()` 还会补一个 `id: randomUUID()`，这里也补上，
          //   让形状和官方路径完全一致。
          agent.followup({
            id: globalThis.crypto?.randomUUID?.() ?? `dm-${Date.now()}`,
            role: 'user',
            content: [{ type: 'text', text }],
            source: { kind: 'user' },
          })
          logger.info?.(`bili-companion: 已投递到 ${sessionId.slice(-8)}`)
          return sendJson(res, 200, { ok: true, delivered: true, sessionId, resumed })
        } catch (error) {
          return sendJson(res, 500, {
            ok: false,
            error: String(error?.message ?? error),
          })
        }
      }

      if (method === 'GET' && route === '/probe') {
        // ★ 动态探测：每次请求都重新列一遍（用户开着会话时才有 agent）
        const live = { ...serviceProbe }
        delete live._agents
        try {
          const agents = serviceProbe._agents
          if (agents && typeof agents.list === 'function') {
            const list = agents.list()
            live.agentCount = Array.isArray(list) ? list.length : 0
            live.agents = (Array.isArray(list) ? list : []).map((agent) => ({
              id: agent?.id ? String(agent.id).slice(0, 60) : null,
              methods: Object.getOwnPropertyNames(Object.getPrototypeOf(agent))
                .filter((k) => typeof k === 'string' && !k.startsWith('_'))
                .sort(),
            }))
          }
        } catch (error) {
          live.liveError = String(error?.message ?? error)
        }
        return sendJson(res, 200, { ok: true, ...live })
      }
      if (route === '/watch') {
        if (method === 'GET') {
          return sendJson(res, 200, { ok: true, ...watchInfo() })
        }
        if (method === 'POST') {
          const action = url.searchParams.get('action') || ''
          if (action === 'start') return sendJson(res, 200, { ok: true, ...startWatch() })
          if (action === 'stop') return sendJson(res, 200, { ok: true, ...stopWatch() })
          return sendJson(res, 400, { ok: false, error: `未知 action：${action}` })
        }
      }
      if (method === 'POST' && route === '/run') {
        const result = startRun()
        return sendJson(res, 200, { ok: true, ...result })
      }
      if (method === 'GET' && route === '/report') {
        const file = reportPath()
        if (!existsSync(file)) return sendJson(res, 200, { ok: true, exists: false, path: file })
        return sendJson(res, 200, { ok: true, exists: true, path: file, text: readFileSync(file, 'utf8') })
      }
      return sendJson(res, 404, { ok: false, error: `未知接口 ${method} ${route}` })
    } catch (error) {
      logger.warn?.(`bili-companion: 接口异常 ${String(error)}`)
      return sendJson(res, 500, { ok: false, error: String(error?.message ?? error) })
    }
  }

  // ---- DSH 服务探测（2026-10-17，验证"插件内驱动思考"是否可行）--------
  //
  // 主人问：「为啥启动第一轮 headless 后保持它在后台不关闭不行吗」——
  // `headless` 设计上答一个就退出，没有服务模式。但 DSH **内部**有服务：
  //   `agents`（注册在 `@deepseek-ai/dsh-agent`，`super(ctx, "agents")`）
  // 如果插件能 inject 到它，就能**进程内**驱动思考 —— 没有 20 秒冷启动。
  //
  // 这里只做**只读探测**：拿到没有、有哪些方法。不发消息、不建会话。
  // ★ 同时探测 agentLoop —— `createAgent` 在这个服务里（不在 agents 里）
  ctx.inject(['agentLoop'], (scoped) => {
    try {
      const loop = scoped.agentLoop
      serviceProbe.agentLoopService = Boolean(loop)
      if (loop) {
        serviceProbe.agentLoopMethods = Object.getOwnPropertyNames(
          Object.getPrototypeOf(loop),
        )
          .filter((key) => typeof key === 'string' && !key.startsWith('_'))
          .sort()
      }
      logger.info?.(
        `bili-companion: agentLoop 已拿到（${serviceProbe.agentLoopMethods.length} 个方法）`,
      )
    } catch (error) {
      serviceProbe.agentLoopError = String(error?.message ?? error)
      logger.warn?.(`bili-companion: 探测 agentLoop 失败 ${serviceProbe.agentLoopError}`)
    }
  })

  ctx.inject(['agents'], (scoped) => {
    try {
      const agents = scoped.agents
      serviceProbe.agentsService = Boolean(agents)
      // ★ 存下来，供 /probe **动态**查询（apply 时 agentCount 一定是 0，
      //   因为那会儿用户还没开会话）
      serviceProbe._agents = agents
      if (agents) {
        const proto = Object.getPrototypeOf(agents)
        serviceProbe.agentsMethods = Object.getOwnPropertyNames(proto)
          .filter((key) => typeof key === 'string' && !key.startsWith('_'))
          .sort()
      }
      // ★ 进一步：列出现有 agent 实例，并看单个 agent 能用什么
      try {
        const roots = typeof agents.list === 'function' ? agents.list() : []
        if (Array.isArray(roots)) {
          serviceProbe.agentCount = roots.length
          const first = roots[0]
          if (first) {
            serviceProbe.agentShape = {
              hasId: typeof first.id !== 'undefined',
              id: first.id ? String(first.id).slice(0, 40) : null,
              methods: Object.getOwnPropertyNames(Object.getPrototypeOf(first))
                .filter((k) => typeof k === 'string' && !k.startsWith('_'))
                .sort()
                .slice(0, 40),
            }
          }
        }
      } catch (error) {
        serviceProbe.listError = String(error?.message ?? error)
      }

      logger.info?.(
        `bili-companion: agents 服务已拿到（${serviceProbe.agentsMethods.length} 个方法，` +
          `${serviceProbe.agentCount ?? 0} 个实例）`,
      )
    } catch (error) {
      serviceProbe.error = String(error?.message ?? error)
      logger.warn?.(`bili-companion: 探测 agents 失败 ${serviceProbe.error}`)
    }
  })

  // ★ 契约 1：必须等 webServer 就绪，不能在 apply 时直接读
  ctx.inject(['webServer'], (scoped) => {
    const server = scoped.webServer
    scoped.effect(
      () => server.register({ kind: 'prefix', path: API_PREFIX, handler }),
      'bili-companion: api routes',
    )
    logger.info?.(`bili-companion: 设置页接口已挂载 ${API_PREFIX}`)
  })

  // ★ 契约 2：所有要收摊的东西都挂在这
  ctx.effect(
    () => () => {
      // 目前没有常驻资源（起一轮的子进程自己 unref 掉了）
    },
    'bili-companion: dispose',
  )

  logger.info?.(`bili-companion: 已加载（家目录 ${home}）`)
}
