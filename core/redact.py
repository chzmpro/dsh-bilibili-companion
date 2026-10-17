"""凭据脱敏。

**红线**：Cookie / SESSDATA / token / API key 的值永不进
LLM 上下文、日志、git、MCP 返回、素材包或回执。

所有出站文本（日志、MCP 返回、日报）都应经过 :func:`redact`。
"""

from __future__ import annotations

import re
from typing import Any

REPLACEMENT = "<REDACTED>"

# B 站登录态常见 cookie 名（大小写不敏感），以及通用凭据字段名。
_SENSITIVE_KEYS: tuple[str, ...] = (
    "sessdata",
    "bili_jct",
    "buvid3",
    "buvid4",
    "dedeuserid",
    "dedeuserid__ckmd5",
    "sid",
    "ac_time_value",
    "cookie",
    "set-cookie",
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
)

_KEY_ALT = "|".join(re.escape(k) for k in _SENSITIVE_KEYS)

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # ★ Bearer 必须排在 key=value **之前**：
    #   否则 `Authorization: Bearer <token>` 会先被 key=value 规则吃掉
    #   `Bearer` 这个词，把真正的 token 留在原地。
    re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9\-._~+/=]{8,})"),
    # key=value / key: value / "key": "value"  —— 保留键名，只抹值。
    # 允许键名两侧带引号（JSON 形态），否则 "api_key": "..." 会漏掉。
    re.compile(
        rf"(?i)([\"']?\b(?:{_KEY_ALT})\b[\"']?)(\s*[:=]\s*)"
        r"(\"[^\"]*\"|'[^']*'|[^\s,;&}}\]]+)"
    ),
    # Cookie 请求头整体：Cookie: a=1; b=2
    re.compile(r"(?i)\b(cookie)\b(\s*:\s*)([^\r\n]+)"),
    re.compile(r"\bsk-[A-Za-z0-9\-_]{8,}\b"),
)

_TOKEN_SHAPED = re.compile(r"^[A-Za-z0-9\-._~+/=]{24,}$")


def redact(text: str) -> str:
    """抹掉文本中的凭据值，保留上下文可读性。"""
    if not text:
        return text
    out = text
    for pattern in _PATTERNS:
        if pattern.groups >= 3:
            out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REPLACEMENT}", out)
        else:
            out = pattern.sub(REPLACEMENT, out)
    return out


def redact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    """递归脱敏一个 mapping（用于日志/MCP 返回的字段级兜底）。"""
    result: dict[str, Any] = {}
    for key, value in data.items():
        if key.lower() in _SENSITIVE_KEYS or _looks_sensitive_name(key):
            result[key] = REPLACEMENT if value else value
        elif isinstance(value, dict):
            result[key] = redact_mapping(value)
        elif isinstance(value, list):
            result[key] = [_redact_item(item) for item in value]
        elif isinstance(value, str):
            result[key] = redact(value)
        else:
            result[key] = value
    return result


def _redact_item(item: Any) -> Any:
    if isinstance(item, dict):
        return redact_mapping(item)
    if isinstance(item, str):
        return redact(item)
    return item


#: 这些名字虽含 token/secret 等词，但**是计数不是凭据**。
#: 例：``prompt_tokens`` / ``completion_tokens`` 是豆包用量统计，
#: 抹掉它们会让"到底省了多少 token"查不出来（本项目真实踩到过）。
_NON_SECRET_NAMES: frozenset[str] = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "max_output_tokens",
        "max_tokens",
        "input_tokens",
        "output_tokens",
        "token_count",
        "tokens",
    }
)


def _looks_sensitive_name(name: str) -> bool:
    lowered = name.lower()
    if lowered in _NON_SECRET_NAMES:
        return False
    # 以 _tokens / _token 结尾的计数类字段一律放行
    if lowered.endswith("_tokens") or lowered.endswith("_token"):
        return False
    return any(
        part in lowered for part in ("secret", "token", "password", "apikey", "api_key")
    )


def looks_like_token(value: str) -> bool:
    """粗判一个值是否"像 token"——用于拒绝把可疑值写进日志的兜底断言。"""
    return bool(value) and bool(_TOKEN_SHAPED.match(value)) and any(
        c.isdigit() for c in value
    ) and any(c.isalpha() for c in value)
