"""从视频里抽出 GitHub / Gitee 仓库地址（2026-10-17）。

## 为什么需要它

主人的原话（纠正我之前的误解）：

> 「我私信部分是让你在**B站搜索或者刷取**，然后**刷到了再去相应的网站看源码**
> 等相关信息进而了解完全」

所以完整闭环是：

```
私信线索 → B站搜索/推荐流 → 读视频 → **顺视频找仓库 → 读源码** → 回复主人
```

最后那一环之前**没有**。

## ★ 参考项目对照（诚实记录）

我读了 `xiaoyaya191/bilibili_learning_bot`（8.5 万行）与 `jiran214/langup-ai`：

* 两个项目**都没有**"从视频提取仓库地址并读源码"的能力；
* 参考项目的 agent 工具只有 B站操作（搜视频/看评论/发弹幕/知识库），
  **没有代码阅读**；
* 而且 `evolution_engine.py` 里明确把「**不修改源码或执行生成代码**」
  写进了 `hard_boundaries` —— 这是**值得学的安全设计**。

⇒ 这一环是**我们要新做的**，但可以复用已有的
:mod:`core.sourcefetch`（多源兜底 + 可审计）。

## 设计要点

1. **只认确定的仓库地址**，不做模糊猜测（猜错会读到无关项目）；
2. **支持 4 种形态**：`github.com/owner/repo`、`gitee.com/owner/repo`、
   `github.com/owner/repo.git`、以及裸 `owner/repo` 只在**明确上下文**下才认；
3. **去重 + 排序**：按出现次数与位置，最多取 N 个；
4. **绝不执行仓库里的代码** —— 只读文本（这是参考项目教我们的红线）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: 支持的平台域名 → 归一化后的平台名
_HOSTS: dict[str, str] = {
    "github.com": "github",
    "www.github.com": "github",
    "gitee.com": "gitee",
    "www.gitee.com": "gitee",
}

#: 匹配 ``https://github.com/owner/repo`` 这类完整 URL（可选 ``.git`` 后缀）。
#:
#: ★ 也接受**没有 ``https://``** 的形态（``github.com/owner/repo``）——
#: 实测：字幕/口播里念出来的地址常常是裸域名（ASR 会把 ``https://`` 吃掉）。
#: 但**必须**带域名（不认裸 ``owner/repo``，那会误判成普通斜杠文本）。
#:
#: ⚠️ 刻意**不**匹配 ``/issues`` ``/pull`` ``/blob`` 这类子路径 ——
#: 那些不是仓库根地址，拿去做"读源码"会 404。
_REPO_URL = re.compile(
    r"(?:https?://)?(?:www\.)?(github\.com|gitee\.com)"
    r"/([A-Za-z0-9_.-]{1,39})/([A-Za-z0-9_.-]{1,100}?)"
    r"(?:\.git)?"
    r"(?=[/\s)\]\"'<>,，。；;]|$)",
    re.IGNORECASE,
)

#: 这些"仓库名"其实是 GitHub 的**功能页**，不是仓库
_RESERVED_OWNERS: frozenset[str] = frozenset(
    {
        "features",
        "topics",
        "collections",
        "trending",
        "marketplace",
        "sponsors",
        "settings",
        "notifications",
        "explore",
        "about",
        "pricing",
        "login",
        "join",
        "search",
        "apps",
        "orgs",
        "users",
    }
)


@dataclass(frozen=True)
class RepoRef:
    """一个仓库引用。"""

    platform: str
    owner: str
    repo: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def url(self) -> str:
        host = "github.com" if self.platform == "github" else "gitee.com"
        return f"https://{host}/{self.owner}/{self.repo}"

    def to_dict(self) -> dict[str, str]:
        return {
            "platform": self.platform,
            "owner": self.owner,
            "repo": self.repo,
            "full_name": self.full_name,
            "url": self.url,
        }


def extract_repos(text: str, *, limit: int = 5) -> list[RepoRef]:
    """从任意文本里抽出**仓库地址**（去重，保持首次出现顺序）。

    Args:
        text: 视频简介 / 字幕 / 评论 / 私信正文。
        limit: 最多返回几个（默认 5）。

    Returns:
        :class:`RepoRef` 列表。**抽不到就返回空列表**（不猜）。

    ★ 为什么"不猜"：从标题猜"这个视频可能讲某个仓库"会读错项目，
    浪费大量 token 且结论错误。**只认明写的 URL。**
    """
    if not text:
        return []

    seen: set[tuple[str, str, str]] = set()
    out: list[RepoRef] = []

    for match in _REPO_URL.finditer(text):
        host = match.group(1).lower()
        platform = _HOSTS.get(host)
        if platform is None:
            continue
        owner = match.group(2)
        repo = match.group(3).rstrip(".")
        if not owner or not repo:
            continue
        # 跳过 GitHub 的功能页（features/topics/...）
        if platform == "github" and owner.lower() in _RESERVED_OWNERS:
            continue
        # 跳过明显不是仓库的（如 owner/repo 里 repo 是 issues）
        if repo.lower() in {"issues", "pulls", "releases", "wiki"}:
            continue

        key = (platform, owner.lower(), repo.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(RepoRef(platform=platform, owner=owner, repo=repo))
        if len(out) >= limit:
            break

    return out


def extract_from_video(
    *, title: str = "", description: str = "", subtitle: str = "", limit: int = 5
) -> list[RepoRef]:
    """从**视频的三个来源**里抽仓库地址（按可信度排序）。

    顺序（**可信度从高到低**）：

    1. ``description``（简介 —— UP 主通常把仓库贴这里）
    2. ``subtitle``（字幕 —— 视频里念出来的地址，可能被 ASR 写错）
    3. ``title``（标题 —— 少见，但有）

    去重后按"先出现的优先"排列。
    """
    ordered: list[RepoRef] = []
    seen: set[tuple[str, str, str]] = set()

    for source in (description, subtitle, title):
        for ref in extract_repos(source, limit=limit):
            key = (ref.platform, ref.owner.lower(), ref.repo.lower())
            if key in seen:
                continue
            seen.add(key)
            ordered.append(ref)
            if len(ordered) >= limit:
                return ordered
    return ordered


def describe(ref: RepoRef) -> dict[str, Any]:
    """给上层用的可读描述（含"读源码用什么通道"的提示）。"""
    return {
        **ref.to_dict(),
        "fetch_hint": (
            "用 core.sourcefetch：读单文件走 jsdelivr，下整包走 gh-proxy"
            "（codeload 直连只有 100KB/s，见 $DSH_HOME/AGENTS.md）"
        ),
    }
