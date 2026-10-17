"""B 站读取层测试（含真实踩到的坑的回归）。"""

from __future__ import annotations

import pytest

from core.bilibili import _search_referer


def test_search_referer_encodes_chinese_keyword() -> None:
    """★ 回归：中文关键词直接拼进 HTTP 头会让 httpx 抛 UnicodeEncodeError。

    实测于真实平台调用：`Referer: ...?keyword=DSH插件` →
    `UnicodeEncodeError: 'ascii' codec can't encode characters`。
    所有请求都会因此失败，而用 ASCII 关键词的单测**发现不了**。
    """
    referer = _search_referer("DSH插件")
    referer.encode("ascii")  # 不抛异常即通过
    assert "%E6%8F%92%E4%BB%B6" in referer, "中文必须被 URL 编码"


def test_search_referer_encodes_space() -> None:
    referer = _search_referer("DeepSeek Harness")
    referer.encode("ascii")
    assert " " not in referer, "空格必须被编码（%20 或 +）"


def test_search_referer_keeps_ascii_readable() -> None:
    referer = _search_referer("DSH")
    assert referer == "https://search.bilibili.com/all?keyword=DSH"


@pytest.mark.parametrize(
    "keyword", ["DSH插件", "中文 带空格", "emoji🐳", "mixed中英mix", "a&b=c"]
)
def test_search_referer_always_ascii_safe(keyword: str) -> None:
    """任何关键词都要能安全放进 HTTP 头。"""
    _search_referer(keyword).encode("ascii")
