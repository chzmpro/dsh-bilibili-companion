"""Durable cycle recovery: real SQLite state, only transport/model boundaries replaced."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from core.cycle import _wait_for_completion, run_cycle
from core.cycle_scheduler import CycleScheduler
from core.dm_deliver import DeliverResult, deliver
from core.pipeline import BatchResult
from core.storage import Database
from tests.test_cycle import FakeRuntime, _batch, _config, _seed


@pytest.mark.parametrize(
    "conclusion", ["delivery_failed", "dsh_error", "delivered_unverified", "ERROR:RuntimeError"]
)
def test_failure_does_not_mark_today_completed(db: Database, conclusion: str) -> None:
    run = db.start_run("host_start")
    db.finish_run(run, conclusion=conclusion)
    assert db.last_finished_run_at() is None
    assert db.last_started_run_at() is not None


async def test_failed_empty_batch_is_not_no_candidates_success(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = FakeRuntime(
        db,
        _config(),
        BatchResult(failed=[{"keyword_or_bvid": "xiaohongshu", "reason": "NOT_CONFIGURED"}]),
    )
    monkeypatch.setattr("core.cycle._write_digest", lambda **kw: None)
    result = await run_cycle(runtime=runtime, session_id="s", wait_seconds=0)
    assert result.conclusion == "batch_failed"
    assert db.last_finished_run_at() is None


async def test_partial_batch_crash_recovers_checkpoint_without_reanalysis(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db)
    run = db.start_run("schedule")
    db.save_cycle_delivery(run, session_id="s", payload={"cards": [], "batch_in_progress": True})
    db.checkpoint_cycle_candidate(run, _batch().candidates[0].to_payload())
    row = db.get_video(_batch().candidates[0].bvid)
    assert row["status"] == "read"

    async def transport(text: str, **kwargs: object) -> DeliverResult:
        return DeliverResult(ok=True, delivered=True, session_id="s")

    runtime = FakeRuntime(db, _config(), BatchResult())

    async def must_not_analyze(**kwargs: object) -> BatchResult:
        raise AssertionError("checkpoint must preserve already analyzed card")

    runtime.pipeline.prepare_batch = must_not_analyze
    monkeypatch.setattr("core.cycle.deliver_async", transport)
    result = await run_cycle(runtime=runtime, session_id="s", wait_seconds=0)
    assert result.delivered is True
    assert result.candidates == 1
    assert "DSH 插件入门" in db.query_one("SELECT prompt FROM cycle_outbox")["prompt"]


def test_candidate_checkpoint_and_read_flag_are_atomic(db: Database) -> None:
    _seed(db)
    card = _batch().candidates[0].to_payload()
    run = db.start_run("schedule")
    db.save_cycle_delivery(run, session_id="s", payload={"cards": []})
    db.execute(
        "CREATE TRIGGER reject_read BEFORE UPDATE OF status ON videos "
        "BEGIN SELECT RAISE(ABORT, 'cannot mark read'); END"
    )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError, match="cannot mark read"):
        db.checkpoint_cycle_candidate(run, card)
    assert "read_cards" not in json.loads(
        db.query_one("SELECT payload_json FROM cycle_outbox")["payload_json"]
    )


def test_recovered_delivery_updates_attempt_clock_without_claiming_success(db: Database) -> None:
    run = db.start_run("schedule")
    db.execute("UPDATE runs SET started_at=100 WHERE run_id=?", (run,))
    db.save_cycle_delivery(run, session_id="s", payload={"cards": []})
    db.update_cycle_delivery(run, state="retry", last_attempt_at=200)
    assert db.last_started_run_at() == 200
    assert db.last_finished_run_at() is None


async def test_real_pipeline_checkpoint_survives_cycle_batch_exception(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.pipeline import Pipeline
    from core.ratelimit import RequestQueue
    from tests.test_content_pipeline import ReadingBackend

    config = _config(
        discovery={"platforms": ["xiaohongshu"]},
        external_platforms={"xiaohongshu": {"min_interval_seconds": 1}},
    )
    backend = ReadingBackend("xiaohongshu")
    pipe = Pipeline(
        db=db,
        config=config,
        reader=None,
        queue=RequestQueue(db=db, config=config),
        adapters={"xiaohongshu": backend},
    )
    runtime = FakeRuntime(db, config, BatchResult())
    runtime.pipeline = pipe
    prepare = pipe.prepare_batch

    async def interrupted(**kwargs: object) -> BatchResult:
        await prepare(**kwargs)
        raise RuntimeError("process interrupted after completed candidate")

    monkeypatch.setattr(pipe, "prepare_batch", interrupted)
    first = await run_cycle(runtime=runtime, session_id="s", wait_seconds=0)
    assert first.conclusion == "ERROR:RuntimeError"
    pending = db.pending_cycle_delivery()
    assert json.loads(pending["payload_json"])["read_cards"][0]["platform"] == "xiaohongshu"
    db.execute("UPDATE cycle_outbox SET next_attempt_at=0")

    async def must_not_prepare(**kwargs: object) -> BatchResult:
        raise AssertionError("candidate already analyzed")

    monkeypatch.setattr(pipe, "prepare_batch", must_not_prepare)

    async def accepted(text: str, **kwargs: object) -> DeliverResult:
        return DeliverResult(ok=True, delivered=True, session_id="s")

    monkeypatch.setattr("core.cycle.deliver_async", accepted)
    second = await run_cycle(runtime=runtime, session_id="s", wait_seconds=0)
    assert second.delivered is True
    assert second.candidates == 1
    assert sum(call[0] == "detail" for call in backend.calls) == 1


async def test_interrupted_batch_reapplies_score_deep_review_and_cap(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = db.start_run("schedule")
    db.save_cycle_delivery(run, session_id="s", payload={"cards": [], "batch_in_progress": True})
    for key, score, deep in [
        ("high", 98, False),
        ("second", 97, False),
        ("review", 1, True),
        ("low", 0, False),
        ("overflow", 0, True),
    ]:
        db.upsert_video(bvid=key, aid=0, title=key)
        db.add_evidence(bvid=key, source_type="description", text=key)
        card = _batch().candidates[0].to_payload()
        card.update(bvid=key, relevance_score=score, needs_deep_review=deep)
        db.checkpoint_cycle_candidate(run, card)

    async def accepted(text: str, **kwargs: object) -> DeliverResult:
        return DeliverResult(ok=True, delivered=True, session_id="s")

    monkeypatch.setattr("core.cycle.deliver_async", accepted)
    result = await run_cycle(
        runtime=FakeRuntime(db, _config(), BatchResult()), session_id="s", wait_seconds=0
    )
    assert result.candidates == 3
    payload = json.loads(db.query_one("SELECT payload_json FROM cycle_outbox")["payload_json"])
    assert [card["bvid"] for card in payload["cards"]] == ["high", "second", "review"]


async def test_retry_after_restart_reuses_saved_prompt_without_reading_or_model(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(db)
    runtime = FakeRuntime(db, _config(), _batch())
    prompts: list[str] = []

    async def offline(text: str, **kwargs: object) -> DeliverResult:
        prompts.append(text)
        return DeliverResult(ok=False, error_code="DSH_NOT_FOUND")

    monkeypatch.setattr("core.cycle.deliver_async", offline)
    monkeypatch.setattr("core.cycle._write_digest", lambda **kw: None)
    first = await run_cycle(runtime=runtime, session_id="s", wait_seconds=0)
    assert first.conclusion == "delivery_failed"
    # The original bug: prepare_batch has already marked the video read, so
    # a restarted pipeline no longer returns it. Recovery must use the outbox.
    recovered_db = Database(db.path)
    recovered_db.migrate()
    try:
        recovered_db.execute("UPDATE cycle_outbox SET next_attempt_at = 0")

        async def must_not_prepare(**kwargs: object) -> BatchResult:
            raise AssertionError("recovery must not prepare_batch again")

        restarted = FakeRuntime(recovered_db, _config(), BatchResult())
        restarted.pipeline.prepare_batch = must_not_prepare

        async def must_not_condense(*args: object, **kwargs: object) -> object:
            raise AssertionError("saved prompt must not call the model again")

        monkeypatch.setattr("core.cycle.prepare_one", must_not_condense)

        async def accepted(text: str, **kwargs: object) -> DeliverResult:
            prompts.append(text)
            return DeliverResult(ok=True, delivered=True, session_id="s")

        monkeypatch.setattr("core.cycle.deliver_async", accepted)
        second = await run_cycle(runtime=restarted, session_id="s", wait_seconds=0)
        assert second.delivered is True
        assert second.conclusion == "delivered_unverified"
        assert prompts == [prompts[0], prompts[0]]
    finally:
        recovered_db.close()


@pytest.mark.parametrize("transport_state", ["accepted", "timeout"])
async def test_uncertain_or_accepted_delivery_is_never_sent_again(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    transport_state: str,
) -> None:
    _seed(db)
    runtime = FakeRuntime(db, _config(), _batch())
    sent: list[str] = []

    async def transport(text: str, **kwargs: object) -> DeliverResult:
        sent.append(text)
        if transport_state == "timeout":
            return DeliverResult(ok=False, error_code="NETWORK")
        return DeliverResult(ok=True, delivered=True, session_id="s")

    monkeypatch.setattr("core.cycle.deliver_async", transport)
    monkeypatch.setattr("core.cycle._write_digest", lambda **kw: None)
    await run_cycle(runtime=runtime, session_id="s", wait_seconds=0)
    db.execute("UPDATE cycle_outbox SET next_attempt_at=0")
    second = await run_cycle(runtime=runtime, session_id="s", wait_seconds=0)
    assert len(sent) == 1
    assert second.conclusion in {"delivered_unverified", "delivery_uncertain"}


async def test_safe_delivery_retries_have_interval_and_limit(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db)
    runtime = FakeRuntime(db, _config(), _batch())
    sent: list[str] = []

    async def offline(text: str, **kwargs: object) -> DeliverResult:
        sent.append(text)
        return DeliverResult(ok=False, error_code="DSH_NOT_FOUND")

    monkeypatch.setattr("core.cycle.deliver_async", offline)
    monkeypatch.setattr("core.cycle._write_digest", lambda **kw: None)
    await run_cycle(runtime=runtime, session_id="s", wait_seconds=0)
    immediate = await run_cycle(runtime=runtime, session_id="s", wait_seconds=0)
    assert immediate.skipped is True
    assert len(sent) == 1
    for _ in range(5):
        db.execute("UPDATE cycle_outbox SET next_attempt_at=0")
        await run_cycle(runtime=runtime, session_id="s", wait_seconds=0)
    assert len(sent) == 3
    assert db.query_one("SELECT state FROM cycle_outbox")["state"] == "exhausted"


async def test_crash_after_send_started_requires_verification_not_resend(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = db.start_run("schedule")
    db.save_cycle_delivery(
        run, session_id="s", payload={"cards": [_batch().candidates[0].to_payload()]}
    )
    db.update_cycle_delivery(run, state="sending", prompt="学习包 run_id=crashed", attempts=1)

    async def must_not_send(*args: object, **kwargs: object) -> DeliverResult:
        raise AssertionError("sending may already be accepted")

    monkeypatch.setattr("core.cycle.deliver_async", must_not_send)
    result = await run_cycle(
        runtime=FakeRuntime(db, _config(), BatchResult()), session_id="s", wait_seconds=0
    )
    assert result.conclusion == "delivery_uncertain"
    assert db.query_one("SELECT attempts FROM cycle_outbox")["attempts"] == 1


async def test_scheduler_recovers_outbox_even_if_earlier_learning_succeeded(db: Database) -> None:
    earlier = db.start_run("schedule")
    db.finish_run(earlier, conclusion="ok")
    failed = db.start_run("schedule")
    db.finish_run(failed, conclusion="delivery_failed")
    db.save_cycle_delivery(failed, session_id="s", payload={"cards": []})
    db.update_cycle_delivery(failed, state="retry", prompt="saved", next_attempt_at=0)
    invoked: list[str] = []

    async def runner(**kwargs: str) -> object:
        from core.cycle import CycleResult

        invoked.append(kwargs["trigger_source"])
        return CycleResult(conclusion="delivered_unverified")

    scheduler = CycleScheduler(db=db, config=_config(), cycle_runner=runner)
    assert (await scheduler.tick_once())["ran"] is True
    assert invoked == ["host_start"]


async def test_scheduler_backoff_on_error_without_outbox(db: Database) -> None:
    failed = db.start_run("schedule")
    db.finish_run(failed, conclusion="ERROR:RuntimeError")
    scheduler = CycleScheduler(db=db, config=_config())
    result = await scheduler.tick_once()
    assert result["ran"] is False
    assert "BACKOFF" in result["reason"]


async def test_owner_message_completion_cannot_finish_learning_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        {"type": "turn/start", "data": {"turn": 4}},
        {
            "type": "user/message",
            "data": {
                "id": "owner-msg",
                "role": "user",
                "content": [{"type": "text", "text": "主人私信"}],
            },
        },
        {"type": "turn/end", "data": {"turn": 4, "reason": {"kind": "completed"}}},
    ]
    monkeypatch.setattr("core.session_status.find_session_file", lambda *a, **kw: "fixture")
    monkeypatch.setattr(
        "core.session_status.decompress", lambda *a: "\n".join(json.dumps(e) for e in events)
    )
    conclusion, _ = await _wait_for_completion(
        "s", expected_text="学习 run_id=unique", wait_seconds=0.02, poll_seconds=0.001
    )
    assert conclusion == "delivered_unverified"


async def test_exact_learning_message_matches_its_turn_even_after_owner_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        {"type": "turn/start", "data": {"turn": 4}},
        {
            "type": "user/message",
            "data": {
                "id": "learning-msg",
                "role": "user",
                "content": [{"type": "text", "text": "学习 run_id=unique"}],
            },
        },
        {"type": "turn/end", "data": {"turn": 4, "reason": {"kind": "completed"}}},
        {"type": "turn/start", "data": {"turn": 5}},
        {
            "type": "user/message",
            "data": {
                "id": "owner-msg",
                "role": "user",
                "content": [{"type": "text", "text": "主人私信"}],
            },
        },
        {
            "type": "turn/end",
            "data": {"turn": 5, "reason": {"kind": "error", "error": {"message": "owner error"}}},
        },
    ]
    monkeypatch.setattr("core.session_status.find_session_file", lambda *a, **kw: "fixture")
    monkeypatch.setattr(
        "core.session_status.decompress", lambda *a: "\n".join(json.dumps(e) for e in events)
    )
    conclusion, detail = await _wait_for_completion(
        "s", expected_text="学习 run_id=unique", wait_seconds=0.02, poll_seconds=0.001
    )
    assert conclusion == "ok"
    assert detail["message_id"] == "learning-msg"
    assert detail["turn_id"] == 4


@pytest.mark.parametrize(
    "events,want",
    [
        (
            [
                {"type": "turn/start", "data": {"turn": 4}},
                {
                    "type": "user/message",
                    "data": {
                        "id": "m",
                        "role": "user",
                        "content": [{"type": "text", "text": "saved"}],
                    },
                },
                {
                    "type": "turn/end",
                    "data": {
                        "turn": 4,
                        "reason": {"kind": "error", "error": {"message": "model unavailable"}},
                    },
                },
                {"type": "turn/start", "data": {"turn": 5}},
                {"type": "turn/end", "data": {"turn": 5, "reason": {"kind": "completed"}}},
            ],
            "dsh_error",
        ),
        (
            [
                {
                    "type": "user/message",
                    "data": {
                        "id": "m",
                        "role": "user",
                        "content": [{"type": "text", "text": "saved"}],
                    },
                },
                {"type": "turn/end", "data": {"turn": 4, "reason": {"kind": "completed"}}},
            ],
            "delivered_unverified",
        ),
    ],
)
async def test_completion_requires_matching_turn_not_just_later_success(
    monkeypatch: pytest.MonkeyPatch, events: list[dict], want: str
) -> None:
    monkeypatch.setattr("core.session_status.find_session_file", lambda *a, **kw: "fixture")
    monkeypatch.setattr(
        "core.session_status.decompress", lambda *a: "\n".join(json.dumps(e) for e in events)
    )
    conclusion, _ = await _wait_for_completion(
        "s", expected_text="saved", wait_seconds=0.02, poll_seconds=0.001
    )
    assert conclusion == want


@pytest.mark.parametrize(
    "error,retry_safe",
    [(httpx.ConnectError("offline"), True), (httpx.ReadTimeout("lost response"), False)],
)
def test_transport_distinguishes_not_sent_from_unknown(
    monkeypatch: pytest.MonkeyPatch, error: Exception, retry_safe: bool
) -> None:
    monkeypatch.setattr("core.dm_deliver.discover_port", lambda: 1234)

    def broken(*args: object, **kwargs: object) -> object:
        raise error

    monkeypatch.setattr(httpx, "post", broken)
    assert deliver("test").retry_safe is retry_safe


async def test_scheduler_reloads_saved_peak_mode_and_daily_limit(
    db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    from pathlib import Path

    config_path = Path(str(tmp_path)) / "effective.yaml"
    monkeypatch.setenv("COMPANION_CONFIG_PATH", str(config_path))
    from core import paths

    paths.reset_cache()
    calls: list[str] = []

    async def runner(**kwargs: str) -> object:
        from core.cycle import CycleResult

        calls.append(kwargs["trigger_source"])
        return CycleResult(conclusion="ok")

    scheduler = CycleScheduler(db=db, config=_config(), cycle_runner=runner)
    now = datetime(2026, 10, 19, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    # Keep the database's "today" on the same date as the injected scheduler clock.
    # Otherwise this test starts failing when the machine crosses local midnight.
    monkeypatch.setattr("core.storage.now_ts", lambda: int(now.timestamp()))
    monkeypatch.setattr(
        "core.storage._start_of_today",
        lambda: int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()),
    )
    config_path.write_text(
        "schedule:\n  mode: continuous\n  peak_valley: true\n  max_runs_per_day: 1\n",
        encoding="utf-8",
    )
    assert (await scheduler.tick_once(now=now))["ran"] is False
    config_path.write_text(
        "schedule:\n  mode: continuous\n  peak_valley: false\n  max_runs_per_day: 1\n",
        encoding="utf-8",
    )
    assert (await scheduler.tick_once(now=now))["ran"] is True
    assert calls == ["schedule"]
    run = db.start_run("schedule")
    db.finish_run(run, conclusion="ok")
    db.execute("UPDATE runs SET started_at=? WHERE run_id=?", (int(now.timestamp()) - 3600, run))
    capped = await scheduler.tick_once(now=now)
    assert capped["ran"] is False
    assert "1/1" in capped["reason"]
    config_path.write_text(
        "schedule:\n  mode: continuous\n  peak_valley: false\n  max_runs_per_day: 2\n",
        encoding="utf-8",
    )
    assert (await scheduler.tick_once(now=now))["ran"] is True
    assert calls == ["schedule", "schedule"]
