"""文字 worker 测试：**证据反查**是防编造的核心闸门。

任务书 §4：*"bridge 必须以真实已读取片段反查 ``evidence_id``、时间戳和引文，
截断过长文本并拒绝不存在的片段；豆包输出仅是**待审草案**。"*

任务书 §8：*"伪造证据/异常 JSON 被拒；豆包失败时不会把全文灌给 DSH"*。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from core.storage import Database
from core.text_worker import (
    PROMPT_VERSION,
    CandidateCard,
    TextWorker,
    chunk_evidence_blocks,
    chunk_text,
    input_sha256,
    verify_key_points,
)
from tests.conftest import make_config


@pytest.fixture
def evidence_setup(db: Database) -> dict[str, str]:
    """一个视频 + 两段真实证据。"""
    db.upsert_video(bvid="BV1test00001", aid=111, title="DSH 插件入门")
    ev1 = db.add_evidence(
        bvid="BV1test00001", source_type="subtitle",
        text="这个插件可以自动整理笔记", start_sec=42.0,
    )
    ev2 = db.add_evidence(
        bvid="BV1test00001", source_type="description",
        text="本视频介绍 DSH 的插件机制",
    )
    return {"ev1": ev1, "ev2": ev2}


def worker_with(db: Database, responses: list[Any], *, model: str = "doubao-test") -> TextWorker:
    """构造一个用 MockTransport 回复固定内容的 worker。"""
    queue_of_responses = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if not queue_of_responses:
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
        item = queue_of_responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)

    config = make_config(text_worker={"model": model, "api_key_env": "TEST_WORKER_KEY"})
    return TextWorker(db=db, config=config, client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ))


def model_reply(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 40},
    }


# ---------------------------------------------------------------------------
# 分块与哈希
# ---------------------------------------------------------------------------


def test_chunk_short_text_is_single_chunk() -> None:
    assert chunk_text("短文本") == ["短文本"]


def test_chunk_empty_returns_empty() -> None:
    assert chunk_text("") == []


def test_chunk_long_text_respects_limit() -> None:
    text = "\n".join("段落" * 50 for _ in range(200))
    chunks = chunk_text(text, max_chars=1000)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 1000 + 1


def test_input_hash_is_stable() -> None:
    assert input_sha256("abc") == input_sha256("abc")
    assert input_sha256("abc") != input_sha256("abd")


# ---------------------------------------------------------------------------
# ★ #2（Codex 审查）：长证据要**真正分块**，不能只取前 N 段
# ---------------------------------------------------------------------------


def test_chunk_evidence_blocks_splits_by_budget_and_keeps_order() -> None:
    blocks = [
        {"evidence_id": f"ev_{i:03d}", "source_type": "subtitle",
         "start_sec": float(i), "text": "字" * 300}
        for i in range(20)
    ]
    chunks, sampled = chunk_evidence_blocks(blocks, max_chars=1000, max_chunks=50)
    assert sampled == 0
    assert sum(len(chunk) for chunk in chunks) == 20
    assert all(sum(len(item["text"]) for item in chunk) <= 1000 for chunk in chunks)
    flattened = [item["evidence_id"] for chunk in chunks for item in chunk]
    assert flattened == [item["evidence_id"] for item in blocks], "顺序必须保持"


def test_chunk_evidence_blocks_splits_one_long_asr_block() -> None:
    """整篇 ASR 转写是一个超长块 → 切开后仍引用同一个 evidence_id。"""
    text = "转写内容" * 1250  # 5000 字
    chunks, sampled = chunk_evidence_blocks(
        [{"evidence_id": "ev_asr", "source_type": "asr", "start_sec": None, "text": text}],
        max_chars=1000,
    )
    assert sampled == 0
    assert len(chunks) == 5
    assert all(item["evidence_id"] == "ev_asr" for chunk in chunks for item in chunk)
    assert "".join(item["text"] for chunk in chunks for item in chunk) == text


def test_chunk_evidence_blocks_samples_beyond_chunk_cap() -> None:
    blocks = [
        {"evidence_id": f"ev_{i:03d}", "source_type": "subtitle", "text": "字" * 300}
        for i in range(12)
    ]
    chunks, sampled = chunk_evidence_blocks(blocks, max_chars=300, max_chunks=3)
    assert sampled == 12, "超过上限必须报告抽样前的总块数"
    assert len(chunks) == 3
    ids = [item["evidence_id"] for chunk in chunks for item in chunk]
    assert ids[0] == "ev_000" and ids[-1] == "ev_011", "首尾必须被保留"


@pytest.mark.parametrize("change", ["none", "title", "focus"])
async def test_analyze_chunks_long_evidence_and_caches_merged_result(
    db: Database, evidence_setup: dict[str, str], monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """长证据走分块；合并结果可缓存，且最后一段的要点不会丢。"""
    monkeypatch.setattr("core.text_worker.resolve_secret", lambda _n: "fake-key")
    db.upsert_video(bvid="BV1long00001", aid=222, title="长视频")
    rows = []
    for index in range(3):
        text = f"第{index}块内容。" + "填充" * 700
        rows.append(
            db.add_evidence(
                bvid="BV1long00001",
                source_type="subtitle",
                text=text,
                start_sec=float(index * 10),
            )
        )
    db.add_evidence(bvid="BV1long00001", source_type="subtitle",
                    text="最后一段的关键结论：证据反查必须能到达这里", start_sec=99.0)
    last = db.evidence_for_bvid("BV1long00001")[-1]
    blocks = [
        {"evidence_id": str(row["evidence_id"]), "source_type": str(row["source_type"]),
         "start_sec": row["start_sec"], "text": str(row["text"])}
        for row in db.evidence_for_bvid("BV1long00001")
    ]

    first_reply = {
        "key_points": [
            {"claim": "第一块要点", "evidence_id": rows[0],
             "short_quote": "第0块内容", "start_sec": 0.0}
        ],
        "scores": {"topic": 10, "usefulness": 10, "novelty": 10, "evidence": 10},
        "candidate_comment": "第一块评论",
    }
    last_reply = {
        "key_points": [
            {"claim": "最后一段的关键结论", "evidence_id": str(last["evidence_id"]),
             "short_quote": "最后一段的关键结论", "start_sec": 99.0}
        ],
        "scores": {"topic": 90, "usefulness": 90, "novelty": 90, "evidence": 90},
        "candidate_comment": "最后一段评论",
    }
    worker = worker_with(db, [model_reply(first_reply), model_reply(last_reply)])
    args: dict[str, Any] = {
        "bvid": "BV1long00001", "aid": 222, "title": "长视频", "source_url": "u",
        "evidence_level": "subtitle", "evidence_blocks": blocks,
        "focus": {"keywords": ["DSH"]},
    }
    if change == "title":
        args["title"] = "改过的标题"
    elif change == "focus":
        args["focus"] = {"keywords": ["别的东西"]}

    card = await worker.analyze(**args)
    assert "MODEL_INPUT_CHUNKS_2" in card.limitations
    assert card.relevance_score == 90, "合并后应取 topic 最高的那一块"
    assert card.candidate_comment == "最后一段评论"
    quotes = [point.short_quote for point in card.key_points]
    assert "最后一段的关键结论" in quotes, "最后一段的要点必须进入短卡"
    totals = db.text_job_totals()
    assert totals["prompt_tokens"] == 240, "两块 usage 必须相加"
    assert totals["completion_tokens"] == 80

    # 相同输入再跑一次：必须命中缓存，不能再调模型
    second = await worker.analyze(**args)
    assert "TEXT_WORKER_CACHE_HIT" in second.limitations
    assert second.relevance_score == 90
    assert second.candidate_comment == "最后一段评论"
    await worker.aclose()


async def test_analyze_partial_chunk_failure_marks_deep_review(
    db: Database, evidence_setup: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """一块失败不能把部分覆盖说成全文；保留成功块并强制深审。"""
    monkeypatch.setattr("core.text_worker.resolve_secret", lambda _n: "fake-key")
    db.upsert_video(bvid="BV1partial001", aid=333, title="部分失败")
    ids = []
    for index in range(2):
        ids.append(
            db.add_evidence(
                bvid="BV1partial001", source_type="subtitle",
                text=f"第{index}块真实内容" + "补" * 3000, start_sec=float(index),
            )
        )
    blocks = [
        {"evidence_id": str(row["evidence_id"]), "source_type": str(row["source_type"]),
         "start_sec": row["start_sec"], "text": str(row["text"])}
        for row in db.evidence_for_bvid("BV1partial001")
    ]
    ok_reply = {
        "key_points": [
            {"claim": "第一块", "evidence_id": ids[0], "short_quote": "第0块真实内容",
             "start_sec": 0.0}
        ],
        "scores": {"topic": 80, "usefulness": 80, "novelty": 80, "evidence": 80},
        "candidate_comment": "部分结果",
    }
    worker = worker_with(
        db,
        [model_reply(ok_reply), httpx.Response(500), httpx.Response(500)],
    )
    card = await worker.analyze(
        bvid="BV1partial001", aid=333, title="部分失败", source_url="u",
        evidence_level="subtitle", evidence_blocks=blocks,
    )
    assert card.needs_deep_review is True
    assert "MODEL_CHUNK_FAILED_1" in card.limitations
    assert [point.short_quote for point in card.key_points] == ["第0块真实内容"]
    await worker.aclose()



# ---------------------------------------------------------------------------
# 证据反查：**防编造**
# ---------------------------------------------------------------------------


def test_valid_point_passes(db: Database, evidence_setup: dict[str, str]) -> None:
    points, dropped, reasons = verify_key_points(
        db,
        bvid="BV1test00001",
        raw_points=[
            {
                "claim": "插件能自动整理笔记",
                "evidence_id": evidence_setup["ev1"],
                "start_sec": 42.0,
                "short_quote": "自动整理笔记",
            }
        ],
    )
    assert dropped == 0
    assert len(points) == 1
    assert points[0].verified is True
    assert reasons == []


def test_nonexistent_evidence_id_rejected(db: Database, evidence_setup: dict[str, str]) -> None:
    """★ 模型自报"我看过"不能凭空生成证据。"""
    points, dropped, reasons = verify_key_points(
        db,
        bvid="BV1test00001",
        raw_points=[
            {
                "claim": "编造的结论",
                "evidence_id": "ev_deadbeef0000",
                "start_sec": 1.0,
                "short_quote": "编造的引文",
            }
        ],
    )
    assert points == []
    assert dropped == 1
    assert "证据不存在" in reasons[0]


def test_evidence_from_other_bvid_rejected(db: Database, evidence_setup: dict[str, str]) -> None:
    """★ 证据必须**属于同一 BV**。"""
    db.upsert_video(bvid="BV1other0002", aid=222)
    other = db.add_evidence(
        bvid="BV1other0002", source_type="subtitle", text="别的视频的字幕", start_sec=1.0
    )
    points, dropped, reasons = verify_key_points(
        db,
        bvid="BV1test00001",
        raw_points=[
            {
                "claim": "张冠李戴",
                "evidence_id": other,
                "start_sec": 1.0,
                "short_quote": "别的视频的字幕",
            }
        ],
    )
    assert points == []
    assert dropped == 1
    assert "不属于" in reasons[0]


def test_fabricated_quote_rejected(db: Database, evidence_setup: dict[str, str]) -> None:
    """★ 引文在原文中找不到 → 拒绝（这是"编造"最常见的形态）。"""
    points, dropped, reasons = verify_key_points(
        db,
        bvid="BV1test00001",
        raw_points=[
            {
                "claim": "看起来合理但引文是编的",
                "evidence_id": evidence_setup["ev1"],
                "start_sec": 42.0,
                "short_quote": "这句原文里根本没有",
            }
        ],
    )
    assert points == []
    assert dropped == 1
    assert "找不到" in reasons[0]


def test_wrong_timestamp_rejected(db: Database, evidence_setup: dict[str, str]) -> None:
    """★ 时间戳与证据片段时间不符（容差外）→ 拒绝。"""
    points, dropped, reasons = verify_key_points(
        db,
        bvid="BV1test00001",
        raw_points=[
            {
                "claim": "时间戳被改了",
                "evidence_id": evidence_setup["ev1"],
                "start_sec": 999.0,
                "short_quote": "自动整理笔记",
            }
        ],
    )
    assert points == []
    assert dropped == 1
    assert "时间戳不符" in reasons[0]


def test_timestamp_within_tolerance_accepted(db: Database, evidence_setup: dict[str, str]) -> None:
    points, dropped, _reasons = verify_key_points(
        db,
        bvid="BV1test00001",
        raw_points=[
            {
                "claim": "轻微偏差可接受",
                "evidence_id": evidence_setup["ev1"],
                "start_sec": 43.5,
                "short_quote": "自动整理笔记",
            }
        ],
    )
    assert dropped == 0
    assert len(points) == 1


def test_missing_quote_rejected(db: Database, evidence_setup: dict[str, str]) -> None:
    points, dropped, reasons = verify_key_points(
        db,
        bvid="BV1test00001",
        raw_points=[{"claim": "无引文", "evidence_id": evidence_setup["ev1"], "start_sec": 42.0}],
    )
    assert points == []
    assert "缺少 short_quote" in reasons[0]


def test_missing_claim_or_id_rejected(db: Database, evidence_setup: dict[str, str]) -> None:
    points, dropped, _ = verify_key_points(
        db,
        bvid="BV1test00001",
        raw_points=[{"claim": "", "evidence_id": evidence_setup["ev1"], "short_quote": "自动"}],
    )
    assert points == []
    assert dropped == 1


def test_mixed_points_keep_only_valid(db: Database, evidence_setup: dict[str, str]) -> None:
    """部分通过：只保留可反查的，其余丢弃并计数。"""
    points, dropped, reasons = verify_key_points(
        db,
        bvid="BV1test00001",
        raw_points=[
            {
                "claim": "好的",
                "evidence_id": evidence_setup["ev1"],
                "start_sec": 42.0,
                "short_quote": "自动整理笔记",
            },
            {"claim": "坏的", "evidence_id": "ev_nope", "short_quote": "x"},
        ],
    )
    assert len(points) == 1
    assert dropped == 1
    assert len(reasons) == 1


def test_whitespace_normalized_in_quote_match(db: Database, evidence_setup: dict[str, str]) -> None:
    """引文与原文的空白差异不应误判为编造。"""
    points, dropped, _ = verify_key_points(
        db,
        bvid="BV1test00001",
        raw_points=[
            {
                "claim": "空白差异",
                "evidence_id": evidence_setup["ev1"],
                "start_sec": 42.0,
                "short_quote": "自动 整理  笔记",
            }
        ],
    )
    assert dropped == 0
    assert len(points) == 1


# ---------------------------------------------------------------------------
# worker 端到端（Mock 模型）
# ---------------------------------------------------------------------------


def test_is_usable_reports_missing_model(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(text_worker={"enabled": True, "model": ""})
    worker = TextWorker(db=db, config=config)
    assert worker.is_usable() == (False, "TEXT_WORKER_NOT_CONFIGURED")


def test_is_usable_reports_missing_key(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    from core import secrets

    monkeypatch.setattr(secrets, "resolve_secret", lambda _name: None)
    monkeypatch.setattr("core.text_worker.resolve_secret", lambda _name: None)
    config = make_config(text_worker={"enabled": True, "model": "doubao-test"})
    worker = TextWorker(db=db, config=config)
    assert worker.is_usable() == (False, "TEXT_WORKER_KEY_MISSING")


async def test_analyze_returns_verified_card(
    db: Database, evidence_setup: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("core.text_worker.resolve_secret", lambda _n: "fake-key")
    worker = worker_with(
        db,
        [
            model_reply(
                {
                    "key_points": [
                        {
                            "claim": "插件能自动整理笔记",
                            "evidence_id": evidence_setup["ev1"],
                            "start_sec": 42.0,
                            "short_quote": "自动整理笔记",
                        }
                    ],
                    "relevance_score": 88,
                    "uncertainties": [],
                    "candidate_comment": "草稿建议",
                    "needs_deep_review": False,
                }
            )
        ],
    )
    card = await worker.analyze(
        bvid="BV1test00001",
        aid=111,
        title="DSH 插件入门",
        source_url="https://www.bilibili.com/video/BV1test00001/",
        evidence_level="subtitle",
        evidence_blocks=[
            {"evidence_id": evidence_setup["ev1"], "source_type": "subtitle",
             "start_sec": 42.0, "text": "这个插件可以自动整理笔记"}
        ],
    )
    assert isinstance(card, CandidateCard)
    assert card.relevance_score == 88
    assert len(card.key_points) == 1
    assert card.key_points[0].verified is True
    assert card.needs_deep_review is False


async def test_analyze_flags_forgery(
    db: Database, evidence_setup: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 模型编造引文 → 要点被拒 + ``needs_deep_review``。"""
    monkeypatch.setattr("core.text_worker.resolve_secret", lambda _n: "fake-key")
    worker = worker_with(
        db,
        [
            model_reply(
                {
                    "key_points": [
                        {
                            "claim": "编造的",
                            "evidence_id": evidence_setup["ev1"],
                            "start_sec": 42.0,
                            "short_quote": "原文绝对没有这句",
                        }
                    ],
                    "relevance_score": 95,
                    "candidate_comment": "看起来很有说服力",
                }
            )
        ],
    )
    card = await worker.analyze(
        bvid="BV1test00001",
        aid=111,
        title="t",
        source_url="u",
        evidence_level="subtitle",
        evidence_blocks=[
            {"evidence_id": evidence_setup["ev1"], "source_type": "subtitle",
             "start_sec": 42.0, "text": "这个插件可以自动整理笔记"}
        ],
    )
    assert card.key_points == []
    assert card.dropped_points == 1
    assert card.needs_deep_review is True
    assert any("REJECTED" in lim for lim in card.limitations)


async def test_analyze_rejects_invalid_json(
    db: Database, evidence_setup: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 非合法 JSON（通常是截断）→ 标 needs_deep_review，**不把全文给 DSH**。"""
    monkeypatch.setattr("core.text_worker.resolve_secret", lambda _n: "fake-key")
    bad = httpx.Response(
        200,
        json={"choices": [{"message": {"content": '{"key_points": [{"claim": "截断'}}]},
    )
    worker = worker_with(db, [bad, bad])
    card = await worker.analyze(
        bvid="BV1test00001",
        aid=111,
        title="t",
        source_url="u",
        evidence_level="subtitle",
        evidence_blocks=[
            {"evidence_id": evidence_setup["ev1"], "source_type": "subtitle",
             "start_sec": 42.0, "text": "原文"}
        ],
    )
    assert card.needs_deep_review is True
    assert "TEXT_WORKER_INVALID_JSON" in card.limitations
    # ★ 关键：没有把原文塞进返回
    assert card.key_points == []
    serialized = json.dumps(card.to_payload(), ensure_ascii=False)
    assert "原文" not in serialized


async def test_analyze_handles_timeout(
    db: Database, evidence_setup: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("core.text_worker.resolve_secret", lambda _n: "fake-key")
    worker = worker_with(db, [httpx.ConnectTimeout("t"), httpx.ConnectTimeout("t")])
    card = await worker.analyze(
        bvid="BV1test00001",
        aid=111,
        title="t",
        source_url="u",
        evidence_level="subtitle",
        evidence_blocks=[
            {"evidence_id": evidence_setup["ev1"], "source_type": "subtitle",
             "start_sec": 42.0, "text": "原文"}
        ],
    )
    assert card.needs_deep_review is True
    assert "TEXT_WORKER_TIMEOUT" in card.limitations


async def test_analyze_marks_unconfigured_worker(
    db: Database, evidence_setup: dict[str, str]
) -> None:
    config = make_config(text_worker={"enabled": True, "model": ""})
    worker = TextWorker(db=db, config=config)
    card = await worker.analyze(
        bvid="BV1test00001", aid=111, title="t", source_url="u",
        evidence_level="subtitle",
        evidence_blocks=[{"evidence_id": evidence_setup["ev1"], "source_type": "subtitle",
                          "start_sec": 42.0, "text": "原文"}],
    )
    assert "TEXT_WORKER_NOT_CONFIGURED" in card.limitations


async def test_analyze_without_evidence_marks_review(db: Database) -> None:
    config = make_config(text_worker={"enabled": True, "model": "doubao-test"})
    worker = TextWorker(db=db, config=config)
    card = await worker.analyze(
        bvid="BV1test00001", aid=111, title="t", source_url="u",
        evidence_level="unavailable", evidence_blocks=[],
    )
    assert card.needs_deep_review is True
    assert "NO_EVIDENCE" in card.limitations


async def test_worker_never_sees_credentials_in_prompt(
    db: Database, evidence_setup: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 发给豆包的请求体里不得含凭据。"""
    monkeypatch.setattr("core.text_worker.resolve_secret", lambda _n: "fake-key")
    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.content)
        return httpx.Response(200, json=model_reply({"key_points": [], "relevance_score": 0}))

    config = make_config(text_worker={"model": "doubao-test", "api_key_env": "TEST_WORKER_KEY"})
    worker = TextWorker(
        db=db, config=config,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False),
    )
    await worker.analyze(
        bvid="BV1test00001", aid=111, title="t", source_url="u",
        evidence_level="subtitle",
        evidence_blocks=[{"evidence_id": evidence_setup["ev1"], "source_type": "subtitle",
                          "start_sec": 42.0, "text": "公开字幕内容"}],
    )
    body = captured[0].decode("utf-8")
    assert "SESSDATA" not in body
    assert "bili_jct" not in body


async def test_text_job_records_usage_tokens(
    db: Database, evidence_setup: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("core.text_worker.resolve_secret", lambda _n: "fake-key")
    worker = worker_with(db, [model_reply({"key_points": [], "relevance_score": 10})])
    await worker.analyze(
        bvid="BV1test00001", aid=111, title="t", source_url="u",
        evidence_level="subtitle",
        evidence_blocks=[{"evidence_id": evidence_setup["ev1"], "source_type": "subtitle",
                          "start_sec": 42.0, "text": "原文"}],
    )
    totals = db.text_job_totals()
    assert totals["prompt_tokens"] == 120
    assert totals["completion_tokens"] == 40


async def test_cache_key_parts_recorded(
    db: Database, evidence_setup: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("core.text_worker.resolve_secret", lambda _n: "fake-key")
    worker = worker_with(db, [model_reply({"key_points": [], "relevance_score": 10})])
    await worker.analyze(
        bvid="BV1test00001", aid=111, title="t", source_url="u",
        evidence_level="subtitle",
        evidence_blocks=[{"evidence_id": evidence_setup["ev1"], "source_type": "subtitle",
                          "start_sec": 42.0, "text": "原文"}],
    )
    row = db.query_one("SELECT * FROM text_jobs ORDER BY created_at DESC LIMIT 1")
    assert row is not None
    assert str(row["model_id"]) == "doubao-test"
    assert str(row["prompt_version"]) == PROMPT_VERSION
    assert len(str(row["input_sha256"])) == 64
    assert str(row["input_sha256"]) != input_sha256("原文"), "缓存不能只按证据正文建立"


@pytest.mark.parametrize("change", ["none", "title", "keywords", "rules", "likes", "dislikes"])
async def test_cache_tracks_model_prompt_context(
    db: Database,
    evidence_setup: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """相同输入复用缓存；主人关注点或标题改变时必须重新分析。"""
    monkeypatch.setattr("core.text_worker.resolve_secret", lambda _n: "fake-key")
    point = {
        "claim": "插件能自动整理笔记", "evidence_id": evidence_setup["ev1"],
        "start_sec": 42.0, "short_quote": "自动整理笔记",
    }
    worker = worker_with(db, [
        model_reply({"key_points": [point], "relevance_score": 10}),
        model_reply({"key_points": [point], "relevance_score": 90}),
    ])
    args: dict[str, Any] = {
        "bvid": "BV1test00001", "aid": 111, "title": "原始标题", "source_url": "u",
        "evidence_level": "subtitle",
        "evidence_blocks": [{
            "evidence_id": evidence_setup["ev1"], "source_type": "subtitle",
            "start_sec": 42.0, "text": "这个插件可以自动整理笔记",
        }],
        "focus": {"keywords": ["原先关注"]},
    }
    first = await worker.analyze(**args)
    assert first.needs_deep_review is False
    assert first.relevance_score == 10
    if change == "title":
        args["title"] = "更新后的标题"
    elif change != "none":
        args["focus"] = {**args["focus"], change: ["新的关注规则"]}
    second = await worker.analyze(**args)
    if change == "none":
        assert "TEXT_WORKER_CACHE_HIT" in second.limitations
        assert second.relevance_score == 10
    else:
        assert "TEXT_WORKER_CACHE_HIT" not in second.limitations
        assert second.relevance_score == 90
    third = await worker.analyze(**args)
    assert "TEXT_WORKER_CACHE_HIT" in third.limitations
    assert third.relevance_score == second.relevance_score
    totals = db.text_job_totals()
    assert totals["prompt_tokens"] == (120 if change == "none" else 240)
    assert totals["completion_tokens"] == (40 if change == "none" else 80)
    await worker.aclose()


# ---------------------------------------------------------------------------
# JSON 解析健壮性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        '好的，这是结果：{"a": 1}',
        '{"a": 1}\n希望有帮助',
    ],
)
def test_json_extraction_variants(content: str) -> None:
    from core.text_worker import _parse_json_object

    assert _parse_json_object(content) == {"a": 1}


@pytest.mark.parametrize(
    "content",
    ["不是 JSON", "", "{未闭合", "[1,2,3]", '{"a": '],
)
def test_json_extraction_rejects_bad(content: str) -> None:
    from core.text_worker import _parse_json_object

    assert _parse_json_object(content) is None


def test_json_extraction_handles_nested_braces_in_strings() -> None:
    from core.text_worker import _parse_json_object

    content = '前缀 {"claim": "里面有 } 和 { 的文本", "n": 1} 后缀'
    assert _parse_json_object(content) == {"claim": "里面有 } 和 { 的文本", "n": 1}


# ---------------------------------------------------------------------------
# 候选包结构（任务书 §4 的字段语义）
# ---------------------------------------------------------------------------


def test_candidate_payload_has_required_fields() -> None:
    card = CandidateCard(
        bvid="BV1a", aid=1, title="t", source_url="u", evidence_level="subtitle"
    )
    payload = card.to_payload()
    for field in (
        "bvid", "aid", "title", "source_url", "evidence_level", "key_points",
        "relevance_score", "uncertainties", "candidate_comment",
        "needs_deep_review", "visual_reviewed", "limitations",
    ):
        assert field in payload, field


def test_candidate_does_not_carry_raw_text() -> None:
    """★ 短候选包不得携带原文。"""
    card = CandidateCard(
        bvid="BV1a", aid=1, title="t", source_url="u", evidence_level="subtitle"
    )
    serialized = json.dumps(card.to_payload(), ensure_ascii=False)
    assert "text" not in serialized
