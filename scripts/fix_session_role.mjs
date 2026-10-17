/**
 * 修复被「缺 role」损坏的 DSH 会话文件（2026-10-17）。
 *
 * ## 这次损坏是怎么来的（我的锅）
 *
 * 我用 `agent.followup({content, source})` 投递消息，**漏了 `role: "user"`**。
 * 官方 `createUserMessage()` 会补这个字段，我绕过它就直接少了。
 *
 * 后果链：
 *   1. 消息写进会话（`user/message` 事件，缺 role）
 *   2. 每次组装模型请求 → `messages[N]: missing field role` → 每轮 turn 报错
 *   3. DSH 校验会话时判定 **corrupt**：
 *        `session event at seq 39 message must have role "user"`
 *   4. **连删除都被拒绝**（删除前要 inspect，inspect 失败）
 *   5. 用户打不了字、删不掉 → 直接卡死
 *
 * ## 这个脚本做什么
 *
 * 只做一件事：给**缺 role 的 `user/message` 事件补上 `role: "user"`**。
 * 不动其他任何字节的语义（事件顺序、seq、时间戳全部保留）。
 *
 * ## 为什么安全
 *
 * - **先备份**（`.bak-<时间戳>`）
 * - 只改 `data.role` 缺失的那几条，其余原样
 * - 校验失败就**不写回**（宁可不修，也不制造第二次损坏）
 * - zstd 重压成单帧（DSH 用流式解压，单帧/多帧都能读）
 *
 * 用法：
 *   node fix-session-role.mjs <session.v4.jsonl.zstd> [--dry-run]
 */

import fs from 'node:fs'
import zlib from 'node:zlib'

const MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd])

/** 逐帧解压（单帧只能拿到 session 头，看不到正文）。 */
function decompressAll(buf) {
  let pos = 0
  let text = ''
  let frames = 0
  while (pos < buf.length) {
    const idx = buf.indexOf(MAGIC, pos)
    if (idx < 0) break
    const next = buf.indexOf(MAGIC, idx + 4)
    const end = next < 0 ? buf.length : next
    try {
      text += zlib.zstdDecompressSync(buf.subarray(idx, end)).toString('utf8')
      frames += 1
    } catch {
      /* 坏帧跳过（不应发生） */
    }
    pos = end
  }
  return { text, frames }
}

function main() {
  const file = process.argv[2]
  const dryRun = process.argv.includes('--dry-run')
  if (!file) {
    console.error('用法: node fix-session-role.mjs <session.v4.jsonl.zstd> [--dry-run]')
    process.exit(2)
  }

  const original = fs.readFileSync(file)
  const { text, frames } = decompressAll(original)
  const lines = text.split('\n')
  console.log(`原文件: ${original.length} 字节 | ${frames} 帧 | ${lines.length} 行`)

  let fixed = 0
  const out = []
  for (const line of lines) {
    const trimmed = line.trim()
    if (!trimmed.startsWith('{')) {
      out.push(line)
      continue
    }
    let event
    try {
      event = JSON.parse(trimmed)
    } catch {
      out.push(line)
      continue
    }

    // ★ 只修 `user/message` 且 data 缺 role 的
    const data = event.data
    if (event.type === 'user/message' && data && data.role === undefined) {
      // 官方 createUserMessage 补的就是 "user"
      data.role = 'user'
      fixed += 1
      console.log(`  修复 seq=${event.seq}（补 role=user）`)
      out.push(JSON.stringify(event))
      continue
    }
    out.push(line)
  }

  console.log(`共修复 ${fixed} 条`)

  if (fixed === 0) {
    console.log('没有需要修复的事件，原文件不动。')
    return
  }
  if (dryRun) {
    console.log('（dry-run，不写回）')
    return
  }

  // ★ 写回前自检：每条 JSON 都能解析、且没有缺 role 的 user/message
  for (const line of out) {
    const trimmed = line.trim()
    if (!trimmed.startsWith('{')) continue
    let event
    try {
      event = JSON.parse(trimmed)
    } catch (error) {
      console.error(`✗ 自检失败：有一行不是合法 JSON —— ${String(error)}`)
      console.error('  未写回。')
      process.exit(1)
    }
    if (event.type === 'user/message' && event.data && event.data.role === undefined) {
      console.error(`✗ 自检失败：seq=${event.seq} 仍然缺 role —— 未写回。`)
      process.exit(1)
    }
  }

  const payload = Buffer.from(out.join('\n'), 'utf8')
  const compressed = zlib.zstdCompressSync(payload, {
    params: { [zlib.constants.ZSTD_c_compressionLevel]: 3 },
  })

  const stamp = new Date()
    .toISOString()
    .replace(/[-:T]/g, '')
    .slice(0, 14)
  const backup = `${file}.bak-${stamp}`
  fs.copyFileSync(file, backup)
  console.log(`已备份 → ${backup}`)

  const tmp = `${file}.tmp`
  fs.writeFileSync(tmp, compressed)
  fs.renameSync(tmp, file)
  console.log(`✅ 已写回：${compressed.length} 字节（原 ${original.length}）`)
}

main()
