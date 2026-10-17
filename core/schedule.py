"""定时调度：下次运行时刻、错过轮次的补偿、单实例语义。

任务书 §7 任务 7 要求"先完成手动 ``run_once``，再接与本机 DSH 版本兼容的日程能力"，
并**明确三条官方语义**（引自 DSH 官方 Schedule 说明）：

1. **宿主运行期间才会调度** —— DSH 没开就不会触发；
2. **发送记录不代表模型完成** —— 定时提醒的投递 ≠ 任务做成；
3. **不保证恰好投递一次** —— 可能重复或遗漏。

因此本模块只负责**算时间**与**判断该不该跑**；
真正的"做没做成"以 ``runs``/``actions`` 表为准（见 :mod:`core.report`）。

## 错过轮次的处理

宿主机停机时错过的时刻**不排队**（与 DSH 一致），但如果当天该跑的还没跑，
下一次唤醒应当**补跑一次**（``should_run_now`` 返回 True 且带 ``catch_up`` 标记），
而不是直接跳到明天——否则关机一晚就白丢一天。
**补跑不会重复发布**：那由幂等键与 ``uncertain`` 冻结保证（见 :mod:`core.publish`）。

## 为什么区分"已完成"与"已尝试"

官方语义"**不保证恰好投递一次**"意味着两次唤醒可能只隔几秒。
若只按"上一次**完成**"判断，第一次运行还没结束时第二次唤醒就会
误判成"今天没跑"→ 又跑一遍。因此：

* "今天干完了吗" 用 ``last_completed_at``（**只算完成的**，崩溃轮次不算数）；
* "刚刚是不是已经唤醒过" 用 ``last_attempt_at``（**含未完成的**）+ 最小间隔。

两道防线的分工：最小间隔挡住**并发窗口**，幂等键挡住**重复发布**。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

#: 一次运行结束后，多久之内不重复触发（防"投递两次"造成双跑）
DEFAULT_MIN_GAP_MINUTES = 120

#: 持续跑模式：两轮之间的默认间隔（分钟）。
#: ★ 刻意**不短**（30 分钟）—— 持续跑不等于高频访问；
#: 本项目红线是不加大请求强度，所以用"间隔换总量"。
DEFAULT_CONTINUOUS_INTERVAL_MIN = 30
#: 持续跑模式：每天默认最多几轮（到点**自动停**，不需要人盯着）
DEFAULT_CONTINUOUS_MAX_RUNS = 8

# ---------------------------------------------------------------------------
# ★ 谷时窗口（DeepSeek 官方峰谷口径）
# ---------------------------------------------------------------------------
#
# 官方口径：**工作日 9–12 与 14–18 是高峰，其余时间（含整个周末）都是谷时**。
# 谷时跑一轮能省钱，而"什么时候跑"本来就是我们自己说了算 —— 没有理由在峰时烧钱。
#
# ⚠️ 与竞品（dsh-bili-scroll）的差异：
#   它把窗口写死成 `noon 12:00–14:00` + `night 18:00–09:00` 两段，
#   我们直接按**官方口径反推**（峰时之外全是谷时），因此**不用维护时段表**，
#   官方调价时只要改下面两个高峰窗口即可。

#: 工作日的**高峰**时段（左闭右开）。其余时间均为谷时。
PEAK_WINDOWS: tuple[tuple[tuple[int, int], tuple[int, int]], ...] = (
    ((9, 0), (12, 0)),
    ((14, 0), (18, 0)),
)

#: 周末全天都是谷时（`date.weekday()`：5=周六，6=周日）
WEEKEND_IS_ALL_VALLEY = True


class ScheduleError(ValueError):
    """调度配置不合法。"""


def parse_daily_time(value: str) -> time:
    """解析 ``"HH:MM"``。"""
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ScheduleError(f"daily_time 必须是 HH:MM，收到 {value!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ScheduleError(f"daily_time 含非数字：{value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(f"daily_time 超出范围：{value!r}")
    return time(hour=hour, minute=minute)


def resolve_tz(name: str) -> tzinfo:
    """把 IANA 时区名解析成 :class:`~datetime.tzinfo`。

    **无法解析时明确报错**，绝不静默退到 UTC——静默回退会让"每天 10:00"
    在实际 18:00 触发，这类错误极难被发现。

    ⚠️ Windows 的 Python **不自带** IANA 时区库，需要 ``tzdata`` 包；
    缺它时本函数会以明确的 ScheduleError 报出来（而不是让调度静默错位）。
    """
    try:
        return ZoneInfo(name)
    except Exception as exc:  # noqa: BLE001 - zoneinfo 抛的具体类型随平台而异
        raise ScheduleError(
            f"无法解析时区 {name!r}：{exc}。"
            "Windows 上通常是因为缺少 tzdata 包（pip install tzdata）。"
        ) from exc


@dataclass(frozen=True)
class NextRun:
    """下一次应当运行的时刻。"""

    when: datetime
    catch_up: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "next_run_at": self.when.isoformat(timespec="seconds"),
            "catch_up": self.catch_up,
            "reason": self.reason,
        }


def next_run_at(
    *,
    daily_time: str,
    tz_name: str,
    now: datetime | None = None,
    last_completed_at: datetime | None = None,
    last_attempt_at: datetime | None = None,
    min_gap_minutes: int = DEFAULT_MIN_GAP_MINUTES,
) -> NextRun:
    """算出下一次该跑的时刻。

    两个"上次"参数承担**不同**职责，不能混用：

    :param last_completed_at: 上一次**跑完**的时刻。回答"今天这一轮的活干完了吗"。
        只用已完成的运行，避免把崩溃的轮次当成"今天干过了"。
    :param last_attempt_at: 上一次**尝试开始**的时刻（含崩溃/未完成的）。
        用于 :data:`min_gap_minutes` 最小间隔——这是防"重复投递造成双跑"的关键：
        官方 Schedule **不保证恰好投递一次**，两次唤醒可能相隔几秒，
        此时第一次运行可能**还没结束**，只看 completed 会误判为"今天没跑"而再跑一遍。

    :param now: 当前时刻（必须带 tzinfo；省略则取该时区的现在）。
    """
    tz = resolve_tz(tz_name)
    target = parse_daily_time(daily_time)

    current = now.astimezone(tz) if now is not None else datetime.now(tz)
    today_at = current.replace(
        hour=target.hour, minute=target.minute, second=0, microsecond=0
    )

    # ① 今天还没到点 → 今天跑
    if current < today_at:
        when, catch_up, reason = today_at, False, "今天尚未到点"
    else:
        # ② 已过点：若今天**还没干完**，补跑（带 catch_up 标记）
        completed_today = (
            last_completed_at is not None
            and last_completed_at.astimezone(tz).date() == current.date()
        )
        if not completed_today:
            when, catch_up, reason = current, True, "已过点且今天尚未完成 → 立即补跑"
        else:
            # ③ 今天已干完 → 明天
            when = today_at + timedelta(days=1)
            catch_up, reason = False, "今天已运行完成 → 排到明天"

    # ④ 最小间隔：只管**尝试**，把刚唤醒过的窗口挡掉（含仍在运行的那一轮）
    if last_attempt_at is not None:
        earliest = last_attempt_at.astimezone(tz) + timedelta(minutes=min_gap_minutes)
        if when < earliest:
            return NextRun(
                when=earliest,
                catch_up=False,
                reason=(
                    f"距上次尝试不足 {min_gap_minutes} 分钟 → 推后"
                    "（防重复投递造成双跑）"
                ),
            )

    return NextRun(when=when, catch_up=catch_up, reason=reason)


def should_run_now(
    *,
    daily_time: str,
    tz_name: str,
    now: datetime | None = None,
    last_completed_at: datetime | None = None,
    last_attempt_at: datetime | None = None,
    min_gap_minutes: int = DEFAULT_MIN_GAP_MINUTES,
) -> NextRun:
    """判断"现在是否该跑"，并给出理由。

    ``catch_up=True`` 表示这是补偿性的一轮（宿主机停机错过）。
    语义与参数见 :func:`next_run_at`。
    """
    return next_run_at(
        daily_time=daily_time,
        tz_name=tz_name,
        now=now,
        last_completed_at=last_completed_at,
        last_attempt_at=last_attempt_at,
        min_gap_minutes=min_gap_minutes,
    )


@dataclass(frozen=True)
class DueCheck:
    """``due`` 的判定结果。"""

    due: bool
    catch_up: bool
    reason: str
    next_run_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "due": self.due,
            "catch_up": self.catch_up,
            "reason": self.reason,
            "next_run_at": self.next_run_at,
        }


def is_due(
    *,
    daily_time: str,
    tz_name: str,
    now: datetime | None = None,
    last_completed_at: datetime | None = None,
    last_attempt_at: datetime | None = None,
    min_gap_minutes: int = DEFAULT_MIN_GAP_MINUTES,
) -> DueCheck:
    """现在是否到了该跑的时刻。

    与 :func:`next_run_at` 的区别：这里回答"是否已经到期"（用于唤醒时判断），
    而后者回答"下一次是什么时候"（用于排程展示）。
    """
    tz = resolve_tz(tz_name)
    current = now.astimezone(tz) if now is not None else datetime.now(tz)
    nxt = next_run_at(
        daily_time=daily_time,
        tz_name=tz_name,
        now=now,
        last_completed_at=last_completed_at,
        last_attempt_at=last_attempt_at,
        min_gap_minutes=min_gap_minutes,
    )
    due = current >= nxt.when
    return DueCheck(
        due=due,
        catch_up=nxt.catch_up if due else False,
        reason=nxt.reason,
        next_run_at=nxt.when.isoformat(timespec="seconds"),
    )



def is_due_on_host_start(
    *,
    tz_name: str,
    now: datetime | None = None,
    last_completed_at: datetime | None = None,
    last_attempt_at: datetime | None = None,
    min_gap_minutes: int = DEFAULT_MIN_GAP_MINUTES,
) -> DueCheck:
    """**打开 DSH 时唤醒**模式的到期判定（主人选的模式）。

    规则很简单、也刻意保守：

    * 今天**还没跑完** → 到期（该干活）；
    * 今天已跑完 → 不到期；
    * 距上次**尝试**不足 ``min_gap_minutes`` → 不到期（防抖：
      反复开关 DSH 不该反复跑）。

    与 :func:`is_due` 的区别：不看"每日固定时刻"，只看"今天做完没有"。
    """
    tz = resolve_tz(tz_name)
    current = now.astimezone(tz) if now is not None else datetime.now(tz)

    completed_today = (
        last_completed_at is not None
        and last_completed_at.astimezone(tz).date() == current.date()
    )
    if completed_today:
        return DueCheck(
            due=False, catch_up=False,
            reason="今天已完成过一轮 → 打开 DSH 不重复跑",
            next_run_at=current.isoformat(timespec="seconds"),
        )

    if last_attempt_at is not None:
        earliest = last_attempt_at.astimezone(tz) + timedelta(minutes=min_gap_minutes)
        if current < earliest:
            return DueCheck(
                due=False, catch_up=False,
                reason=(
                    f"距上次尝试不足 {min_gap_minutes} 分钟 → 防抖跳过"
                    "（避免反复开关 DSH 反复跑）"
                ),
                next_run_at=earliest.isoformat(timespec="seconds"),
            )

    return DueCheck(
        due=True,
        catch_up=True,
        reason="打开 DSH 且今天尚未完成 → 立即跑一轮",
        next_run_at=current.isoformat(timespec="seconds"),
    )


def in_valley_window(current: datetime) -> bool:
    """现在是不是**谷时**（DeepSeek 官方峰谷口径）。

    规则：**工作日 9–12 与 14–18 是高峰，其余（含整个周末）都是谷时**。

    :param current: **已带时区**的时刻（调用方负责换算到配置时区）
    """
    if WEEKEND_IS_ALL_VALLEY and current.weekday() >= 5:
        return True

    minutes = current.hour * 60 + current.minute
    in_peak = any(
        start_h * 60 + start_m <= minutes < end_h * 60 + end_m
        for (start_h, start_m), (end_h, end_m) in PEAK_WINDOWS
    )
    return not in_peak


def next_valley_start(current: datetime) -> datetime:
    """下一个谷时窗口的起点（`current` 已在谷时则返回它自己）。

    实现刻意朴素：**按分钟向前找**，最多找 24 小时。
    好处是不用维护"跨零点"之类的特例（竞品的 `inWindow()` 就要处理跨零点）。
    """
    if in_valley_window(current):
        return current
    probe = current
    for _ in range(24 * 60):
        probe = probe + timedelta(minutes=1)
        # 把秒归零，给出一个干净的"起点"
        if in_valley_window(probe):
            return probe.replace(second=0, microsecond=0)
    return current


def is_due_continuous(
    *,
    tz_name: str,
    now: datetime | None = None,
    # ⚠️ 刻意**不用** ``last_completed_at``：持续模式的语义是"今天接着做"，
    #    所以只看"距上次**尝试**多久"，不看"今天做完没有"。
    #    保留这个参数是为了与 :func:`is_due` / :func:`is_due_on_host_start`
    #    签名对齐（:func:`evaluate_due` 统一转发）。
    last_completed_at: datetime | None = None,  # noqa: ARG001
    last_attempt_at: datetime | None = None,
    interval_minutes: int = DEFAULT_CONTINUOUS_INTERVAL_MIN,
    max_runs_per_day: int = DEFAULT_CONTINUOUS_MAX_RUNS,
    runs_today: int = 0,
) -> DueCheck:
    """**持续跑**模式的到期判定（2026-10-17，主人要求）。

    主人原话：「每天跑一轮太少了，而且跑一轮速度太快了，能不能出一个让其一直跑的开关」

    ★ 刻意**不做真正的无限循环** —— 那会明显增加请求量与风控风险
    （本项目红线：不加大重试强度、不绕过风控）。
    这里是"**每 N 分钟一轮 + 每天总轮次上限 + 到点自动停**"：

    1. **今天已跑满 ``max_runs_per_day`` 轮** → 不到期（**自动停**，附人话说明）；
    2. 距上次**尝试**不足 ``interval_minutes`` → 不到期（等下一轮）；
    3. 否则到期。

    ⚠️ 与 ``on_host_start`` 的关键差别：**不看"今天做过没有"** ——
    持续模式的语义就是"今天还要接着做"。
    防重入靠 ``interval_minutes`` + 调用方的任务锁（``core.lock``）。

    Args:
        runs_today: 今天**已完成**的轮次（由调用方从 ``runs`` 表数出来）。
    """
    tz = resolve_tz(tz_name)
    current = now.astimezone(tz) if now is not None else datetime.now(tz)

    if runs_today >= max_runs_per_day:
        return DueCheck(
            due=False,
            catch_up=False,
            reason=(
                f"持续模式：今天已跑满 {runs_today}/{max_runs_per_day} 轮 → **自动停**"
                "（明天继续；想多跑就调大 schedule.max_runs_per_day）"
            ),
            next_run_at=current.isoformat(timespec="seconds"),
        )

    if last_attempt_at is not None:
        earliest = last_attempt_at.astimezone(tz) + timedelta(minutes=interval_minutes)
        if current < earliest:
            return DueCheck(
                due=False,
                catch_up=False,
                reason=(
                    f"持续模式：距上轮不足 {interval_minutes} 分钟 → 等下一轮"
                    f"（{earliest.isoformat(timespec='seconds')}）"
                ),
                next_run_at=earliest.isoformat(timespec="seconds"),
            )

    return DueCheck(
        due=True,
        catch_up=False,
        reason=(
            f"持续模式：间隔已满（{interval_minutes} 分钟），"
            f"今天第 {runs_today + 1}/{max_runs_per_day} 轮"
        ),
        next_run_at=current.isoformat(timespec="seconds"),
    )


def evaluate_due(
    *,
    mode: str,
    daily_time: str,
    tz_name: str,
    now: datetime | None = None,
    last_completed_at: datetime | None = None,
    last_attempt_at: datetime | None = None,
    min_gap_minutes: int = DEFAULT_MIN_GAP_MINUTES,
    peak_valley: bool = False,
    interval_minutes: int = DEFAULT_CONTINUOUS_INTERVAL_MIN,
    max_runs_per_day: int = DEFAULT_CONTINUOUS_MAX_RUNS,
    runs_today: int = 0,
) -> DueCheck:
    """按配置的 ``schedule.mode`` 分派。

    * ``on_host_start`` —— 打开 DSH 时唤醒（**主人选的**）
    * ``daily`` —— 每日固定时刻（保留备用）
    * ``continuous`` —— **持续跑**：每 ``interval_minutes`` 一轮，
      每天最多 ``max_runs_per_day`` 轮，到点自动停（2026-10-17 新增）

    ``peak_valley=True`` 时，额外要求"**现在处于谷时窗口**"才真跑；
    峰时会被挡下并给出 ``WAIT_FOR_VALLEY`` 语义的理由（见 :func:`in_valley_window`）。
    """
    if mode == "on_host_start":
        check = is_due_on_host_start(
            tz_name=tz_name,
            now=now,
            last_completed_at=last_completed_at,
            last_attempt_at=last_attempt_at,
            min_gap_minutes=min_gap_minutes,
        )
    elif mode == "daily":
        check = is_due(
            daily_time=daily_time,
            tz_name=tz_name,
            now=now,
            last_completed_at=last_completed_at,
            last_attempt_at=last_attempt_at,
            min_gap_minutes=min_gap_minutes,
        )
    elif mode == "continuous":
        check = is_due_continuous(
            tz_name=tz_name,
            now=now,
            last_completed_at=last_completed_at,
            last_attempt_at=last_attempt_at,
            interval_minutes=interval_minutes,
            max_runs_per_day=max_runs_per_day,
            runs_today=runs_today,
        )
    else:
        raise ScheduleError(
            f"未知 schedule.mode：{mode!r}（应为 on_host_start / daily / continuous）"
        )

    if not (peak_valley and check.due):
        return check

    # ★ 谷时闸门：到期了，但现在是峰时 → 等
    tz = resolve_tz(tz_name)
    current = now.astimezone(tz) if now is not None else datetime.now(tz)
    if in_valley_window(current):
        return check

    wait_until = next_valley_start(current)
    return DueCheck(
        due=False,
        catch_up=False,
        reason=(
            "现在处于 DeepSeek 高峰时段 → 等到谷时再跑（省钱）"
            f"；下一个谷时起点 {wait_until.isoformat(timespec='seconds')}"
        ),
        next_run_at=wait_until.isoformat(timespec="seconds"),
    )


SCHEDULE_WIRING_NOTE = """\
## 接到 DSH 的日程能力

任务书 §7 任务 7 引用官方 Schedule 说明的**三条语义**（本项目据此设计）：

1. **宿主运行期间才调度** —— DSH 关闭时不会触发；
2. **发送记录不代表模型完成** —— 定时提醒投递成功 ≠ 任务做成；
3. **不保证恰好投递一次** —— 可能重复或遗漏。

因此交接点是这样的：

| 关注点 | 由谁负责 |
|---|---|
| "什么时候唤醒" | DSH Schedule（宿主级） |
| "这一轮该不该跑" | 本项目 `is_due()`（读 `runs` 表判断今天是否跑完） |
| "有没有重复发布" | 本项目幂等键 + `uncertain` 冻结（**不依赖**调度器） |
| "任务做成没有" | 本项目 `runs`/`actions` 表（**最终事实来源**） |

**推荐接线**（人用，需主人授权改 profile）：

0. **唤醒时机（主人已定）**：`schedule.mode: on_host_start` ——
   **打开 DSH 时唤醒**，而不是每日固定时刻。
   打开后先调 `companion_status`，看 `schedule.due`：
   * `due=true` → 今天还没跑完，干活；
   * `due=false` → 今天已完成（或距上次尝试不足 `min_gap_minutes`）→ 跳过。
   `schedule.daily_time` 仅在 `mode: daily` 时生效。
1. 用 `dsh/cordis.patch.fragment.yml` 里的 `bili-companion` preset
   （只挂窄 MCP 工具，不含通用浏览器/shell）。
2. 在 DSH 的日程里设一个与 `config.yaml` 的 `schedule.daily_time` 一致的唤醒点。
3. 唤醒后在**那个 preset 的会话**里让模型先调 `companion_status`，
   再按需 `prepare_batch`。

> ⚠️ **不要**把 `@daily` 的唤醒时间既写在 DSH 里又写成系统计划任务——
> 两个调度器同时唤醒仍会被本项目的单实例锁挡住（不会双发），
> 但会产生令人困惑的失败记录。选一个。

> ⚠️ 由于"不保证恰好投递一次"，本项目**不假设**每天只被唤醒一次：
> `is_due`（含最小间隔）+ 幂等键保证重复唤醒既不重复学习也不重复发言。
"""
