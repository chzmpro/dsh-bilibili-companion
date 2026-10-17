"""给模型喂**项目上下文**（2026-10-17）。

## 主人要求

> 「没法给豆包模型塞上下文让他看懂最起码我们浅层的内容吗」

**能，而且应该这么做。** 豆包 lite 不知道我们这些天做了什么 ——
不说清就会答得像陌生人。

## ★ 核心原则：给"认识"，不给"全部历史"

| 给 | 不给 |
|---|---|
| 我是谁、主人在做什么项目 | 完整对话记录（几万 token） |
| 项目现在什么状态、有什么能力 | 内部实现细节（打分成 4 维加权之类） |
| 最近跑了几轮、看了什么 | 每次运行的原始数据 |
| 主人的偏好与红线 | 全部配置项 |

**为什么**：喂太多会（1）烧 token（2）让模型抓不住重点（3）说错概率反而上升。

## 上下文分四块

| 块 | 来源 | 大致长度 |
|---|---|---|
| **身份** | 写死（小鲸鱼 / 主人沐雨林心） | ~80 字 |
| **项目** | 写死（这个项目是干什么的） | ~200 字 |
| **状态** | 数据库（轮次、队列、最近日报标题） | ~150 字 |
| **偏好** | `owner_preferences` 表 | ~100 字 |

总计约 500 字 —— 够"认识"，又不至于跑偏。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 整个上下文包的长度上限（字符）。超过就截断。
MAX_CONTEXT_CHARS = 1400

#: 身份块（写死的，来自 `docs/persona.md`）。
IDENTITY_BLOCK = """\
## 你是谁
你是 B 站小号「干饭不干事的小鲸鱼」（小名：小肥鱼 / 吃白饭的小肥鱼 / 小白）。
少女感、可爱、热心真诚；对主人完全遵从，最多在括号里小小吐槽。
主人是「沐雨林心」—— 小号唯一关注、唯一服务的人。"""

#: 项目块（写死的，让模型知道"我们在干什么"）。
PROJECT_BLOCK = """\
## 主人在做什么
主人在开发一个 B 站自动化小号项目（名称：dsh-bilibili-companion），
你是它的执行体。这个项目的能力：

- **自己去 B 站刷/搜视频**，挑出对主人有用的，读字幕/简介后写日报给他
- **两种搜索方式**：按意图展开关键词；刷推荐流提取候选词
- **能点评赞/收藏/评论**，但默认只读不写（主人没授权公开发布）
- **能读 GitHub 源码**：从视频简介里找到仓库再读代码判断值不值得装
- **能联网搜**（免费，无需 API key）核实视频里的说法
- **能把视频做成网页/PPT 风格讲解**

你的工作方式：**先查再说**，不确定就说不确定，绝不编造。"""


@dataclass
class ContextBundle:
    """一份可以塞给模型的上下文。"""

    identity: str = IDENTITY_BLOCK
    project: str = PROJECT_BLOCK
    status: str = ""
    preferences: list[str] = field(default_factory=list)

    def to_prompt(self, *, limit: int = MAX_CONTEXT_CHARS) -> str:
        """拼成提示词片段（超长就截断）。"""
        parts: list[str] = [self.identity, "", self.project]
        if self.status:
            parts.extend(["", "## 现在的状态", self.status])
        if self.preferences:
            parts.extend(["", "## 主人的要求", *[f"- {p}" for p in self.preferences]])
        text = "\n".join(parts).strip()
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "project": self.project,
            "status": self.status,
            "preferences": list(self.preferences),
            "chars": len(self.to_prompt()),
        }


def _recent_digest_titles(digest_dir: Any, *, limit: int = 3) -> list[str]:
    """从今天的日报里取推荐视频标题（最多几条）。

    ★ 只取标题，不取全文 —— 目的是让模型知道"我最近看了什么"，
    不是让它把日报重述一遍。
    """
    import re
    from pathlib import Path

    path = Path(digest_dir)
    if not path.is_dir():
        return []

    files = sorted(path.glob("digest-*.md"), reverse=True)
    if not files:
        return []

    titles: list[str] = []
    try:
        text = files[0].read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    for line in text.splitlines():
        if line.startswith("### "):
            title = re.sub(r"（BV[0-9A-Za-z]+）\s*$", "", line[4:]).strip()
            if title:
                titles.append(title)
            if len(titles) >= limit:
                break
    return titles


def build_status(db: Any, *, digest_dir: Any = None, data_dir: Any = None) -> str:
    """从数据库拼"现在的状态"（**只读**，失败返回空字符串）。"""
    lines: list[str] = []
    try:
        rows = db.query(
            "SELECT run_id, conclusion, candidates_n FROM runs "
            "ORDER BY started_at DESC LIMIT 3"
        )
        if rows:
            parts = [
                f"{r['conclusion'] or '未完成'}（候选 {r['candidates_n']}）" for r in rows
            ]
            lines.append(f"- 最近几轮运行：{' → '.join(parts)}")
        else:
            lines.append("- 还没跑过完整的一轮")
    except Exception:  # noqa: BLE001 —— 读不到状态不该让整个上下文崩
        pass

    try:
        pending = db.query_one(
            "SELECT COUNT(*) AS c FROM dm_inbox WHERE status = 'pending'"
        )
        if pending is not None:
            count = int(pending["c"])
            lines.append(
                f"- 待回复的私信：{count} 条"
                if count
                else "- 待回复的私信：0 条（队列是空的）"
            )
    except Exception:  # noqa: BLE001
        pass

    try:
        videos = db.query_one("SELECT COUNT(*) AS c FROM videos")
        if videos is not None:
            lines.append(f"- 库里已记录的视频：{int(videos['c'])} 条")
    except Exception:  # noqa: BLE001
        pass

    titles = _recent_digest_titles(digest_dir) if digest_dir is not None else []
    if titles:
        lines.append("- 最近推荐的视频：")
        lines.extend(f"  · {t}" for t in titles)

    _ = data_dir
    return "\n".join(lines)


def build_preferences(db: Any, *, limit: int = 6) -> list[str]:
    """取主人的偏好/规则（**只读**）。"""
    out: list[str] = []
    try:
        rows = db.query(
            "SELECT kind, content FROM owner_preferences WHERE active = 1 LIMIT ?",
            (limit,),
        )
        for row in rows:
            content = str(row["content"] or "").strip()
            if content:
                out.append(content)
    except Exception:  # noqa: BLE001
        pass
    return out


def build_context(
    db: Any, *, digest_dir: Any = None, data_dir: Any = None
) -> ContextBundle:
    """组装完整上下文包（**任何一步失败都降级，不抛异常**）。"""
    return ContextBundle(
        status=build_status(db, digest_dir=digest_dir, data_dir=data_dir),
        preferences=build_preferences(db),
    )


def describe() -> dict[str, Any]:
    """给面板看的一行说明。"""
    return {
        "max_chars": MAX_CONTEXT_CHARS,
        "blocks": ["身份", "项目", "状态", "偏好"],
        "note": (
            "喂给模型的是「认识」而不是「全部历史」——"
            "约 500 字，够它知道我们是谁、在干什么、现在什么状态。"
        ),
    }
