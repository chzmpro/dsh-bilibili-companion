"""用**新评分机制**重算指定视频（2026-10-17）。

## 为什么需要它

主人反馈过「为什么我感觉没什么用的能给这么高分」——
旧机制是四维里有大量"recency/discuss"之类的噪音，
且 `analyze()` **连关键词都没传**，模型根本不知道对谁有用。

新机制（已上线）：

```
relevance_score = topic×0.80 + usefulness×0.10 + novelty×0.06 + evidence×0.04
```

其中 **topic 是绝对主导**（0.80），判断方法是
「这条内容讲的主题，是不是主人会主动去搜的？」

## 这个脚本做什么

对给定的 bvid 列表：
1. 从 `evidence` 表取**已登记**的证据块（不重新抓 B 站）；
2. 用 `TextWorker.analyze()` 按新 rubric + **主人的关注点**重算；
3. 与 `notes` 表里的旧分数逐个维度对比；
4. 输出差异表（可 `--json` 给别的程序用）。

**只读 + 只算，不写库**（`--save` 才写回 notes）。

用法：

    ./.venv/Scripts/python.exe -u scripts/rescore.py BV1xxx BV2yyy
    ./.venv/Scripts/python.exe -u scripts/rescore.py BV1xxx --json
    ./.venv/Scripts/python.exe -u scripts/rescore.py BV1xxx --save
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config  # noqa: E402
from core.storage import Database  # noqa: E402
from core.text_worker import TextWorker  # noqa: E402

#: 每个视频取多少条证据喂给模型。
#:
#: ★ 2026-10-18：从 8 提到 200。旧值只覆盖简介 + 开头几条字幕，长视频后半段
#:   永远不参与重算；真正的模型输入预算改由 ``chunk_evidence_blocks`` 按字符分块，
#:   不再靠"少取几条"控制成本。
MAX_EVIDENCE = 200


def collect(db: Database, bvid: str) -> dict[str, Any]:
    """取一个视频的标题 + 证据 + 旧分数。"""
    video = db.query_one("SELECT bvid, aid, title, source_url FROM videos WHERE bvid = ?", (bvid,))
    if video is None:
        return {"bvid": bvid, "error": "视频不在库里"}

    rows = db.query(
        "SELECT evidence_id, source_type, text, start_sec "
        "FROM evidence WHERE bvid = ? AND availability = 'available' "
        "ORDER BY CASE source_type WHEN 'description' THEN 0 ELSE 1 END, start_sec "
        "LIMIT ?",
        (bvid, MAX_EVIDENCE),
    )
    blocks = [
        {
            "evidence_id": str(row["evidence_id"]),
            "source_type": str(row["source_type"]),
            "text": str(row["text"]),
            "start_sec": row["start_sec"],
        }
        for row in rows
    ]

    old = db.query_one(
        "SELECT score_total, score_topic, score_novelty, score_evidence, "
        "score_recency, score_discuss FROM notes WHERE bvid = ?",
        (bvid,),
    )
    return {
        "bvid": bvid,
        "aid": int(video["aid"] or 0),
        "title": str(video["title"]),
        "source_url": str(video["source_url"]),
        "blocks": blocks,
        "old": dict(old) if old is not None else None,
    }


async def rescore_one(worker: TextWorker, db: Database, bvid: str) -> dict[str, Any]:
    """重算一个视频。"""
    info = collect(db, bvid)
    if "error" in info:
        return info
    if not info["blocks"]:
        return {**info, "error": "没有可用证据（无法重算）"}

    card = await worker.analyze(
        bvid=info["bvid"],
        aid=info["aid"],
        title=info["title"],
        source_url=info["source_url"],
        evidence_level="subtitle" if len(info["blocks"]) > 1 else "description",
        evidence_blocks=info["blocks"],
        focus=None,  # 下面用 worker 自己的 focus（若有）
    )
    return {
        "bvid": bvid,
        "title": info["title"],
        "evidence_count": len(info["blocks"]),
        "old": info["old"],
        "new": {
            "score_total": int(card.relevance_score),
            **(worker.last_dimensions or {}),
        },
        "comment": str(card.candidate_comment or "")[:100],
    }


def _pair(
    old: dict[str, Any], new: dict[str, Any], old_key: str, new_key: str
) -> str:
    """把「旧分 → 新分」渲染成一段文本（缺值显示 —）。

    ★ 两边的键名不一样：
      · `notes` 表     → score_topic / score_novelty / ...
      · `dimensions`   → topic / novelty / usefulness / evidence
    第一次写这脚本时按同一套键读，结果全显示 None（数据其实一直在）。
    """
    before = old.get(old_key)
    after = new.get(new_key)
    return f"{'—' if before is None else before}→{'—' if after is None else after}"


def render(results: list[dict[str, Any]]) -> None:
    """人看的对比表。"""
    for item in results:
        print("=" * 70)
        if "error" in item:
            print(f"{item['bvid']}  ❌ {item['error']}")
            continue
        old = item.get("old") or {}
        new = item.get("new") or {}
        old_total = old.get("score_total")
        new_total = new.get("score_total")
        delta = None
        if isinstance(old_total, int) and isinstance(new_total, int):
            delta = new_total - old_total
        arrow = ""
        if delta is not None:
            arrow = f"（{'+' if delta >= 0 else ''}{delta}）"
        print(f"{item['bvid']}  {item['title'][:52]}")
        print(f"  证据 {item['evidence_count']} 条")
        print(f"  总分：{old_total} → {new_total} {arrow}")
        # ★ 键名不一样：`notes` 表是 score_topic/score_novelty/...，
        #   而 `dimensions`（TextWorker）是 topic/novelty/usefulness/evidence。
        #   第一次写这脚本时按同一套键读，结果全显示 None（数据其实在）。

        print(
            "  维度："
            f"topic {_pair(old, new, 'score_topic', 'topic')} | "
            f"usefulness {_pair(old, new, 'score_usefulness', 'usefulness')} | "
            f"novelty {_pair(old, new, 'score_novelty', 'novelty')} | "
            f"evidence {_pair(old, new, 'score_evidence', 'evidence')}"
        )
        if item.get("summary"):
            print(f"  摘要：{item['summary']}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="用新评分机制重算视频")
    parser.add_argument("bvids", nargs="+", help="要重算的 BV 号")
    parser.add_argument("--json", action="store_true", help="输出 JSON（给程序用）")
    parser.add_argument("--save", action="store_true", help="把新分数写回 notes 表")
    args = parser.parse_args()

    config = load_config()
    db = Database()
    db.migrate()
    worker = TextWorker(config=config, db=db)
    try:
        results: list[dict[str, Any]] = []
        for bvid in args.bvids:
            results.append(await rescore_one(worker, db, bvid))

        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            render(results)

        if args.save:
            saved = 0
            for item in results:
                if "error" in item or not item.get("new"):
                    continue
                new = item["new"]
                db.execute(
                    "UPDATE notes SET score_total=?, score_topic=?, score_novelty=?, "
                    "score_evidence=? WHERE bvid=?",
                    (
                        new["score_total"],
                        new["score_topic"],
                        new["score_novelty"],
                        new["score_evidence"],
                        item["bvid"],
                    ),
                )
                saved += 1
            print(f"\n已写回 notes：{saved} 条")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
