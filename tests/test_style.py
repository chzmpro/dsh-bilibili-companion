"""R6：从高分视频评论区学习表达技巧（受控、写偏好，不改人设）。"""

from __future__ import annotations

import json

import httpx
import pytest

from core.storage import Database, now_ts
from core.style import STYLE_SUBJECT, collect_comments, learn_style
from tests.conftest import make_config


def _seed(db: Database, *, score: int = 90) -> None:
    db.upsert_video(bvid="BV1style0001", aid=1, title="技术视频")
    db.execute(
        "INSERT OR REPLACE INTO notes (bvid, summary, score_total, evidence_level, updated_at) "
        "VALUES (?,?,?,?,?)",
        ("BV1style0001", "有干货", score, "subtitle", now_ts()),
    )
    for text in [
        "这个插件的坑在于必须先设置环境变量，不然一直报错",
        "我实测 300 条评论用了 2 分钟，速度可以接受",
        "建议先备份数据库再升级，别问我怎么知道的",
    ]:
        db.add_evidence(bvid="BV1style0001", source_type="comment", text=text)


def _client(points: list[str]) -> httpx.AsyncClient:
    body = {
        "choices": [
            {"message": {"content": json.dumps(points, ensure_ascii=False)}}
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 30},
    }
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
        trust_env=False,
    )


async def test_learn_style_writes_preference(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db)
    monkeypatch.setattr("core.style.resolve_secret", lambda _n: "fake-key")
    client = _client(["开头先给结论", "用具体数字代替形容词"])
    try:
        result = await learn_style(
            db, make_config(text_worker={"model": "doubao-test"}), client=client
        )
    finally:
        await client.aclose()
    assert result.ok is True
    assert result.points == ["开头先给结论", "用具体数字代替形容词"]
    rows = [row for row in db.list_owner_preferences() if str(row["subject"]) == STYLE_SUBJECT]
    assert len(rows) == 1
    assert "开头先给结论" in str(rows[0]["content"])


async def test_learn_style_replaces_previous_version(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db)
    monkeypatch.setattr("core.style.resolve_secret", lambda _n: "fake-key")
    for points in (["旧技巧"], ["新技巧"]):
        client = _client(points)
        try:
            await learn_style(db, make_config(text_worker={"model": "doubao-test"}), client=client)
        finally:
            await client.aclose()
    active = [
        row for row in db.list_owner_preferences(active_only=True)
        if str(row["subject"]) == STYLE_SUBJECT
    ]
    assert len(active) == 1
    assert "新技巧" in str(active[0]["content"])


async def test_learn_style_needs_enough_comments(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.upsert_video(bvid="BV1style0002", aid=2, title="没人评论")
    result = await learn_style(db, make_config())
    assert result.ok is False
    assert result.reason == "COMMENTS_NOT_ENOUGH"


def test_collect_comments_filters_low_scores(db: Database) -> None:
    _seed(db, score=60)
    assert collect_comments(db) == []
