"""验证投递给「值班员」是否**真的成功**（2026-10-17）。

## 为什么需要这个脚本

我之前犯过两次错：

1. 只看 `POST /deliver` 返回 `delivered: true` 就说"成功"；
2. 只看会话文件变大就说"有反应"。

**两次都错了。** 真相是：投递成功、文件变大，但每轮 `turn/end` 的
`reason.kind` 都是 `"error"`（缺 `role` 字段），会话在界面上显示"运行失败"。

⇒ 唯一可信的成功信号是：**最后一个 `turn/end` 事件的 `reason.kind == "completed"`**。

## 会话文件是 zstd 多帧

`zstdDecompressSync` 只解第一帧（只剩 session 头）。
必须按 `0x28B52FFD` magic 切帧逐个解。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.dm_deliver import deliver  # noqa: E402

#: 会话根目录（DSH_HOME 下按工作区分）。
DEFAULT_HOME = Path(
    r"C:\Users\yp-chenzhuoming\AppData\Roaming\dsh-desktop\harness\sessions"
)

#: 解压脚本（用 Node 的 zlib，逐帧切）。
NODE_DECOMPRESS = r"""
const fs=require('fs'), zlib=require('zlib');
const buf=fs.readFileSync(process.argv[1]);
const MAGIC = Buffer.from([0x28,0xB5,0x2F,0xFD]);
let pos = 0, text = '';
while (pos < buf.length) {
  const idx = buf.indexOf(MAGIC, pos);
  if (idx < 0) break;
  const next = buf.indexOf(MAGIC, idx + 4);
  const end = next < 0 ? buf.length : next;
  try { text += zlib.zstdDecompressSync(buf.subarray(idx, end)).toString('utf8'); } catch {}
  pos = end;
}
process.stdout.write(text);
"""


def find_session_file(home: Path, session_id: str) -> Path | None:
    """按 session id 找到那个 zstd 文件。"""
    for candidate in home.glob(f"*/{session_id}/session.v4.jsonl.zstd"):
        return candidate
    return None


def decompress(path: Path) -> str:
    """逐帧解压（单帧只能拿到 session 头）。"""
    try:
        result = subprocess.run(  # noqa: S603, S607
            ["node", "-e", NODE_DECOMPRESS, str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"__ERROR__ {exc}"
    return result.stdout or ""


def analyze(text: str) -> dict[str, object]:
    """从事件流里读**结论**：最后一条 turn/end 的 reason 是什么。"""
    turns: list[dict[str, object]] = []
    errors: list[str] = []
    delivered: list[str] = []

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue

        kind = event.get("type", "")
        data = event.get("data") or {}

        if kind == "turn/end":
            reason = data.get("reason") or {}
            entry: dict[str, object] = {
                "turn": data.get("turn"),
                "kind": reason.get("kind", "?"),
            }
            if reason.get("kind") == "error":
                error = reason.get("error") or {}
                entry["error"] = str(error.get("message") or "")[:160]
                errors.append(str(entry["error"]))
            turns.append(entry)

        elif kind == "user/message":
            for block in data.get("content") or []:
                if block.get("type") == "text":
                    delivered.append(str(block.get("text") or "")[:60])

    last = turns[-1] if turns else None
    return {
        "turns": len(turns),
        "last_turn": last,
        "completed": bool(last and last.get("kind") == "completed"),
        "errors": errors[-3:],
        "last_delivered": delivered[-1] if delivered else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="验证投递是否真的成功")
    parser.add_argument("--session", required=True, help="值班员会话 id")
    parser.add_argument("--text", default="【链路验证】请回复一句：收到。")
    parser.add_argument("--wait", type=float, default=30.0, help="投递后等几秒")
    parser.add_argument("--home", default=str(DEFAULT_HOME))
    parser.add_argument(
        "--check-only", action="store_true", help="只分析现有内容，不投递"
    )
    args = parser.parse_args()

    home = Path(args.home)
    path = find_session_file(home, args.session)
    if path is None:
        print(f"❌ 找不到会话文件：{args.session}")
        return 2

    before = analyze(decompress(path))
    print(f"投递前：{before['turns']} 轮，最后 {before['last_turn']}")

    if not args.check_only:
        print(f"投递：{args.text[:40]}")
        result = deliver(args.text, session_id=args.session)
        print(f"  接口返回：{result.to_dict()}")
        if not result.ok:
            print("❌ 投递失败，不必等了")
            return 1
        print(f"等 {args.wait:.0f} 秒…")
        time.sleep(args.wait)

    after = analyze(decompress(path))
    print()
    print("=" * 58)
    print(f"轮数：{before['turns']} → {after['turns']}")
    print(f"最后一条 turn/end：{after['last_turn']}")
    if after["last_delivered"]:
        print(f"最后投递的消息：{after['last_delivered']}")
    if after["errors"]:
        print("最近的错误：")
        for message in after["errors"]:
            print(f"  · {message}")
    print("=" * 58)

    if after["completed"] and after["turns"] > before["turns"]:
        print("✅ 成功：新增的一轮以 completed 结束")
        return 0
    if after["completed"] and args.check_only:
        print("✅ 现有内容最后一轮是 completed")
        return 0
    print("❌ 失败：最后一轮不是 completed（或没有新轮次）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
