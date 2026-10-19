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
import contextlib
import time
from datetime import datetime
from typing import Any

from core import health
from core.config import load_config
from core.paths import default_config_path
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
        on_event: Any = None,
    ) -> None:
        self.db = db
        self.config = config
        self.session_id = session_id
        self.start_delay = max(0.0, float(start_delay))
        self.check_interval = max(5.0, float(check_interval))
        self._runner = cycle_runner
        #: 事件回调（给面板/日志看）——`(kind, detail) -> None`
        self._on_event = on_event
        self._stop = False
        #: 同一条「不跑」的理由连续出现多少次（省日志）
        self._last_reason = ""
        self._reason_repeats = 0
        #: 上一次真的跑了一轮的时刻（用于"距上次跑多久"）
        self._last_ran_at = 0.0

    def _emit(self, kind: str, detail: str) -> None:
        if self._on_event is not None:
            with contextlib.suppress(Exception):
                self._on_event(kind, detail)

    def status(self) -> dict[str, Any]:
        """调度器自己的可观测状态（给面板/status 用）。"""
        return {
            "start_delay_seconds": self.start_delay,
            "check_interval_seconds": self.check_interval,
            "last_reason": self._last_reason,
            "reason_repeats": self._reason_repeats,
            "last_ran_at": self._last_ran_at,
        }

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
            "AND run_id NOT IN (SELECT run_id FROM cycle_outbox WHERE next_attempt_at <= ?) "
            "ORDER BY started_at DESC LIMIT 1",
            (int(current - STALE_RUN_SECONDS), int(current)),
        )
        return str(row["run_id"]) if row is not None else ""

    def _due(self, *, now: datetime | None = None, recovering: bool = False) -> Any:
        schedule = self.config.schedule
        tz = resolve_tz(schedule.timezone)

        def _aware(value: int | None) -> datetime | None:
            return datetime.fromtimestamp(value, tz=tz) if value else None

        return evaluate_due(
            mode=schedule.mode,
            daily_time=schedule.daily_time,
            tz_name=schedule.timezone,
            now=now,
            # Recovery is the same learning run. Its lease/retry budget is
            # checked separately; a previous successful run or today's new-run
            # quota must not strand a material package already in the outbox.
            last_completed_at=None if recovering else _aware(self.db.last_finished_run_at()),
            last_attempt_at=None if recovering else _aware(self.db.last_started_run_at()),
            min_gap_minutes=int(schedule.min_gap_minutes),
            peak_valley=bool(schedule.peak_valley),
            interval_minutes=int(schedule.interval_minutes),
            max_runs_per_day=int(schedule.max_runs_per_day),
            runs_today=0 if recovering else self.db.runs_today(),
        )

    def _note_skip(self, reason: str, *, key: str = "") -> dict[str, Any]:
        """记一条「这轮不跑」的理由，并**按变化去重**后再上报事件。

        ★ 为什么需要（2026-10-19 主人要观察"峰谷是否真实生效"）：
        没有这条日志时，被峰时闸门挡住的那些 tick 是**完全静默**的 ——
        只能靠"没有 run / 没有日报"间接推断，无法确认闸门真的在工作。

        ★ ``key`` 是**去重键**：展示文本里会带上"下次几点"这种每秒都在变的
        字段，若拿完整文本去重会永远"不重复" ⇒ 每个 tick 都刷一条。
        """
        dedup = key or reason
        self._reason_repeats = self._reason_repeats + 1 if dedup == self._last_reason else 1
        first = self._reason_repeats == 1
        if first or self._reason_repeats % 10 == 0:
            self._emit("scheduled", f"不跑：{reason}（第 {self._reason_repeats} 次）")
        self._last_reason = dedup
        return {"ran": False, "reason": reason}

    async def tick_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        """检查一次；到期就跑一轮。返回结果（给日志/面板看）。"""
        if self._stop:
            return {"ran": False, "reason": "stopped"}
        # Settings are saved to disk while dm-watch keeps running. Refresh the
        # effective config before every gate, including pause and peak/valley.
        try:
            if default_config_path().exists():
                self.config = load_config()
        except Exception as exc:
            return self._note_skip(f"CONFIG_ERROR:{type(exc).__name__}")
        if bool(getattr(self.config.interaction, "pause", False)):
            return self._note_skip("PAUSED（暂停中，恢复要人工做）")
        state = str(self.db.health_state())
        if state in health.BLOCKING_STATES:
            return self._note_skip(f"HEALTH_BLOCKED:{state}（平台阻断，需人工恢复）")
        unfinished = self._unfinished_run()
        if unfinished:
            return self._note_skip(f"UNFINISHED_RUN:{unfinished}（上一轮还没结束）")

        pending = self.db.pending_cycle_delivery()
        current = now.timestamp() if now is not None else time.time()
        if pending is not None:
            if pending["state"] in {"exhausted", "dsh_error"}:
                return self._note_skip(f"RECOVERY_REQUIRES_REVIEW:{pending['run_id']}")
            if int(pending["next_attempt_at"]) > current:
                return self._note_skip(f"RECOVERY_BACKOFF:{pending['run_id']}")
        else:
            # A failed attempt without an outbox (e.g. prepare_batch crashed)
            # must not become a tight loop when min_gap_minutes is set to zero.
            latest = self.db.query_one(
                "SELECT started_at, finished_at, conclusion FROM runs "
                "ORDER BY started_at DESC, rowid DESC LIMIT 1"
            )
            if (
                latest is not None
                and latest["finished_at"] is not None
                and latest["conclusion"] not in {"ok", "no_candidates", "no_content"}
                and current - max(latest["started_at"], latest["finished_at"]) < 300
            ):
                return self._note_skip("FAILED_ATTEMPT_BACKOFF（失败后至少等待 5 分钟）")

        check = self._due(now=now, recovering=pending is not None)
        if not check.due:
            return self._note_skip(
                f"{check.reason}｜下次 {check.next_run_at}",
                # ★ 去重键用**稳定**部分，不含 next_run_at（否则每秒都"变了"）
                key=check.reason,
            )

        # 真的要跑了：把"为什么现在跑"也记下来（含模式与触发来源）
        trigger = "host_start" if self.config.schedule.mode == "on_host_start" else "schedule"
        self._emit(
            "scheduled",
            f"到期 → 开跑（mode={self.config.schedule.mode}，trigger={trigger}）：{check.reason}",
        )
        self._last_reason = ""
        self._reason_repeats = 0
        result = await self._run_cycle(trigger)
        self._last_ran_at = time.time()
        payload = result.to_dict() if hasattr(result, "to_dict") else {"result": str(result)}
        payload["ran"] = True
        payload["trigger_source"] = trigger
        self._emit("scheduled", f"本轮结束：conclusion={payload.get('conclusion')}")
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
