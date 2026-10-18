"""自动调度的"该不该跑"判定与循环（R1）。

## 为什么在 dm-watch 里跑

插件已经把 ``dm-watch`` 做成了**跟随 DSH 启停**的常驻进程
（5 秒后起、DSH 退出就 kill）。它天然满足"打开 DSH 后自动启动"，
不用再加第二个进程/第二套生命周期。

## 一轮的判定

1. 暂停 / 平台阻断 → 跳过；
2. 有**未结束**的 run（2 小时内）→ 跳过（不并发开两轮）；
3. ``evaluate_due``（模式/谷时/防抖/每日轮次）→ 不到期就跳过；
4. 到期 → ``run_cycle``。

## 不做的事

* 不在这里发请求；
* 不绕过任何门禁；
* 不自己发私信（DSH 决策）。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from core import health
from core.schedule import evaluate_due, resolve_tz

#: 默认：DSH 启动后多久才跑第一轮（给插件/会话加载留时间）。
DEFAULT_START_DELAY_SECONDS = 60

#: 默认：两次检查之间隔多久（秒）。
DEFAULT_CHECK_INTERVAL_SECONDS = 60

#: 未结束的 run 超过这个年龄就当作崩溃残留，不再挡住新一轮。
STALE_RUN_SECONDS = 2 * 60 * 60


class CycleScheduler:
    """常驻在 dm-watch 的 asyncio 循环里，按配置决定何时跑 ``run_cycle``。"""

    def __init__(
        self,
        *,
        db: Any,
        config: Any,
        session_id: str = "",
        start_delay: float = DEFAULT_START_DELAY_SECONDS,
        check_interval: float = DEFAULT_CHECK_INTERVAL_SECONDS,
        cycle_runner: Any = None,
    ) -> None:
        self.db = db
        self.config = config
        self.session_id = session_id
        self.start_delay = max(0.0, float(start_delay))
        self.check_interval = max(5.0, float(check_interval))
        self._runner = cycle_runner
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    async def _run_cycle(self, trigger_source: str) -> Any:
        if self._runner is not None:
            return await self._runner(trigger_source=trigger_source, session_id=self.session_id)
        from core.cycle import run_cycle

        return await run_cycle(trigger_source=trigger_source, session_id=self.session_id)

    # ---- 判定 --------------------------------------------------------------

    def _unfinished_run(self, *, now: float | None = None) -> str:
        """最近 2 小时内是否有**未结束**的 run（有就返回 run_id）。"""
        current = time.time() if now is None else now
        row = self.db.query_one(
            "SELECT run_id FROM runs "
            "WHERE finished_at IS NULL AND started_at >= ? "
            "ORDER BY started_at DESC LIMIT 1",
            (int(current - STALE_RUN_SECONDS),),
        )
        return str(row["run_id"]) if row is not None else ""

    def _due(self, *, now: datetime | None = None) -> Any:
        schedule = self.config.schedule
        tz = resolve_tz(schedule.timezone)

        def _aware(value: int | None) -> datetime | None:
            return datetime.fromtimestamp(value, tz=tz) if value else None

        return evaluate_due(
            mode=schedule.mode,
            daily_time=schedule.daily_time,
            tz_name=schedule.timezone,
            now=now,
            last_completed_at=_aware(self.db.last_finished_run_at()),
            last_attempt_at=_aware(self.db.last_started_run_at()),
            min_gap_minutes=int(schedule.min_gap_minutes),
            peak_valley=bool(schedule.peak_valley),
            interval_minutes=int(schedule.interval_minutes),
            max_runs_per_day=int(schedule.max_runs_per_day),
            runs_today=self.db.runs_today(),
        )

    async def tick_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        """检查一次；到期就跑一轮。返回结果（给日志/面板看）。"""
        if self._stop:
            return {"ran": False, "reason": "stopped"}
        if bool(getattr(self.config.interaction, "pause", False)):
            return {"ran": False, "reason": "PAUSED"}
        state = str(self.db.health_state())
        if state in health.BLOCKING_STATES:
            return {"ran": False, "reason": f"HEALTH_BLOCKED:{state}"}
        unfinished = self._unfinished_run()
        if unfinished:
            return {"ran": False, "reason": f"UNFINISHED_RUN:{unfinished}"}

        check = self._due(now=now)
        if not check.due:
            return {"ran": False, "reason": check.reason, "next_run_at": check.next_run_at}

        trigger = "host_start" if self.config.schedule.mode == "on_host_start" else "schedule"
        result = await self._run_cycle(trigger)
        payload = result.to_dict() if hasattr(result, "to_dict") else {"result": str(result)}
        payload["ran"] = True
        payload["trigger_source"] = trigger
        return payload

    async def run(self) -> None:
        """常驻循环：先等 ``start_delay``，之后每 ``check_interval`` 检查一次。"""
        remaining = self.start_delay
        while remaining > 0 and not self._stop:
            await asyncio.sleep(min(1.0, remaining))
            remaining -= 1.0
        while not self._stop:
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001 - 单轮失败不能让调度死掉
                pass
            slept = 0.0
            while slept < self.check_interval and not self._stop:
                await asyncio.sleep(min(1.0, self.check_interval - slept))
                slept += 1.0
