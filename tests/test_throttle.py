"""分频道节流测试（2026-10-17）。

## 背景

主人要求：*"进行实践时也要看看他出现过的问题不要再犯，
同时找找其他相似的项目看看它们的实现方式"*。

这里实现的三件事，全部**照抄参考项目**（不是自创）：

| 来源 | 抄了什么 |
|---|---|
| `bilibili_learning_bot/brain/monitor.py` | **分频道独立间隔** + `-509` 退避 10s |
| `bilibili_learning_bot/api/throttle.py` | **`-799` 全局冷却 90~180s** + **随机抖动** |
| `bilibili_learning_bot/api/client.py:67` | **`buvid3` 格式校验**（格式错 → B站**永久** -799） |

## 钉死的不变量

* **频道隔离**：私信查得勤不该拖慢评论；
* **`-799` 是全站级**：命中后**所有**频道一起停；
* **冷却不叠加**：连续命中不该越等越久；
* **抖动**：间隔不是固定值（固定节奏像爬虫）；
* **`buvid3` 格式**：不符合 UUID+infoc 就要拒绝（否则永久限流）。
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from core.throttle import (
    BACKOFF_509_SECONDS,
    CODE_RATE_509,
    CODE_RATE_799,
    COOLDOWN_799_MAX,
    COOLDOWN_799_MIN,
    DEFAULT_INTERVALS,
    JITTER_MAX_SECONDS,
    Throttle,
    check_buvid3_format,
    is_rate_limit_error,
)


def make_throttle(seed: int = 42) -> Throttle:
    return Throttle(rng=random.Random(seed))


# ---------------------------------------------------------------------------
# 频道隔离（最核心）
# ---------------------------------------------------------------------------


def test_first_call_never_waits() -> None:
    throttle = make_throttle()
    assert throttle.wait_seconds("messages", now=1000.0) == 0.0


def test_same_channel_respects_interval() -> None:
    throttle = make_throttle()
    throttle.mark_call("messages", now=1000.0)
    wait = throttle.wait_seconds("messages", now=1000.0)
    assert wait > 0, "刚调完不该立刻允许再调"
    assert wait >= DEFAULT_INTERVALS["messages"], "至少等于该频道的间隔"


def test_channels_are_independent() -> None:
    """★★ 核心：一个频道刚调完，**不该**影响另一个频道。

    这就是我们要修的问题 —— 之前评论/私信共用一个限速桶。
    """
    throttle = make_throttle()
    throttle.mark_call("messages", now=1000.0)

    assert throttle.wait_seconds("messages", now=1000.0) > 0
    assert throttle.wait_seconds("comments", now=1000.0) == 0.0, (
        "私信刚调完，评论不该被拖累"
    )


def test_interval_elapsed_allows_call() -> None:
    throttle = make_throttle()
    throttle.mark_call("messages", now=1000.0)
    later = 1000.0 + DEFAULT_INTERVALS["messages"] + JITTER_MAX_SECONDS + 1
    assert throttle.wait_seconds("messages", now=later) == 0.0


def test_unknown_channel_uses_conservative_default() -> None:
    """未知频道用 `metadata` 的间隔（偏保守），不崩。"""
    throttle = make_throttle()
    assert throttle.interval_for("从未见过的频道") == DEFAULT_INTERVALS["metadata"]


# ---------------------------------------------------------------------------
# 抖动
# ---------------------------------------------------------------------------


def test_interval_has_jitter() -> None:
    """★ 间隔要带随机量 —— 固定节奏像爬虫。"""
    waits = set()
    for seed in range(12):
        throttle = make_throttle(seed)
        throttle.mark_call("messages", now=1000.0)
        waits.add(round(throttle.wait_seconds("messages", now=1000.0), 3))
    assert len(waits) > 1, "不同随机种子应产生不同等待时间（说明有抖动）"


def test_jitter_is_bounded() -> None:
    """抖动不能无限大（否则间隔形同虚设）。"""
    throttle = make_throttle()
    throttle.mark_call("messages", now=1000.0)
    wait = throttle.wait_seconds("messages", now=1000.0)
    upper = DEFAULT_INTERVALS["messages"] + JITTER_MAX_SECONDS + 0.001
    assert wait <= upper


# ---------------------------------------------------------------------------
# -509 / -799
# ---------------------------------------------------------------------------


def test_backoff_509_is_per_channel() -> None:
    """`-509` 只让**该频道**退避，不牵连别的频道。"""
    throttle = make_throttle()
    waited = throttle.back_off("comments", code=CODE_RATE_509, now=1000.0)
    assert waited == pytest.approx(BACKOFF_509_SECONDS)

    assert throttle.wait_seconds("comments", now=1000.0) > 0
    assert throttle.wait_seconds("messages", now=1000.0) == 0.0, "别的频道不该被牵连"


def test_backoff_799_is_global() -> None:
    """★★ `-799` 是**全站级**限流 —— 命中后所有频道一起停。

    这是参考项目 `api/throttle.py` 的核心设计：`-799` 意味着
    "你这个账号/这个 IP 被限流了"，继续请求任何接口都只会加重。
    """
    throttle = make_throttle()
    waited = throttle.back_off("comments", code=CODE_RATE_799, now=1000.0)
    assert COOLDOWN_799_MIN <= waited <= COOLDOWN_799_MAX

    # 所有频道都被挡住（不是只有 comments）
    for channel in ("comments", "messages", "mentions", "metadata", "search"):
        assert throttle.wait_seconds(channel, now=1000.0) > 0, f"{channel} 也该停"


def test_global_cooldown_does_not_stack() -> None:
    """★ 连续命中 `-799` **不该**越等越久（否则会被永久卡住）。"""
    throttle = make_throttle()
    first = throttle.back_off("comments", code=CODE_RATE_799, now=1000.0)
    second = throttle.back_off("messages", code=CODE_RATE_799, now=1001.0)
    assert second <= first + 1.0, "第二次不该在第一次基础上再叠一个 90~180s"


def test_cooldown_expires() -> None:
    throttle = make_throttle()
    waited = throttle.back_off("comments", code=CODE_RATE_799, now=1000.0)
    after = 1000.0 + waited + 1
    assert throttle.wait_seconds("comments", now=after) == 0.0


def test_unknown_code_does_not_backoff() -> None:
    throttle = make_throttle()
    assert throttle.back_off("comments", code="-400", now=1000.0) == 0.0
    assert throttle.wait_seconds("comments", now=1000.0) == 0.0


# ---------------------------------------------------------------------------
# 限流识别
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        (-799, "", CODE_RATE_799),
        ("-799", "", CODE_RATE_799),
        (-509, "", CODE_RATE_509),
        ("-509", "", CODE_RATE_509),
        (None, "请求过于频繁", CODE_RATE_799),
        (-400, "参数错误", None),
        (0, "", None),
        (None, "", None),
    ],
)
def test_is_rate_limit_error(code: Any, message: str, expected: str | None) -> None:
    assert is_rate_limit_error(code=code, message=message) == expected


def test_rate_limit_error_checks_both_code_and_message() -> None:
    """★ 只看 code 或只看 message 都会漏判（B站两种都会出现）。"""
    assert is_rate_limit_error(code=None, message="code -799") == CODE_RATE_799
    assert is_rate_limit_error(code=-509, message="") == CODE_RATE_509


# ---------------------------------------------------------------------------
# ★ buvid3 格式（永久 -799 的坑）
# ---------------------------------------------------------------------------


def test_valid_buvid3_passes() -> None:
    ok, message = check_buvid3_format("A1B2C3D4-1234-5678-9ABC-DEF012345678infoc")
    assert ok is True
    assert message == "OK"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "not-a-uuid",
        "A1B2C3D4-1234-5678-9ABC-DEF012345678",  # 缺 infoc 后缀
        "A1B2C3D4-1234-5678-9ABC-DEF012345678infocX",  # 后缀不对
        "1234-5678-9ABC-DEF012345678-A1B2C3D4infoc",  # 段序不对
    ],
)
def test_invalid_buvid3_rejected(bad: str) -> None:
    """★★ 格式不对必须**拒绝** —— 否则 B站**永久** `-799`。

    参考项目 `api/client.py:67` 注释明确记了这个坑：
    "校验 buvid3 格式：必须是标准 UUID+infoc，否则 B站 永久 -799"。
    """
    ok, message = check_buvid3_format(bad)
    assert ok is False
    # 空值走"为空"分支，非空非法值走"格式不对"分支 —— 两者都要给得出人话原因
    assert message, "拒绝时必须给出原因（不能返回空字符串）"
    assert ("永久" in message) or ("格式" in message) or ("为空" in message)


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------


def test_snapshot_shape() -> None:
    throttle = make_throttle()
    throttle.mark_call("messages", now=1000.0)
    snap = throttle.snapshot(now=1000.0)
    assert "global_cooldown_remaining" in snap
    assert "messages" in snap["channels"]
    assert snap["channels"]["messages"]["interval"] == DEFAULT_INTERVALS["messages"]


def test_snapshot_never_negative() -> None:
    """快照里的"剩余时间"不该是负数（时间倒流/注入 now 时要稳）。"""
    throttle = make_throttle()
    throttle.back_off("comments", code=CODE_RATE_799, now=1000.0)
    snap = throttle.snapshot(now=9999.0)
    assert snap["global_cooldown_remaining"] >= 0


# ---------------------------------------------------------------------------
# 保守性（本项目红线）
# ---------------------------------------------------------------------------


def test_default_intervals_are_conservative() -> None:
    """★★ 默认间隔必须**保守** —— 参考项目用 5 秒，我们不能照抄。

    本项目红线："不加大请求强度"。所以评论/私信都是**分钟级**。
    """
    assert DEFAULT_INTERVALS["comments"] >= 60
    assert DEFAULT_INTERVALS["messages"] >= 30
    assert DEFAULT_INTERVALS["mentions"] >= 60


def test_backoff_constants_are_sane() -> None:
    assert 5 <= BACKOFF_509_SECONDS <= 60
    assert 30 <= COOLDOWN_799_MIN < COOLDOWN_799_MAX <= 600
