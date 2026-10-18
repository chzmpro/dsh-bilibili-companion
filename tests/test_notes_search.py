"""R5：知识笔记检索（只返回短结果）。"""

from __future__ import annotations

import json

from core.storage import Database, now_ts


def _seed_note(db: Database, bvid: str, summary: str, facts: list[str]) -> None:
    db.upsert_video(bvid=bvid, aid=1, title=f"视频 {bvid}")
    db.execute(
        "INSERT OR REPLACE INTO notes (bvid, summary, score_total, facts_json, "
        "knowledge_tags, evidence_level, updated_at) VALUES (?,?,?,?,?,?,?)",
        (
            bvid,
            summary,
            88,
            json.dumps(facts, ensure_ascii=False),
            '["DSH","插件"]',
            "subtitle",
            now_ts(),
        ),
    )


def test_search_notes_matches_summary_and_facts(db: Database) -> None:
    _seed_note(db, "BV1n1", "DSH 插件如何做证据反查", ["引用必须能反查"])
    _seed_note(db, "BV1n2", "无关视频", ["没有相关内容"])
    rows = db.search_notes(query="证据", limit=5)
    assert len(rows) == 1
    assert str(rows[0]["bvid"]) == "BV1n1"
    assert "反查" in str(rows[0]["summary"])


def test_search_notes_empty_query_returns_nothing(db: Database) -> None:
    assert db.search_notes(query="  ", limit=5) == []


def test_search_notes_limit_is_bounded(db: Database) -> None:
    for i in range(30):
        _seed_note(db, f"BV1n{i:02d}", f"关键词 {i}", [])
    rows = db.search_notes(query="关键词", limit=100)
    assert len(rows) == 20
