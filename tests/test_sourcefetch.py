"""取源码通道测试（2026-10-17）。

背景：本机对 GitHub 的访问**不完整** —— 实测

============================  ==========  ==================================
主机                          结果        用途
============================  ==========  ==================================
``github.com``                ❌ shell    网页
``raw.githubusercontent.com`` ❌ shell    读单文件（**浏览器能通**）
``api.github.com``            ✅ 200      元数据
``codeload.github.com``       ✅ 200      下 zip
``cdn.jsdelivr.net``          ✅ 200      替代 raw
``gitee.com``                 ✅ GET 200  国内镜像（HEAD 返 405！）
============================  ==========  ==================================

核心不变量（都在这里钉死）：

* **多源兜底**：一个源失败要自动换下一个，全失败才报错；
* **``tried`` 可审计**：每个源为什么失败要留痕；
* 全失败时**不抛异常**，返回 ``(None, 结果)`` 让人决定怎么办；
* **gitee 的 405 只针对 HEAD** —— 探测必须用 GET；
* **zip 解压防路径穿越**。

**绝不联网**：全部用假的 httpx client。
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest

from core import sourcefetch


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "", content: bytes = b"") -> None:
        self.status_code = status_code
        self.text = text
        self.content = content or text.encode("utf-8")

    def json(self) -> Any:
        import json

        return json.loads(self.text)


class _FakeClient:
    """按 URL 前缀返回预设结果；未预设的抛异常（模拟网络不通）。"""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requested: list[str] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.requested.append(url)
        for prefix, outcome in self.routes.items():
            if url.startswith(prefix):
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        raise ConnectionError(f"本机不通：{url}")


def _install(monkeypatch: pytest.MonkeyPatch, routes: dict[str, Any]) -> _FakeClient:
    client = _FakeClient(routes)
    monkeypatch.setattr(sourcefetch, "_client", lambda timeout=None: client)
    return client


# ---------------------------------------------------------------------------
# raw 多源兜底
# ---------------------------------------------------------------------------


async def test_fetch_raw_uses_first_working_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """第一个源就通 → 直接用，不试后面的。"""
    client = _install(
        monkeypatch,
        {"https://cdn.jsdelivr.net": _FakeResponse(200, text="内容来自 jsdelivr")},
    )
    text, result = await sourcefetch.fetch_raw_file("o", "r", "README.md")

    assert result.ok is True
    assert result.source == "jsdelivr"
    assert text == "内容来自 jsdelivr"
    assert len(client.requested) == 1, "第一个源通了就不该再试"


async def test_fetch_raw_falls_back_through_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 前面的源失败 → **自动换下一个**（这是本模块存在的意义）。"""
    client = _install(
        monkeypatch,
        {
            # jsdelivr 抛网络错
            "https://cdn.jsdelivr.net": ConnectionError("不通"),
            # gh-proxy 返回 404
            "https://gh-proxy.com": _FakeResponse(404),
            # ghproxy.net 成功
            "https://ghproxy.net": _FakeResponse(200, text="兜底成功"),
        },
    )
    text, result = await sourcefetch.fetch_raw_file("o", "r", "README.md")

    assert result.ok is True
    assert result.source == "ghproxy.net"
    assert text == "兜底成功"
    assert len(client.requested) == 3, "应该按顺序试到第三个"
    # ★ 可审计：前两个为什么失败要留痕
    assert result.tried[0]["ok"] is False
    assert result.tried[0]["error"] == "ConnectionError"
    assert result.tried[1]["ok"] is False
    assert result.tried[1]["status"] == 404
    assert result.tried[2]["ok"] is True


async def test_fetch_raw_all_sources_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 全失败 → 返回 ``(None, ...)``，**不抛异常**（让人决定怎么办）。"""
    _install(monkeypatch, {})  # 全都不通
    text, result = await sourcefetch.fetch_raw_file("o", "r", "README.md")

    assert text is None
    assert result.ok is False
    assert result.code == "ALL_SOURCES_FAILED"
    assert len(result.tried) == len(sourcefetch.RAW_SOURCES), "每个源都要留痕"


async def test_fetch_raw_empty_body_counts_as_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """200 但**空内容**也要继续换源（空文件不是我们要的东西）。"""
    _install(
        monkeypatch,
        {
            "https://cdn.jsdelivr.net": _FakeResponse(200, text=""),
            "https://gh-proxy.com": _FakeResponse(200, text="真内容"),
        },
    )
    text, result = await sourcefetch.fetch_raw_file("o", "r", "README.md")
    assert text == "真内容"
    assert result.source == "gh-proxy"


# ---------------------------------------------------------------------------
# zip
# ---------------------------------------------------------------------------


async def test_fetch_repo_zip_uses_codeload(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"PK\x03\x04fake-zip"
    _install(
        monkeypatch,
        {"https://codeload.github.com": _FakeResponse(200, content=payload)},
    )
    data, result = await sourcefetch.fetch_repo_zip("o", "r", ref="main")

    assert result.ok is True
    assert data == payload
    assert result.source == "codeload"


async def test_fetch_repo_zip_tries_tag_when_branch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """分支不是 main 时，第二个源（tags）要能兜住。"""
    payload = b"PK\x03\x04tag-zip"
    # ⚠️ fake client 是**前缀匹配**，所以键要写完整 URL 前缀（ref 在路径中间）
    client = _install(
        monkeypatch,
        {
            "https://codeload.github.com/o/r/zip/refs/heads/v1.0": _FakeResponse(404),
            "https://codeload.github.com/o/r/zip/refs/tags/v1.0": _FakeResponse(
                200, content=payload
            ),
        },
    )
    data, result = await sourcefetch.fetch_repo_zip("o", "r", ref="v1.0")

    assert result.ok is True
    assert data == payload
    assert len(client.requested) == 2


async def test_fetch_repo_zip_all_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {})
    data, result = await sourcefetch.fetch_repo_zip("o", "r")
    assert data is None
    assert result.code == "ALL_SOURCES_FAILED"


# ---------------------------------------------------------------------------
# zip 解压
# ---------------------------------------------------------------------------


def _make_zip(files: dict[str, str], prefix: str = "") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(prefix + name, content)
    return buffer.getvalue()


def test_extract_zip_strips_github_prefix(tmp_path: Any) -> None:
    """★ GitHub 的 zip 顶层有 ``<repo>-<ref>/`` 前缀，要**去掉**。"""
    payload = _make_zip({"README.md": "hi", "src/a.py": "x"}, prefix="repo-main/")
    written = sourcefetch.extract_zip(payload, tmp_path)

    names = sorted(path.relative_to(tmp_path).as_posix() for path in written)
    assert names == ["README.md", "src/a.py"]
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "hi"


def test_extract_zip_without_prefix(tmp_path: Any) -> None:
    payload = _make_zip({"a.txt": "x"})
    written = sourcefetch.extract_zip(payload, tmp_path)
    assert [path.name for path in written] == ["a.txt"]


def test_extract_zip_rejects_path_traversal(tmp_path: Any) -> None:
    """★★ 安全：zip 里带 ``../`` 的路径**必须拒绝**（防写到目录外）。"""
    payload = _make_zip({"../evil.txt": "坏东西"})
    with pytest.raises(sourcefetch.FetchError) as excinfo:
        sourcefetch.extract_zip(payload, tmp_path / "inner")
    assert excinfo.value.code == "PATH_TRAVERSAL"


# ---------------------------------------------------------------------------
# gitee
# ---------------------------------------------------------------------------


async def test_fetch_gitee_raw_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"https://gitee.com": _FakeResponse(200, text="gitee 内容")})
    text, result = await sourcefetch.fetch_gitee_raw("m", "r", "README.md", ref="master")

    assert result.ok is True
    assert result.source == "gitee"
    assert text == "gitee 内容"


async def test_fetch_gitee_405_is_reported_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ gitee 对 **HEAD** 返回 405 —— 这里用 GET，遇到 405 也要**如实报**不崩。

    （实测教训：我第一轮用 ``curl -I`` 探测，差点误判 gitee 不可用。）
    """
    _install(monkeypatch, {"https://gitee.com": _FakeResponse(405, text="Method Not Allowed")})
    text, result = await sourcefetch.fetch_gitee_raw("m", "r", "README.md")

    assert text is None
    assert result.ok is False
    assert result.code == "HTTP_405"
    assert "405" in result.message


# ---------------------------------------------------------------------------
# 元数据
# ---------------------------------------------------------------------------


async def test_fetch_repo_meta_success(monkeypatch: pytest.MonkeyPatch) -> None:
    body = (
        '{"full_name":"o/r","description":"d","default_branch":"main",'
        '"stargazers_count":42,"license":{"spdx_id":"MIT"},"archived":false}'
    )
    _install(monkeypatch, {"https://api.github.com": _FakeResponse(200, text=body)})
    meta = await sourcefetch.fetch_repo_meta("o", "r")

    assert meta["ok"] is True
    data = meta["data"]
    assert data["stars"] == 42
    assert data["default_branch"] == "main"
    assert data["license"] == "MIT"


async def test_fetch_repo_meta_failure_is_not_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """元数据只是"补充信息" —— 失败**不抛异常**。"""
    _install(monkeypatch, {"https://api.github.com": _FakeResponse(404)})
    meta = await sourcefetch.fetch_repo_meta("o", "nope")
    assert meta["ok"] is False
    assert meta["code"] == "HTTP_404"


# ---------------------------------------------------------------------------
# 通道表
# ---------------------------------------------------------------------------


def test_channel_report_lists_both_working_and_broken() -> None:
    """通道表要**如实**包含不通的那些（否则会误导后来的人）。"""
    report = sourcefetch.channel_report()
    by_channel = {item["channel"]: item for item in report}

    assert by_channel["api.github.com"]["works"] is True
    assert by_channel["codeload.github.com"]["works"] is True
    assert by_channel["cdn.jsdelivr.net"]["works"] is True
    # ★ 不通的也要列出来，并说明替代方案
    assert by_channel["github.com 网页"]["works"] is False
    assert by_channel["raw.githubusercontent.com"]["works"] is False
    assert "jsdelivr" in by_channel["raw.githubusercontent.com"]["use_for"]


def test_raw_sources_are_ordered_with_working_first() -> None:
    """★ 兜底顺序：**先试实测可用的**，直连放最后（本机不通）。"""
    labels = [label for label, _ in sourcefetch.RAW_SOURCES]
    assert labels[0] == "jsdelivr", "jsdelivr 实测可用，应排第一"
    assert labels[-1] == "raw 直连", "直连本机不通，放最后兜底"


def test_fetch_result_to_dict_is_serializable() -> None:
    import json

    result = sourcefetch.FetchResult(ok=True, source="x", tried=[{"a": 1}])
    assert json.loads(json.dumps(result.to_dict()))["source"] == "x"
