"""「高分候选中转包」——把视频内容整理成上层模型能直接读的形态（2026-10-18）。

## 主人要求

> 「但凡是主动发给我的都是上层模型查看过的」
> 「我记得我之前要求的时候不是说了中转的时候把获取的高评分内容的
>   字幕评论等需要交给上层模型吗」

**这一步之前确实没做。** 原来投递给值班员的内容只有：

```
【主人的新私信】<私信文本>
请调用 dm_inbox 取出待回复的私信…
```

视频的字幕、评论、简介**一个字都没带** —— 值班员拿到的是"去处理吧"，
而不是"这是内容，你读"。

## 参数取自参考项目（bilibili_learning_bot）实测口径

```python
# brain/_brain_video.py:455
metadata = (
    f"标题：{title}\\n"
    f"简介：{str(description)[:2500]}\\n"
    f"评论：{str(comments)[:2500]}\\n"
    f"参考文本：{subtitle_text[:2500]}"
)
raw_comments = await get_hot_comments(aid, limit=8)
```

| 项 | 值 |
|---|---|
| 一轮读几个视频 | **3**（`dive_videos_default=3`，最多 10） |
| 单项截断 | **2500 字** |
| 评论条数 | **8 条** |

★ 它的注释很关键：`[FIX] 不再截断字幕——长视频字幕可能数万字，全部保留`
⇒ **存储不截断（evidence 表存全文），喂给模型时才截**。本模块也是这个分层。

## 为什么不能无限塞

3 个视频 × (2500+2500+2500) ≈ 22500 字，和"给豆包的 19268 字上下文"同量级。
再多会让上层模型抓不住重点（实测教训：上下文塞爆时它反而会漏读关键段）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 简介喂给模型的最大字数（参考项目 2500）。
MAX_DESCRIPTION_CHARS = 2500

#: 字幕喂给模型的最大字数（参考项目 2500）。
MAX_SUBTITLE_CHARS = 2500

#: 评论喂给模型的最大字数（参考项目 2500）。
MAX_COMMENTS_CHARS = 2500

#: 每个视频取几条热评（参考项目 limit=8）。
MAX_COMMENTS = 8

#: 一轮最多中转几个候选（参考项目 dive_videos_default=3）。
MAX_CANDIDATES = 3


@dataclass
class CandidateBrief:
    """一个候选视频的**中转包**（上层模型据此写推荐语）。"""

    bvid: str
    title: str
    score: int
    source_url: str
    up_name: str = ""
    description: str = ""
    subtitle: str = ""
    comments: list[str] = field(default_factory=list)
    dimensions: dict[str, int] = field(default_factory=dict)
    #: 这个候选来自哪个运行（便于追溯）
    run_id: str = ""

    def to_prompt(self) -> str:
        """渲染成给上层模型看的文本（**每项按参考项目口径截断**）。"""
        lines = [
            f"【候选】{self.score} 分 {self.bvid}",
            f"标题：{self.title}",
        ]
        if self.up_name:
            lines.append(f"UP：{self.up_name}")
        if self.dimensions:
            dims = self.dimensions
            lines.append(
                f"分维度：topic {dims.get('topic', 0)} / "
                f"实用性 {dims.get('usefulness', 0)} / "
                f"新颖 {dims.get('novelty', 0)} / "
                f"证据 {dims.get('evidence', 0)}"
            )
        lines.append(f"链接：{self.source_url}")

        if self.description.strip():
            lines.append("")
            lines.append("【简介】")
            lines.append(_cap(self.description, MAX_DESCRIPTION_CHARS))

        if self.subtitle.strip():
            lines.append("")
            lines.append("【字幕】")
            lines.append(_cap(self.subtitle, MAX_SUBTITLE_CHARS))

        if self.comments:
            lines.append("")
            lines.append(f"【热评（前 {len(self.comments)} 条）】")
            joined = "\n".join(f"- {c}" for c in self.comments)
            lines.append(_cap(joined, MAX_COMMENTS_CHARS))

        return "\n".join(lines)


def _cap(text: str, limit: int) -> str:
    """截断并**明确标注**被截了（避免上层模型误以为这就是全部）。

    ★ 为什么要标注：不标注的话模型会把"截断后的内容"当成完整内容，
    写推荐时可能漏掉后半段的关键信息，而且**它自己不会意识到漏了**。
    """
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n…（原文共 {len(value)} 字，此处截断至 {limit} 字）"


def build_briefs(
    db: Any, *, limit: int = MAX_CANDIDATES, min_score: int = 75
) -> list[CandidateBrief]:
    """从库里挑**最高分的候选**，组装成中转包。

    只挑有证据的；按分数降序；最多 `limit` 个。

    Args:
        db: 数据库。
        limit: 最多几个（默认 3，对齐参考项目）。
        min_score: 分数下限（默认 75 = `min_score_for_comment`）。
    """
    rows = db.query(
        "SELECT n.bvid, n.score_total, n.score_topic, n.score_novelty, "
        "n.score_evidence, v.title, v.source_url, v.up_name "
        "FROM notes n LEFT JOIN videos v ON v.bvid = n.bvid "
        "WHERE n.score_total >= ? "
        "ORDER BY n.score_total DESC LIMIT ?",
        (min_score, limit),
    )

    briefs: list[CandidateBrief] = []
    for row in rows:
        bvid = str(row["bvid"])
        brief = CandidateBrief(
            bvid=bvid,
            title=str(row["title"] or ""),
            score=int(row["score_total"]),
            source_url=str(
                row["source_url"] or f"https://www.bilibili.com/video/{bvid}"
            ),
            up_name=str(row["up_name"] or ""),
            dimensions={
                "topic": int(row["score_topic"] or 0),
                "novelty": int(row["score_novelty"] or 0),
                "evidence": int(row["score_evidence"] or 0),
            },
        )
        _fill_evidence(db, brief)
        briefs.append(brief)
    return briefs


def _fill_evidence(db: Any, brief: CandidateBrief) -> None:
    """从 `evidence` 表取简介/字幕/评论填进 brief（**失败降级，不抛异常**）。"""
    try:
        rows = db.query(
            "SELECT source_type, text FROM evidence "
            "WHERE bvid = ? AND availability = 'available' "
            "ORDER BY CASE source_type "
            "  WHEN 'description' THEN 0 "
            "  WHEN 'subtitle' THEN 1 "
            "  ELSE 2 END, start_sec",
            (brief.bvid,),
        )
    except Exception:  # noqa: BLE001
        return

    description_parts: list[str] = []
    subtitle_parts: list[str] = []
    comments: list[str] = []
    for row in rows:
        kind = str(row["source_type"])
        text = str(row["text"] or "").strip()
        if not text:
            continue
        if kind == "description":
            description_parts.append(text)
        elif kind == "subtitle":
            subtitle_parts.append(text)
        elif kind == "comment" and len(comments) < MAX_COMMENTS:
            comments.append(text)

    brief.description = "\n".join(description_parts)
    # 字幕按时间顺序拼起来（evidence 已按 start_sec 排序）
    brief.subtitle = "\n".join(subtitle_parts)
    brief.comments = comments


def render_bundle(briefs: list[CandidateBrief], *, intro: str = "") -> str:
    """把多个 brief 渲染成**一条**投递文本（给值班员会话）。"""
    if not briefs:
        return ""
    parts: list[str] = []
    if intro.strip():
        parts.append(intro.strip())
        parts.append("")
    parts.append(
        f"以下是 {len(briefs)} 个高分候选的**完整内容**（简介/字幕/热评已附上）。"
    )
    parts.append(
        "请你**真的读一遍**这些内容，然后用小鲸鱼的人设写推荐语发给主人。"
    )
    parts.append("不要只复述标题，要说清「为什么值得主人看」。")
    parts.append("")
    for index, brief in enumerate(briefs, start=1):
        parts.append(f"===== 候选 {index}/{len(briefs)} =====")
        parts.append(brief.to_prompt())
        parts.append("")
    return "\n".join(parts).rstrip()


def describe() -> dict[str, Any]:
    """给面板/CLI 看的一行说明。"""
    return {
        "max_candidates": MAX_CANDIDATES,
        "per_item_chars": {
            "description": MAX_DESCRIPTION_CHARS,
            "subtitle": MAX_SUBTITLE_CHARS,
            "comments": MAX_COMMENTS_CHARS,
        },
        "max_comments": MAX_COMMENTS,
        "note": "口径对齐参考项目 bilibili_learning_bot（3 个视频 / 各 2500 字 / 热评 8 条）",
    }
