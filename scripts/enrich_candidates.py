"""给**高分候选**补全完整内容：字幕 + 热评（2026-10-18）。

## 主人要求

> 「完整读一遍就是全部都要」
> 「中转的时候把获取的高评分内容的字幕评论等需要交给上层模型」

## 现状问题（实测）

```
高分视频全是补抓来的，只有简介：
  98 BV1nSJk6BEGd: {'description': 1}
全库只有 28 个视频有字幕，2 个有评论
```

原因：`backfill_evidence.py` 当初**只抓简介**（那是为了"先能打分"）。
要满足"完整读一遍"，必须补上字幕和评论。

## 为什么不补全 98 条

只补**高分候选**（默认 ≥75 分）。理由：
1. 只有高分会被推荐，低分补了也没人看；
2. 抓字幕/评论是最贵的操作（可能触发 ASR、要翻页），
   不该为 60 多条已判定"无关"的视频付这个成本。

## 节奏（主人指定）

每条之间 `--delay` 秒（默认 10），每 `--batch-size` 条多休息一会儿。
**绝不批量硬打** —— 2026-10-18 一次连打 75 条已经吃过教训。

用法：

    ./.venv/Scripts/python.exe -u scripts/enrich_candidates.py            # 预览
    ./.venv/Scripts/python.exe -u scripts/enrich_candidates.py --run      # 真抓
    ./.venv/Scripts/python.exe -u scripts/enrich_candidates.py --run --limit 3
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.bilibili import BilibiliReader  # noqa: E402
from core.config import load_config  # noqa: E402
from core.credentials import CredentialError, load_cookie  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from core.ratelimit import RequestQueue  # noqa: E402
from core.storage import Database  # noqa: E402

#: 默认节奏（对齐主人 2026-10-18 的指定）。
DEFAULT_DELAY = 10.0
DEFAULT_BATCH_SIZE = 5
DEFAULT_BATCH_REST = 20.0

#: 每个视频取几条热评（参考项目 limit=8）。
HOT_COMMENTS = 8


def targets(db: Database, *, limit: int = 0, min_score: int = 75) -> list[dict[str, Any]]:
    """挑出**高分且内容不全**的视频（缺字幕或缺评论）。"""
    sql = """
        SELECT n.bvid, n.score_total FROM notes n
        WHERE n.score_total >= ?
          AND EXISTS (
            SELECT 1 FROM videos v WHERE v.bvid = n.bvid
          )
          AND (
            NOT EXISTS (
              SELECT 1 FROM evidence e
              WHERE e.bvid = n.bvid AND e.availability = 'available'
                AND e.source_type IN ('subtitle','asr')
            )
            OR NOT EXISTS (
              SELECT 1 FROM evidence e2
              WHERE e2.bvid = n.bvid AND e2.availability = 'available'
                AND e2.source_type = 'comment'
            )
          )
        ORDER BY n.score_total DESC
    """
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    return [
        {"bvid": str(r["bvid"]), "score": int(r["score_total"])}
        for r in db.query(sql, (min_score,))
    ]


def _has(db: Database, bvid: str, kinds: tuple[str, ...]) -> bool:
    """该视频是否已有某类证据。"""
    placeholders = ",".join("?" for _ in kinds)
    row = db.query_one(
        f"SELECT 1 AS x FROM evidence WHERE bvid = ? AND availability = 'available' "
        f"AND source_type IN ({placeholders}) LIMIT 1",
        (bvid, *kinds),
    )
    return row is not None


async def main() -> int:
    parser = argparse.ArgumentParser(description="给高分候选补全字幕+热评")
    parser.add_argument("--run", action="store_true", help="真的抓（默认只预览）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条")
    parser.add_argument("--min-score", type=int, default=75, help="只处理 >= 该分数的")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="单条间隔秒数")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--batch-rest", type=float, default=DEFAULT_BATCH_REST)
    args = parser.parse_args()

    config = load_config()
    db = Database()
    db.migrate()
    queue = RequestQueue(db=db, config=config)
    try:
        picked = targets(db, limit=args.limit, min_score=args.min_score)
        print(f"高分候选缺少完整内容：{len(picked)} 条（>= {args.min_score} 分）")
        if not picked:
            print("所有高分候选的内容都齐了。")
            return 0
        if not args.run:
            for item in picked[:5]:
                print(f"  {item['score']}分 {item['bvid']}")
            if len(picked) > 5:
                print(f"  …还有 {len(picked) - 5} 条")
            print("\n（预览模式。要真抓：加 --run）")
            return 0

        try:
            bundle = load_cookie()
        except CredentialError as exc:
            print(f"❌ 没有凭据：{exc}")
            return 3
        cookies = bundle.values if hasattr(bundle, "values") else dict(bundle)
        reader = BilibiliReader(queue=queue, cookies=cookies)
        pipeline = Pipeline(db=db, config=config, reader=reader, queue=queue)

        ok = 0
        failed = 0
        total = len(picked)
        for index, item in enumerate(picked, start=1):
            bvid = item["bvid"]
            print(f"  [{index}/{total}] {item['score']}分 {bvid} …", end="", flush=True)
            try:
                # ① 字幕（read_video 内部会走 ASR 兜底，若配置开启）
                if not _has(db, bvid, ("subtitle", "asr")):
                    await pipeline.read_video(bvid)
                has_sub = _has(db, bvid, ("subtitle", "asr"))

                # ② 热评
                if not _has(db, bvid, ("comment",)):
                    detail = await reader.get_video_detail(bvid)
                    page = await reader.get_comments(detail.aid, page_size=HOT_COMMENTS)
                    stored = 0
                    # ⚠️ 字段名：CommentPage.replies / CommentItem.message
                    #    （我第一次写成 .comments / .content，AttributeError）
                    for comment in page.replies[:HOT_COMMENTS]:
                        body = str(getattr(comment, "message", "") or "").strip()
                        if not body:
                            continue
                        db.add_evidence(
                            bvid=bvid,
                            source_type="comment",
                            text=body,
                            source_url=detail.source_url,
                        )
                        stored += 1
                    print(f" 评论{stored}条", end="", flush=True)

                print(f" ✅ 字幕{'有' if has_sub else '无'}")
                ok += 1
            except Exception as exc:  # noqa: BLE001 —— 单条失败继续
                failed += 1
                print(f" ❌ {type(exc).__name__}")

            if index < total:
                is_batch_end = args.batch_size > 0 and index % args.batch_size == 0
                wait = args.batch_rest if is_batch_end else args.delay
                if wait > 0:
                    time.sleep(wait)

        print(f"\n补全完成：成功 {ok} 条，失败 {failed} 条")
        return 0
    finally:
        await queue.aclose()
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
