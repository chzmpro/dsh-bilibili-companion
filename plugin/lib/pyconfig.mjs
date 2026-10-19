/**
 * lib/pyconfig.mjs —— 读写**项目的** `config.yaml` 里那几个"可以由界面改"的字段。
 *
 * ## 为什么不是随便改 YAML
 *
 * `config.yaml` 是**授权文件**：里面既有"主人写的普通偏好"（关键词、每天几条），
 * 也有"**改授权**"的字段（`mode` / `dry_run` / `publish_enabled`）。
 *
 * ★★ 本模块**只碰白名单里的字段**，其余一律**原样保留、逐行不动**。
 *    原因：设置页是"人在浏览器里点"，它可以承载普通偏好；
 *    但"能不能真发东西"必须由人在终端显式改 ——
 *    这条边界与项目既有的「人用入口不进 MCP」是同一个原则。
 *
 * ## 为什么用"逐行替换"而不是 YAML 解析再写回
 *
 * 解析再写回会把**注释全部丢掉**（我们的 `config.yaml` 里几乎每行都有注释，
 * 那些注释解释了"为什么是这个值"）。逐行替换只动目标行，注释与格式都不变。
 * 这与 DSH 自己的 patch 语义一致：**按整行替换，不做深合并**。
 */

import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

/**
 * 允许界面改的字段白名单。
 *
 * `key` 是**点分路径**（`<段>.<段>`），`type` 决定怎么写。
 *
 * ## ★★ 加字段前必须过的三道闸（2026-10-17 扩充时定的规矩）
 *
 * 1. **是不是"改授权"？** —— `interaction.mode` / `dry_run` / `publish_enabled`
 *    三个字段决定"**能不能真的发东西到公开平台**"，**永远不进这个表**。
 *    它们只能由人在终端改（`cli.app`），因为那是"授权"不是"偏好"。
 * 2. **是不是"安全停机/恢复"？** —— `interaction.pause` 不进。
 *    暂停是安全机制，**恢复必须由人做**（任务书：恢复暂停走人用 CLI）。
 * 3. **是不是"写开关"？** —— `interaction.dm_reply_enabled` 不进
 *    （它一开就能真发私信）。
 *
 * 其余字段都是"偏好"（看什么、跑多勤、阈值多少），可以放心让界面改。
 *
 * `type` 取值：`bool` / `int` / `float` / `str` / `choice` /
 * `inline-list`（`["a","b"]`）/ `block-list`（多行 `- "a"`）。
 * `choice` 要带 `options`。
 */
export const EDITABLE_KEYS = Object.freeze({
  // ---- 关注什么 ----------------------------------------------------------
  'discovery.keywords': { type: 'block-list', note: '关注关键词' },
  'discovery.published_within_days': { type: 'int', note: '只看最近几天' },
  'discovery.max_candidates_per_run': { type: 'int', note: '每轮最多几个候选' },
  'discovery.max_full_reads_per_run': { type: 'int', note: '每轮最多完整读几条' },
  'discovery.min_score_for_comment': { type: 'int', note: '低于这个分不评论（阈值）' },
  'discovery.score_topic_weight': {
    type: 'float',
    note: '打分权重：主题相关度占比（0.5~0.9，越大越只看"跟我有关"）',
  },
  'discovery.auto_accept_suggestions': {
    type: 'bool',
    note: '推荐流候选词自动回填关键词（默认关；开了算法会直接改你的关注词）',
  },
  'discovery.max_keywords': {
    type: 'int',
    note: '关键词总数上限（自动回填到这里就停，防止无限膨胀）',
  },

  // ---- 什么时候跑 --------------------------------------------------------
  'schedule.mode': {
    type: 'choice',
    options: ['on_host_start', 'daily', 'continuous'],
    // ★ 界面显示中文，写回 `config.yaml` 的仍是英文枚举值（值不能翻译）。
    optionLabels: {
      on_host_start: '打开 DSH 时跑一轮',
      daily: '每天固定时刻',
      continuous: '持续跑（每隔 N 分钟）',
    },
    note: '唤醒方式：打开 DSH / 每日固定时刻 / 持续跑',
  },
  'schedule.daily_time': { type: 'str', note: '每日时刻（仅 daily 模式）' },
  'schedule.timezone': { type: 'str', note: '时区（如 Asia/Shanghai）' },
  'schedule.min_gap_minutes': { type: 'int', note: '防抖：多久内不重复跑' },
  'schedule.peak_valley': { type: 'bool', note: '只在谷时跑（省钱）' },
  'schedule.interval_minutes': { type: 'int', note: '持续跑：两轮间隔（分钟）' },
  'schedule.max_runs_per_day': { type: 'int', note: '持续跑：每天最多几轮' },

  // ---- 频率与配额（⚠️ 不含 mode/dry_run/publish_enabled/pause）----------
  'interaction.frequency_tier': {
    type: 'choice',
    options: ['conservative', 'normal', 'active'],
    optionLabels: {
      conservative: '保守',
      normal: '正常',
      active: '积极',
    },
    note: '频率档位',
  },
  'interaction.max_top_comments_per_day': { type: 'int', note: '每天最多几条评论' },
  'interaction.max_replies_per_day': { type: 'int', note: '每天最多几条回复' },
  'interaction.max_replies_per_thread_per_day': { type: 'int', note: '同一楼最多回复几次' },
  'interaction.max_likes_per_day': { type: 'int', note: '每天最多几个赞' },
  'interaction.max_favorites_per_day': { type: 'int', note: '每天最多几个收藏' },
  'interaction.min_action_interval_min': { type: 'int', note: '两个动作至少隔几分钟' },
  'interaction.allow_third_party_threads': { type: 'bool', note: '允许在别人的楼里回复' },

  // ---- 豆包（文字 worker）------------------------------------------------
  'text_worker.enabled': { type: 'bool', note: '启用文字 worker（关了就只读不算）' },
  // ⚠️ provider 在 core/config.py 里是**自由字符串**（默认 volcengine_ark），
  //    不是枚举 —— 所以这里只能是 str，写成 choice 会漏掉合法值。
  'text_worker.provider': {
    type: 'str',
    note: '接口类型（默认 volcengine_ark；OpenAI 兼容服务填对应名字）',
  },
  'text_worker.model': { type: 'str', note: '模型名' },
  'text_worker.base_url': { type: 'str', note: '接口地址' },
  'text_worker.timeout_seconds': { type: 'float', note: '单次调用超时（秒）' },
  'text_worker.max_batch_seconds': { type: 'float', note: '单批时间预算（秒）' },
  'text_worker.max_batch_chars': { type: 'int', note: '单批最多多少字' },
  'text_worker.max_candidates_to_dsh': { type: 'int', note: '每批最多给 DSH 几个包（≤3）' },
  'text_worker.max_excerpt_chars': { type: 'int', note: '单次摘录上限（≤500）' },
  // 本地 ASR 兜底（默认关）：装了 faster-whisper 才有意义
  'text_worker.asr_fallback': { type: 'bool', note: '本地转写兜底（需装依赖）' },
  'text_worker.asr_model': { type: 'str', note: 'Whisper 模型大小' },

  // ---- 人设 --------------------------------------------------------------
  'persona.display_name': { type: 'str', note: '小号自称' },
  'persona.ai_identity': {
    type: 'choice',
    options: ['never', 'when_asked', 'proactive'],
    optionLabels: {
      never: '不提 AI 身份',
      when_asked: '被问到才说明',
      proactive: '主动说明',
    },
    note: 'AI 身份怎么提',
  },
  'persona.nicknames': { type: 'inline-list', note: '小名（逗号分隔）' },
  'persona.enable_liking': { type: 'bool', note: '允许点赞' },
  'persona.enable_favoriting': { type: 'bool', note: '允许收藏' },
  'persona.like_min_score': { type: 'float', note: '点赞分数线' },
  'persona.like_min_not_hype': { type: 'float', note: '点赞"不炒作"分数线' },
  'persona.like_max_per_author_per_day': { type: 'int', note: '同一个 UP 每天最多赞几个' },
  'persona.favorite_min_score': { type: 'float', note: '收藏分数线' },
  'persona.favorite_folders': { type: 'inline-list', note: '收藏夹（逗号分隔）' },
  'persona.allow_create_folders': { type: 'bool', note: '允许新建收藏夹' },
  'persona.allow_reply_others': { type: 'bool', note: '允许回复别人的评论' },
  'persona.allow_follow': { type: 'bool', note: '允许关注' },
  'persona.allow_negative_feedback': { type: 'bool', note: '允许点踩' },
  'persona.max_items_per_run': { type: 'int', note: '每轮最多处理几条' },

  // ---- 网络与限速（2026-10-17 新增）-------------------------------------
  //
  // ★ 为什么开放这几项：参考项目 `bilibili_learning_bot` 有 **391** 个配置项。
  //   我们的原则是**只锁"授权"，开放"调参"** —— 这几项调的是快慢/容忍度，
  //   最坏情况是变慢或更容易停机，**不会**变成乱发。
  'bilibili_requests.min_interval_seconds': {
    type: 'float',
    note: '两次请求之间至少隔几秒（越大越保守）',
  },
  'bilibili_requests.max_read_retries': { type: 'int', note: '读请求失败重试几次' },
  'bilibili_requests.backoff_base_seconds': { type: 'float', note: '退避基数（秒）' },
  'bilibili_requests.backoff_cap_seconds': { type: 'float', note: '退避上限（秒）' },
  'bilibili_requests.max_consecutive_rate_limits': {
    type: 'int',
    note: '连续限流几次就停机等人工',
  },
  'bilibili_requests.search_cache_ttl_seconds': {
    type: 'int',
    note: '搜索缓存多久算过期（秒）',
  },
  'bilibili_requests.metadata_cache_ttl_seconds': {
    type: 'int',
    note: '元数据缓存多久算过期（秒）',
  },
  'bilibili_requests.subtitle_cache_ttl_seconds': {
    type: 'int',
    note: '字幕缓存多久算过期（秒）',
  },

  // ---- 日报与通知（2026-10-17 新增）-------------------------------------
  'report.local_digest': { type: 'bool', note: '是否写本地日报' },
  'report.mention_owner_on_high_value_comment': {
    type: 'bool',
    note: '高质量评论是否 @ 主号',
  },
  'report.mention_like_threshold': { type: 'int', note: '@ 主号的点赞数门槛' },
  // ⚠️ 校验：share_like_threshold 必须**小于** mention_like_threshold（config 里有断言）
  'report.share_like_threshold': { type: 'int', note: '私信分享的点赞数门槛（须小于上一项）' },
  'report.push_times': {
    type: 'inline-list',
    note: '推送时刻（逗号分隔，如 09:00,20:00）',
  },
})

/**
 * **永远不允许**从界面改的字段（有测试钉住）。
 *
 * 这三类改的是"授权"与"安全状态"，必须由人在终端显式操作：
 * 界面上点一下就能把发布打开，等于把"人审"这道闸门绕过去了。
 */
export const NEVER_EDITABLE = Object.freeze([
  'interaction.mode',
  'interaction.dry_run',
  'interaction.publish_enabled',
  'interaction.pause',
  'interaction.dm_reply_enabled',
  'text_worker.api_key_env',
])


/** `config.yaml` 的路径。 */
export function configYamlPath(projectDir) {
  return join(projectDir, 'config.yaml')
}

/** 读一行，返回 `{ indent, key, rawValue, line }`；不是 `key:` 行则返回 null。 */
function parseKeyLine(line) {
  const match = /^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$/.exec(line)
  if (!match) return null
  return { indent: match[1], key: match[2], rawValue: match[3] }
}

/** 去掉行尾注释（只处理 ` #` 之前的部分，避免把 `#` 当值的一部分）。 */
function stripComment(rawValue) {
  const index = rawValue.indexOf('#')
  if (index === -1) return rawValue.trim()
  return rawValue.slice(0, index).trim()
}

/**
 * 按点分路径找到那一行的**行号**（0-based）。
 *
 * 做法：先找到第一段（顶层键，缩进 0），再在它之后找第二段
 * （缩进更深的同名键），遇到下一个顶层键就停。
 */
function findLineIndex(lines, path) {
  const [section, key] = path.split('.')
  let sectionStart = -1
  let sectionIndent = 0

  for (let i = 0; i < lines.length; i += 1) {
    const parsed = parseKeyLine(lines[i])
    if (!parsed || parsed.indent !== '') continue
    if (parsed.key === section) {
      sectionStart = i
      sectionIndent = 0
      break
    }
  }
  if (sectionStart === -1) return { sectionStart: -1, lineIndex: -1 }

  if (key === undefined) return { sectionStart, lineIndex: sectionStart }

  for (let i = sectionStart + 1; i < lines.length; i += 1) {
    const parsed = parseKeyLine(lines[i])
    if (!parsed) continue
    // 缩进回到 0 ⇒ 已经出了这一节
    if (parsed.indent === '') break
    if (parsed.key === key) return { sectionStart, lineIndex: i }
  }
  return { sectionStart, lineIndex: -1 }
}

/**
 * 把 JS 值渲染成 YAML 行尾文本（只支持我们白名单里的几种类型）。
 *
 * @param {string} type 白名单里的 `type`
 * @param {unknown} value 要写的值
 * @param {object} [spec] 该字段的白名单条目（`choice` 要读它的 `options`）
 */
function renderValue(type, value, spec) {
  if (type === 'bool') return value ? 'true' : 'false'
  if (type === 'int') {
    const n = Number(value)
    if (!Number.isFinite(n)) throw new Error(`不是合法数字：${value}`)
    return String(Math.trunc(n))
  }
  // ★ float：保留小数但**去掉多余的 0**（8.0 → 8，8.5 → 8.5）
  if (type === 'float') {
    const n = Number(value)
    if (!Number.isFinite(n)) throw new Error(`不是合法数字：${value}`)
    return String(Math.round(n * 1000) / 1000)
  }
  // ★ choice：**先校验在允许集合里**，否则界面传个脏值会把 config 写坏，
  //   而 Python 侧要到启动时才发现（报错点离现场很远，很难查）。
  if (type === 'choice') {
    const options = (spec && spec.options) || []
    const text = String(value)
    if (options.length > 0 && !options.includes(text)) {
      throw new Error(`不是合法取值：${text}（应为 ${options.join(' / ')}）`)
    }
    return `"${text.replace(/"/g, '\\"')}"`
  }
  if (type === 'str') return `"${String(value).replace(/"/g, '\\"')}"`
  if (type === 'inline-list') {
    const items = Array.isArray(value) ? value : []
    const rendered = items
      .filter((item) => typeof item === 'string' && item.trim() !== '')
      .map((item) => `"${item.replace(/"/g, '\\"')}"`)
      .join(', ')
    return `[${rendered}]`
  }
  throw new Error(`未知字段类型 ${type}`)
}

/**
 * 块状列表（
 * ```yaml
 * keywords:
 *   - "甲"
 *   - "乙"
 * ```
 * ）：读那一行的值并返回 `{ items, endIndex }`。
 *
 * `endIndex` 是最后一个 `- ` 行的下标（不含则等于 startIndex）。
 */
function readBlockList(lines, startIndex) {
  const items = []
  let endIndex = startIndex
  for (let i = startIndex + 1; i < lines.length; i += 1) {
    const line = lines[i]
    if (line.trim() === '') continue
    const match = /^(\s*)-\s+(.*)$/.exec(line)
    if (!match) break
    // 只收比 `- ` 缩进更深的项
    const outer = parseKeyLine(lines[startIndex])
    const outerIndent = outer ? outer.indent.length : 0
    if (match[1].length <= outerIndent) break
    items.push(match[2].trim().replace(/^["']/, '').replace(/["']$/, ''))
    endIndex = i
  }
  return { items, endIndex }
}

/**
 * 把块状列表写成 YAML 行（替换原来的那几行）。
 *
 * 返回替换后的行数组。**保留原缩进风格**（沿用第一条 `- ` 的缩进）。
 */
function renderBlockList(lines, startIndex, endIndex, items) {
  const outer = parseKeyLine(lines[startIndex])
  const outerIndent = outer ? outer.indent : ''
  // 沿用已有的缩进；没有就按"父缩进 + 2 空格"
  let itemIndent = `${outerIndent}  `
  if (endIndex > startIndex) {
    const first = /^(\s*)-\s+/.exec(lines[startIndex + 1])
    if (first) itemIndent = first[1]
  }

  const body = items
    .filter((item) => typeof item === 'string' && item.trim() !== '')
    .map((item) => `${itemIndent}- "${item.replace(/"/g, '\\"')}"`)

  return [...lines.slice(0, startIndex + 1), ...body, ...lines.slice(endIndex + 1)]
}

/**
 * 读：把白名单字段的**当前值**读出来（用于和插件自己的 config 对齐）。
 *
 * 只做"尽力而为"的解析，读不到就返回 undefined —— 不抛错，
 * 因为它的用途是"显示给用户看"，而不是校验。
 */
export function readEditable(projectDir) {
  const file = configYamlPath(projectDir)
  let lines
  try {
    lines = readFileSync(file, 'utf8').split(/\r?\n/)
  } catch {
    return {}
  }

  const out = {}
  for (const [path, spec] of Object.entries(EDITABLE_KEYS)) {
    const { lineIndex } = findLineIndex(lines, path)
    if (lineIndex === -1) continue
    const parsed = parseKeyLine(lines[lineIndex])
    if (!parsed) continue
    const raw = stripComment(parsed.rawValue)

    if (spec.type === 'bool') {
      out[path] = raw === 'true'
    } else if (spec.type === 'int') {
      const n = Number(raw)
      if (Number.isFinite(n)) out[path] = Math.trunc(n)
    } else if (spec.type === 'float') {
      const n = Number(raw)
      if (Number.isFinite(n)) out[path] = n
    } else if (spec.type === 'str' || spec.type === 'choice') {
      out[path] = raw.replace(/^["']/, '').replace(/["']$/, '')
    } else if (spec.type === 'inline-list') {
      // 只处理 `["a", "b"]` 形态
      const inner = raw.replace(/^\[/, '').replace(/\]$/, '')
      out[path] = inner
        .split(',')
        .map((piece) => piece.trim().replace(/^["']/, '').replace(/["']$/, ''))
        .filter((piece) => piece !== '')
    } else if (spec.type === 'block-list') {
      // 块状列表：`key:` 之后跟着若干 `- "项"`
      const { items } = readBlockList(lines, lineIndex)
      out[path] = items
    }
  }
  return out
}

/**
 * 写：**只替换白名单字段那一行**，其余原样保留（含所有注释）。
 *
 * @returns {{written: string[], skipped: string[]}} 真的改了哪些 / 跳过了哪些
 */
export function writeEditable(projectDir, patch, { backupSuffix = '.bak-plugin' } = {}) {
  const file = configYamlPath(projectDir)
  const original = readFileSync(file, 'utf8')
  const lines = original.split(/\r?\n/)

  const written = []
  const skipped = []

  for (const [path, value] of Object.entries(patch)) {
    const spec = EDITABLE_KEYS[path]
    if (spec === undefined) {
      // ★ 不在白名单里 → 直接跳过（**绝不**因为界面上多传了字段就改授权）
      skipped.push(path)
      continue
    }
    const { lineIndex } = findLineIndex(lines, path)
    if (lineIndex === -1) {
      skipped.push(path)
      continue
    }
    const parsed = parseKeyLine(lines[lineIndex])
    if (!parsed) {
      skipped.push(path)
      continue
    }

    if (spec.type === 'block-list') {
      const { endIndex } = readBlockList(lines, lineIndex)
      const rebuilt = renderBlockList(lines, lineIndex, endIndex, Array.isArray(value) ? value : [])
      lines.length = 0
      lines.push(...rebuilt)
      written.push(path)
      continue
    }

    const commentIndex = lines[lineIndex].indexOf('#')
    const comment = commentIndex === -1 ? '' : ` ${lines[lineIndex].slice(commentIndex)}`
    lines[lineIndex] = `${parsed.indent}${parsed.key}: ${renderValue(spec.type, value, spec)}${comment}`
    written.push(path)
  }

  if (written.length > 0) {
    // 先备份再写（出问题能手工对照）
    try {
      writeFileSync(`${file}${backupSuffix}`, original, 'utf8')
    } catch {
      // 备份失败不该阻止写入（它只是保险）
    }
    writeFileSync(file, lines.join('\n'), 'utf8')
  }

  return { written, skipped }
}
