"""用本地休眠子进程验证 ASR 的取消回收；不加载模型、不访问网络。"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest

from core import asr
from core.storage import Database
from tests.conftest import FakeClock
from tests.test_pipeline_and_report import default_handler, make_pipeline, make_reader


@pytest.mark.parametrize("mode", ["cancel", "own_timeout", "batch_deadline"])
async def test_asr_process_is_reaped_before_cancellation_returns(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    processes: list[asyncio.subprocess.Process] = []
    started = asyncio.Event()

    async def spawn(argv: Any) -> asyncio.subprocess.Process:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(60)",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        processes.append(proc)
        started.set()
        return proc

    def must_not_use_thread(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("pipeline 必须复用可取消的 ASR 子进程入口")

    monkeypatch.setattr(asr, "_spawn_worker", spawn)
    monkeypatch.setattr(asr, "is_available", lambda: (True, "OK"))
    monkeypatch.setattr(asr, "transcribe_target", must_not_use_thread)
    try:
        if mode == "cancel":
            task = asyncio.create_task(asr.transcribe_async("BV1test00001"))
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif mode == "own_timeout":
            result = await asr.transcribe_async("BV1test00001", timeout_sec=0.02)
            assert result.code == asr.CODE_TIMEOUT
        else:
            reader = make_reader(db, default_handler(subtitles=False), clock)
            pipeline = make_pipeline(db, reader, clock)
            pipeline.config.text_worker = pipeline.config.text_worker.model_copy(
                update={"asr_fallback": True, "max_batch_seconds": 0.2}
            )
            batch = await asyncio.wait_for(pipeline.prepare_batch(), timeout=2)
            assert batch.deferred_bvids == ["BV1test00001"]
            assert batch.read_bvids == []
            assert pipeline._pending_videos(limit=10) == ["BV1test00001"]
            await reader.queue.aclose()
        assert processes, "必须启动可回收的子进程，不能退回不可取消线程"
        assert processes[0].returncode is not None, "返回前必须已终止并回收子进程"
    finally:
        # 旧实现失败时也不遗留测试进程。
        for proc in processes:
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()
