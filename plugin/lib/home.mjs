/**
 * lib/home.mjs —— 找插件的"家"（放日志与插件自己的配置）。
 *
 * ## 为什么要单独一个文件
 *
 * `ctx.get('dshHomePath')` **可能是函数，不是字符串**。
 * 当字符串用它，会把**函数源码**拼进路径 → 目录建不出来 → apply 抛错 →
 * **整个插件不激活**（DSH 只留一句 `1 entry did not activate … failed to import`）。
 * 竞品在 `lib/state.mjs` 里专门记了这个坑。这里两种形态都处理。
 *
 * ⚠️ 本项目的**业务数据**（看过哪些视频、发过什么、配额）按主人决定
 * **仍留在 `<项目>/.local-data/`，不搬到这个家目录**（见 docs/plugin-plan.md §4）。
 * 这里只放插件自己的配置与日志。
 */

import { mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

/** 家目录下的子目录名（= 插件 id）。 */
export const HOME_DIR_NAME = 'bili-companion'

/**
 * 解析插件家目录并确保存在。
 *
 * 顺序：`ctx.get('dshHomePath')` → 环境变量 `DSH_HOME` → `~/.dsh`。
 *
 * @param {unknown} ctx cordis 上下文（可能是 undefined，例如 selfcheck 里）
 * @returns {string} 绝对路径
 */
export function resolvePluginHome(ctx) {
  let base = null
  try {
    const candidate = typeof ctx === 'object' && ctx !== null && typeof ctx.get === 'function'
      ? ctx.get('dshHomePath')
      : undefined
    if (typeof candidate === 'function') {
      // ★ 函数形态：dshHomePath(...segments) => join(resolveDshHome(), ...segments)
      base = candidate(HOME_DIR_NAME)
    } else if (typeof candidate === 'string' && candidate.trim() !== '') {
      base = join(candidate, HOME_DIR_NAME)
    }
  } catch {
    // 服务不存在（老版本 / selfcheck 的假 ctx）→ 走下面的兜底
  }

  if (typeof base !== 'string' || base === '') {
    const env = process.env.DSH_HOME
    const root = typeof env === 'string' && env.trim() !== '' ? env : join(homedir(), '.dsh')
    base = join(root, HOME_DIR_NAME)
  }

  mkdirSync(base, { recursive: true })
  return base
}
