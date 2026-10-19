"""自动学习轮（R1）与调度判定测试。

钉死这几条：

* 有候选 → 投递一次「短包 + 完整内容」，DSH 的 turn/end 完成后才记 ok；
* 投递失败 / 没有候选 / DSH 超时 → conclusion 如实，不冒充成功；
* 调度器：暂停、平台阻断、未结束的 run、不到期 → 不跑；
* 会话状态解析：只认最后一个 turn/end 的 reason。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.cycle import CycleResult, run_cycle
from core.cycle_scheduler import CycleScheduler
from core.dm_deliver import DeliverResult
from core.pipeline import BatchResult
from core.session_status import TurnStatus, parse_turns
from core.storage import Database
from core.text_worker import CandidateCard
from tests.conftest import make_config

BVID = "BV1cycle00001"


class FakePipeline:
    def __init__(self, batch: BatchResult) -> None:
        self.batch = batch
        self.discussions: list[str] = []

    async def prepare_batch(self, **kwargs: Any) -> BatchResult:
        return self.batch

    async def get_discussion(self, *, bvid: str, limit: int = 20) -> Any:
        self.discussions.append(bvid)
        return None, 3


class FakeRuntime:
    def __init__(self, db: Database, config: Any, batch: BatchResult) -> None:
        self.db = db
        self.config = config
        self.pipeline = FakePipeline(batch)

    async def aclose(self) -> None:
        return None


def _seed(db: Database) -> None:
    db.upsert_video(bvid=BVID, aid=111, title="DSH 插件入门", up_name="某UP")
    db.add_evidence(bvid=BVID, source_type="description", text="这是简介")
    db.add_evidence(bvid=BVID, source_type="subtitle", text="字幕第一段", start_sec=1.0)
    db.add_evidence(bvid=BVID, source_type="comment", text="评论：讲得很清楚")


def _batch() -> BatchResult:
    card = CandidateCard(
        bvid=BVID,
        aid=111,
        title="DSH 插件入门",
        source_url=f"https://www.bilibili.com/video/{BVID}/",
        evidence_level="subtitle",
        relevance_score=92,
    )
    return BatchResult(candidates=[card], read_bvids=[BVID])


def _config(**overrides: Any) -> Any:
    return make_config(schedule={"min_gap_minutes": 0}, **overrides)


async def test_cycle_delivers_short_cards_and_full_content(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db)
    db.add_owner_preference(
        kind="note", subject="表达风格（自动学习）", content="开头先给结论"
    )
    runtime = FakeRuntime(db, _config(), _batch())
    captured: dict[str, Any] = {}

    async def fake_deliver(
        text: str, *, session_id: str = "", timeout: float = 15
    ) -> DeliverResult:
        captured["text"] = text
        captured["session_id"] = session_id
        return DeliverResult(ok=True, delivered=True, session_id=session_id)

    def fake_write_digest(**kwargs: Any) -> None:
        return None

    turns = {"n": 0}

    def fake_turn(session_id: str) -> TurnStatus:
        turns["n"] += 1
        if turns["n"] <= 1:
            return TurnStatus(found=True, turns=3, completed=False, last_kind="completed", error="")
        return TurnStatus(found=True, turns=4, completed=True, last_kind="completed", error="")

    monkeypatch.setattr("core.cycle.deliver_async", fake_deliver)
    monkeypatch.setattr("core.cycle._write_digest", fake_write_digest)
    monkeypatch.setattr("core.cycle.read_turn_status", fake_turn)

    result = await run_cycle(
        trigger_source="host_start",
        session_id="session-test",
        runtime=runtime,
        wait_seconds=0.2,
        poll_seconds=0.01,
    )
    assert result.delivered is True
    assert result.conclusion == "ok"
    assert result.candidates == 1
    assert captured["session_id"] == "session-test"
    text = captured["text"]
    assert "短候选包" in text and "完整内容" in text
    assert "dm_push" in text and BVID in text
    assert "评论区" in text, "评论也要进入完整内容"
    assert "开头先给结论" in text, "学习到的表达技巧要带进投递"
    row = db.query_one(
        "SELECT conclusion, candidates_n FROM runs WHERE run_id = ?", (result.run_id,)
    )
    assert row is not None and row["conclusion"] == "ok" and row["candidates_n"] == 1


async def test_cycle_delivery_failure_is_reported(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db)
    runtime = FakeRuntime(db, _config(), _batch())

    async def fake_deliver(
        text: str, *, session_id: str = "", timeout: float = 15
    ) -> DeliverResult:
        return DeliverResult(
            ok=False, delivered=False, error_code="DSH_NOT_FOUND", message="找不到 DSH"
        )

    monkeypatch.setattr("core.cycle.deliver_async", fake_deliver)
    monkeypatch.setattr("core.cycle._write_digest", lambda **kw: None)
    result = await run_cycle(runtime=runtime, session_id="s", wait_seconds=0.1, poll_seconds=0.01)
    assert result.delivered is False
    assert result.conclusion == "delivery_failed"
    row = db.query_one("SELECT conclusion FROM runs WHERE run_id = ?", (result.run_id,))
    assert row is not None and row["conclusion"] == "delivery_failed"


async def test_cycle_no_candidates_finishes_honestly(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db)
    runtime = FakeRuntime(db, _config(), BatchResult())
    monkeypatch.setattr("core.cycle._write_digest", lambda **kw: None)
    result = await run_cycle(runtime=runtime, wait_seconds=0.1, poll_seconds=0.01)
    assert result.conclusion == "no_candidates"
    assert result.delivered is False


async def test_cycle_dsh_timeout_marks_unverified(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db)
    runtime = FakeRuntime(db, _config(), _batch())

    async def fake_deliver(
        text: str, *, session_id: str = "", timeout: float = 15
    ) -> DeliverResult:
        return DeliverResult(ok=True, delivered=True, session_id=session_id)

    monkeypatch.setattr("core.cycle.deliver_async", fake_deliver)
    monkeypatch.setattr("core.cycle._write_digest", lambda **kw: None)
    monkeypatch.setattr(
        "core.cycle.read_turn_status",
        lambda session_id: TurnStatus(found=True, turns=3, completed=False, last_kind="", error=""),
    )
    result = await run_cycle(runtime=runtime, session_id="s", wait_seconds=0.05, poll_seconds=0.01)
    assert result.conclusion == "delivered_unverified"


async def test_cycle_runs_trends_backfill_before_discover(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ C 项：开自动回填时，cycle 必须**先**跑推荐流抽词再去发现。

    同一个 run 内，回填的关键词要能立刻被本轮搜索用上
    （所以顺序是 trends → prepare_batch，而不是反过来）。
    """
    _seed(db)
    order: list[str] = []

    class TrackingPipeline(FakePipeline):
        async def prepare_batch(self, **kwargs: Any) -> BatchResult:
            order.append("prepare_batch")
            return self.batch

    runtime = FakeRuntime(
        db,
        _config(discovery={"keywords": ["DSH插件"], "auto_accept_suggestions": True}),
        _batch(),
    )
    runtime.pipeline = TrackingPipeline(_batch())  # type: ignore[assignment]

    async def fake_trends(runtime_arg: Any, **kwargs: Any) -> Any:
        order.append("trends")
        from core.trends_round import TrendsRoundResult

        return TrendsRoundResult(
            ok=True, code="OK", fetched=8, new_in_pool=2, backfilled=["新词A"], quota={}
        )

    async def fake_deliver(
        text: str, *, session_id: str = "", timeout: float = 15
    ) -> DeliverResult:
        return DeliverResult(ok=True, delivered=True, session_id=session_id)

    monkeypatch.setattr("core.trends_round.run_trends_round", fake_trends)
    monkeypatch.setattr("core.cycle.deliver_async", fake_deliver)
    monkeypatch.setattr("core.cycle._write_digest", lambda **kw: None)
    polls = {"n": 0}

    def fake_turn(session_id: str) -> TurnStatus:
        polls["n"] += 1
        # 第一次是"投递前"的基线（turns=3），之后出现新的 completed
        return TurnStatus(
            found=True,
            turns=3 if polls["n"] == 1 else 4,
            completed=polls["n"] > 1,
            last_kind="completed",
            error="",
        )

    monkeypatch.setattr("core.cycle.read_turn_status", fake_turn)

    result = await run_cycle(
        runtime=runtime, session_id="s", wait_seconds=0.2, poll_seconds=0.01
    )
    assert order == ["trends", "prepare_batch"], "必须先抽词再发现"
    assert result.conclusion == "ok"
    row = db.query_one("SELECT detail_json FROM runs WHERE run_id = ?", (result.run_id,))
    assert row is not None and "新词A" in str(row["detail_json"])


async def test_cycle_skips_trends_when_backfill_off(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认（关）时**不**跑推荐流，只记一条 skipped 说明。"""
    _seed(db)
    runtime = FakeRuntime(db, _config(), _batch())
    calls: list[str] = []

    async def fake_trends(runtime_arg: Any, **kwargs: Any) -> Any:
        calls.append("trends")
        raise AssertionError("关着时不该调用")

    async def fake_deliver(
        text: str, *, session_id: str = "", timeout: float = 15
    ) -> DeliverResult:
        return DeliverResult(ok=True, delivered=True, session_id=session_id)

    monkeypatch.setattr("core.trends_round.run_trends_round", fake_trends)
    monkeypatch.setattr("core.cycle.deliver_async", fake_deliver)
    monkeypatch.setattr("core.cycle._write_digest", lambda **kw: None)
    polls = {"n": 0}

    def fake_turn2(session_id: str) -> TurnStatus:
        polls["n"] += 1
        return TurnStatus(
            found=True,
            turns=3 if polls["n"] == 1 else 4,
            completed=polls["n"] > 1,
            last_kind="completed",
            error="",
        )

    monkeypatch.setattr("core.cycle.read_turn_status", fake_turn2)
    result = await run_cycle(
        runtime=runtime, session_id="s", wait_seconds=0.2, poll_seconds=0.01
    )
    assert calls == []
    row = db.query_one("SELECT detail_json FROM runs WHERE run_id = ?", (result.run_id,))
    assert row is not None and "auto_accept_suggestions=false" in str(row["detail_json"])


async def test_cycle_unexpected_error_finishes_run(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenPipeline(FakePipeline):
        async def prepare_batch(self, **kwargs: Any) -> BatchResult:
            raise RuntimeError("boom")

    runtime = FakeRuntime(db, _config(), BatchResult())
    runtime.pipeline = BrokenPipeline(BatchResult())  # type: ignore[assignment]
    monkeypatch.setattr("core.cycle._write_digest", lambda **kw: None)
    result = await run_cycle(runtime=runtime, wait_seconds=0, poll_seconds=0)
    assert result.conclusion == "ERROR:RuntimeError"
    row = db.query_one(
        "SELECT conclusion, finished_at FROM runs WHERE run_id = ?", (result.run_id,)
    )
    assert row is not None
    assert row["conclusion"] == "ERROR:RuntimeError"
    assert row["finished_at"] is not None


# ---------------------------------------------------------------------------
# 调度器
# ---------------------------------------------------------------------------


class _Runner:
    def __init__(self, db: Database | None = None) -> None:
        self.calls: list[str] = []
        self.db = db
        self.run_id = ""

    async def __call__(self, *, trigger_source: str, session_id: str = "") -> CycleResult:
        self.calls.append(trigger_source)
        if self.db is not None:
            self.run_id = self.db.start_run(trigger_source)
        return CycleResult(conclusion="ok", delivered=True)


async def test_scheduler_runs_when_due(db: Database) -> None:
    runner = _Runner(db)
    scheduler = CycleScheduler(db=db, config=_config(), session_id="s", cycle_runner=runner)
    payload = await scheduler.tick_once()
    assert payload["ran"] is True
    assert runner.calls == ["host_start"]


async def test_scheduler_skips_paused_and_blocked(db: Database) -> None:
    paused = CycleScheduler(
        db=db, config=_config(interaction={"pause": True}), cycle_runner=_Runner()
    )
    assert (await paused.tick_once())["ran"] is False

    db.record_health(state="platform_error")
    blocked = CycleScheduler(db=db, config=_config(), cycle_runner=_Runner())
    assert (await blocked.tick_once())["ran"] is False


async def test_scheduler_skips_unfinished_or_completed(db: Database) -> None:
    runner = _Runner()
    scheduler = CycleScheduler(db=db, config=_config(), session_id="s", cycle_runner=runner)
    db.start_run("host_start")  # 未结束
    assert (await scheduler.tick_once())["ran"] is False

    # 结束它 → 今天已完成 → 不重复跑
    row = db.query_one("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1")
    assert row is not None
    db.finish_run(str(row["run_id"]), conclusion="ok")
    assert (await scheduler.tick_once())["ran"] is False


async def test_scheduler_continuous_interval(db: Database) -> None:
    config = make_config(
        schedule={
            "mode": "continuous",
            "interval_minutes": 30,
            "max_runs_per_day": 8,
            "min_gap_minutes": 0,
        }
    )
    runner = _Runner(db)
    scheduler = CycleScheduler(db=db, config=config, session_id="s", cycle_runner=runner)
    first = await scheduler.tick_once()
    assert first["ran"] is True
    # 刚跑过 → 间隔不够 → 不跑
    assert (await scheduler.tick_once())["ran"] is False


# ---------------------------------------------------------------------------
# 会话状态解析
# ---------------------------------------------------------------------------


def test_parse_turns_only_last_kind_counts() -> None:
    text = "\n".join(
        [
            '{"type":"turn/end","data":{"reason":{"kind":"completed"}}}',
            '{"type":"turn/end","data":{"reason":{"kind":"error","error":{"message":"boom"}}}}',
        ]
    )
    turns, last_kind, completed, error = parse_turns(text)
    assert turns == 2 and last_kind == "error" and completed is False and "boom" in error


def test_parse_turns_ignores_noise() -> None:
    turns, last_kind, completed, error = parse_turns("not json\n{}\n")
    assert (turns, last_kind, completed, error) == (0, "", False, "")
