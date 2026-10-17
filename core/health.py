"""平台健康状态：5 态分类。

任务书 §5 原文要求：**不把 ``-509``、``-412`` 或 HTTP 429 硬编码为同一含义**。

状态集合：

===============  ====================================================
state            含义与处置
===============  ====================================================
``ready``         正常
``rate_limited``  明确单次限频 → 有界指数退避，**仅重试只读请求**
``verification_required`` 验证码 / 账号风险提示 → **立即停止所有新自动请求**，人工恢复
``auth_expired``  登录失效 → 停需要认证的任务，主人重新扫码
``platform_error`` 含义不明或疑似风控（如 ``-412``）→ **停机调查**，不盲目重试
===============  ====================================================

**恢复必须由人用入口发起**；不得由模型、豆包或计时自动恢复。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

HealthState = Literal[
    "ready",
    "rate_limited",
    "verification_required",
    "auth_expired",
    "platform_error",
    #: 单个资源不存在（视频被删 / 评论区关闭 / 端点不存在）。
    #: ★ **不是**平台故障：不该阻断后续所有请求，否则一个被删的视频
    #:   会把整套系统冻住（真实踩到过：探测一个不存在的端点 → 全线停机）。
    "not_found",
]

READY: HealthState = "ready"
RATE_LIMITED: HealthState = "rate_limited"
VERIFICATION_REQUIRED: HealthState = "verification_required"
AUTH_EXPIRED: HealthState = "auth_expired"
PLATFORM_ERROR: HealthState = "platform_error"
NOT_FOUND: HealthState = "not_found"

ALL_STATES: tuple[HealthState, ...] = (
    READY,
    RATE_LIMITED,
    VERIFICATION_REQUIRED,
    AUTH_EXPIRED,
    PLATFORM_ERROR,
    NOT_FOUND,
)

# ★ 这些状态一律**停止所有新的自动 B 站请求**
BLOCKING_STATES: frozenset[HealthState] = frozenset(
    {VERIFICATION_REQUIRED, AUTH_EXPIRED, PLATFORM_ERROR}
)

# ★ 只有这些状态允许"有界退避后重试只读请求"
RETRYABLE_STATES: frozenset[HealthState] = frozenset({READY, RATE_LIMITED})


@dataclass(frozen=True)
class Classification:
    """一次平台响应的分类结果。"""

    state: HealthState
    retryable: bool
    blocks_all_requests: bool
    category: str
    human_action: str = ""


# B 站业务 code（实测，见 docs/handoff/04 §1.1）
_CODE_TABLE: dict[str, Classification] = {
    "0": Classification(READY, True, False, "ok"),
    "-352": Classification(
        RATE_LIMITED,
        True,
        False,
        "risk_control_missing_signature",
        "已补 buvid + WBI 签名后重试；仍失败则停机",
    ),
    "-400": Classification(
        PLATFORM_ERROR, False, True, "bad_request", "参数错误，通常是本程序的 bug"
    ),
    "-403": Classification(
        AUTH_EXPIRED, False, True, "forbidden", "检查登录态与签名，不要盲目重试"
    ),
    # ★ 单个资源不存在 = **确定性失败**，不重试，但**不阻断**其它请求。
    #   任务书 §5 也明说"评论关闭、目标已删除等确定性失败不重试"。
    "-404": Classification(
        NOT_FOUND, False, False, "not_found", "该资源不存在（已删/关闭），跳过即可，不影响其它请求"
    ),
    "-412": Classification(
        PLATFORM_ERROR,
        False,
        True,
        "ip_banned_suspected",
        "疑似 IP 级风控，立即停机调查，不按普通限流重试",
    ),
    "-509": Classification(
        RATE_LIMITED, True, False, "rate_limited", "指数退避，连续达阈值则转人工"
    ),
    "-101": Classification(AUTH_EXPIRED, False, True, "not_logged_in", "登录失效，需主人重新扫码"),
    "-111": Classification(AUTH_EXPIRED, False, True, "csrf_failed", "csrf 校验失败，检查凭据"),
}

_HTTP_TABLE: dict[str, Classification] = {
    "429": Classification(
        RATE_LIMITED, True, False, "http_429", "指数退避，连续达阈值则转人工"
    ),
    "412": Classification(
        PLATFORM_ERROR, False, True, "http_412", "疑似风控，停机调查，不盲目重试"
    ),
    "401": Classification(AUTH_EXPIRED, False, True, "http_401", "登录失效，需主人重新扫码"),
    "403": Classification(AUTH_EXPIRED, False, True, "http_403", "权限不足，检查凭据"),
    "404": Classification(
        NOT_FOUND, False, False, "http_404", "该资源不存在（端点/内容已失效），跳过即可"
    ),
    "5xx": Classification(PLATFORM_ERROR, False, True, "http_5xx", "平台故障，转人工"),
}

# 验证码 / 账号风险提示的文本特征（**内容分类，不是破解**）
_VERIFICATION_MARKERS: tuple[str, ...] = (
    "验证码",
    "captcha",
    "geetest",
    "极验",
    "人机",
    "risk control",
    "账号风险",
    "异常操作",
    "请完成验证",
    "安全验证",
    "v_voucher",
)


def classify(
    *, code: str | int | None = None, http_status: int | None = None, message: str = ""
) -> Classification:
    """把一次平台响应分类成 5 态之一。

    优先级：验证码文本 > HTTP 状态 > 业务 code。
    （验证码是"必须人工介入"的最强信号，不能因为 code=0 就被忽略。）
    """
    lowered = message.lower()
    for marker in _VERIFICATION_MARKERS:
        if marker.lower() in lowered:
            return Classification(
                VERIFICATION_REQUIRED,
                False,
                True,
                "captcha_detected",
                "立即停止所有新的自动请求；由主人在官方页面/App 人工完成验证；"
                "程序不代解、不转包打码、不绕过",
            )

    if http_status is not None:
        key = str(http_status)
        if key in _HTTP_TABLE:
            return _HTTP_TABLE[key]
        if 500 <= http_status <= 599:
            return _HTTP_TABLE["5xx"]
        if 200 <= http_status <= 299:
            # HTTP 成功仍要看业务 code
            pass
        elif http_status >= 400:
            return Classification(PLATFORM_ERROR, False, True, f"http_{key}", "未知 HTTP 错误")

    if code is not None:
        key = str(code)
        if key in _CODE_TABLE:
            return _CODE_TABLE[key]
        try:
            numeric = int(key)
        except ValueError:
            return Classification(PLATFORM_ERROR, False, True, "unknown_code", "未知响应码")
        if numeric == 0:
            return _CODE_TABLE["0"]
        return Classification(
            PLATFORM_ERROR, False, True, f"unknown_code_{numeric}", "语义不明，停机调查"
        )

    return _CODE_TABLE["0"]


def backoff_seconds(
    attempt: int, *, base: float, cap: float, state: HealthState = RATE_LIMITED
) -> float:
    """指数退避（仅对 ``rate_limited`` 有意义）。

    ``attempt`` 从 1 开始。返回秒数，不超过 ``cap``。
    """
    if state != RATE_LIMITED or attempt <= 0:
        return 0.0
    return float(min(cap, base * (2 ** (attempt - 1))))


def human_action_for(state: HealthState) -> str:
    """该状态下需要的**人工**动作（写进日报与 MCP 返回）。"""
    return {
        READY: "",
        RATE_LIMITED: "降低频率并等待；连续达阈值后需人工确认再恢复",
        VERIFICATION_REQUIRED: "主人在官方页面/App 完成人机验证后，用人用入口恢复",
        AUTH_EXPIRED: "主人在官方 App 重新扫码登录后，用人用入口恢复",
        PLATFORM_ERROR: "疑似风控或平台故障：停机调查，人工确认后再恢复",
        NOT_FOUND: "",
    }[state]


def as_state(value: str) -> HealthState:
    """把数据库里读出的状态字符串收窄回 ``HealthState``。

    数据库有 CHECK 约束保证取值合法；这里只做类型层收窄，
    遇到未知值退回 ``platform_error``（宁可停机也不放行）。
    """
    if value in ALL_STATES:
        return value
    return PLATFORM_ERROR
