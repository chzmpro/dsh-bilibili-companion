"""``uv`` 自举测试（P2，2026-10-16）。

源码参考：竞品 `lib/uvfetch.mjs`。它踩过的坑**逐条**变成这里的断言：

* **PyPI 的 ``urls`` 顺序不保证最新在前** ⇒ 必须自己按版本号排序
  （它注释：曾因此取到 ``0.2.33`` / 2024-08 的老版本）；
* **"下载下来是要被执行的"** ⇒ sha256 **对不上就换源**，绝不降级使用；
* **取不到**官方 sha256 → 只记「跳过校验」，**不拦**；
* 解压后在**整棵树里按文件名找**（不管官方放哪一层）；
* 已有就返回（幂等）。

**绝不联网**：全部用 ``httpx.MockTransport`` 或直接测纯函数。
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import httpx
import pytest

from core.uvfetch import (
    ASSETS,
    UvFetchResult,
    ensure_uv,
    extract_archive,
    find_bin,
    pick_newest_wheel,
    sha256_of,
)

# ---------------------------------------------------------------------------
# 纯函数：版本排序（竞品踩过的坑）
# ---------------------------------------------------------------------------


def test_pick_newest_wheel_ignores_pypi_order() -> None:
    """★★ 核心回归：``urls`` 顺序**不保证**最新在前 —— 必须自己排序。

    竞品注释原文：*曾取到 0.2.33/2024-08*。
    这里故意把**旧的放前面**（模拟 PyPI 的真实行为），断言选出的是最新。
    """
    urls = [
        {"filename": "uv-0.2.33-py3-none-win_amd64.whl", "url": "https://x/old.whl"},
        {"filename": "uv-0.9.1-py3-none-win_amd64.whl", "url": "https://x/new.whl"},
        {"filename": "uv-0.5.0-py3-none-win_amd64.whl", "url": "https://x/mid.whl"},
    ]
    chosen = pick_newest_wheel(urls)
    assert chosen is not None
    assert chosen["filename"] == "uv-0.9.1-py3-none-win_amd64.whl"


def test_pick_newest_wheel_handles_double_digit_versions() -> None:
    """字符串排序会把 0.9 排到 0.10 后面 —— 必须按数字比。"""
    urls = [
        {"filename": "uv-0.9.0-py3-none-win_amd64.whl"},
        {"filename": "uv-0.10.0-py3-none-win_amd64.whl"},
    ]
    chosen = pick_newest_wheel(urls)
    assert chosen is not None
    assert chosen["filename"] == "uv-0.10.0-py3-none-win_amd64.whl"


def test_pick_newest_wheel_returns_none_without_candidates() -> None:
    assert pick_newest_wheel([]) is None
    assert pick_newest_wheel([{"filename": "uv-1.0-py3-none-manylinux.whl"}]) is None


# ---------------------------------------------------------------------------
# 解压与查找
# ---------------------------------------------------------------------------


def test_find_bin_searches_recursively(tmp_path: Path) -> None:
    """★ 官方可能把可执行文件放在任意层级 ⇒ 必须递归找。"""
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    target = nested / "uv.exe"
    target.write_bytes(b"fake")

    assert find_bin("uv.exe", tmp_path) == target
    assert find_bin("nope.exe", tmp_path) is None


def test_extract_archive_from_zip(tmp_path: Path) -> None:
    archive = tmp_path / "uv.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("deep/dir/uv.exe", "fake-binary")

    into = tmp_path / "out"
    hit = extract_archive("uv.exe", archive, into)
    assert hit is not None
    assert hit.name == "uv.exe"


def test_extract_archive_returns_none_for_garbage(tmp_path: Path) -> None:
    archive = tmp_path / "not-an-archive.bin"
    archive.write_bytes(b"\x00\x01\x02 not a zip")
    assert extract_archive("uv.exe", archive, tmp_path / "out") is None


# ---------------------------------------------------------------------------
# sha256
# ---------------------------------------------------------------------------


def test_sha256_of_matches_hashlib(tmp_path: Path) -> None:
    target = tmp_path / "x.bin"
    payload = b"hello uv"
    target.write_bytes(payload)
    assert sha256_of(target) == hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# ensure_uv：幂等 / 校验 / 换源
# ---------------------------------------------------------------------------


async def test_ensure_uv_is_idempotent(tmp_path: Path) -> None:
    """已有就直接返回，**不发任何请求**。"""
    existing = tmp_path / "uv.exe"
    existing.write_bytes(b"already there")

    result = await ensure_uv(target_dir=tmp_path)
    assert result.ok is True
    assert result.path == existing
    assert result.source == "已存在"


def _make_zip_bytes() -> bytes:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("uv.exe", "real-binary")
    return buffer.getvalue()


async def test_ensure_uv_downloads_and_verifies(tmp_path: Path, monkeypatch) -> None:
    """正常路径：下载 → sha256 对得上 → 解压 → 就位，且标记 verified。"""
    zip_bytes = _make_zip_bytes()
    good_sha = hashlib.sha256(zip_bytes).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith(".sha256"):
            return httpx.Response(200, text=good_sha + "  uv.zip\n")
        if "pypi" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, content=zip_bytes)

    # 把 httpx.AsyncClient 换成走 MockTransport 的
    original = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return original(transport=httpx.MockTransport(handler), follow_redirects=True)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    result = await ensure_uv(target_dir=tmp_path)
    assert result.ok is True, result.message
    assert result.verified is True, "校验通过时应标记 verified"
    assert result.path is not None and result.path.exists()


async def test_ensure_uv_rejects_bad_sha_and_tries_next_source(tmp_path: Path, monkeypatch) -> None:
    """★★ 核心安全断言：sha256 **对不上就换源**，绝不降级使用。

    这里让**第一个源**返回坏校验值、**第二个源**返回正确的，
    断言最终用的是第二个源（而不是把坏文件装上去）。
    """
    zip_bytes = _make_zip_bytes()
    good_sha = hashlib.sha256(zip_bytes).hexdigest()
    bad_sha = "0" * 64

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if url.endswith(".sha256"):
            # 第一个源（GitHub 直连）给坏值；代理源给正确值
            if "gh-proxy.com" in url:
                return httpx.Response(200, text=good_sha)
            return httpx.Response(200, text=bad_sha)
        if "pypi" in url:
            return httpx.Response(404)
        return httpx.Response(200, content=zip_bytes)

    original = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return original(transport=httpx.MockTransport(handler), follow_redirects=True)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    logs: list[str] = []
    result = await ensure_uv(target_dir=tmp_path, log=logs.append)

    assert result.ok is True
    assert result.source == "GitHub 加速代理 gh-proxy.com", (
        f"应该换到第二个源，实际 {result.source}"
    )
    assert any("sha256 校验不过" in line for line in logs), "应该记下校验失败"


async def test_ensure_uv_all_sources_fail(tmp_path: Path, monkeypatch) -> None:
    """全部源失败 → ok=False + 人话原因，**不抛异常**。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    original = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return original(transport=httpx.MockTransport(handler), follow_redirects=True)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    result = await ensure_uv(target_dir=tmp_path)
    assert result.ok is False
    assert result.code == "ALL_SOURCES_FAILED"
    assert "uv" in result.message


async def test_ensure_uv_skips_check_when_no_sha(tmp_path: Path, monkeypatch) -> None:
    """取不到官方 sha256 → **只记跳过校验，不拦**（照竞品行为）。"""
    zip_bytes = _make_zip_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith(".sha256"):
            return httpx.Response(404)
        if "pypi" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, content=zip_bytes)

    original = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return original(transport=httpx.MockTransport(handler), follow_redirects=True)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    logs: list[str] = []
    result = await ensure_uv(target_dir=tmp_path, log=logs.append)
    assert result.ok is True
    assert result.verified is False, "没校验成功就不该声称 verified"
    assert any("跳过校验" in line for line in logs)


def test_assets_cover_three_platforms() -> None:
    for key in ("win32", "darwin", "linux"):
        assert key in ASSETS
        name, binary = ASSETS[key]
        assert name and binary


def test_uv_fetch_result_defaults() -> None:
    result = UvFetchResult(ok=False)
    assert result.path is None
    assert result.verified is False
    assert result.code == "OK"


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("uv-0.9.1-py3-none-win_amd64.whl", (0, 9, 1, 0)),
        ("uv-0.10.0-py3-none-win_amd64.whl", (0, 10, 0, 0)),
        # 解析不出时返回全零占位（长度与上面不同没关系 —— 只用来排序）
        ("nonsense.whl", (0, 0, 0)),
    ],
)
def test_version_parsing(filename: str, expected: tuple[int, ...]) -> None:
    from core.uvfetch import _version_of_wheel

    assert _version_of_wheel(filename) == expected
