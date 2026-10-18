"""免费联网搜索（`modsearch`）测试（2026-10-17）。

## 背景

主人：*"这个搜索不是我想要的免费互联网搜索那种"* ⇒ 接入 `liustack/modsearch`。

## 两个真机踩到的坑（这里都钉死）

1. **`npx` 在 Windows 是 `npx.CMD`**，裸名 `subprocess.run(["npx"])` → `FileNotFoundError`；
2. ★★ **npm 缓存写在 `%LOCALAPPDATA%\\npm-cache` 会 `EPERM`**
   （Python 进程写不了工作区外的目录）⇒ 必须把 `npm_config_cache` 指到项目内。

不解决这两条，`web_search` 100% 失败，而且报错**完全看不出是环境问题**。

## 钉死的其它不变量

* **永远不抛异常**（搜索是辅助功能，失败不能让主流程崩）；
* **只回短结果**（摘要截断，整页正文不进来）；
* 输出形状不可信（JSON 坏了 / 不是 dict / items 缺 url）也要能降级。
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from core import websearch
from core.websearch import (
    MAX_QUERY_CHARS,
    MAX_SNIPPET_CHARS,
    WebSearchResult,
    cache_dir,
    parse_output,
    web_search,
)


def _payload(*, items: list[dict[str, Any]], status: str = "ok") -> str:
    import json

    return json.dumps(
        {
            "mode": "search",
            "query": "q",
            "results": [
                {
                    "source": "web",
                    "requestedSource": "web",
                    "engine": "firecrawl",
                    "status": status,
                    "summary": "一段摘要",
                    "items": items,
                    "uncertainty": [],
                    "warnings": ["机械摘要，无 LLM 综合"],
                }
            ],
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# ★★ 坑 2：npm 缓存必须在项目内
# ---------------------------------------------------------------------------


def test_cache_dir_is_inside_configured_data_dir() -> None:
    """npm 缓存遵循可写数据目录覆盖，不退回系统 npm-cache。"""
    from pathlib import Path

    from core.paths import data_dir

    path = cache_dir()
    assert Path(path) == data_dir() / "npm-cache"


def test_cache_dir_is_created() -> None:
    import os

    assert os.path.isdir(cache_dir())


def test_build_env_overrides_cache() -> None:
    """子进程环境必须覆盖 `npm_config_cache`。"""
    env = websearch._build_env()
    assert env["npm_config_cache"] == cache_dir()


# ---------------------------------------------------------------------------
# ★ 坑 1：npx 解析
# ---------------------------------------------------------------------------


def test_resolve_npx_returns_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(websearch.shutil, "which", lambda name: rf"C:\fake\{name}")
    resolved = websearch._resolve_npx()
    assert resolved is not None
    assert "npx" in resolved.lower()


def test_resolve_npx_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(websearch.shutil, "which", lambda name: None)
    assert websearch._resolve_npx() is None


# ---------------------------------------------------------------------------
# 解析（纯函数）
# ---------------------------------------------------------------------------


def test_parse_ok() -> None:
    raw = _payload(
        items=[
            {"title": "标题A", "url": "https://a.example", "snippet": "摘要A", "source": "a"},
            {"title": "标题B", "url": "https://b.example", "snippet": "摘要B", "source": "b"},
        ]
    )
    result = parse_output(raw, query="q", limit=5)
    assert result.ok is True
    assert result.engine == "firecrawl"
    assert [item.title for item in result.items] == ["标题A", "标题B"]
    assert result.items[0].url == "https://a.example"


def test_parse_respects_limit() -> None:
    items = [
        {"title": f"t{i}", "url": f"https://e{i}.example", "snippet": "s"} for i in range(10)
    ]
    result = parse_output(_payload(items=items), query="q", limit=3)
    assert len(result.items) == 3


def test_parse_skips_items_without_url() -> None:
    """★ 没有 URL 的结果没用 —— 跳过，不给上层垃圾。"""
    items = [
        {"title": "无链接", "url": "", "snippet": "s"},
        {"title": "有链接", "url": "https://ok.example", "snippet": "s"},
    ]
    result = parse_output(_payload(items=items), query="q", limit=5)
    assert [item.title for item in result.items] == ["有链接"]


def test_parse_truncates_snippet() -> None:
    long_text = "字" * (MAX_SNIPPET_CHARS + 500)
    result = parse_output(
        _payload(items=[{"title": "t", "url": "https://x.example", "snippet": long_text}]),
        query="q",
        limit=5,
    )
    assert len(result.items[0].snippet) <= MAX_SNIPPET_CHARS


def test_parse_empty_output() -> None:
    result = parse_output("", query="q", limit=5)
    assert result.ok is False
    assert result.error_code == "EMPTY_OUTPUT"


def test_parse_bad_json() -> None:
    result = parse_output("这不是 JSON", query="q", limit=5)
    assert result.ok is False
    assert result.error_code == "BAD_JSON"


def test_parse_non_dict() -> None:
    result = parse_output("[1,2,3]", query="q", limit=5)
    assert result.ok is False
    assert result.error_code == "BAD_SHAPE"


def test_parse_no_results_key() -> None:
    result = parse_output('{"mode":"search"}', query="q", limit=5)
    assert result.ok is False
    assert result.error_code == "NO_RESULTS"


def test_parse_items_empty() -> None:
    result = parse_output(_payload(items=[]), query="q", limit=5)
    assert result.ok is False
    assert result.error_code in {"NO_ITEMS", "NO_RESULTS"}


def test_parse_picks_ok_entry_first() -> None:
    """多 source 时优先取 `status=ok` 的那条。"""
    import json

    raw = json.dumps(
        {
            "results": [
                {"status": "unavailable", "engine": "grok", "items": []},
                {
                    "status": "ok",
                    "engine": "firecrawl",
                    "items": [{"title": "好", "url": "https://good.example", "snippet": "s"}],
                },
            ]
        },
        ensure_ascii=False,
    )
    result = parse_output(raw, query="q", limit=5)
    assert result.engine == "firecrawl"
    assert result.items[0].title == "好"


def test_parse_carries_warnings() -> None:
    result = parse_output(
        _payload(items=[{"title": "t", "url": "https://x.example", "snippet": "s"}]),
        query="q",
        limit=5,
    )
    assert result.warnings


# ---------------------------------------------------------------------------
# ★ 执行层：失败必须降级，绝不抛
# ---------------------------------------------------------------------------


def test_run_blocking_without_npx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(websearch.shutil, "which", lambda name: None)
    result = websearch._run_blocking("q", 3, 10.0)
    assert result.ok is False
    assert result.error_code == "NPX_MISSING"
    assert "Node.js" in result.message


def test_run_blocking_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(websearch.shutil, "which", lambda name: r"C:\fake\npx.CMD")

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="npx", timeout=1)

    monkeypatch.setattr(websearch.subprocess, "run", boom)
    result = websearch._run_blocking("q", 3, 1.0)
    assert result.ok is False
    assert result.error_code == "TIMEOUT"


def test_run_blocking_spawn_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(websearch.shutil, "which", lambda name: r"C:\fake\npx.CMD")

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("cannot execute")

    monkeypatch.setattr(websearch.subprocess, "run", boom)
    result = websearch._run_blocking("q", 3, 5.0)
    assert result.ok is False
    assert result.error_code == "SPAWN_FAILED"


def test_run_blocking_cli_failure_mentions_eperm_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ `EPERM` 时给出"缓存目录"提示 —— 否则完全看不出是环境问题。"""
    monkeypatch.setattr(websearch.shutil, "which", lambda name: r"C:\fake\npx.CMD")

    class _Completed:
        returncode = 1
        stdout = ""
        stderr = "npm error code EPERM npm error path .../npm-cache/_cacache/tmp/x"

    monkeypatch.setattr(websearch.subprocess, "run", lambda *a, **k: _Completed())
    result = websearch._run_blocking("q", 3, 5.0)
    assert result.ok is False
    assert result.error_code == "CLI_FAILED"
    assert "npm_config_cache" in result.message or "缓存" in result.message


def test_run_blocking_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(websearch.shutil, "which", lambda name: r"C:\fake\npx.CMD")

    class _Completed:
        returncode = 0
        stdout = _payload(items=[{"title": "t", "url": "https://x.example", "snippet": "s"}])
        stderr = ""

    monkeypatch.setattr(websearch.subprocess, "run", lambda *a, **k: _Completed())
    result = websearch._run_blocking("q", 3, 30.0)
    assert result.ok is True
    assert result.items[0].url == "https://x.example"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


async def test_web_search_empty_query() -> None:
    result = await web_search("   ")
    assert result.ok is False
    assert result.error_code == "EMPTY_QUERY"


async def test_web_search_truncates_long_query(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_blocking(query: str, limit: int, timeout: float) -> WebSearchResult:
        captured["query"] = query
        return WebSearchResult(ok=True, query=query)

    monkeypatch.setattr(websearch, "_run_blocking", fake_blocking)
    await web_search("字" * (MAX_QUERY_CHARS + 100))
    assert len(captured["query"]) <= MAX_QUERY_CHARS


async def test_web_search_clamps_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, int] = {}

    def fake_blocking(query: str, limit: int, timeout: float) -> WebSearchResult:
        captured["limit"] = limit
        return WebSearchResult(ok=True, query=query)

    monkeypatch.setattr(websearch, "_run_blocking", fake_blocking)
    await web_search("q", limit=999)
    assert captured["limit"] <= 10


def test_result_to_dict_marks_untrusted() -> None:
    """★ 成功结果里要提示"外部内容不可信"。"""
    from core.websearch import WebSearchItem

    result = WebSearchResult(
        ok=True,
        query="q",
        items=[WebSearchItem(title="t", url="https://x.example", snippet="s")],
    )
    payload = result.to_dict()
    assert "不可信" in payload["note"]
    assert payload["items"][0]["url"] == "https://x.example"
