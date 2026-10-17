"""给「从没读过」的视频补抓证据（简介为主），好让它们能被评分（2026-10-17）。

## 为什么需要它

主人要「全部已搜索视频的打分」。实测本机：

```
视频总量        115 条
有证据（可评分） 40 条
无证据          75 条  ← 74 discovered + 1 read，从没读过
```

无证据的没法评分（强行打就是让模型瞎猜）。这个脚本**补抓简介**，
让它们至少有 `description` 证据可评。

## 抓什么、不抓什么

| 抓 | 不抓 | 理由 |
|---|---|---|
| **简介** `description` | 字幕 / ASR |
  字幕要逐条请求 + 可能触发 ASR，风险和成本高得多；简介够判 topic |
| | 评论 | 评论对"主题相关度"帮助最小 |

⇒ 每条视频**只 1 次请求**（view 接口），走 metadata 通道限速（3 秒/次）。

## 用法

    ./.venv/Scripts/python.exe -u scripts/backfill_evidence.py            # 预览要抓多少
    ./.venv/Scripts/python.exe -u scripts/backfill_evidence.py --run      # 真的抓
    ./.venv/Scripts/python.exe -u scripts/backfill_evidence.py --run --limit 5
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.bilibili import BilibiliReader  # noqa: E402
from core.config import load_config  # noqa: E402
from core.credentials import CredentialError, load_cookie  # noqa: E402
from core.ratelimit import RequestQueue  # noqa: E402
from core.storage import Database  # noqa: E402


def targets(db: Database, *, limit: int = 0) -> list[dict[str, Any]]:
    """挑出「完全没有证据」的视频。

    ★ 只挑**一条证据都没有**的：已经有字幕/评论的不需要补简介，
      补了也不会改变评分（评分优先用简介 + 字幕）。
    """
    sql = (
        "SELECT v.bvid, v.aid, v.title FROM videos v WHERE NOT EXISTS ("
        "  SELECT 1 FROM evidence e WHERE e.bvid = v.bvid) "
        "ORDER BY v.first_seen_at DESC"
    )
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    return [
        {"bvid": str(r["bvid"]), "aid": int(r["aid"] or 0), "title": str(r["title"] or "")}
        for r in db.query(sql)
    ]


def _store_description(db: Database, bvid: str, source_url: str, text: str) -> bool:
    """把简介存进 evidence（幂等：同内容不重复插）。"""
    body = (text or "").strip()
    if not body:
        return False
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    existing = db.query_one(
        "SELECT evidence_id FROM evidence WHERE bvid = ? AND text_sha256 = ?",
        (bvid, digest),
    )
    if existing is not None:
        return False
    evidence_id = f"ev_desc_{digest[:12]}"
    db.execute(
        "INSERT OR IGNORE INTO evidence "
        "(evidence_id, bvid, source_type, source_url, start_sec, end_sec, "
        " text, text_sha256, fetched_at, availability) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            evidence_id,
            bvid,
            "description",
            source_url,
            None,
            None,
            body,
            digest,
            int(time.time()),
            "available",
        ),
    )
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description="给无证据的视频补抓简介")
    parser.add_argument("--run", action="store_true", help="真的抓（默认只预览）")
    parser.add_argument("--limit", type=int, default=0, help="只抓前 N 条")
    args = parser.parse_args()

    config = load_config()
    db = Database()
    db.migrate()
    queue = RequestQueue(db=db, config=config)
    try:
        picked = targets(db, limit=args.limit)
        print(f"需要补抓的视频：{len(picked)} 条")
        if not picked:
            print("没有需要补抓的。")
            return 0
        if not args.run:
            for item in picked[:5]:
                print(f"  {item['bvid']}  {item['title'][:44]}")
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

        stored = 0
        skipped = 0
        failed = 0
        total = len(picked)
        for index, item in enumerate(picked, start=1):
            print(f"  [{index}/{total}] {item['bvid']} …", end="", flush=True)
            try:
                detail = await reader.get_video_detail(item["bvid"])
            except Exception as exc:  # noqa: BLE001 —— 单条失败继续
                failed += 1
                print(f" ❌ {type(exc).__name__}")
                continue
            source_url = detail.source_url or str(item.get("source_url") or "")
            if _store_description(db, item["bvid"], source_url, detail.description):
                stored += 1
                print(f" ✅ 简介 {len(detail.description)} 字")
            else:
                skipped += 1
                print(" ⏭ 简介为空或已存在")

        print(f"\n补抓完成：新增 {stored} 条，跳过 {skipped} 条，失败 {failed} 条")
        return 0
    finally:
        await queue.aclose()
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
