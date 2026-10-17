"""统一返回契约与错误码。

任务书 §4：*"工具名稳定，返回 JSON 的 ``ok``、``code``、``data``、``message`` 四字段。
错误必须有明确 ``code``，**不能把异常吞成空列表**。"*

**本模块的所有输出都必须过脱敏**——MCP 返回会进入 DSH 上下文，
凭据绝不能出现在那里。
"""

from __future__ import annotations

from typing import Any

from core.redact import redact, redact_mapping

# ---- 稳定错误码 -------------------------------------------------------------
OK = "OK"
INVALID_ARGUMENT = "INVALID_ARGUMENT"
NOT_CONFIGURED = "NOT_CONFIGURED"
TEXT_WORKER_NOT_CONFIGURED = "TEXT_WORKER_NOT_CONFIGURED"
TEXT_WORKER_DISABLED = "TEXT_WORKER_DISABLED"
TEXT_WORKER_KEY_MISSING = "TEXT_WORKER_KEY_MISSING"
EVIDENCE_NOT_FOUND = "EVIDENCE_NOT_FOUND"
EVIDENCE_BVID_MISMATCH = "EVIDENCE_BVID_MISMATCH"
EVIDENCE_REQUIRED = "EVIDENCE_REQUIRED"
DRAFT_NOT_FOUND = "DRAFT_NOT_FOUND"
NOT_FOUND = "NOT_FOUND"
BLOCKED = "BLOCKED"
RATE_LIMITED = "RATE_LIMITED"
RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
AUTH_REQUIRED = "AUTH_REQUIRED"
PLATFORM_ERROR = "PLATFORM_ERROR"
READ_FAILED = "READ_FAILED"
LOCKED = "LOCKED"
RUN_FAILED = "RUN_FAILED"
PAUSED = "PAUSED"
UNSUPPORTED = "UNSUPPORTED"
INTERNAL_ERROR = "INTERNAL_ERROR"


def ok(data: Any = None, message: str = "") -> dict[str, Any]:
    """成功返回。``data`` 会过脱敏。"""
    return {"ok": True, "code": OK, "data": _sanitize(data), "message": redact(message)}


def fail(code: str, message: str = "", data: Any = None) -> dict[str, Any]:
    """失败返回。``code`` 必须明确。"""
    return {"ok": False, "code": code, "data": _sanitize(data), "message": redact(message)}


def _sanitize(data: Any) -> Any:
    if data is None:
        return None
    if isinstance(data, dict):
        return redact_mapping(data)
    if isinstance(data, str):
        return redact(data)
    if isinstance(data, list):
        return [_sanitize(item) for item in data]
    return data
