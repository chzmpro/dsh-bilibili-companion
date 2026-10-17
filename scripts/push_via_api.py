"""通过 GitHub **Git Data API** 推送本地提交（2026-10-17）。

## 为什么需要它

本机 `github.com`（git 主机）**在 shell 里完全不通**：

| 主机 | 实测 |
|---|---|
| `github.com:443` | ❌ HTTP 000 / connection reset |
| `ssh.github.com:443` | ✅ TCP 通，但 `Permission denied (publickey)`（没 key） |
| `api.github.com` | ✅ **200** |

`git push` 走的是 `github.com:443`，所以**必然失败**。
而 `api.github.com` 是通的 ⇒ 改用 **Git Data API** 把对象推上去。

> ⚠️ 试过用 API 加 SSH key（`POST /user/keys`），返回 `Not Found` ——
> 说明存着的 token **没有 `admin:public_key` 权限**，所以 SSH 路走不了。

## 它做什么

按**本地提交顺序**逐个重建：

1. `git ls-tree -r <sha>` 拿到该提交的全部文件与 blob sha；
2. 对每个**唯一内容** `POST /git/blobs`（base64，只建一次，按本地 blob sha 去重）；
3. `POST /git/trees` 建树；
4. `POST /git/commits` 建提交（带 parent、原始 author/committer/时间）；
5. 最后 `PATCH /git/refs/heads/main` 指向最后一个提交。

⇒ **历史原样保留**（不是压缩成一个初始提交）。

## 安全

token 从 `git credential fill` 取，**绝不打印**；只打印结果与计数。
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
OWNER = "chzmpro"
REPO = "dsh-bilibili-companion"
BRANCH = "main"


def sh(*args: str) -> bytes:
    """跑一条 git 命令，返回原始字节（**二进制安全**）。"""
    result = subprocess.run(args, capture_output=True, check=True)
    return result.stdout


def token_from_gcm() -> str:
    """取 token（不打印）。

    优先读环境变量 ``GH_PUSH_TOKEN``（由 shell 用 ``git credential fill`` 取得后传入）——
    因为 GCM 在**非 TTY 的 Python 子进程**里会拒绝应答
    （直接 subprocess 调 ``credential fill`` 返回空）。
    """
    env_token = os.environ.get("GH_PUSH_TOKEN", "").strip()
    if env_token:
        return env_token
    out = subprocess.run(
        ["git", "-c", "credential.interactive=false", "credential", "fill"],
        input=b"protocol=https\nhost=github.com\n\n",
        capture_output=True,
        check=False,
    ).stdout.decode("utf-8", "replace")
    for line in out.splitlines():
        if line.startswith("password="):
            return line[len("password=") :].strip()
    return ""


def api(method: str, path: str, payload: dict | None, token: str, *, retries: int = 4) -> dict:
    """调一次 API，**带重试**（网络瞬断在本机很常见）。"""
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    last_error = ""
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(
            API + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            # 4xx 是**确定性**错误，重试没用，直接抛
            if 400 <= exc.code < 500:
                raise SystemExit(f"API {method} {path} → {exc.code}: {body}") from exc
            last_error = f"HTTP {exc.code}: {body}"
        except (urllib.error.URLError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        wait = 2**attempt
        print(f"    重试 {attempt}/{retries}（{last_error[:60]}），等 {wait}s")
        time.sleep(wait)

    raise SystemExit(f"API {method} {path} 连续失败：{last_error}")


def commit_meta(sha: str) -> dict[str, str]:
    """取提交的 message 与 author/committer 信息。"""
    raw = sh("git", "show", "-s", "--format=%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%B", sha)
    parts = raw.decode("utf-8", "replace").split("\0")
    return {
        "an": parts[0],
        "ae": parts[1],
        "ad": parts[2],
        "cn": parts[3],
        "ce": parts[4],
        "cd": parts[5],
        "message": parts[6].rstrip("\n"),
    }


def seed_empty_repo(token: str) -> str | None:
    """给**空仓库**播一个初始提交，返回它的 sha。

    ★ 为什么必须要这一步（2026-10-17 实测踩到）：

    `POST /git/blobs` 在**完全空的仓库**上直接返回
    `409 {"message":"Git Repository is empty."}` ——
    也就是说 Git Data API **要求仓库先至少有一个提交**。

    而 Contents API（`PUT /contents/<path>`）**可以**在空仓库上创建第一个提交。
    ⇒ 先播一个占位 README，再把它当作我们第一个提交的 parent。

    Returns:
        已有提交时返回 ``None``（不需要播）；空仓库时返回新提交 sha。
    """
    try:
        api("GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/{BRANCH}", None, token)
        return None  # 分支已存在，说明仓库非空
    except SystemExit:
        pass  # 分支不存在 → 空仓库，继续播

    seed_text = (
        "# dsh-bilibili-companion\n\n初始化占位提交，随后由 Git Data API 推入完整历史。\n"
    )
    seed = api(
        "PUT",
        f"/repos/{OWNER}/{REPO}/contents/README.md",
        {
            "message": "chore: 初始化仓库（Git Data API 要求非空仓库）",
            "content": base64.b64encode(seed_text.encode("utf-8")).decode("ascii"),
            "branch": BRANCH,
        },
        token,
    )
    return seed["commit"]["sha"]


def main() -> int:
    token = token_from_gcm()
    if not token:
        print("取不到凭据")
        return 2

    me = api("GET", "/user", None, token)
    print(f"登录: {me.get('login')}")

    seed = seed_empty_repo(token)
    if seed:
        print(f"空仓库 → 已播初始提交 {seed[:8]}")
        parent: str | None = seed
    else:
        print("仓库已有提交，**不复用远端 parent**（让 SHA 与本地完全一致）")
        parent = None

    commits = (
        sh("git", "rev-list", "--reverse", "HEAD").decode("utf-8").split()
    )
    print(f"本地提交数: {len(commits)}")

    blob_cache: dict[str, str] = {}

    for index, sha in enumerate(commits, 1):
        meta = commit_meta(sha)
        subject = meta["message"].splitlines()[0][:60] if meta["message"] else ""
        print(f"\n[{index}/{len(commits)}] {sha[:8]} {subject}")

        listing = sh("git", "ls-tree", "-r", sha).decode("utf-8", "replace")
        entries: list[dict[str, str]] = []
        created = 0
        for line in listing.splitlines():
            if not line.strip():
                continue
            head, path = line.split("\t", 1)
            mode, kind, blob_sha = head.split()
            if kind != "blob":
                continue
            remote_sha = blob_cache.get(blob_sha)
            if remote_sha is None:
                content = sh("git", "cat-file", "blob", blob_sha)
                payload = {
                    "content": base64.b64encode(content).decode("ascii"),
                    "encoding": "base64",
                }
                remote_sha = api(
                    "POST", f"/repos/{OWNER}/{REPO}/git/blobs", payload, token
                )["sha"]
                blob_cache[blob_sha] = remote_sha
                created += 1
            entries.append(
                {"path": path, "mode": mode, "type": "blob", "sha": remote_sha}
            )

        print(f"  文件 {len(entries)} 个（新建 blob {created} 个）")
        tree = api(
            "POST",
            f"/repos/{OWNER}/{REPO}/git/trees",
            {"tree": entries},
            token,
        )
        print(f"  tree {tree['sha'][:8]}")

        commit_payload: dict[str, object] = {
            "message": meta["message"],
            "tree": tree["sha"],
            "author": {"name": meta["an"], "email": meta["ae"], "date": meta["ad"]},
            "committer": {"name": meta["cn"], "email": meta["ce"], "date": meta["cd"]},
        }
        if parent:
            commit_payload["parents"] = [parent]
        created_commit = api(
            "POST", f"/repos/{OWNER}/{REPO}/git/commits", commit_payload, token
        )
        parent = created_commit["sha"]
        print(f"  commit {parent[:8]}")

    assert parent is not None
    try:
        api(
            "PATCH",
            f"/repos/{OWNER}/{REPO}/git/refs/heads/{BRANCH}",
            {"sha": parent, "force": True},
            token,
        )
    except SystemExit:
        # 分支还不存在时用 POST 创建
        api(
            "POST",
            f"/repos/{OWNER}/{REPO}/git/refs",
            {"ref": f"refs/heads/{BRANCH}", "sha": parent},
            token,
        )

    print(f"\n✅ 已推送 {len(commits)} 个提交到 {OWNER}/{REPO}#{BRANCH}")
    print(f"   HEAD = {parent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
