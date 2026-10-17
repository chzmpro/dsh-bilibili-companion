"""从 GitHub / Gitee 取源码的**可用通道**（2026-10-17 实测固化）。

## 为什么要有它

本机对 GitHub 的**访问是不完整的**（实测）：

============================  ==========  ==========================================
主机                          结果        用途
============================  ==========  ==========================================
``github.com``                ❌ 超时     网页浏览（用 gitee 镜像替代）
``raw.githubusercontent.com`` ❌ 秒失败   读单个文件（DNS 不通）
**``api.github.com``**        ✅ 200      仓库元数据 / 列文件 / 查最新版本
**``codeload.github.com``**   ✅ 200      **下整个仓库 zip**
**``gitee.com``**             ✅ 200      国内镜像
``cdn.jsdelivr.net``          ✅ 200      **替代 raw 读单文件**
``gh-proxy.com`` / ``ghproxy.net``  ✅ 200  代理 raw
============================  ==========  ==========================================

★ 结论：**不是"GitHub 不通"，而是"部分主机不通"**。
只要用对通道，读源码、下仓库、查版本**全都能做**。

## 通道选择（按用途）

| 我要做什么 | 用哪个 |
|---|---|
| 下整个仓库 | ``codeload`` zip（:func:`fetch_repo_zip`） |
| 读一个文件 | ``jsdelivr``（:func:`fetch_raw_file`，自动多源兜底） |
| 查仓库信息 / 最新 release | ``api.github.com``（:func:`fetch_repo_meta`） |
| 国内镜像项目 | ``gitee``（:func:`fetch_gitee_raw`） |
| 网页看 README | 浏览器通道（见下） |

## ★ 还有一条 shell 之外的通道：**浏览器**

实测（2026-10-17）：**同一台机器上，shell 不通的主机，浏览器能通**。

* shell: ``curl https://raw.githubusercontent.com/...`` → ❌ DNS 失败
* 浏览器（ego）打开同一个 URL → ✅ 拿到 "Hello World!"

⇒ 所以"GitHub 读不了"是**不准确的**：读不了的是 **shell 的 DNS**，
而浏览器走的是另一条网络路径。

**何时用浏览器通道**：shell 侧四个源全失败、又确实需要那个文件时。
用 ``ego_navigate`` + ``ego_js`` 取 ``document.body.innerText`` 即可
（本项目早先读抖音签名逆向文档就是这么做的）。

⚠️ 但**优先用 shell 侧的多源兜底**：它能被脚本化、可复现、进测试；
浏览器通道是**人工兜底**，不该写进自动化流程。

## 实测的多源兜底顺序（raw 类）

1. ``cdn.jsdelivr.net/gh/<owner>/<repo>@<ref>/<path>``
2. ``gh-proxy.com/https://raw.githubusercontent.com/...``
3. ``ghproxy.net/https://raw.githubusercontent.com/...``
4. 直连 ``raw.githubusercontent.com``（本机 shell 不通，留作其他环境兜底）

**逐源失败就换下一个**，全失败才报错 —— 与项目里 `core/uvfetch.py`
的"三源异构兜底"是同一套思路。

## 关于 gitee

实测：``gitee.com/<owner>/<repo>`` 对 **HEAD 返回 405**（不允许 HEAD），
但 **GET 正常**。所以探测可用性**必须用 GET**，别用 ``curl -I`` 下结论
（我第一轮就差点因此误判 gitee 不可用）。

gitee 的 raw 是**两跳**：``/raw/<ref>/<path>`` → 302 →
``raw.giteeusercontent.com/...``（带签名）。``httpx`` 开了 ``follow_redirects``
所以无感；用 ``curl`` 要加 ``-L``。
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 默认超时（下载 zip 可能较大）
DEFAULT_TIMEOUT_SEC = 120.0

#: raw 文件的多源模板（``{owner}`` / ``{repo}`` / ``{ref}`` / ``{path}``）
RAW_SOURCES: tuple[tuple[str, str], ...] = (
    ("jsdelivr", "https://cdn.jsdelivr.net/gh/{owner}/{repo}@{ref}/{path}"),
    (
        "gh-proxy",
        "https://gh-proxy.com/https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}",
    ),
    (
        "ghproxy.net",
        "https://ghproxy.net/https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}",
    ),
    ("raw 直连", "https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"),
)

#: zip 下载源（codeload 实测可用）
ZIP_SOURCES: tuple[tuple[str, str], ...] = (
    ("codeload", "https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{ref}"),
    ("codeload(tag)", "https://codeload.github.com/{owner}/{repo}/zip/refs/tags/{ref}"),
)

#: gitee raw（国内镜像项目）
GITEE_RAW = "https://gitee.com/{owner}/{repo}/raw/{ref}/{path}"

#: API 基址（实测可用）
API_BASE = "https://api.github.com"


class FetchError(RuntimeError):
    """取源码失败。``code`` 是稳定语义码。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


@dataclass
class FetchResult:
    """一次抓取的结果（``ok=False`` 时看 ``code`` / ``tried``）。"""

    ok: bool
    source: str = ""
    code: str = "OK"
    message: str = ""
    #: 每个源的尝试结果（**可审计**：哪条通、哪条不通、为什么）
    tried: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "source": self.source,
            "code": self.code,
            "message": self.message,
            "tried": self.tried,
        }


def _client(timeout: float = DEFAULT_TIMEOUT_SEC) -> Any:
    import httpx

    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
        trust_env=False,
        headers={"User-Agent": "dsh-bili-companion/0.1 (+source-fetch)"},
    )


async def fetch_raw_file(
    owner: str,
    repo: str,
    path: str,
    *,
    ref: str = "main",
    timeout: float = 30.0,
) -> tuple[str | None, FetchResult]:
    """读仓库里的**单个文件**（多源兜底）。

    Returns:
        ``(文件内容 或 None, 结果说明)``。内容为 ``None`` 时看 ``result.tried``。
    """
    result = FetchResult(ok=False)
    async with _client(timeout) as client:
        for label, template in RAW_SOURCES:
            url = template.format(owner=owner, repo=repo, ref=ref, path=path)
            try:
                response = await client.get(url)
            except Exception as exc:  # noqa: BLE001 —— 单源失败继续下一个
                result.tried.append({"source": label, "ok": False, "error": type(exc).__name__})
                continue
            if response.status_code == 200 and response.text:
                result.tried.append({"source": label, "ok": True, "bytes": len(response.content)})
                result.ok = True
                result.source = label
                return response.text, result
            result.tried.append(
                {"source": label, "ok": False, "status": response.status_code}
            )

    result.code = "ALL_SOURCES_FAILED"
    result.message = f"所有源都读不到 {owner}/{repo}/{path}@{ref}"
    return None, result


async def fetch_repo_contents(
    owner: str, repo: str, *, path: str = "", ref: str = "", timeout: float = 25.0
) -> tuple[list[dict[str, Any]] | None, FetchResult]:
    """用 **`api.github.com/contents`** 列目录（**不下载整包**）。

    ★ 为什么必须有它（2026-10-17 真机踩到）：

    第一版 `read_source` 为了"列根目录"直接下**整个 zip** ——
    一个 13 MB 的仓库要下 13 MB 才能知道它有哪些文件，
    而且实测**卡住不返回**（`fetch_repo_zip` 超过 240 秒）。

    `contents` API 一次只返回目录元数据（几 KB），**快几十倍**。

    Returns:
        ``(条目列表, 结果)``；条目形如
        ``{"name": "src", "type": "dir", "size": 0}``。
        失败时 ``(None, ...)``。
    """
    result = FetchResult(ok=False)
    url = f"{API_BASE}/repos/{owner}/{repo}/contents"
    if path:
        url += f"/{path.strip('/')}"
    params: dict[str, str] = {}
    if ref:
        params["ref"] = ref

    async with _client(timeout) as client:
        try:
            response = await client.get(url, params=params or None)
        except Exception as exc:  # noqa: BLE001
            result.tried.append(
                {"source": "api contents", "ok": False, "error": type(exc).__name__}
            )
            result.code = "NETWORK"
            result.message = f"列不出 {owner}/{repo} 的目录"
            return None, result

        if response.status_code != 200:
            result.tried.append(
                {"source": "api contents", "ok": False, "status": response.status_code}
            )
            result.code = f"HTTP_{response.status_code}"
            result.message = f"contents API 返回 {response.status_code}"
            return None, result

        try:
            payload = response.json()
        except ValueError:
            result.tried.append({"source": "api contents", "ok": False, "error": "bad json"})
            result.code = "BAD_SHAPE"
            return None, result

        if not isinstance(payload, list):
            # 传了具体文件路径时 API 返回 dict 而不是 list
            result.tried.append({"source": "api contents", "ok": False, "error": "not a list"})
            result.code = "NOT_A_DIRECTORY"
            result.message = f"{path} 不是目录"
            return None, result

        entries: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            entries.append(
                {
                    "name": str(item.get("name") or ""),
                    "type": "dir" if item.get("type") == "dir" else "file",
                    "size": int(item.get("size") or 0),
                }
            )
        result.tried.append({"source": "api contents", "ok": True, "entries": len(entries)})
        result.ok = True
        result.source = "api contents"
        return entries, result


async def fetch_repo_zip(
    owner: str,
    repo: str,
    *,
    ref: str = "main",
    timeout: float = DEFAULT_TIMEOUT_SEC,
) -> tuple[bytes | None, FetchResult]:
    """下**整个仓库的 zip**（``codeload``，实测可用）。

    Returns:
        ``(zip 字节 或 None, 结果说明)``。
    """
    result = FetchResult(ok=False)
    async with _client(timeout) as client:
        for label, template in ZIP_SOURCES:
            url = template.format(owner=owner, repo=repo, ref=ref)
            try:
                response = await client.get(url)
            except Exception as exc:  # noqa: BLE001
                result.tried.append({"source": label, "ok": False, "error": type(exc).__name__})
                continue
            if response.status_code == 200 and response.content:
                result.tried.append(
                    {"source": label, "ok": True, "bytes": len(response.content)}
                )
                result.ok = True
                result.source = label
                return response.content, result
            result.tried.append({"source": label, "ok": False, "status": response.status_code})

    result.code = "ALL_SOURCES_FAILED"
    result.message = f"下不到 {owner}/{repo}@{ref} 的 zip"
    return None, result


def _zip_common_prefix(names: list[str]) -> str:
    """判断 zip 是否有 GitHub 那种 ``<repo>-<ref>/`` 顶层前缀。

    ★ 安全要点（2026-10-17 测试暴露）：**绝不能**把 ``..`` 或单文件名当前缀。

    第一版逻辑是"只要所有条目都以第一段开头就当前缀"，结果：

    * ``["a.txt"]`` → 把 ``a.txt/`` 当前缀 → **整个文件名被剥掉**（文件丢失）；
    * ``["../evil.txt"]`` → 把 ``../`` 当前缀 → **路径穿越检查被绕过**！

    ⇒ 现在要求：第一段**必须真的像一个目录**
    （至少有一条名字是 ``<first>/...`` 形式），且不能是 ``.`` / ``..`` / 含 ``..``。
    """
    if not names:
        return ""
    first = names[0].split("/", 1)[0]
    if not first or first in {".", ".."} or ".." in first:
        return ""
    # 必须有条目是 "<first>/<something>"（而不是只有 "a.txt" 本身）
    has_nested = any(name.startswith(first + "/") and len(name) > len(first) + 1 for name in names)
    if not has_nested:
        return ""
    if not all(name == first or name.startswith(first + "/") for name in names):
        return ""
    return first + "/"


def extract_zip(payload: bytes, into: Path | str) -> list[Path]:
    """把 zip 解到目录，返回写出的文件（**不越界**：只写 ``into`` 之下）。

    GitHub 的 zip 顶层有一个 ``<repo>-<ref>/`` 前缀，这里**去掉它**，
    让解出来的结构就是仓库本身。
    """
    target = Path(into)
    target.mkdir(parents=True, exist_ok=True)
    resolved_root = target.resolve()
    written: list[Path] = []

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        prefix = _zip_common_prefix(archive.namelist())

        for info in archive.infolist():
            if info.is_dir():
                continue
            relative = info.filename[len(prefix) :] if prefix else info.filename
            if not relative:
                continue
            # ★ 防路径穿越：解析后必须仍在 target 之下
            destination = (target / relative).resolve()
            if destination != resolved_root and resolved_root not in destination.parents:
                raise FetchError("PATH_TRAVERSAL", f"zip 里有越界路径：{info.filename}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(info))
            written.append(destination)

    return written


async def fetch_gitee_raw(
    owner: str,
    repo: str,
    path: str,
    *,
    ref: str = "master",
    timeout: float = 30.0,
) -> tuple[str | None, FetchResult]:
    """从 **gitee** 读单个文件（国内镜像项目用）。

    ★ 实测要点：gitee 的 raw 是**两跳**
    （``/raw/<ref>/<path>`` → 302 → ``raw.giteeusercontent.com/...``），
    所以必须开 ``follow_redirects``。本函数已开。

    ⚠️ 别用 ``curl -I`` 判断 gitee 可用性 —— 它对 HEAD 返回 **405**，
    但 GET 完全正常。
    """
    result = FetchResult(ok=False)
    url = GITEE_RAW.format(owner=owner, repo=repo, ref=ref, path=path)
    async with _client(timeout) as client:
        try:
            response = await client.get(url)
        except Exception as exc:  # noqa: BLE001
            result.tried.append({"source": "gitee", "ok": False, "error": type(exc).__name__})
            result.code = "NETWORK"
            result.message = f"gitee 读不到 {owner}/{repo}/{path}@{ref}"
            return None, result

        if response.status_code == 200 and response.text:
            result.tried.append({"source": "gitee", "ok": True, "bytes": len(response.content)})
            result.ok = True
            result.source = "gitee"
            return response.text, result

        result.tried.append({"source": "gitee", "ok": False, "status": response.status_code})
        result.code = f"HTTP_{response.status_code}"
        result.message = f"gitee 返回 {response.status_code}：{owner}/{repo}/{path}@{ref}"
        return None, result


async def fetch_repo_meta(owner: str, repo: str, *, timeout: float = 20.0) -> dict[str, Any]:
    """查仓库元数据（``api.github.com``，实测可用）。

    拿得到时返回 ``{"ok": True, "data": {...}}``；失败返回 ``{"ok": False, ...}``
    —— **不抛异常**（它只是"补充信息"）。
    """
    async with _client(timeout) as client:
        try:
            response = await client.get(f"{API_BASE}/repos/{owner}/{repo}")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "code": "NETWORK", "message": type(exc).__name__}
        if response.status_code != 200:
            return {"ok": False, "code": f"HTTP_{response.status_code}"}
        data = response.json()
        if not isinstance(data, dict):
            return {"ok": False, "code": "BAD_SHAPE"}
        return {
            "ok": True,
            "data": {
                "full_name": data.get("full_name"),
                "description": data.get("description"),
                "default_branch": data.get("default_branch"),
                "stars": data.get("stargazers_count"),
                "license": ((data.get("license") or {}) or {}).get("spdx_id"),
                "updated_at": data.get("updated_at"),
                "archived": data.get("archived"),
            },
        }


def channel_report() -> list[dict[str, Any]]:
    """可用通道一览（给 ``cli.app sources`` 显示）。

    ⚠️ 这里的 ``works`` 是**实测结论**（2026-10-17），不是猜测。
    """
    return [
        {
            "channel": "api.github.com",
            "works": True,
            "use_for": "仓库元数据 / 列文件 / 查 release",
        },
        {
            "channel": "codeload.github.com",
            "works": True,
            "use_for": "下整个仓库 zip",
        },
        {
            "channel": "cdn.jsdelivr.net",
            "works": True,
            "use_for": "替代 raw 读单个文件（**首选**）",
        },
        {
            "channel": "gh-proxy.com / ghproxy.net",
            "works": True,
            "use_for": "代理 raw（jsdelivr 失败时的兜底）",
        },
        {
            "channel": "gitee.com",
            "works": True,
            "use_for": "国内镜像项目（⚠️ 探测要用 GET，HEAD 返 405）",
        },
        {
            "channel": "浏览器（ego）",
            "works": True,
            "use_for": "shell 全失败时的人工兜底（走另一条网络路径）",
        },
        {
            "channel": "github.com 网页",
            "works": False,
            "use_for": "shell 不通；用 jsdelivr / 浏览器替代",
        },
        {
            "channel": "raw.githubusercontent.com",
            "works": False,
            "use_for": "**shell** 不通（浏览器能通）；shell 侧走 jsdelivr",
        },
    ]
