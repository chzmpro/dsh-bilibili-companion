import { readEditable, writeEditable } from '../lib/pyconfig.mjs'
import { copyFileSync, readFileSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

// 用一份**拷贝**测试，不碰真配置
const src = 'D:/DeepSeek/projects/dsh-bilibili-companion/config.yaml'
const dir = mkdtempSync(join(tmpdir(), 'pyconfig-test-'))
const target = join(dir, 'config.yaml')
copyFileSync(src, target)

const before = readFileSync(target, 'utf8')

console.log('=== 1. 读白名单字段 ===')
console.log(JSON.stringify(readEditable(dir), null, 1))

console.log('\n=== 2. 写：改谷时开关 + 关键词 ===')
const r = writeEditable(dir, {
  'schedule.peak_valley': true,
  'discovery.keywords': ['测试A', '测试B'],
})
console.log('written:', r.written, '| skipped:', r.skipped)

const after = readFileSync(target, 'utf8')
console.log('\n=== 3. 改后的那两行 ===')
for (const line of after.split('\n')) {
  if (/peak_valley|keywords/.test(line)) console.log('  ' + line)
}

console.log('\n=== 4. 授权字段必须原样不动（逐行精确对比） ===')
const authLines = (text) =>
  text
    .split('\n')
    .filter((l) => /^\s*(mode|dry_run|publish_enabled):/.test(l))
    .map((l) => l.trim())
const b4 = authLines(before)
const a4 = authLines(after)
console.log('  改前的授权行:', JSON.stringify(b4))
console.log('  改后的授权行:', JSON.stringify(a4))
console.log('  ' + (JSON.stringify(b4) === JSON.stringify(a4) ? 'OK 一字未动' : 'FAIL 被改了!'))

console.log('\n=== 5. 注释保留（授权行 + 注释行都应完好） ===')
const commentCount = (text) =>
  text.split('\n').filter((l) => l.trim().startsWith('#')).length
const bc = commentCount(before)
const ac = commentCount(after)
console.log(`  注释行数 改前 ${bc} / 改后 ${ac} ${bc === ac ? 'OK 未丢注释' : 'FAIL 注释丢了'}`)
console.log(`  行数变化 改前 ${before.split('\n').length} / 改后 ${after.split('\n').length}（块状列表项数变化属正常）`)

console.log('\n=== 5b. 关键词是否真的改成 2 项 ===')
const kwBlock = after.split('\n').slice(
  after.split('\n').findIndex((l) => /^\s*keywords:/.test(l)),
  after.split('\n').findIndex((l) => /^\s*keywords:/.test(l)) + 4,
)
for (const l of kwBlock) console.log('  ' + l)
console.log('  ' + (kwBlock.filter((l) => /^\s*-/.test(l)).length === 2 ? 'OK 两项' : 'FAIL 项数不对'))

console.log('\n=== 6. 非白名单字段必须被拒绝 ===')
const r2 = writeEditable(dir, {
  'interaction.mode': 'auto',
  'interaction.dry_run': false,
  'evil.field': 1,
})
console.log('  written:', r2.written, '| skipped:', r2.skipped)
const finalText = readFileSync(target, 'utf8')
console.log(
  '  interaction.mode 是否还是 observe:',
  finalText.includes('mode: "observe"') ? 'OK 没被改' : 'FAIL 被改了',
)

rmSync(dir, { recursive: true, force: true })
