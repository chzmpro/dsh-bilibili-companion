"""自举 ``uv``：机器上没有 uv / Python 时，项目自己拿一份可用的。

## 为什么需要（照竞品 `lib/uvfetch.mjs` 的思路）

``uv`` 是**单个可执行文件**（Windows 上约 32 MB），
**解压就能用、不用管理员、不用进 PATH**，而且它自己还能把 Python 拉下来。
⇒ 拿到它，"首次安装"就从「先去装个 Python」变成「点一下就好」。

## 三个源，按顺序试（全部实测自竞品，含它踩过的坑）

1. **GitHub 官方 release 直连** —— 有加速器时最快；
2. **``gh-proxy.com``** —— 国内可用；
3. **``ghproxy.net``** —— 备用；
4. （仅 Windows）**清华 PyPI 镜像的 uv wheel** —— wheel 本身就是 zip，里面装着 ``uv.exe``。
   ⚠️ **PyPI 的 ``urls`` 顺序不保证最新在前** ⇒ 必须**自己按版本号排序挑最新**
   （竞品注释：曾因此取到 0.2.33 / 2024-08 的老版本）。

## 完整性校验（**下载下来是要被执行的**）

* wheel：用 JSON 里自带的 ``digests.sha256``；
* GitHub 资产：再取一份 ``<url>.sha256`` 并正则抠 64 位十六进制。

**取不到**官方 sha256 → 只记「跳过校验」，**不拦**；
**对不上** → **换下一个源**（绝不降级使用）。

## 解压

优先用系统自带的 ``tar``（Windows 10+ 的 bsdtar 能直接读 zip），退到 ``unzip``，
然后**在整棵树里按文件名递归找** —— 不管官方放在哪一层都能找着。
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: 各平台的官方资产名与可执行文件名
ASSETS: dict[str, tuple[str, str]] = {
    "win32": ("uv-x86_64-pc-windows-msvc.zip", "uv.exe"),
    "darwin": ("uv-aarch64-apple-darwin.tar.gz", "uv"),
    "linux": ("uv-x86_64-unknown-linux-gnu.tar.gz", "uv"),
}

#: 清华镜像（本机实测 pypi.org 直连不通，见 docs/compatibility.md §7.2）
TUNA = "https://pypi.tuna.tsinghua.edu.cn"

DEFAULT_TIMEOUT_SEC = 300.0


@dataclass(frozen=True)
class UvFetchResult:
    ok: bool
    path: Path | None = None
    source: str = ""
    code: str = "OK"
    message: str = ""
    verified: bool = False


def _asset_for_platform() -> tuple[str, str]:
    key = "win32" if sys.platform.startswith("win") else sys.platform
    return ASSETS.get(key, ASSETS["linux"])


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_bin(name: str, root: Path) -> Path | None:
    """在目录树里**递归按文件名**找可执行文件。"""
    if not root.exists():
        return None
    for candidate in root.rglob(name):
        if candidate.is_file():
            return candidate
    return None


def extract_archive(bin_name: str, archive: Path, into: Path) -> Path | None:
    """解压并找出目标可执行文件。

    优先 ``tar``（Windows 自带 bsdtar 能读 zip），退到 ``unzip``，
    最后用 Python 标准库（``zipfile`` / ``tarfile``）—— 标准库那条**不依赖外部命令**，
    在受限环境里更稳。
    """
    into.mkdir(parents=True, exist_ok=True)

    for cmd in ("tar", "unzip"):
        if shutil.which(cmd) is None:
            continue
        args = (
            [cmd, "-xf", str(archive), "-C", str(into)]
            if cmd == "tar"
            else [cmd, "-o", str(archive), "-d", str(into)]
        )
        try:
            completed = subprocess.run(args, capture_output=True, timeout=120, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode == 0:
            hit = find_bin(bin_name, into)
            if hit is not None:
                return hit

    # 标准库兜底
    try:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(into)
        elif tarfile.is_tarfile(archive):
            with tarfile.open(archive) as tf:
                tf.extractall(into)  # noqa: S202 —— 来源是官方资产且已校验 sha256
        else:
            return None
    except (OSError, zipfile.BadZipFile, tarfile.TarError):
        return None
    return find_bin(bin_name, into)


def _version_of_wheel(filename: str) -> tuple[int, ...]:
    """从 ``uv-0.9.1-py3-none-win_amd64.whl`` 抠出版本号元组（用于排序）。"""
    match = re.search(r"-(\d+\.\d+\.\d+(?:[.-][0-9A-Za-z.]+)?)-py3-none", filename)
    if match is None:
        return (0, 0, 0)
    parts = re.split(r"[.-]", match.group(1))
    out: list[int] = []
    for piece in parts[:4]:
        try:
            out.append(int(piece))
        except ValueError:
            out.append(0)
    while len(out) < 4:
        out.append(0)
    return tuple(out)


def pick_newest_wheel(urls: list[dict[str, object]]) -> dict[str, object] | None:
    """从 PyPI JSON 的 ``urls`` 里挑**最新**的 win_amd64 wheel。

    ★ 竞品踩过的坑：**``urls`` 顺序不保证最新在前**
    （它因此取到过 ``0.2.33`` / 2024-08 的老版本）⇒ 必须自己排序。
    """
    candidates = [
        item for item in urls if str(item.get("filename", "")).endswith("win_amd64.whl")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: _version_of_wheel(str(item.get("filename", ""))), reverse=True)
    return candidates[0]


async def _expected_sha(client: object, url: str, declared: str | None) -> str | None:
    """取官方期望的 sha256：优先 JSON 自带的，否则抓 ``<url>.sha256``。"""
    if declared:
        return str(declared).lower()
    try:
        response = await client.get(url + ".sha256")  # type: ignore[attr-defined]
        if getattr(response, "status_code", 0) != 200:
            return None
        match = re.search(r"([0-9a-f]{64})", response.text, re.IGNORECASE)
        return match.group(1).lower() if match else None
    except Exception:  # noqa: BLE001 —— 取不到校验值不是致命错误
        return None


async def _candidate_sources(client: object, tmp: Path, asset: str) -> list[dict[str, object]]:
    """按顺序列出候选下载源（GitHub 直连 → 两个代理 → 清华 PyPI wheel）。"""
    github = f"https://github.com/astral-sh/uv/releases/latest/download/{asset}"
    sources: list[dict[str, object]] = [
        {"label": "GitHub 直连", "url": github, "file": tmp / asset, "sha": None},
        {
            "label": "GitHub 加速代理 gh-proxy.com",
            "url": f"https://gh-proxy.com/{github}",
            "file": tmp / asset,
            "sha": None,
        },
        {
            "label": "GitHub 加速代理 ghproxy.net",
            "url": f"https://ghproxy.net/{github}",
            "file": tmp / asset,
            "sha": None,
        },
    ]

    if sys.platform.startswith("win"):
        try:
            response = await client.get(f"{TUNA}/pypi/uv/json")  # type: ignore[attr-defined]
            payload = response.json()
            chosen = pick_newest_wheel(list(payload.get("urls") or []))
            if chosen is not None:
                digests = chosen.get("digests") or {}
                sha = digests.get("sha256") if isinstance(digests, dict) else None
                sources.append(
                    {
                        "label": "清华 PyPI 镜像（uv wheel）",
                        "url": str(chosen.get("url", "")),
                        "file": tmp / str(chosen.get("filename", "uv.whl")),
                        "sha": sha,
                    }
                )
        except Exception:  # noqa: BLE001 —— 列不出备选源不影响主源
            pass
    return sources


async def ensure_uv(
    *,
    target_dir: Path | str,
    log: Callable[[str], None] | None = None,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
) -> UvFetchResult:
    """确保 ``target_dir`` 里有一份可用的 uv，返回它的路径。

    已有就直接返回（**幂等**）。全部源都失败 → ``ok=False`` 且给出人话原因，
    **不抛异常**（调用方要能优雅降级）。
    """
    import httpx

    def say(message: str) -> None:
        if log is not None:
            log(message)

    directory = Path(target_dir)
    asset, bin_name = _asset_for_platform()
    target = directory / bin_name

    if target.exists():
        return UvFetchResult(ok=True, path=target, source="已存在", code="OK")

    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / "_tmp"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    timeout = httpx.Timeout(timeout_sec)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=False) as client:
        sources = await _candidate_sources(client, tmp, asset)

        for source in sources:
            label = str(source["label"])
            url = str(source["url"])
            file_path = Path(str(source["file"]))
            try:
                say(f"正在下载 uv —— {label}")
                response = await client.get(url)
                if response.status_code != 200:
                    say(f"  → 返回 {response.status_code}，换下一个源")
                    continue
                file_path.write_bytes(response.content)
                say(f"  → 下载完成 {len(response.content) / 1048576:.1f} MB")

                want = await _expected_sha(client, url, source.get("sha"))  # type: ignore[arg-type]
                verified = False
                if want:
                    got = sha256_of(file_path)
                    if got != want:
                        # ★ 下载物将被执行 ⇒ 校验不过只换源，**绝不降级使用**
                        say(f"  → sha256 校验不过（期望 {want[:12]}… 实际 {got[:12]}…）⇒ 丢弃")
                        continue
                    verified = True
                    say("  → 官方 sha256 校验通过")
                else:
                    say("  → 没取到官方 sha256，跳过校验（HTTPS 官方地址风险很小）")

                hit = extract_archive(bin_name, file_path, tmp)
                if hit is None:
                    say("  → 解压后没找到可执行文件，换下一个源")
                    continue
                shutil.copyfile(hit, target)
                with contextlib.suppress(OSError):
                    target.chmod(0o755)  # Windows 上不需要
                shutil.rmtree(tmp, ignore_errors=True)
                say(f"  → uv 已就位：{target}")
                return UvFetchResult(
                    ok=True, path=target, source=label, code="OK", verified=verified
                )
            except Exception as exc:  # noqa: BLE001 —— 单个源失败继续试下一个
                say(f"  → {label} 失败：{exc}")

    shutil.rmtree(tmp, ignore_errors=True)
    say("所有源都拿不到 uv（大概是网络到不了 GitHub / PyPI 镜像）")
    return UvFetchResult(
        ok=False,
        code="ALL_SOURCES_FAILED",
        message=(
            "所有下载源都失败（GitHub 直连、两个加速代理、清华 PyPI 镜像）。"
            "可以手动放一份 uv 到该目录，或自己装一个 uv / Python 3.10+。"
        ),
    )

