"""健康状态 5 态分类测试。

任务书 §5：**不把 ``-509`` / ``-412`` / HTTP 429 硬编码为同一含义**。
"""

from __future__ import annotations

import pytest

from core import health


def test_zero_code_is_ready() -> None:
    verdict = health.classify(code=0, http_status=200)
    assert verdict.state == health.READY
    assert verdict.retryable is True
    assert verdict.blocks_all_requests is False


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (-509, health.RATE_LIMITED),
        (-352, health.RATE_LIMITED),
        (-412, health.PLATFORM_ERROR),
        (-400, health.PLATFORM_ERROR),
        (-404, health.NOT_FOUND),
        (-403, health.AUTH_EXPIRED),
        (-101, health.AUTH_EXPIRED),
    ],
)
def test_business_codes_are_not_collapsed(code: int, expected: str) -> None:
    verdict = health.classify(code=code)
    assert verdict.state == expected


def test_rate_limit_and_banned_are_distinct() -> None:
    """-509 与 -412 必须是不同状态、不同可重试性。"""
    limited = health.classify(code=-509)
    banned = health.classify(code=-412)
    assert limited.state != banned.state
    assert limited.retryable is True
    assert banned.retryable is False
    assert banned.blocks_all_requests is True


def test_http_429_differs_from_http_412() -> None:
    assert health.classify(http_status=429).state == health.RATE_LIMITED
    assert health.classify(http_status=412).state == health.PLATFORM_ERROR


@pytest.mark.parametrize(
    "message",
    ["请完成验证", "captcha required", "geetest challenge", "账号风险提示", "v_voucher"],
)
def test_verification_markers_win_over_zero_code(message: str) -> None:
    """验证码是"必须人工介入"的最强信号，不能被 code=0 淹没。"""
    verdict = health.classify(code=0, http_status=200, message=message)
    assert verdict.state == health.VERIFICATION_REQUIRED
    assert verdict.blocks_all_requests is True
    assert verdict.retryable is False


def test_auth_expiry_blocks() -> None:
    verdict = health.classify(code=-101)
    assert verdict.state == health.AUTH_EXPIRED
    assert verdict.state in health.BLOCKING_STATES
    assert "扫码" in verdict.human_action


def test_unknown_code_is_platform_error_not_silent_success() -> None:
    verdict = health.classify(code=-99999)
    assert verdict.state == health.PLATFORM_ERROR
    assert verdict.blocks_all_requests is True


def test_backoff_grows_and_caps() -> None:
    delays = [health.backoff_seconds(n, base=5.0, cap=60.0) for n in range(1, 8)]
    assert delays[0] == 5.0
    assert delays[1] == 10.0
    assert delays[2] == 20.0
    assert max(delays) <= 60.0
    assert delays == sorted(delays)


def test_backoff_zero_when_not_rate_limited() -> None:
    assert health.backoff_seconds(3, base=5.0, cap=60.0, state=health.PLATFORM_ERROR) == 0.0
    assert health.backoff_seconds(0, base=5.0, cap=60.0) == 0.0


def test_every_state_has_human_action_text() -> None:
    """每个状态都要有人工动作说明，**除了** ready 与 not_found。

    ``not_found`` 是单个资源的确定性失败（视频被删/评论关闭），
    跳过即可，不需要人工介入。
    """
    for state in health.ALL_STATES:
        action = health.human_action_for(state)
        if state in (health.READY, health.NOT_FOUND):
            assert action == "", state
        else:
            assert action, state


def test_not_found_does_not_block() -> None:
    """★ 单个资源不存在**不该**阻断后续请求。

    真实踩到过：探测一个不存在的端点返回 404，被当成 platform_error，
    之后每个请求都 RequestsBlocked，整个系统停摆。
    """
    verdict = health.classify(http_status=404)
    assert verdict.state == health.NOT_FOUND
    assert verdict.blocks_all_requests is False
    assert verdict.retryable is False
    assert health.NOT_FOUND not in health.BLOCKING_STATES


def test_not_found_keeps_five_state_contract_intact() -> None:
    """任务书 §5 要求的 5 态仍然齐全（not_found 是**额外**细化，不是替换）。"""
    required = {
        "ready", "rate_limited", "verification_required",
        "auth_expired", "platform_error",
    }
    assert required <= set(health.ALL_STATES)
