"""分频道节流 + B站限流码专门处理（2026-10-17）。

## 为什么需要它（本项目实测 + 参考项目踩坑）

### 我们的现状（缺口）

`core/ratelimit.py` 有**指数退避**，但它是**全局一个桶**：
评论、私信、@通知、推荐流**共用同一个限速状态**。
后果：私信查得勤，会拖慢评论；评论被限流，私信也跟着停。

### 参考项目怎么做的（`xiaoyaya191/bilibili_learning_bot`）

1. **分频道独立间隔**（`brain/monitor.py`）：
   评论 120s / 私信 60s / @通知 5s，各算各的。
2. **B站限流码专门识别**：
   - `-509` → 该频道**退避 10 秒**
   - `-799` → **全局限流风暴**，所有 API 冷却 **90~180 秒**
3. **随机抖动**（`api/throttle.py`）：
   `gap = 基础间隔 + random(0, 1.0)` —— **避免可预测的固定频率**
   （固定 0.3s 一次的节奏太像机器，容易被判定为爬虫）。

### 它踩过的坑（我们照抄时**必须避开**）

| # | 坑 | 出处 | 我们的处理 |
|---|---|---|---|
| 1 | **`buvid3` 格式不对 → B站永久 `-799`** | `api/client.py:67` | :func:`check_buvid3_format` |
| 2 | **登录后请求堆积触发 -799** | `api/client.py:112` | 全局冷却 + 首调免等待 |
| 3 | **监听循环无异常保护 → 线程死** | `langup-ai` 的 `while 1` | 见 `core/monitor.py` |

> ★ 第 1 条最要命：`buvid3` 不是标准 `UUID+infoc` 格式时，
> B站会**永久** `-799`（不是临时限流）—— 换 cookie 都不一定好，
> 因为 buvid3 是从浏览器会话派生的。**发请求前先校验格式**成本极低。

## 设计要点

* **频道隔离**：每个频道有自己的"上次调用时间"与"冷却截止时间"；
* **全局冷却**：`-799` 是**全站级**的，所以命中后**所有频道**一起停；
* **抖动**：间隔加随机量，避免固定节奏；
* **可测**：时间通过 `now` 参数注入，不依赖真实时钟。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any

#: 各频道的**默认最小间隔**（秒）。
#:
#: ★ 刻意比参考项目**保守**（它评论 5s / 私信 5s）——
#: 本项目红线是"不加大请求强度"，所以用分钟级。
DEFAULT_INTERVALS: dict[str, int] = {
    "comments": 120,
    "messages": 60,
    "mentions": 300,
    "metadata": 3,
    "search": 10,
}

#: 单频道命中 `-509` 后的退避秒数（照抄参考项目）
BACKOFF_509_SECONDS = 10

#: 全局命中 `-799` 后的冷却区间（秒）。**随机取**，避免所有实例同时恢复。
COOLDOWN_799_MIN = 90
COOLDOWN_799_MAX = 180

#: 间隔抖动上限（秒）—— 避免固定节奏
JITTER_MAX_SECONDS = 1.0

#: B站限流相关错误码（字符串匹配，因为 code 可能是 int 也可能在 message 里）
CODE_RATE_509 = "-509"
CODE_RATE_799 = "-799"
#: 中文提示（B站有时只给 message 不给 code）
MSG_RATE_HINTS: tuple[str, ...] = ("请求过于频繁", "请求太快", "操作太频繁")

#: `buvid3` 的标准形态：UUID（8-4-4-4-12）+ `infoc` 后缀
#:
#: ⚠️ 格式不对会导致 B站**永久** `-799`（参考项目 `api/client.py:67` 实测）。
_BUVID3_PATTERN = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
    r"infoc$"
)


def is_rate_limit_error(*, code: Any = None, message: str = "") -> str | None:
    """判断一次失败是不是**B站限流**，返回命中的码（``-509`` / ``-799``）或 ``None``。

    ★ 为什么要同时看 ``code`` 和 ``message``：
    B站有时返回 ``code=-799``，有时只在 ``message`` 里写"请求过于频繁"。
    只看一个会漏判。
    """
    text = f"{code if code is not None else ''} {message}"
    if CODE_RATE_799 in text:
        return CODE_RATE_799
    for hint in MSG_RATE_HINTS:
        if hint in text:
            # 没有明确码时，按"全站限流"处理（更保守）
            return CODE_RATE_799
    if CODE_RATE_509 in text:
        return CODE_RATE_509
    return None


def check_buvid3_format(buvid3: str) -> tuple[bool, str]:
    """校验 ``buvid3`` 格式。

    ★ **这是本项目从参考项目抄来的最值钱的一条防御**（`api/client.py:67`）：

    > `buvid3` 必须是标准 ``UUID + infoc`` 格式，否则 B站 **永久** `-799`。

    "永久"意味着换 cookie、等冷却都没用 —— 只能换一个正确的 buvid3。
    所以**发请求前先校验**，成本几乎为零。

    Returns:
        ``(是否合法, 人话说明)``。
    """
    value = (buvid3 or "").strip()
    if not value:
        return False, "buvid3 为空（未登录或未取到）"
    if _BUVID3_PATTERN.match(value):
        return True, "OK"
    return False, (
        f"buvid3 格式不对（{value[:20]}…）：应为 UUID+infoc 形态。"
        "⚠️ 格式不对会让 B站**永久**返回 -799（换 cookie 也未必好）——"
        "请重新从浏览器导出 cookie。"
    )


@dataclass
class _ChannelState:
    """单个频道的节流状态。"""

    last_call_at: float = 0.0
    cooldown_until: float = 0.0


@dataclass
class Throttle:
    """分频道节流器（**纯内存**，不落库）。

    ★ 刻意不落库：节流状态是"进程内的短期记忆"，
    跨进程共享反而会让重启后的第一轮被上一个进程的冷却卡住。
    真正的跨进程保护由 `core/ratelimit.py` 的持久化状态负责。

    用法：

    ```python
    throttle = Throttle()
    wait = throttle.wait_seconds("messages", now=time.time())
    if wait > 0:
        await asyncio.sleep(wait)
    throttle.mark_call("messages", now=time.time())
    ```
    """

    intervals: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_INTERVALS))
    #: 注入随机源，便于测试（默认用 `random`）
    rng: Any = field(default=random)
    _channels: dict[str, _ChannelState] = field(default_factory=dict)
    #: 全局冷却截止（`-799` 是**全站级**，所有频道一起停）
    global_cooldown_until: float = 0.0

    def _state(self, channel: str) -> _ChannelState:
        if channel not in self._channels:
            self._channels[channel] = _ChannelState()
        return self._channels[channel]

    def interval_for(self, channel: str) -> int:
        """该频道的最小间隔（未知频道用 ``metadata`` 的间隔，偏保守）。"""
        return int(self.intervals.get(channel, self.intervals.get("metadata", 3)))

    def wait_seconds(self, channel: str, *, now: float) -> float:
        """**还需要等多少秒**才能调这个频道（0 表示可以立刻调）。

        考虑三件事：① 全局冷却；② 频道自身冷却；③ 正常间隔 + 抖动。
        """
        # ① 全局冷却优先（-799 是全站的）
        if now < self.global_cooldown_until:
            return self.global_cooldown_until - now

        state = self._state(channel)
        # ② 该频道被 -509 退避中
        if now < state.cooldown_until:
            return state.cooldown_until - now

        # ③ 正常间隔 + 抖动
        interval = self.interval_for(channel)
        jitter = float(self.rng.uniform(0, JITTER_MAX_SECONDS))
        earliest = state.last_call_at + interval + jitter
        return max(0.0, earliest - now)

    def mark_call(self, channel: str, *, now: float) -> None:
        """记一次成功调用。"""
        self._state(channel).last_call_at = now

    def back_off(self, channel: str, *, code: str, now: float) -> float:
        """命中限流码后**设置冷却**，返回要等多少秒。

        * ``-509`` → 该频道退避 :data:`BACKOFF_509_SECONDS`
        * ``-799`` → **全局**冷却 :data:`COOLDOWN_799_MIN` ~ :data:`COOLDOWN_799_MAX`

        Returns:
            实际要等的秒数。
        """
        if code == CODE_RATE_799:
            if now < self.global_cooldown_until:
                # 已在冷却中就不叠加（否则连续命中会越等越久）
                return self.global_cooldown_until - now
            duration = float(self.rng.uniform(COOLDOWN_799_MIN, COOLDOWN_799_MAX))
            self.global_cooldown_until = now + duration
            return duration

        if code == CODE_RATE_509:
            state = self._state(channel)
            state.cooldown_until = max(state.cooldown_until, now + BACKOFF_509_SECONDS)
            return max(0.0, state.cooldown_until - now)

        return 0.0

    def snapshot(self, *, now: float) -> dict[str, Any]:
        """当前节流状态（给 status / 调试用）。"""
        return {
            "global_cooldown_remaining": max(0.0, self.global_cooldown_until - now),
            "channels": {
                name: {
                    "interval": self.interval_for(name),
                    "cooldown_remaining": max(0.0, state.cooldown_until - now),
                    "since_last_call": max(0.0, now - state.last_call_at),
                }
                for name, state in self._channels.items()
            },
        }
