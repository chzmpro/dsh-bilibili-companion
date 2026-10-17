"""从 rescore 日志里把结果写回 notes（2026-10-17）。

## 为什么需要它

`rescore_all.py --no-save` 跑完后结果只在日志里。这次跑了 18 分钟（40 条），
不可能为了落库再跑一遍 —— 解析日志更快也更省钱。

## 日志格式（`rescore_all.render()` 的输出）

```
  分数  topic   实用   新颖   证据  BV / 标题
------------------------------------------------------------------------------
  97    100   90   75   90  BV1VwJe6BEfn  多 Agent 和 Prompt Chain 有什么区别？
```

列顺序：总分 / topic / 实用 / 新颖 / 证据 / BV / 标题。

用法：

    ./.venv/Scripts/python.exe -u scripts/import_rescore_log.py <日志路径>
    ./.venv/Scripts/python.exe -u scripts/import_rescore_log.py <日志路径> --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.storage import Database  # noqa: E402

#: 匹配表格数据行：4 个分数 + BV 号 + 标题。
ROW = re.compile(
    r"^\s*(\d{1,3})\s+(\d{1,3})\s+(\d{1,3})\s+(\d{1,3})\s+(\d{1,3})\s+"
    r"(BV[0-9A-Za-z]{10})\s+(.*)$"
)


def parse(log_text: str) -> list[dict[str, object]]:
    """把日志里的表格行解析成结构化数据。"""
    out: list[dict[str, object]] = []
    for line in log_text.splitlines():
        match = ROW.match(line)
        if match is None:
            continue
        out.append(
            {
                "score_total": int(match.group(1)),
                "topic": int(match.group(2)),
                "usefulness": int(match.group(3)),
                "novelty": int(match.group(4)),
                "evidence": int(match.group(5)),
                "bvid": match.group(6),
                "title": match.group(7).strip(),
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="从 rescore 日志导回 notes")
    parser.add_argument("log", help="日志文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只解析，不写库")
    args = parser.parse_args()

    path = Path(args.log)
    if not path.is_file():
        print(f"❌ 找不到日志：{path}")
        return 2
    rows = parse(path.read_text(encoding="utf-8", errors="replace"))
    print(f"解析到 {len(rows)} 条")
    if not rows:
        print("（没匹配到表格行，检查日志格式）")
        return 1
    if args.dry_run:
        for item in rows[:5]:
            print(f"  {item['score_total']:>3} {item['bvid']} {str(item['title'])[:40]}")
        print("（dry-run，没写库）")
        return 0

    db = Database()
    db.migrate()
    now = int(time.time())
    written = 0
    try:
        for item in rows:
            bvid = str(item["bvid"])
            existing = db.query_one("SELECT bvid FROM notes WHERE bvid = ?", (bvid,))
            values = (
                int(item["score_total"]),
                int(item["topic"]),
                int(item["novelty"]),
                int(item["evidence"]),
                str(item["title"]),
                now,
            )
            if existing is None:
                db.execute(
                    "INSERT INTO notes (bvid, summary, score_total, score_topic, "
                    "score_novelty, score_evidence, score_recency, score_discuss, "
                    "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        bvid,
                        str(item["title"]),
                        int(item["score_total"]),
                        int(item["topic"]),
                        int(item["novelty"]),
                        int(item["evidence"]),
                        0,
                        0,
                        now,
                    ),
                )
            else:
                db.execute(
                    "UPDATE notes SET score_total=?, score_topic=?, score_novelty=?, "
                    "score_evidence=?, summary=?, updated_at=? WHERE bvid=?",
                    (*values, bvid),
                )
            written += 1
        print(f"✅ 已写回 notes：{written} 条")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
