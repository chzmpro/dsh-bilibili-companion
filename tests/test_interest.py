"""R4：主人反馈 → 兴趣信号 → 打分（不额外调模型）。

钉死：

* ``feedback_signals`` 只根据净反馈给出喜欢/不喜欢的 UP 与发现关键词；
* 这些信号进入 ``build_focus``；
* 打分时喜欢/不喜欢的 UP 会改 topic（抬下限 / 封顶），但总分仍由代码算。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.context_pack import build_focus
from core.storage import Database
from core.text_worker import _apply_feedback_bias
from tests.conftest import make_config


def _seed(db: Database, bvid: str, *, up: str, keyword: str) -> None:
    db.upsert_video(
        bvid=bvid, aid=1, title=f"{keyword} 的视频", up_name=up,
        discovery_keyword=keyword,
    )


def test_feedback_signals_aggregate_up_and_keyword(db: Database) -> None:
    _seed(db, "BV1a", up="好UP", keyword="DSH")
    _seed(db, "BV1b", up="好UP", keyword="DSH")
    _seed(db, "BV1c", up="坏UP", keyword="游戏")
    _seed(db, "BV1d", up="坏UP", keyword="游戏")
    db.add_feedback(bvid="BV1a", verdict="useful")
    db.add_feedback(bvid="BV1b", verdict="useful")
    db.add_feedback(bvid="BV1c", verdict="not_useful")
    db.add_feedback(bvid="BV1d", verdict="not_useful")

    signals = db.feedback_signals()
    assert signals["liked_ups"] == ["好UP"]
    assert signals["disliked_ups"] == ["坏UP"]
    assert signals["liked_keywords"] == ["DSH"]
    assert signals["disliked_keywords"] == ["游戏"]


def test_feedback_signals_are_empty_without_feedback(db: Database) -> None:
    _seed(db, "BV1e", up="某UP", keyword="DSH")
    assert db.feedback_signals() == {
        "liked_ups": [], "disliked_ups": [],
        "liked_keywords": [], "disliked_keywords": [],
    }


def test_build_focus_includes_feedback_signals(db: Database) -> None:
    _seed(db, "BV1a", up="好UP", keyword="DSH")
    db.add_feedback(bvid="BV1a", verdict="useful")
    focus = build_focus(db, make_config())
    assert focus["liked_ups"] == ["好UP"]
    assert focus["liked_keywords"] == ["DSH"]


def test_feedback_bias_floors_liked_up_and_caps_disliked() -> None:
    liked = _apply_feedback_bias(
        {"topic": 10, "usefulness": 50},
        up_name="好UP", title="t", text="", focus={"liked_ups": ["好UP"]},
    )
    assert liked["topic"] >= 80
    disliked = _apply_feedback_bias(
        {"topic": 95, "usefulness": 50},
        up_name="坏UP", title="t", text="", focus={"disliked_ups": ["坏UP"]},
    )
    assert disliked["topic"] <= 35


@pytest.mark.parametrize(
    ("up_name", "focus", "expected"),
    [
        ("好UP", {"liked_ups": ["好UP"]}, "floor"),
        ("坏UP", {"disliked_ups": ["坏UP"]}, "cap"),
        ("无关UP", {"liked_ups": ["好UP"]}, "none"),
    ],
)
def test_feedback_bias_is_limited_to_known_ups(
    up_name: str, focus: dict[str, Any], expected: str
) -> None:
    result = _apply_feedback_bias(
        {"topic": 50}, up_name=up_name, title="t", text="", focus=focus
    )
    if expected == "floor":
        assert result["topic"] >= 80
    elif expected == "cap":
        assert result["topic"] <= 35
    else:
        assert result["topic"] == 50


async def test_analyze_applies_up_bias(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_text_worker import model_reply, worker_with

    db.upsert_video(bvid="BV1test00001", aid=111, title="t", up_name="好UP")
    ev1 = db.add_evidence(
        bvid="BV1test00001", source_type="subtitle",
        text="这个插件可以自动整理笔记", start_sec=42.0,
    )
    db.add_feedback(bvid="BV1test00001", verdict="useful")
    monkeypatch.setattr("core.text_worker.resolve_secret", lambda _n: "fake-key")
    worker = worker_with(
        db,
        [
            model_reply(
                {
                    "key_points": [],
                    "scores": {"topic": 10, "usefulness": 10, "novelty": 10, "evidence": 10},
                    "candidate_comment": "x",
                }
            )
        ],
    )
    card = await worker.analyze(
        bvid="BV1test00001", aid=111, title="t", source_url="u",
        evidence_level="subtitle",
        evidence_blocks=[
            {"evidence_id": ev1, "source_type": "subtitle",
             "start_sec": 42.0, "text": "这个插件可以自动整理笔记"}
        ],
        focus={"liked_ups": ["好UP"]},
        up_name="好UP",
    )
    assert worker.last_dimensions.get("topic", 0) >= 80
    assert card.relevance_score > 0
    await worker.aclose()
