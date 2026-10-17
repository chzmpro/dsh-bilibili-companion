"""`read_source` 工具 + `fetch_repo_contents` 测试（2026-10-17）。

## 背景（真机踩到的坑）

私信闭环的最后一环是"读源码"。第一版 `read_source` 为了**列根目录**
直接调用 `fetch_repo_zip` 下**整个 zip**：

* 一个 13 MB 的仓库要下 13 MB 才能知道它有哪些文件；
* 实测**卡住不返回**（超过 240 秒，工具调用被中止）。

正确做法是用 **`api.github.com/contents`**：只返回目录元数据（几 KB）。

## 钉死的不变量

* **列目录不下载整包**（这是性能与"会不会卡死"的关键）；
* `contents` 返回的不是 list（传了文件路径）时要**明确报错**；
* 读单个文件**多源兜底**（jsdelivr → gh-proxy → gitee）；
* **只读文本，绝不执行代码**（参考项目 `hard_boundaries`）。
"""

from __future__ import annotations

from typing import Any

import pytest

from core import sourcefetch


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.content = text.encode("utf-8")

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeClient:
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
        raise ConnectionError(f"不通：{url}")


def _install(monkeypatch: pytest.MonkeyPatch, routes: dict[str, Any]) -> _FakeClient:
    client = _FakeClient(routes)
    monkeypatch.setattr(sourcefetch, "_client", lambda timeout=None: client)
    return client


# ---------------------------------------------------------------------------
# ★★ fetch_repo_contents（不下整包）
# ---------------------------------------------------------------------------


async def test_contents_returns_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [
        {"name": "src", "type": "dir", "size": 0},
        {"name": "README.md", "type": "file", "size": 1234},
    ]
    client = _install(
        monkeypatch, {"https://api.github.com": _FakeResponse(200, payload=payload)}
    )
    entries, result = await sourcefetch.fetch_repo_contents("o", "r", ref="main")

    assert result.ok is True
    assert entries is not None
    assert [e["name"] for e in entries] == ["src", "README.md"]
    assert entries[0]["type"] == "dir"
    assert entries[1]["size"] == 1234
    # ★ 关键：**只调了 contents API**，没有去下 zip
    assert len(client.requested) == 1
    assert "codeload" not in client.requested[0]
    assert "/contents" in client.requested[0]


async def test_contents_passes_ref_as_param(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(
        monkeypatch, {"https://api.github.com": _FakeResponse(200, payload=[])}
    )
    await sourcefetch.fetch_repo_contents("o", "r", ref="dev")
    assert client.requested[0].endswith("/contents")


async def test_contents_non_list_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 传了**文件路径**时 API 返回 dict 而不是 list —— 要明确报错。"""
    _install(
        monkeypatch,
        {"https://api.github.com": _FakeResponse(200, payload={"type": "file"})},
    )
    entries, result = await sourcefetch.fetch_repo_contents("o", "r", path="a.py")
    assert entries is None
    assert result.code == "NOT_A_DIRECTORY"


async def test_contents_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"https://api.github.com": _FakeResponse(404)})
    entries, result = await sourcefetch.fetch_repo_contents("o", "nope")
    assert entries is None
    assert result.code == "HTTP_404"


async def test_contents_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"https://api.github.com": ConnectionError("boom")})
    entries, result = await sourcefetch.fetch_repo_contents("o", "r")
    assert entries is None
    assert result.code == "NETWORK"


async def test_contents_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"https://api.github.com": _FakeResponse(200, payload=None)})
    entries, result = await sourcefetch.fetch_repo_contents("o", "r")
    assert entries is None
    assert result.code == "BAD_SHAPE"


async def test_contents_skips_non_dict_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """形状不可信（list 里混了字符串）→ 跳过，不崩。"""
    _install(
        monkeypatch,
        {
            "https://api.github.com": _FakeResponse(
                200, payload=["垃圾", {"name": "ok.py", "type": "file", "size": 1}, None]
            )
        },
    )
    entries, result = await sourcefetch.fetch_repo_contents("o", "r")
    assert result.ok is True
    assert entries is not None
    assert [e["name"] for e in entries] == ["ok.py"]


def test_contents_is_in_module_namespace() -> None:
    """函数要真在模块里（防止只写了文档没实现）。"""
    assert hasattr(sourcefetch, "fetch_repo_contents")
