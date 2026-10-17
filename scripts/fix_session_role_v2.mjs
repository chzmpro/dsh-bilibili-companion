/**
 * 修复被「缺 role」损坏的 DSH 会话文件（2026-10-17，第二版）。
 *
 * ## 第一版错在哪
 *
 * 第一版把整个文件**重压成单帧**，结果 DSH 报：
 *
 *     corrupt Zstandard session log:
 *     first frame is not exactly one header line
 *
 * ⇒ DSH 的会话 zstd 是**多帧**格式，且 **第一帧必须恰好只有 header 那一行**。
 *   帧边界是有语义的，不能整体重压。
 *
 * ## 这一版怎么做
 *
 * 逐帧解压 → **只改包含坏事件的那几帧** → 逐帧重新压缩 → 原样拼回去。
 * 帧数、帧顺序、第一帧"只含 header"的约束全部保留。
 *
 * ## 只改什么
 *
 * 只给 `user/message` 且 `data.role === undefined` 的事件补 `role: "user"`。
 * 官方 `createUserMessage()` 补的就是这个。
 *
 * 用法：
 *   node fix_session_role_v2.mjs <session.v4.jsonl.zstd> [--dry-run]
 */

import fs from 'node:fs'
import zlib from 'node:zlib'

const MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd])

/** 按 magic 切出所有帧的原始字节。 */
function splitFrames(buf) {
  const frames = []
  let pos = 0
  while (pos < buf.length) {
    const idx = buf.indexOf(MAGIC, pos)
    if (idx < 0) break
    const next = buf.indexOf(MAGIC, idx + 4)
    const end = next < 0 ? buf.length : next
    frames.push(buf.subarray(idx, end))
    pos = end
  }
  return frames
}

function main() {
  const file = process.argv[2]
  const dryRun = process.argv.includes('--dry-run')
  if (!file) {
    console.error('用法: node fix_session_role_v2.mjs <session.v4.jsonl.zstd> [--dry-run]')
    process.exit(2)
  }

  const original = fs.readFileSync(file)
  const frames = splitFrames(original)
  console.log(`原文件: ${original.length} 字节 | ${frames.length} 帧`)

  // ★ 先校验原结构：第一帧必须恰好一行（header）
  const firstText = zlib.zstdDecompressSync(frames[0]).toString('utf8')
  const firstLines = firstText.split('\n').filter((l) => l.trim() !== '')
  if (firstLines.length !== 1) {
    console.error(
      `✗ 第一帧不是恰好一行（实际 ${firstLines.length} 行）—— 这不是标准会话日志，拒绝处理。`,
    )
    process.exit(1)
  }
  console.log(`第一帧校验通过（header 一行，${firstText.length} 字符）`)

  let fixed = 0
  const outFrames = []
  for (let i = 0; i < frames.length; i += 1) {
    const raw = frames[i]
    let text
    try {
      text = zlib.zstdDecompressSync(raw).toString('utf8')
    } catch (error) {
      console.error(`✗ 第 ${i} 帧无法解压：${String(error)}`)
      process.exit(1)
    }

    // 逐行处理；只改坏事件，其余原样
    let changed = false
    const outLines = []
    for (const line of text.split('\n')) {
      const trimmed = line.trim()
      if (!trimmed.startsWith('{')) {
        outLines.push(line)
        continue
      }
      let event
      try {
        event = JSON.parse(trimmed)
      } catch {
        outLines.push(line)
        continue
      }
      if (
        event.type === 'user/message' &&
        event.data &&
        event.data.role === undefined
      ) {
        event.data.role = 'user'
        changed = true
        fixed += 1
        console.log(`  修复 seq=${event.seq}（第 ${i} 帧，补 role=user）`)
        outLines.push(JSON.stringify(event))
      } else {
        outLines.push(line)
      }
    }

    if (!changed) {
      // ★ 没改动就**原样保留原始字节** —— 连压缩参数都不碰，最安全
      outFrames.push(raw)
      continue
    }

    const payload = Buffer.from(outLines.join('\n'), 'utf8')
    outFrames.push(
      zlib.zstdCompressSync(payload, {
        params: { [zlib.constants.ZSTD_c_compressionLevel]: 3 },
      }),
    )
  }

  console.log(`共修复 ${fixed} 条（改动 ${outFrames.filter((f, i) => f !== frames[i]).length} 帧）`)

  if (fixed === 0) {
    console.log('没有需要修复的事件，原文件不动。')
    return
  }
  if (dryRun) {
    console.log('（dry-run，不写回）')
    return
  }

  const result = Buffer.concat(outFrames)

  // ★ 写回前自检：重新切帧、逐帧解压、复查
  const checkFrames = splitFrames(result)
  if (checkFrames.length !== frames.length) {
    console.error(
      `✗ 自检失败：帧数变了（${frames.length} → ${checkFrames.length}）—— 未写回。`,
    )
    process.exit(1)
  }
  let remaining = 0
  for (const raw of checkFrames) {
    let text
    try {
      text = zlib.zstdDecompressSync(raw).toString('utf8')
    } catch {
      console.error('✗ 自检失败：有帧解不开 —— 未写回。')
      process.exit(1)
    }
    for (const line of text.split('\n')) {
      const trimmed = line.trim()
      if (!trimmed.startsWith('{')) continue
      try {
        const event = JSON.parse(trimmed)
        if (
          event.type === 'user/message' &&
          event.data &&
          event.data.role === undefined
        ) {
          remaining += 1
        }
      } catch {
        console.error('✗ 自检失败：有行不是合法 JSON —— 未写回。')
        process.exit(1)
      }
    }
  }
  if (remaining > 0) {
    console.error(`✗ 自检失败：仍有 ${remaining} 条缺 role —— 未写回。`)
    process.exit(1)
  }
  // 第一帧必须仍是一行
  const firstAfter = zlib.zstdDecompressSync(checkFrames[0]).toString('utf8')
  if (firstAfter.split('\n').filter((l) => l.trim() !== '').length !== 1) {
    console.error('✗ 自检失败：第一帧不再是单行 —— 未写回。')
    process.exit(1)
  }

  const stamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14)
  const backup = `${file}.bak2-${stamp}`
  fs.copyFileSync(file, backup)
  console.log(`已备份 → ${backup}`)

  const tmp = `${file}.tmp`
  fs.writeFileSync(tmp, result)
  fs.renameSync(tmp, file)
  console.log(
    `✅ 已写回：${result.length} 字节 | ${checkFrames.length} 帧（原 ${original.length} 字节 / ${frames.length} 帧）`,
  )
}

main()
