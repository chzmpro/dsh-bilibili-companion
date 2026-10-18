"""批处理预算必须覆盖发现阶段及正在执行的模型调用，而非只在视频间检查。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.pipeline import Pipeline
from core.storage import Database
from core.text_worker import TextWorker
from tests.conftest import FakeClock
from tests.test_pipeline_and_report import default_handler, make_pipeline, make_reader


def short_budget(pipeline: Pipeline) -> None:
    # 测试中把 10s 生产下限缩短，避免用真实长等待验证同一异步取消语义。
    pipeline.config.text_worker = pipeline.config.text_worker.model_copy(
        update={"max_batch_seconds": 0.02}
    )


async def test_deadline_cancels_stalled_discovery(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = make_reader(db, default_handler(), clock)
    pipeline = make_pipeline(db, reader, clock)
    short_budget(pipeline)
    cancelled = asyncio.Event()

    async def stalled(*args: Any, **kwargs: Any) -> Any:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(reader, "search_videos", stalled)
    batch = await asyncio.wait_for(pipeline.prepare_batch(), timeout=0.5)
    assert cancelled.is_set(), "超时后必须取消正在等待的网络请求"
    assert batch.read_bvids == []
    assert any("TIME_BUDGET" in str(item) for item in batch.failed)
    assert batch.elapsed_seconds < 0.5
    await reader.queue.aclose()


async def test_deadline_cancels_model_and_keeps_video_retryable(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = make_reader(db, default_handler(), clock)
    pipeline = make_pipeline(db, reader, clock)
    worker = TextWorker(db=db, config=pipeline.config)
    pipeline.worker = worker
    short_budget(pipeline)
    cancelled = asyncio.Event()

    async def stalled(**kwargs: Any) -> Any:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(worker, "analyze", stalled)
    batch = await asyncio.wait_for(pipeline.prepare_batch(), timeout=0.5)
    assert cancelled.is_set()
    assert batch.read_bvids == []
    assert batch.deferred_bvids == ["BV1test00001"]
    assert pipeline._pending_videos(limit=10) == ["BV1test00001"]
    assert db.evidence_for_bvid("BV1test00001"), "已登记证据可保留复用"
    assert any("TIME_BUDGET_EXHAUSTED" in item for item in batch.limitations)
    await reader.queue.aclose()
    await worker.aclose()
