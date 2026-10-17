"""调度测试（任务书 §7 任务 7）。

核心断言：

* "该不该跑"的判断**不依赖调度器的投递记录**（那不代表任务完成）；
* 宿主机停机错过的时刻**不排队**，但当天没跑过会**补跑**；
* **最小间隔**防止重复投递造成双跑；
* 时区解析失败时**明确报错**，不静默退到 UTC
  （否则"每天 10:00"会在实际 18:00 触发，很难发现）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.schedule import (
    DEFAULT_MIN_GAP_MINUTES,
    SCHEDULE_WIRING_NOTE,
    DueCheck,
    NextRun,
    ScheduleError,
    is_due,
    next_run_at,
    parse_daily_time,
    resolve_tz,
    should_run_now,
)

TZ = "Asia/Shanghai"


def at(hour: int, minute: int = 0, *, day: int = 15) -> datetime:
    """构造一个该时区内的明确时刻。"""
    return datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo(TZ))


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def test_parse_daily_time() -> None:
    assert parse_daily_time("10:00").hour == 10
    assert parse_daily_time("00:00").minute == 0
    assert parse_daily_time("23:59").hour == 23


@pytest.mark.parametrize("bad", ["10", "10:0:0", "25:00", "10:60", "aa:bb", ""])
def test_parse_daily_time_rejects_bad(bad: str) -> None:
    with pytest.raises(ScheduleError):
        parse_daily_time(bad)


def test_resolve_tz_works() -> None:
    assert resolve_tz("Asia/Shanghai") is not None
    assert resolve_tz("UTC") is not None


def test_resolve_tz_rejects_unknown() -> None:
    """★ 不能静默退到 UTC——那会让每天 10:00 在错误时间触发。"""
    with pytest.raises(ScheduleError, match="无法解析时区"):
        resolve_tz("Not/AZone")


def test_zoneinfo_available() -> None:
    """Windows 的 Python 需要 tzdata 包，否则 zoneinfo 完全不可用。"""
    from zoneinfo import available_timezones

    assert len(available_timezones()) > 0, "缺少 tzdata：Windows 上必须安装"


# ---------------------------------------------------------------------------
# 下一次运行时刻
# ---------------------------------------------------------------------------


def test_before_target_schedules_today() -> None:
    result = next_run_at(daily_time="10:00", tz_name=TZ, now=at(9, 0))
    assert result.when == at(10, 0)
    assert result.catch_up is False


def test_after_target_never_ran_catches_up() -> None:
    """★ 已过点且今天没跑过 → 立即补跑（否则关机一晚白丢一天）。"""
    result = next_run_at(daily_time="10:00", tz_name=TZ, now=at(15, 0))
    assert result.catch_up is True
    assert result.when == at(15, 0)


def test_after_target_already_completed_schedules_tomorrow() -> None:
    result = next_run_at(
        daily_time="10:00", tz_name=TZ, now=at(15, 0), last_completed_at=at(10, 30)
    )
    assert result.catch_up is False
    assert result.when == at(10, 0, day=16), "今天已完成 → 排到明天 10:00"


def test_ran_today_before_target_schedules_today() -> None:
    """当天跑过但还没到点（例如凌晨补跑过）→ 仍按今天的点排。"""
    result = next_run_at(
        daily_time="10:00", tz_name=TZ, now=at(8, 0), last_completed_at=at(3, 0)
    )
    assert result.when == at(10, 0)


def test_min_gap_pushes_next_run_forward() -> None:
    """★ 最小间隔：刚尝试过就又排上 → 推后，避免双跑。

    场景：15:00 刚试过；下一个计划点是 15:30，但距今不足 120 分钟
    → 推到 17:00（= 15:00 + 最小间隔）。
    """
    just_tried = at(15, 0)
    result = next_run_at(
        daily_time="15:30", tz_name=TZ, now=at(15, 5), last_attempt_at=just_tried
    )
    assert result.when == just_tried + timedelta(minutes=DEFAULT_MIN_GAP_MINUTES)
    assert result.catch_up is False
    assert "不足" in result.reason


def test_min_gap_does_not_delay_a_later_plan() -> None:
    """间隔已过时不干预正常排程。"""
    result = next_run_at(
        daily_time="22:00", tz_name=TZ, now=at(15, 5), last_attempt_at=at(15, 0)
    )
    assert result.when == at(22, 0)


def test_min_gap_blocks_second_wakeup_while_first_still_running() -> None:
    """★ 官方"不保证恰好投递一次"：两次唤醒只隔几秒，第一次**还没结束**。

    只看"已完成"会误判成"今天没跑"→ 又跑一遍。
    关键：``last_attempt_at`` 必须能挡住这个窗口。
    """
    started = at(10, 0)
    # 第一轮仍在运行（未完成），但已尝试过 → 第二次唤醒必须被挡住
    check = is_due(
        daily_time="10:00", tz_name=TZ, now=at(10, 1),
        last_completed_at=None,
        last_attempt_at=started,
    )
    assert check.due is False, "最小间隔必须挡住并发窗口"

    nxt = next_run_at(
        daily_time="10:00", tz_name=TZ, now=at(10, 1),
        last_completed_at=None, last_attempt_at=started,
    )
    assert nxt.when == started + timedelta(minutes=DEFAULT_MIN_GAP_MINUTES)
    assert nxt.catch_up is False


def test_min_gap_zero_allows_immediate() -> None:
    result = next_run_at(
        daily_time="10:00", tz_name=TZ, now=at(15, 0), last_attempt_at=at(15, 0),
        min_gap_minutes=0,
    )
    assert result.catch_up is True


def test_yesterday_run_does_not_block_today() -> None:
    """昨天跑过不影响今天补跑。"""
    result = next_run_at(
        daily_time="10:00", tz_name=TZ, now=at(15, 0),
        last_completed_at=at(10, 0, day=14),
    )
    assert result.catch_up is True


def test_before_target_not_due() -> None:
    result = should_run_now(daily_time="10:00", tz_name=TZ, now=at(9, 0))
    assert result.catch_up is False
    assert result.when == at(10, 0)


def test_next_run_serializable() -> None:
    result = next_run_at(daily_time="10:00", tz_name=TZ, now=at(9, 0))
    assert isinstance(result, NextRun)
    payload = result.to_dict()
    assert "next_run_at" in payload
    assert "catch_up" in payload
    assert isinstance(payload["catch_up"], bool)


# ---------------------------------------------------------------------------
# 到期判定
# ---------------------------------------------------------------------------


def test_is_due_false_before_target() -> None:
    check = is_due(daily_time="10:00", tz_name=TZ, now=at(9, 59))
    assert check.due is False
    assert isinstance(check, DueCheck)


def test_is_due_true_at_target() -> None:
    check = is_due(daily_time="10:00", tz_name=TZ, now=at(10, 0))
    assert check.due is True


def test_is_due_false_when_already_completed_today() -> None:
    """★ 今天已干完 → 不到期（幂等的第一道防线；第二道是发布幂等键）。"""
    check = is_due(
        daily_time="10:00", tz_name=TZ, now=at(15, 0), last_completed_at=at(10, 30)
    )
    assert check.due is False


def test_is_due_catch_up_flagged() -> None:
    check = is_due(daily_time="10:00", tz_name=TZ, now=at(15, 0))
    assert check.due is True
    assert check.catch_up is True


def test_is_due_serializable() -> None:
    payload = is_due(daily_time="10:00", tz_name=TZ, now=at(15, 0)).to_dict()
    assert set(payload) == {"due", "catch_up", "reason", "next_run_at"}


def test_repeated_wakeups_same_day_only_first_is_due() -> None:
    """★ 官方"不保证恰好投递一次" → 两次唤醒只有第一次到期。"""
    first = is_due(daily_time="10:00", tz_name=TZ, now=at(15, 0))
    assert first.due is True
    # 第一次跑完后（记了 last_run_at），第二次唤醒不再到期
    second = is_due(
        daily_time="10:00", tz_name=TZ, now=at(15, 10), last_completed_at=at(15, 0)
    )
    assert second.due is False


def test_second_wakeup_within_gap_is_not_due() -> None:
    """★ 同一天两次唤醒相隔很近（第一次未完成）→ 第二次不到期。"""
    check = is_due(
        daily_time="10:00", tz_name=TZ, now=at(10, 2),
        last_completed_at=None, last_attempt_at=at(10, 0),
    )
    assert check.due is False


def test_unknown_timezone_raises_not_silently_utc() -> None:
    with pytest.raises(ScheduleError):
        is_due(daily_time="10:00", tz_name="Bad/Zone", now=at(9, 0))


def test_wiring_note_documents_official_semantics() -> None:
    """交接说明必须写清官方三条语义（防被误当成"跑过了=做成了"）。"""
    assert "宿主运行期间" in SCHEDULE_WIRING_NOTE
    assert "不代表模型完成" in SCHEDULE_WIRING_NOTE
    assert "不保证恰好投递一次" in SCHEDULE_WIRING_NOTE


# ---------------------------------------------------------------------------
# 与存储层的衔接
# ---------------------------------------------------------------------------


def test_last_finished_run_at_none_when_no_runs(db: object) -> None:
    from core.storage import Database

    assert isinstance(db, Database)
    assert db.last_finished_run_at() is None


def test_last_finished_run_at_ignores_unfinished(db: object) -> None:
    """★ 只有**已结束**的运行才算数，避免把崩溃的轮次当成"今天跑过了"。"""
    from core.storage import Database

    assert isinstance(db, Database)
    db.start_run("schedule")  # 故意不 finish
    assert db.last_finished_run_at() is None


def test_last_finished_run_at_returns_latest(db: object) -> None:
    from core.storage import Database

    assert isinstance(db, Database)
    first = db.start_run("schedule")
    db.finish_run(first, conclusion="ok")
    assert db.last_finished_run_at() is not None

    second = db.start_run("schedule")
    db.finish_run(second, conclusion="ok")
    latest = db.last_finished_run_at()
    assert latest is not None
    assert latest >= db.get_run(first)["started_at"]


def test_wiring_doc_is_in_sync_with_module() -> None:
    """★ 接线文档必须与模块常量一致（防文档漂移）。"""
    from pathlib import Path

    from core.schedule import SCHEDULE_WIRING_NOTE

    doc = Path(__file__).resolve().parent.parent / "docs" / "schedule-wiring.md"
    assert doc.exists(), "docs/schedule-wiring.md 缺失"
    text = doc.read_text(encoding="utf-8")
    assert SCHEDULE_WIRING_NOTE.strip() in text, (
        "接线文档与 core/schedule.py 的 SCHEDULE_WIRING_NOTE 不一致 —— "
        "改了常量就要重新生成文档"
    )


# ---------------------------------------------------------------------------
# on_host_start 模式（★ 主人选定：打开 DSH 时唤醒）
# ---------------------------------------------------------------------------


def test_host_start_due_when_not_completed_today() -> None:
    from core.schedule import is_due_on_host_start

    check = is_due_on_host_start(tz_name=TZ, now=at(9, 0))
    assert check.due is True
    assert check.catch_up is True


def test_host_start_not_due_when_completed_today() -> None:
    from core.schedule import is_due_on_host_start

    check = is_due_on_host_start(
        tz_name=TZ, now=at(20, 0), last_completed_at=at(9, 30)
    )
    assert check.due is False
    assert "不重复跑" in check.reason


def test_host_start_debounces_repeated_launches() -> None:
    """★ 反复开关 DSH 不该反复跑（防抖用 last_attempt）。"""
    from core.schedule import is_due_on_host_start

    check = is_due_on_host_start(
        tz_name=TZ, now=at(9, 10), last_completed_at=None, last_attempt_at=at(9, 0)
    )
    assert check.due is False
    assert "防抖" in check.reason


def test_host_start_ignores_daily_time() -> None:
    """该模式**不看**每日时刻：凌晨打开也该跑（今天没跑完）。"""
    from core.schedule import is_due_on_host_start

    check = is_due_on_host_start(tz_name=TZ, now=at(3, 0))
    assert check.due is True


def test_evaluate_due_dispatches_by_mode() -> None:
    from core.schedule import evaluate_due

    host = evaluate_due(
        mode="on_host_start", daily_time="10:00", tz_name=TZ, now=at(3, 0)
    )
    assert host.due is True, "on_host_start：凌晨也该跑"

    daily = evaluate_due(mode="daily", daily_time="10:00", tz_name=TZ, now=at(3, 0))
    assert daily.due is False, "daily：凌晨未到点"


def test_evaluate_due_rejects_unknown_mode() -> None:
    from core.schedule import evaluate_due

    with pytest.raises(ScheduleError, match="未知 schedule.mode"):
        evaluate_due(mode="hourly", daily_time="10:00", tz_name=TZ)


def test_default_mode_is_host_start() -> None:
    """★ 主人明确要求"打开 DSH 时唤醒" → 默认必须是 on_host_start。"""
    from core.config import Config

    assert Config().schedule.mode == "on_host_start"


def test_push_times_are_not_the_wake_schedule() -> None:
    """推送时间（10:00/17:00）是**汇报节奏**，不是唤醒时机。"""
    from core.config import Config

    config = Config()
    assert config.report.push_times == ["10:00", "17:00"]
    assert config.schedule.mode == "on_host_start"


# ---------------------------------------------------------------------------
# ★ 谷时窗口（P0，2026-10-16 加：来自竞品精读的省钱项）
# ---------------------------------------------------------------------------
#
# 官方口径：**工作日 9–12 与 14–18 是高峰，其余（含整个周末）都是谷时**。
# 竞品把窗口写死成两段（noon/night），我们直接按官方口径反推 —— 不用维护时段表。


def test_valley_window_weekday_peaks() -> None:
    """工作日的两段高峰必须被判为"非谷时"。"""
    from core.schedule import in_valley_window

    # 2026-10-14 是周三（weekday=2）
    assert in_valley_window(at(9, 0, day=14)) is False, "9:00 是高峰起点"
    assert in_valley_window(at(10, 30, day=14)) is False, "上午高峰内"
    assert in_valley_window(at(11, 59, day=14)) is False, "上午高峰末尾"
    assert in_valley_window(at(14, 0, day=14)) is False, "14:00 是下午高峰起点"
    assert in_valley_window(at(17, 59, day=14)) is False, "下午高峰末尾"


def test_valley_window_weekday_off_peaks() -> None:
    """高峰之外的时间都是谷时。"""
    from core.schedule import in_valley_window

    assert in_valley_window(at(0, 0, day=14)) is True, "凌晨"
    assert in_valley_window(at(8, 59, day=14)) is True, "上午高峰之前"
    assert in_valley_window(at(12, 0, day=14)) is True, "午间（12:00 起）"
    assert in_valley_window(at(13, 59, day=14)) is True, "午间末尾"
    assert in_valley_window(at(18, 0, day=14)) is True, "18:00 起进入谷时"
    assert in_valley_window(at(23, 59, day=14)) is True, "深夜"


def test_valley_window_weekend_is_all_valley() -> None:
    """周末全天都是谷时 —— 即使处在"工作日高峰"的钟点上。"""
    from core.schedule import in_valley_window

    # 2026-10-17 周六、10-18 周日
    assert in_valley_window(at(10, 0, day=17)) is True, "周六上午（工作日是高峰）"
    assert in_valley_window(at(15, 0, day=17)) is True, "周六下午"
    assert in_valley_window(at(10, 0, day=18)) is True, "周日上午"
    assert in_valley_window(at(15, 0, day=18)) is True, "周日下午"


def test_next_valley_start_returns_self_when_already_in_valley() -> None:
    from core.schedule import next_valley_start

    now = at(20, 30, day=14)  # 周三晚上，已是谷时
    assert next_valley_start(now) == now


def test_next_valley_start_from_morning_peak() -> None:
    """上午高峰（10:30）→ 下一个谷时起点应是 12:00。"""
    from core.schedule import next_valley_start

    nxt = next_valley_start(at(10, 30, day=14))
    assert (nxt.hour, nxt.minute) == (12, 0), f"实际 {nxt}"


def test_next_valley_start_from_afternoon_peak() -> None:
    """下午高峰（15:00）→ 下一个谷时起点应是 18:00。"""
    from core.schedule import next_valley_start

    nxt = next_valley_start(at(15, 0, day=14))
    assert (nxt.hour, nxt.minute) == (18, 0), f"实际 {nxt}"


def test_peak_valley_off_still_runs_in_peak() -> None:
    """★ 开关**默认关**：关着时峰时照样跑（不改变主人原本的行为）。"""
    from core.schedule import evaluate_due

    check = evaluate_due(
        mode="on_host_start",
        daily_time="10:00",
        tz_name=TZ,
        now=at(10, 30, day=14),  # 周三上午高峰
        peak_valley=False,
    )
    assert check.due is True, "没开谷时闸门就该照跑"


def test_peak_valley_on_blocks_in_peak() -> None:
    """★ 开着时：到期但处在峰时 → due=False，理由里给出下一个谷时起点。"""
    from core.schedule import evaluate_due

    check = evaluate_due(
        mode="on_host_start",
        daily_time="10:00",
        tz_name=TZ,
        now=at(10, 30, day=14),
        peak_valley=True,
    )
    assert check.due is False, "峰时不该跑"
    assert "高峰" in check.reason
    assert "12:00" in check.reason, f"应给出下一个谷时起点，实际：{check.reason}"


def test_peak_valley_on_allows_in_valley() -> None:
    """开着闸门时，谷时内照常到期。"""
    from core.schedule import evaluate_due

    check = evaluate_due(
        mode="on_host_start",
        daily_time="10:00",
        tz_name=TZ,
        now=at(20, 0, day=14),  # 周三晚上，谷时
        peak_valley=True,
    )
    assert check.due is True


def test_peak_valley_does_not_override_completed_today() -> None:
    """已经跑完的日子，开着闸门也不该变成"该跑"。"""
    from core.schedule import evaluate_due

    check = evaluate_due(
        mode="on_host_start",
        daily_time="10:00",
        tz_name=TZ,
        now=at(20, 0, day=14),
        last_completed_at=at(13, 0, day=14),
        peak_valley=True,
    )
    assert check.due is False, "今天已完成 → 不该跑（与闸门无关）"


def test_peak_valley_default_is_off() -> None:
    """★ 默认必须是关的 —— 不擅自改变主人"打开 DSH 就干活"的设定。"""
    from core.config import Config

    assert Config().schedule.peak_valley is False


# ---------------------------------------------------------------------------
# ★ 持续跑（mode="continuous"，2026-10-17）
# ---------------------------------------------------------------------------


def test_continuous_runs_when_interval_elapsed() -> None:
    """间隔已满 → 该跑（持续模式的核心：今天还要接着做）。"""
    from core.schedule import evaluate_due

    check = evaluate_due(
        mode="continuous",
        daily_time="10:00",
        tz_name=TZ,
        now=at(15, 0, day=14),
        last_attempt_at=at(14, 20, day=14),  # 40 分钟前
        interval_minutes=30,
        max_runs_per_day=8,
        runs_today=0,
    )
    assert check.due is True
    assert "持续模式" in check.reason


def test_continuous_waits_when_interval_not_elapsed() -> None:
    """★ 间隔未满 → 不跑（这是"持续跑"不等于"高频访问"的关键）。"""
    from core.schedule import evaluate_due

    check = evaluate_due(
        mode="continuous",
        daily_time="10:00",
        tz_name=TZ,
        now=at(15, 0, day=14),
        last_attempt_at=at(14, 50, day=14),  # 才 10 分钟
        interval_minutes=30,
        max_runs_per_day=8,
        runs_today=0,
    )
    assert check.due is False
    assert "不足" in check.reason
    # 要告诉人"下一轮什么时候"
    assert check.next_run_at


def test_continuous_stops_when_daily_cap_reached() -> None:
    """★★ 跑满每日上限 → **自动停**（这是"不做无限循环"的保证）。"""
    from core.schedule import evaluate_due

    check = evaluate_due(
        mode="continuous",
        daily_time="10:00",
        tz_name=TZ,
        now=at(23, 0, day=14),
        last_attempt_at=at(22, 0, day=14),  # 间隔早就够了
        interval_minutes=30,
        max_runs_per_day=8,
        runs_today=8,
    )
    assert check.due is False, "跑满必须停，哪怕间隔已够"
    assert "自动停" in check.reason


def test_continuous_ignores_completed_today() -> None:
    """★ 与 on_host_start 的关键差别：**今天做过也要接着做**。"""
    from core.schedule import evaluate_due

    check = evaluate_due(
        mode="continuous",
        daily_time="10:00",
        tz_name=TZ,
        now=at(15, 0, day=14),
        last_completed_at=at(14, 0, day=14),  # 今天已跑完一轮
        last_attempt_at=at(14, 0, day=14),
        interval_minutes=30,
        max_runs_per_day=8,
        runs_today=1,
    )
    assert check.due is True, "持续模式下今天做过不代表不再做"


def test_continuous_cap_takes_priority_over_interval() -> None:
    """跑满优先于间隔判断（顺序不能反，否则跑满后还会因为"间隔够"而跑）。"""
    from core.schedule import evaluate_due

    check = evaluate_due(
        mode="continuous",
        daily_time="10:00",
        tz_name=TZ,
        now=at(23, 0, day=14),
        last_attempt_at=at(1, 0, day=14),
        interval_minutes=5,
        max_runs_per_day=1,
        runs_today=1,
    )
    assert check.due is False
    assert "自动停" in check.reason, "应该是「跑满」而不是「间隔」"


def test_continuous_defaults_are_conservative() -> None:
    """★ 默认值必须**保守**：间隔不短、轮次有上限。

    这是"不做无限循环"的默认保证 —— 主人要更激进可以自己调，
    但**开箱默认不该是高强度访问**。
    """
    from core.schedule import DEFAULT_CONTINUOUS_INTERVAL_MIN, DEFAULT_CONTINUOUS_MAX_RUNS

    assert DEFAULT_CONTINUOUS_INTERVAL_MIN >= 15, "间隔太短会构成高频访问"
    assert 1 <= DEFAULT_CONTINUOUS_MAX_RUNS <= 24, "必须有上限"


def test_continuous_config_defaults() -> None:
    """配置默认值：mode 仍是 on_host_start（**不擅自改成持续跑**）。"""
    from core.config import Config

    cfg = Config()
    assert cfg.schedule.mode == "on_host_start", "默认不该是持续跑"
    assert cfg.schedule.interval_minutes >= 5
    assert cfg.schedule.max_runs_per_day >= 1


def test_continuous_config_rejects_too_fast_interval() -> None:
    """★ 间隔有下限 —— 防止把"持续跑"配成高频轮询。"""
    from pydantic import ValidationError

    from core.config import ScheduleConfig

    with pytest.raises(ValidationError):
        ScheduleConfig(interval_minutes=1)
    with pytest.raises(ValidationError):
        ScheduleConfig(max_runs_per_day=0)


def test_unknown_mode_still_raises() -> None:
    """未知 mode 仍要明确报错（错误信息里列出 continuous）。"""
    from core.schedule import evaluate_due

    with pytest.raises(ScheduleError) as excinfo:
        evaluate_due(mode="nonsense", daily_time="10:00", tz_name=TZ)
    assert "continuous" in str(excinfo.value)
