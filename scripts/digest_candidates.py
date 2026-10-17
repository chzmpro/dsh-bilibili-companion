"""对高分候选做**豆包深度提炼**（不截断，读全文）2026-10-18。

## 主人要求

> 「我既然有不要钱的豆包模型，那么就可以不采用截断的方式，
>   而是让豆包采取最有价值的字幕等信息，评论区中有许多@其主人介绍的
>   类似于本项目的大肥鱼，这些也可以让豆包分辨后总结它们的观点，
>   然后查看真实人类评论中的有效信息，最后再发给值班员」

## 流程位置

```
高分候选（>=75）
  ↓  ← 本脚本在这里
豆包读**全文** → 产出 core_value / highlights / peers / human_bits / caveats
  ↓
交接给值班员（用提炼结果，而不是原文，也不是截断）
  ↓
值班员用人设写推荐 → 私信主人
```

## 为什么这比截断好

| | 截断 | 提炼 |
|---|---|---|
| 完整读到了吗 | ❌ 只看前 2500 字 | ✅ **全文** |
| 关键结论（常在中间） | 被截掉 | 能抓到 |
| 交给值班员的量 | 7500 字/视频 | **约 600 字/视频** |
| 成本 | 省 | 豆包免费 |

用法：

    ./.venv/Scripts/python.exe -u scripts/digest_candidates.py            # 预览
    ./.venv/Scripts/python.exe -u scripts/digest_candidates.py --run      # 真提炼
    ./.venv/Scripts/python.exe -u scripts/digest_candidates.py --run --limit 2
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from core.candidate_digest import (  # noqa: E402
    digest_video,
    load_digest,
    save_digest,
)
from core.config import load_config  # noqa: E402
from core.storage import Database  # noqa: E402

#: 每条之间的间隔（秒）。豆包便宜但也别瞬发一堆。
DEFAULT_DELAY = 2.0


def collect(db: Database, bvid: str) -> dict[str, Any]:
    """取一个视频的**全文内容**（简介/字幕/评论）。"""
    video = db.query_one(
        "SELECT title, source_url FROM videos WHERE bvid = ?", (bvid,)
    )
    title = str(video["title"] or "") if video else ""

    rows = db.query(
        "SELECT source_type, text FROM evidence "
        "WHERE bvid = ? AND availability = 'available' ORDER BY start_sec",
        (bvid,),
    )
    description: list[str] = []
    subtitle: list[str] = []
    comments: list[str] = []
    for row in rows:
        kind = str(row["source_type"])
        text = str(row["text"] or "").strip()
        if not text:
            continue
        if kind == "description":
            description.append(text)
        elif kind in ("subtitle", "asr"):
            subtitle.append(text)
        elif kind == "comment":
            comments.append(text)

    return {
        "bvid": bvid,
        "title": title,
        "description": "\n".join(description),
        "subtitle": "\n".join(subtitle),
        "comments": comments,
    }


def targets(db: Database, *, limit: int = 0, min_score: int = 75) -> list[dict[str, Any]]:
    """挑出**高分但还没提炼**的视频。"""
    sql = (
        "SELECT n.bvid, n.score_total FROM notes n "
        "WHERE n.score_total >= ? "
        "AND NOT EXISTS (SELECT 1 FROM candidate_digests d WHERE d.bvid = n.bvid) "
        "ORDER BY n.score_total DESC"
    )
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    return [
        {"bvid": str(r["bvid"]), "score": int(r["score_total"])}
        for r in db.query(sql, (min_score,))
    ]


async def main() -> int:
    parser = argparse.ArgumentParser(description="豆包深度提炼高分候选")
    parser.add_argument("--run", action="store_true", help="真的调模型（默认只预览）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条")
    parser.add_argument("--min-score", type=int, default=75, help="只处理 >= 该分数")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="调用间隔秒数")
    parser.add_argument("--show", action="store_true", help="显示已提炼的结果")
    parser.add_argument(
        "--bvid", default="", help="只处理指定 bvid（调试用，忽略范围）"
    )
    args = parser.parse_args()

    config = load_config()
    db = Database()
    db.migrate()
    try:
        if args.show:
            rows = db.query(
                "SELECT bvid FROM candidate_digests ORDER BY created_at DESC LIMIT 10"
            )
            if not rows:
                print("还没有任何提炼结果。")
                return 0
            for row in rows:
                digest = load_digest(db, str(row["bvid"]))
                if digest is None:
                    continue
                print("=" * 60)
                print(f"{digest.bvid}")
                print(digest.to_prompt())
            return 0

        if args.bvid:
            picked = [{"bvid": args.bvid, "score": 0}]
        else:
            picked = targets(db, limit=args.limit, min_score=args.min_score)

        print(f"待提炼：{len(picked)} 条（>= {args.min_score} 分）")
        if not picked:
            print("没有需要提炼的（可能都已完成）。")
            return 0

        if not args.run:
            for item in picked[:5]:
                info = collect(db, item["bvid"])
                print(
                    f"  {item['score']}分 {item['bvid']} "
                    f"简介{len(info['description'])}字 "
                    f"字幕{len(info['subtitle'])}字 "
                    f"评论{len(info['comments'])}条"
                )
            print("\n（预览模式。要真提炼：加 --run）")
            return 0

        ok = 0
        failed = 0
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            for index, item in enumerate(picked, start=1):
                info = collect(db, item["bvid"])
                print(
                    f"  [{index}/{len(picked)}] {item['bvid']} "
                    f"(字幕{len(info['subtitle'])}字/评论{len(info['comments'])}条) …",
                    end="",
                    flush=True,
                )
                digest = await digest_video(
                    bvid=info["bvid"],
                    title=info["title"],
                    description=info["description"],
                    subtitle=info["subtitle"],
                    comments=info["comments"],
                    config=config,
                    client=client,
                )
                if digest.error:
                    failed += 1
                    print(f" ❌ {digest.error}")
                elif save_digest(db, digest):
                    ok += 1
                    print(f" ✅ 同类项目{len(digest.peers)}个 人类经验{len(digest.human_bits)}条")
                else:
                    failed += 1
                    print(" ❌ 入库失败")
                if index < len(picked) and args.delay > 0:
                    time.sleep(args.delay)

        print(f"\n完成：成功 {ok} 条，失败 {failed} 条")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
