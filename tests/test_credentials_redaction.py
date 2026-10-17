"""凭据红线测试：**任何输出都不得包含凭据值**。"""

from __future__ import annotations

import json
from pathlib import Path

from core.credentials import (
    CookieBundle,
    CredentialError,
    cookie_diagnostics,
    load_cookie,
    parse_cookie_header,
    save_cookie,
)
from core.redact import redact, redact_mapping

SECRET = "verySecretSessdataValue1234567890"
CSRF = "abcdef1234567890abcdef1234567890"


def test_cookie_bundle_repr_hides_values() -> None:
    bundle = CookieBundle({"SESSDATA": SECRET, "bili_jct": CSRF})
    assert SECRET not in repr(bundle)
    assert SECRET not in str(bundle)
    assert "2 keys" in repr(bundle)


def test_parse_cookie_header() -> None:
    bundle = parse_cookie_header(f"SESSDATA={SECRET}; bili_jct={CSRF}; junk")
    assert bundle.values["SESSDATA"] == SECRET
    assert bundle.has_write_credentials() is True
    assert bundle.csrf_token() == CSRF


def test_cookie_diagnostics_never_contains_values(isolated_data_dir: Path) -> None:
    save_cookie(parse_cookie_header(f"SESSDATA={SECRET}; bili_jct={CSRF}"))
    diag = cookie_diagnostics()
    serialized = json.dumps(diag, ensure_ascii=False)
    assert SECRET not in serialized
    assert CSRF not in serialized
    assert diag["present"] is True
    assert diag["writable"] is True
    assert sorted(diag["names"]) == ["bili_jct", "sessdata"]


def test_load_cookie_missing_raises_without_value() -> None:
    try:
        load_cookie()
    except CredentialError as exc:
        assert SECRET not in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应当抛出 CredentialError")


def test_redact_masks_cookie_header() -> None:
    text = f"Cookie: SESSDATA={SECRET}; bili_jct={CSRF}"
    out = redact(text)
    assert SECRET not in out
    assert CSRF not in out
    assert "<REDACTED>" in out


def test_redact_masks_key_value_forms() -> None:
    for text in (
        f"SESSDATA={SECRET}",
        f"sessdata: {SECRET}",
        f'"api_key": "{SECRET}"',
        f"Authorization: Bearer {SECRET}",
        f"secret={SECRET}",
    ):
        out = redact(text)
        assert SECRET not in out, text


def test_redact_mapping_recurses() -> None:
    payload = {
        "cookie": SECRET,
        "nested": {"access_token": SECRET, "safe": "标题"},
        "list": [{"password": SECRET}],
    }
    out = redact_mapping(payload)
    serialized = json.dumps(out, ensure_ascii=False)
    assert SECRET not in serialized
    assert out["nested"]["safe"] == "标题"


def test_redact_leaves_normal_text_alone() -> None:
    text = "这是一个关于 DeepSeek Harness 插件的视频标题"
    assert redact(text) == text


# ---------------------------------------------------------------------------
# ★ token **计数**不是凭据（实测踩到的过度脱敏）
# ---------------------------------------------------------------------------


def test_usage_counters_are_not_redacted() -> None:
    """★ 回归：``prompt_tokens`` 等**用量计数**必须保留原值。

    真实踩到：脱敏规则把任何含 "token" 的字段都抹成 ``<REDACTED>``，
    结果 ``companion_status`` 里的 token 用量变成不可读 ——
    而"省了多少 token"正是本项目的核心指标之一。
    """
    payload = {
        "prompt_tokens": 12264,
        "completion_tokens": 11473,
        "total_tokens": 23737,
        "max_output_tokens": 8192,
        "jobs": 8,
    }
    out = redact_mapping(payload)
    assert out == payload, f"计数被误抹：{out}"


def test_real_credentials_still_redacted() -> None:
    """★ 修完之后，真正的凭据**仍然**要抹掉（不能矫枉过正）。"""
    for key in ("api_key", "access_token", "SESSDATA", "bili_jct", "password", "secret"):
        out = redact_mapping({key: "abcdef1234567890abcdef1234567890"})
        assert out[key] == "<REDACTED>", f"{key} 没被抹掉"


def test_token_string_form_keeps_counter() -> None:
    """字符串形态：计数保留、凭据抹掉。"""
    assert "12264" in redact("prompt_tokens: 12264")
    assert "<REDACTED>" in redact("api_key: sk-abcdefghij1234567890")
