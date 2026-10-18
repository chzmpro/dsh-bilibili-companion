"""编排层与日报测试（任务书 §6、§7 任务 3/4/6）。

覆盖：
* 短候选包**不含原文**
* 候选数上限（≤3）与批字符上限
* 同一视频重复运行**不重复建笔记**
* 字幕缺失**降级**而不编造
* 日报**区分**实读 / 缓存 / 模拟，且无候选时给出"今日无值得推荐内容"
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from core.bilibili import BilibiliReader
from core.pipeline import (
    LEVEL_DESCRIPTION_ONLY,
    LEVEL_SUBTITLE,
    Pipeline,
)
from core.ratelimit import RequestQueue
from core.report import DigestInput, DigestWriter
from core.storage import Database
from tests.conftest import FakeClock, build_client, make_config, nav_payload, ok_payload

SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"


def recent_pubdate(offset_days: int = 0) -> int:
    """搜索发现会按 `published_within_days` 过滤，样本必须用**新的**时间戳。"""
    from core.storage import now_ts

    return now_ts() - offset_days * 86400
VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
PLAYER_URL = "https://api.bilibili.com/x/player/wbi/v2"


def make_reader(db: Database, handler: Any, clock: FakeClock) -> BilibiliReader:
    config = make_config(bilibili_requests={"min_interval_seconds": 1.0})
    queue = RequestQueue(
        db=db, config=config, client=build_client(handler), clock=clock, sleep=clock.sleep
    )
    return BilibiliReader(queue=queue, cookies={})


def make_pipeline(db: Database, reader: BilibiliReader, clock: FakeClock) -> Pipeline:
    config = make_config(bilibili_requests={"min_interval_seconds": 1.0})
    queue = reader.queue
    return Pipeline(db=db, config=config, reader=reader, queue=queue, worker=None)


def default_handler(
    *,
    subtitles: bool = True,
    description: str = "这是视频简介文本",
    search_items: list[dict[str, Any]] | None = None,
    comments: list[dict[str, Any]] | None = None,
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "search/type" in url:
            items = search_items if search_items is not None else [
                {
                    "bvid": "BV1test00001", "aid": 111, "title": "DSH 插件入门",
                    "author": "某UP", "mid": 2002, "pubdate": recent_pubdate(1),
                    "duration": "12:34", "description": description,
                    "pic": "//i0.hdslb.com/x.jpg",
                }
            ]
            return httpx.Response(200, json=ok_payload({"result": items}))
        if "/x/web-interface/view" in url:
            return httpx.Response(
                200,
                json=ok_payload(
                    {
                        "bvid": "BV1test00001", "aid": 111, "cid": 222,
                        "title": "DSH 插件入门", "desc": description,
                        "pubdate": 1_700_000_000, "duration": 754,
                        "owner": {"mid": 2002, "name": "某UP"},
                        "stat": {"reply": 10},
                        "pages": [{"cid": 222}],
                    }
                ),
            )
        if "nav" in url:
            return httpx.Response(200, json=nav_payload())
        if "player" in url:
            subs = (
                [{"lan": "zh-CN", "lan_doc": "中文（自动生成）",
                  "subtitle_url": "//aisubtitle.hdslb.com/bfs/ai_subtitle/x.json"}]
                if subtitles else []
            )
            return httpx.Response(200, json=ok_payload({"subtitle": {"subtitles": subs}}))
        if "aisubtitle" in url:
            return httpx.Response(
                200,
                json={"body": [
                    {"from": 1.0, "to": 3.0, "content": "这个插件可以自动整理笔记"},
                    {"from": 3.0, "to": 6.0, "content": "它把字幕压成要点"},
                ]},
            )
        if "reply/wbi/main" in url:
            return httpx.Response(
                200,
                json=ok_payload(
                    {
                        "replies": comments if comments is not None else [
                            {"rpid_str": "900", "root_str": "900", "parent_str": "0",
                             "member": {"mid": 5555, "uname": "路人甲"},
                             "content": {"message": "这个插件看起来不错"},
                             "like": 3, "ctime": 1_700_000_500, "rcount": 0}
                        ],
                        "cursor": {"next": 0, "is_end": True, "all_count": 1},
                    }
                ),
            )
        return httpx.Response(404, json={"code": -404, "message": "no route"})

    return handler


# ---------------------------------------------------------------------------
# 读取与证据登记
# ---------------------------------------------------------------------------


async def test_read_video_registers_evidence_and_returns_short_card(
    db: Database, clock: FakeClock
) -> None:
    reader = make_reader(db, default_handler(), clock)
    pipeline = make_pipeline(db, reader, clock)

    card, blocks = await pipeline.read_video("BV1test00001")

    assert card.bvid == "BV1test00001"
    assert card.evidence_level == LEVEL_SUBTITLE
    assert blocks, "必须登记证据"
    # ★ 短候选包**不含**原文
    serialized = json.dumps(card.to_payload(), ensure_ascii=False)
    assert "这个插件可以自动整理笔记" not in serialized
    assert "text" not in serialized

    # 证据确实入库，且可反查
    for block in blocks:
        row = db.get_evidence(block["evidence_id"])
        assert row is not None
        assert str(row["bvid"]) == "BV1test00001"


async def test_subtitle_segments_keep_timestamps(db: Database, clock: FakeClock) -> None:
    reader = make_reader(db, default_handler(), clock)
    pipeline = make_pipeline(db, reader, clock)
    _card, blocks = await pipeline.read_video("BV1test00001")

    subtitle_blocks = [b for b in blocks if b["source_type"] == "subtitle"]
    assert subtitle_blocks
    assert subtitle_blocks[0]["start_sec"] == 1.0
    row = db.get_evidence(subtitle_blocks[0]["evidence_id"])
    assert float(row["start_sec"]) == 1.0


async def test_missing_subtitle_degrades_not_fabricates(
    db: Database, clock: FakeClock
) -> None:
    """★ 字幕不可用 → 降级为 description_only 且**降低评分**，不编造字幕。"""
    reader = make_reader(db, default_handler(subtitles=False), clock)
    pipeline = make_pipeline(db, reader, clock)
    card, _blocks = await pipeline.read_video("BV1test00001")

    assert card.evidence_level == LEVEL_DESCRIPTION_ONLY
    assert card.relevance_score <= 40
    assert any("DESCRIPTION_ONLY" in lim for lim in card.limitations)
    assert any("SUBTITLE" in lim for lim in card.limitations)
    # 不得出现字幕证据
    rows = db.evidence_for_bvid("BV1test00001")
    assert all(str(r["source_type"]) != "subtitle" for r in rows)


async def test_description_only_uncertainty_text_is_honest(
    db: Database, clock: FakeClock
) -> None:
    """★ 仅简介时不得写成"视频讲到"。"""
    reader = make_reader(db, default_handler(subtitles=False), clock)
    pipeline = make_pipeline(db, reader, clock)
    card, _ = await pipeline.read_video("BV1test00001")
    joined = " ".join(card.uncertainties)
    assert "仅简介可用" in joined


async def test_status_advances_to_read(db: Database, clock: FakeClock) -> None:
    reader = make_reader(db, default_handler(), clock)
    pipeline = make_pipeline(db, reader, clock)
    await pipeline.read_video("BV1test00001")
    assert str(db.get_video("BV1test00001")["status"]) == "read"


async def test_repeat_read_does_not_duplicate_evidence(
    db: Database, clock: FakeClock
) -> None:
    """同一视频重复读取不产生重复证据（幂等），也不重复建笔记。"""
    reader = make_reader(db, default_handler(), clock)
    pipeline = make_pipeline(db, reader, clock)

    _c1, blocks1 = await pipeline.read_video("BV1test00001")
    _c2, blocks2 = await pipeline.read_video("BV1test00001")
    assert [b["evidence_id"] for b in blocks1] == [b["evidence_id"] for b in blocks2]
    rows = db.evidence_for_bvid("BV1test00001")
    assert len(rows) == len(blocks1)


# ---------------------------------------------------------------------------
# 发现与批次
# ---------------------------------------------------------------------------


async def test_discover_dedupes_bvids(db: Database, clock: FakeClock) -> None:
    reader = make_reader(db, default_handler(), clock)
    pipeline = make_pipeline(db, reader, clock)

    first = await pipeline.discover(keywords=["DSH"])
    assert first.new_count == 1
    second = await pipeline.discover(keywords=["DSH"])
    assert second.new_count == 0
    assert second.seen_count == 1


async def test_prepare_batch_caps_cards_at_three(db: Database, clock: FakeClock) -> None:
    """★ 任务书 §4：prepare_batch 最多 3 个短候选包。"""
    items = [
        {
            "bvid": f"BV1multi{i:05d}", "aid": 100 + i, "title": f"视频 {i}",
            "author": "UP", "mid": 1, "pubdate": recent_pubdate(1) + i,
            "duration": "10:00", "description": "简介", "pic": "//x/y.jpg",
        }
        for i in range(6)
    ]
    handler = default_handler(search_items=items)
    reader = make_reader(db, handler, clock)
    pipeline = make_pipeline(db, reader, clock)

    batch = await pipeline.prepare_batch(keywords=["DSH"])
    assert len(batch.candidates) <= 3, "不得超过 3 个短候选包"
    assert batch.limitations, "必须说明批次限制"


async def test_batch_respects_char_budget(db: Database, clock: FakeClock) -> None:
    """整批不超过 ``max_batch_chars``。"""
    items = [
        {
            "bvid": f"BV1big{i:07d}", "aid": 200 + i, "title": f"很长的标题 {i}" * 10,
            "author": "UP" * 20, "mid": 1, "pubdate": recent_pubdate(1) + i,
            "duration": "10:00", "description": "简介" * 200, "pic": "//x/y.jpg",
        }
        for i in range(6)
    ]
    handler = default_handler(search_items=items)
    reader = make_reader(db, handler, clock)
    pipeline = make_pipeline(db, reader, clock)

    batch = await pipeline.prepare_batch(keywords=["DSH"])
    payload = batch.to_payload(max_batch_chars=1200)
    assert payload["batch_chars"] == len(json.dumps(payload, ensure_ascii=False))
    assert len(json.dumps(payload, ensure_ascii=False)) <= 1200, "整包 JSON 必须受预算约束"


# ---------------------------------------------------------------------------
# ★ #3（Codex 审查）：字符预算必须按完整返回对象计算，首卡也受限
# ---------------------------------------------------------------------------


def test_budget_limits_complete_payload_even_with_huge_first_card() -> None:
    """首张卡不能无条件放行；整包长度（不是候选卡之和）才是预算口径。"""
    from core.pipeline import BatchResult
    from core.text_worker import CandidateCard

    huge = CandidateCard(
        bvid="BV1huge00001", aid=1, title="标" * 3000, source_url="u",
        evidence_level="subtitle", candidate_comment="评" * 5000,
        uncertainties=["疑" * 500] * 10,
    )
    payload = BatchResult(candidates=[huge]).to_payload(max_batch_chars=600)
    serialized = json.dumps(payload, ensure_ascii=False)
    assert len(serialized) <= 600
    assert payload["batch_chars"] == len(serialized)
    # 放不下就必须**整张降级/跳过并报数**，不能假装覆盖
    if not payload["candidates"]:
        assert payload["truncated"].get("candidates") == 1


def test_budget_counts_evidence_ids_and_reports_truncation() -> None:
    """1000 个 evidence_id 不能绕过预算，也不能静默消失。"""
    from core.pipeline import BatchResult

    batch = BatchResult(evidence_ids=[f"ev_{i:012d}" for i in range(1000)])
    payload = batch.to_payload(max_batch_chars=500)
    serialized = json.dumps(payload, ensure_ascii=False)
    assert len(serialized) <= 500
    assert payload["batch_chars"] == len(serialized)
    assert payload["truncated"]["evidence_ids"] > 0
    assert payload["truncated"]["evidence_ids"] + len(payload["evidence_ids"]) == 1000


def test_budget_truncation_is_stable_and_does_not_mutate() -> None:
    """同一对象重复序列化结果稳定；**不许**往 limitations 里追加。"""
    from core.pipeline import BatchResult
    from core.text_worker import CandidateCard

    batch = BatchResult(
        candidates=[
            CandidateCard(bvid=f"BV1{k}", aid=k, title="题" * 600, source_url="u",
                          evidence_level="subtitle")
            for k in range(3)
        ],
        limitations=["ORIGINAL"],
    )
    first = batch.to_payload(max_batch_chars=700)
    second = batch.to_payload(max_batch_chars=700)
    assert first == second
    assert batch.limitations == ["ORIGINAL"]
    assert json.dumps(first, ensure_ascii=False) == json.dumps(second, ensure_ascii=False)


async def test_subtitle_registers_every_segment_not_just_first_40(
    db: Database, clock: FakeClock
) -> None:
    """★ #2（Codex 审查）：长视频后半段不能被静默丢掉。

    * 全部片段都要入库（否则 `read_evidence_excerpt` 也取不回）；
    * 分块后的模型输入必须能覆盖到最后一段。
    """
    segments = [
        {"from": float(i), "to": float(i + 1), "content": f"第{i}段内容"}
        for i in range(100)
    ]
    segments[-1]["content"] = "最后一段的关键结论：证据反查必须能到达这里"
    base = default_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if "aisubtitle" in str(request.url):
            return httpx.Response(200, json={"body": segments})
        return base(request)

    reader = make_reader(db, handler, clock)
    pipeline = make_pipeline(db, reader, clock)
    await pipeline.discover(keywords=["DSH"])
    _card, blocks = await pipeline.read_video("BV1test00001")

    rows = db.evidence_for_bvid("BV1test00001")
    assert len(rows) == 101, "简介 1 条 + 字幕 100 段都要入库"
    assert "最后一段的关键结论：证据反查必须能到达这里" in {
        str(row["text"]) for row in rows
    }

    from core.text_worker import chunk_evidence_blocks

    chunks, sampled = chunk_evidence_blocks(blocks)
    assert sampled == 0, "100 段不应该触发抽样"
    assert any(
        "最后一段的关键结论" in str(block["text"])
        for chunk in chunks
        for block in chunk
    ), "最后一段必须进入某一分块，而不是被截掉"


async def test_skipped_videos_are_recorded_not_silently_dropped(
    db: Database, clock: FakeClock
) -> None:
    """★ 被筛掉的视频要记录，供回看（任务书 §2.1.3）。"""
    items = [
        {
            "bvid": f"BV1skip{i:06d}", "aid": 300 + i, "title": f"低分视频 {i}",
            "author": "UP", "mid": 1, "pubdate": recent_pubdate(1) + i,
            "duration": "10:00", "description": "简介", "pic": "//x/y.jpg",
        }
        for i in range(5)
    ]
    handler = default_handler(search_items=items)
    reader = make_reader(db, handler, clock)
    pipeline = make_pipeline(db, reader, clock)

    batch = await pipeline.prepare_batch(keywords=["DSH"])
    # 6 个视频但只读 4 个（max_full_reads_per_run），至少有一个被记入 skipped
    assert batch.skipped, "超出批次上限的视频必须被记录，而不是静默丢弃"
    reasons = {s["reason"] for s in batch.skipped}
    assert any(r.startswith("OVER_BATCH_LIMIT") or r.startswith("LOW_SCORE") for r in reasons)


# ---------------------------------------------------------------------------
# 分析与草稿
# ---------------------------------------------------------------------------


async def test_save_analysis_requires_valid_evidence(
    db: Database, clock: FakeClock
) -> None:
    reader = make_reader(db, default_handler(), clock)
    pipeline = make_pipeline(db, reader, clock)
    _card, blocks = await pipeline.read_video("BV1test00001")

    ok, code, draft_id = pipeline.save_analysis(
        bvid="BV1test00001",
        evidence_ids=["ev_fake"],
        score_total=80,
        summary="总结",
    )
    assert ok is False
    assert code == "EVIDENCE_NOT_FOUND"
    assert draft_id is None

    eid = blocks[0]["evidence_id"]
    ok2, code2, draft_id2 = pipeline.save_analysis(
        bvid="BV1test00001",
        evidence_ids=[eid],
        score_total=80,
        summary="总结",
        draft_text="一条具体评论",
    )
    assert ok2 is True
    assert code2 == "OK"
    assert draft_id2 is not None


async def test_save_analysis_same_bvid_twice_does_not_duplicate_note(
    db: Database, clock: FakeClock
) -> None:
    """★ 同一视频重复运行不会重复创建笔记（notes 以 bvid 为主键）。"""
    reader = make_reader(db, default_handler(), clock)
    pipeline = make_pipeline(db, reader, clock)
    _card, blocks = await pipeline.read_video("BV1test00001")
    eid = blocks[0]["evidence_id"]

    for _ in range(3):
        pipeline.save_analysis(
            bvid="BV1test00001", evidence_ids=[eid], score_total=70, summary="同一份"
        )
    rows = db.query("SELECT * FROM notes WHERE bvid = 'BV1test00001'")
    assert len(rows) == 1


async def test_save_analysis_without_draft_text_creates_no_draft(
    db: Database, clock: FakeClock
) -> None:
    reader = make_reader(db, default_handler(), clock)
    pipeline = make_pipeline(db, reader, clock)
    _card, blocks = await pipeline.read_video("BV1test00001")

    ok, _code, draft_id = pipeline.save_analysis(
        bvid="BV1test00001", evidence_ids=[blocks[0]["evidence_id"]],
        score_total=50, summary="只记笔记",
    )
    assert ok is True
    assert draft_id is None
    assert db.list_drafts() == []


async def test_note_markdown_export_contains_bv_and_evidence(
    db: Database, clock: FakeClock, tmp_path: Path
) -> None:
    reader = make_reader(db, default_handler(), clock)
    pipeline = make_pipeline(db, reader, clock)
    _card, blocks = await pipeline.read_video("BV1test00001")
    eid = blocks[0]["evidence_id"]
    pipeline.save_analysis(
        bvid="BV1test00001", evidence_ids=[eid], score_total=77,
        summary="摘要内容", facts=["事实一"], inferences=["推断一"], doubts=["疑点一"],
    )

    out = pipeline.export_note_markdown("BV1test00001", tmp_path / "notes")
    assert out is not None
    assert out.name == "note-BV1test00001.md"
    text = out.read_text(encoding="utf-8")
    assert "BV1test00001" in text
    assert eid in text
    assert "事实一" in text
    assert "疑点一" in text


# ---------------------------------------------------------------------------
# 讨论区
# ---------------------------------------------------------------------------


async def test_get_discussion_registers_comments_as_evidence(
    db: Database, clock: FakeClock
) -> None:
    reader = make_reader(db, default_handler(), clock)
    pipeline = make_pipeline(db, reader, clock)
    db.upsert_video(bvid="BV1test00001", aid=111, title="t")

    page, registered = await pipeline.get_discussion(bvid="BV1test00001", limit=20)
    assert registered >= 1
    assert page.replies
    rows = db.evidence_for_bvid("BV1test00001")
    assert any(str(r["source_type"]) == "comment" for r in rows)


async def test_discussion_marks_seen_replies(db: Database, clock: FakeClock) -> None:
    reader = make_reader(db, default_handler(), clock)
    pipeline = make_pipeline(db, reader, clock)
    db.upsert_video(bvid="BV1test00001", aid=111)
    await pipeline.get_discussion(bvid="BV1test00001")
    assert db.list_seen_replies()


# ---------------------------------------------------------------------------
# 日报
# ---------------------------------------------------------------------------


def test_digest_lists_recommendations(db: Database, tmp_path: Path) -> None:
    writer = DigestWriter(db=db, out_dir=tmp_path / "digests")
    path = writer.write(
        DigestInput(
            run_id="run_x",
            trigger_source="manual",
            keywords=["DSH"],
            candidates_n=3,
            read_n=2,
            drafts_n=1,
            recommended=[
                {
                    "bvid": "BV1test00001",
                    "title": "DSH 插件入门",
                    "source_url": "https://www.bilibili.com/video/BV1test00001/",
                    "evidence_level": "subtitle",
                    "score": 88,
                    "evidence_ids": ["ev_abc"],
                    "key_points": [{"claim": "插件能整理笔记", "evidence_id": "ev_abc"}],
                    "comment_url": "https://www.bilibili.com/x",
                }
            ],
        )
    )
    text = path.read_text(encoding="utf-8")
    assert "BV1test00001" in text
    assert "ev_abc" in text
    assert "DSH" in text
    assert "评论链接" in text


def test_digest_with_no_candidates_says_so(db: Database, tmp_path: Path) -> None:
    """★ 无候选时也要生成日报，明确写"今日无值得推荐内容"，且**不发空评论**。"""
    writer = DigestWriter(db=db, out_dir=tmp_path / "digests")
    path = writer.write(
        DigestInput(run_id="run_y", trigger_source="manual", keywords=["DSH"])
    )
    text = path.read_text(encoding="utf-8")
    assert "今日无值得推荐内容" in text


def test_digest_distinguishes_cache_from_fresh_read(db: Database, tmp_path: Path) -> None:
    """★ 任务书 §6/§3：不能把缓存内容称为本轮新抓取。"""
    writer = DigestWriter(db=db, out_dir=tmp_path / "digests")
    path = writer.write(
        DigestInput(
            run_id="run_z",
            trigger_source="manual",
            keywords=["DSH"],
            cache_stats={"requests_made": 2, "cache_hits": 5, "cache_expired": 1},
        )
    )
    text = path.read_text(encoding="utf-8")
    assert "真实请求数：2" in text
    assert "缓存命中：5" in text
    assert "不是本轮新抓取" in text


def test_digest_marks_simulated(db: Database, tmp_path: Path) -> None:
    writer = DigestWriter(db=db, out_dir=tmp_path / "digests")
    path = writer.write(
        DigestInput(run_id="run_s", trigger_source="manual", simulated=True)
    )
    assert "本次为模拟：是" in path.read_text(encoding="utf-8")


def test_digest_lists_uncertain_actions_as_frozen(db: Database, tmp_path: Path) -> None:
    """★ 不确定动作必须在日报里标出来（需人工核对）。"""
    db.upsert_video(bvid="BV1test00001", aid=111)
    action_id, _ = db.reserve_action(
        agent_mid=1001, action_type="top", bvid="BV1test00001",
        target_rpid=None, draft_id=None, is_simulated=False,
    )
    db.settle_action(action_id=action_id, status="uncertain", error_code="TIMEOUT")

    writer = DigestWriter(db=db, out_dir=tmp_path / "digests")
    path = writer.write(DigestInput(run_id="run_u", trigger_source="manual"))
    text = path.read_text(encoding="utf-8")
    assert "不确定动作" in text
    assert "不自动重发" in text


def test_digest_surfaces_platform_health_event(db: Database, tmp_path: Path) -> None:
    db.record_health(state="verification_required", category="captcha", detail="需人工完成验证")
    writer = DigestWriter(db=db, out_dir=tmp_path / "digests")
    path = writer.write(DigestInput(run_id="run_h", trigger_source="manual"))
    text = path.read_text(encoding="utf-8")
    assert "verification_required" in text


def test_digest_records_history(db: Database, tmp_path: Path) -> None:
    writer = DigestWriter(db=db, out_dir=tmp_path / "digests")
    writer.write(DigestInput(run_id="run_1", trigger_source="manual"))
    writer.write(DigestInput(run_id="run_2", trigger_source="manual"))
    history = (tmp_path / "digests" / "digest-history.jsonl").read_text(encoding="utf-8")
    assert len(history.strip().splitlines()) == 2


def test_digest_states_runs_are_source_of_truth(db: Database, tmp_path: Path) -> None:
    writer = DigestWriter(db=db, out_dir=tmp_path / "digests")
    path = writer.write(DigestInput(run_id="run_t", trigger_source="manual"))
    text = path.read_text(encoding="utf-8")
    assert "最终事实来源" in text

# ---------------------------------------------------------------------------
# 日报：主号汇报状态（任务书 §6.6）
# ---------------------------------------------------------------------------


def test_digest_shows_mention_section_when_no_videos(db: Database, tmp_path: Path) -> None:
    """没有需要 @ 的视频时，日报也要有该小节并说明。"""
    writer = DigestWriter(db=db, out_dir=tmp_path / "digests")
    path = writer.write(DigestInput(run_id="run_m1", trigger_source="manual"))
    text = path.read_text(encoding="utf-8")
    assert "主号汇报（@ 提及）状态" in text
    assert "本轮没有需要 @ 主号的视频" in text


def test_digest_warns_when_mention_not_verified(db: Database, tmp_path: Path) -> None:
    """★ 非 verified 的 @ 必须在日报里显著标出"未确认成功"。"""
    db.upsert_video(bvid="BV1test00001", aid=111)
    nid = db.plan_owner_notification(bvid="BV1test00001", owner_mid=2002)
    db.settle_owner_notification(
        notification_id=nid, status="unsupported", error_code="NO_MENTION_CHANNEL",
        detail="没有可用通道",
    )
    writer = DigestWriter(db=db, out_dir=tmp_path / "digests")
    path = writer.write(DigestInput(run_id="run_m2", trigger_source="manual"))
    text = path.read_text(encoding="utf-8")
    assert "[unsupported]" in text
    assert "未确认成功" in text
    assert "不算**提及成功" in text or "不算" in text


def test_digest_does_not_warn_for_verified_mention(db: Database, tmp_path: Path) -> None:
    """已验证的 @ 不应出现"未确认成功"警告。"""
    db.upsert_video(bvid="BV1test00001", aid=111)
    nid = db.plan_owner_notification(bvid="BV1test00001", owner_mid=2002)
    db.settle_owner_notification(
        notification_id=nid, status="verified", observed_mid=2002, detail="已核验"
    )
    writer = DigestWriter(db=db, out_dir=tmp_path / "digests")
    path = writer.write(DigestInput(run_id="run_m3", trigger_source="manual"))
    text = path.read_text(encoding="utf-8")
    assert "[verified]" in text
    assert "observed_mid=2002" in text


def test_digest_mentions_mismatched_uid(db: Database, tmp_path: Path) -> None:
    """★ 提到了错误对象时，日报要能看出实际 UID。"""
    db.upsert_video(bvid="BV1test00001", aid=111)
    nid = db.plan_owner_notification(bvid="BV1test00001", owner_mid=2002)
    db.settle_owner_notification(
        notification_id=nid, status="failed", observed_mid=9999,
        error_code="MENTION_TARGET_MISMATCH", detail="提到了别人",
    )
    writer = DigestWriter(db=db, out_dir=tmp_path / "digests")
    path = writer.write(DigestInput(run_id="run_m4", trigger_source="manual"))
    text = path.read_text(encoding="utf-8")
    assert "observed_mid=9999" in text
    assert "[failed]" in text


def test_batch_distinguishes_read_videos_from_evidence() -> None:
    """★ ``read_bvids`` 与 ``evidence_ids`` 是两回事。

    一条视频会登记几十条证据；混用会把"读了 3 条视频"报成"读了 155 条"，
    这正是本项目在真实运行中踩到的报告失真。
    """
    from core.pipeline import BatchResult

    batch = BatchResult()
    batch.read_bvids = ["BV1a", "BV1b"]
    batch.evidence_ids = [f"ev_{i}" for i in range(80)]
    payload = batch.to_payload(max_batch_chars=100000)
    assert payload["read_bvids"] == ["BV1a", "BV1b"]
    assert len(payload["evidence_ids"]) == 80
    assert len(payload["read_bvids"]) == 2


# ---------------------------------------------------------------------------
# ★ 时间预算：防止超过 DSH 的 toolCallTimeoutMs（实测踩到）
# ---------------------------------------------------------------------------


def test_batch_result_carries_budget_fields() -> None:
    """``BatchResult`` 必须能表达"因预算耗尽而延后"与"耗时"。"""
    from core.pipeline import BatchResult

    batch = BatchResult()
    batch.deferred_bvids = ["BV1a"]
    batch.elapsed_seconds = 91.5
    payload = batch.to_payload(max_batch_chars=100000)
    assert payload["deferred_bvids"] == ["BV1a"]
    assert payload["elapsed_seconds"] == 91.5


async def test_prepare_batch_respects_time_budget(db: Database, clock: FakeClock) -> None:
    """★ 回归：预算耗尽时必须**主动收尾**，而不是让调用被掐断。

    实测：真实一轮 `prepare_batch` 花 198s，而 DSH 超时 120s
    → 调用会在中途失败，但 bridge 里的活儿还在跑（状态不一致）。

    这里把预算设成 0，断言：一个视频都不读，全部进 `deferred_bvids`，
    并且 `limitations` 里说明原因。
    """
    items = [
        {
            "bvid": f"BV1budget{i:04d}", "aid": 500 + i, "title": f"视频 {i}",
            "author": "UP", "mid": 1, "pubdate": recent_pubdate(1) + i,
            "duration": "10:00", "description": "简介", "pic": "//x/y.jpg",
        }
        for i in range(3)
    ]
    reader = make_reader(db, default_handler(search_items=items), clock)
    config = make_config(
        bilibili_requests={"min_interval_seconds": 1.0},
        text_worker={"max_batch_seconds": 10.0},
    )
    pipeline = Pipeline(db=db, config=config, reader=reader, queue=reader.queue, worker=None)

    # ★ 必须**先有 discovered 状态的视频**，否则 prepare_batch 会走
    #   NO_PENDING_VIDEOS 提前返回，压根到不了预算判断那段。
    for item in items:
        db.upsert_video(bvid=item["bvid"], aid=item["aid"], title=item["title"])
    assert pipeline._pending_videos(limit=10), "前置条件：应有待处理视频"

    # 让 monotonic 一上来就"已超预算"。
    # ⚠️ 必须打在 **pipeline 模块**上：它 `import time` 后按 `time.monotonic()`
    #   取属性，改全局 time 模块的属性不够稳。
    import core.pipeline as pl

    real_monotonic = pl.time.monotonic
    # 用一个可推进的假时钟：第一次取基准，之后每取一次就跳 1000 秒。
    state = {"t": 0.0, "n": 0}

    def fake_monotonic() -> float:
        state["n"] += 1
        if state["n"] == 1:
            return 0.0  # started_at 基准
        state["t"] += 1000.0  # 之后任何一次 remaining() 都判定超预算
        return state["t"]

    pl.time.monotonic = fake_monotonic  # type: ignore[assignment]
    try:
        batch = await pipeline.prepare_batch(keywords=["DSH"])
    finally:
        pl.time.monotonic = real_monotonic  # type: ignore[assignment]

    assert batch.deferred_bvids, "预算耗尽时应把未读的视频记入 deferred"
    assert any("TIME_BUDGET_EXHAUSTED" in lim for lim in batch.limitations), batch.limitations


def test_budget_default_is_below_dsh_timeout() -> None:
    """★ 预算必须**小于** DSH 的 toolCallTimeoutMs，否则等于没加。

    当前：预算默认 90s，DSH 超时 300s（含余量）。
    """
    from core.config import Config

    budget = Config().text_worker.max_batch_seconds
    dsh_timeout_ms = 300_000  # 与 dsh/cordis.patch.fragment.yml 保持一致
    assert budget < dsh_timeout_ms / 1000, (
        f"预算 {budget}s 不小于 DSH 超时 {dsh_timeout_ms/1000}s"
    )


def test_fragment_timeout_exceeds_budget() -> None:
    """★ 配置片段里的超时也要大于预算（防文档/配置漂移）。"""
    import re
    from pathlib import Path

    from core.config import Config

    frag = Path(__file__).resolve().parent.parent / "dsh" / "cordis.patch.fragment.yml"
    text = frag.read_text(encoding="utf-8")
    m = re.search(r"toolCallTimeoutMs:\s*(\d+)", text)
    assert m, "片段里没有 toolCallTimeoutMs"
    timeout_s = int(m.group(1)) / 1000
    budget = Config().text_worker.max_batch_seconds
    assert timeout_s > budget, (
        f"DSH 超时 {timeout_s}s 必须大于批处理预算 {budget}s"
    )
