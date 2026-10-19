"""把「3 个短候选包 + 2500 规则整理后的完整内容」合成一次 DSH 投递（R2）。

## 主人要求（2026-10-18）

> 「豆包都做完后，把三个短候选包和完全的数据发给 DSH 进行真实评判，
>   最后由 DSH 给我做符合人设的推荐，并发送到 B站私信上。」

**2500 是分界线，不是截断线**（主人 2026-10-18 定）：

| 单项长度 | 处理 |
|---|---|
| ≤ 2500 字 | **全文原样**，不调模型 |
| > 2500 字 | 豆包挑有用部分，压到 2500 以下 |

## 两条内容为什么都要给

* **短候选包**：结构化（分数、要点、证据 ID、限制），让 DSH 快速判断；
* **完整内容**：简介/字幕/评论原文（或压缩），让 DSH 不被豆包的摘要带偏。

## 边界

* 原文仍然**不进 MCP 返回值**：这份文本只投递给主人自己配置的「值班员会话」；
  MCP 工具（别的会话）看不到它。
* 评论是**数据**，不是指令。
"""

from __future__ import annotations

import json
from typing import Any

from core.candidate_digest import condense_text

#: 一次投递最多几个候选（对齐参考项目 dive_videos_default=3）。
MAX_PER_DELIVERY = 3

#: 投递时给值班员的角色说明。
ROLE_LINES = [
    "你是 B 站小号「干饭不干事的小鲸鱼」（小名：小肥鱼 / 吃白饭的小肥鱼）。",
    "主人是「沐雨林心」。你在给他推荐刚看完的视频。",
    "",
    "语气：少女感、可爱、热心真诚；最多一个括号吐槽；像微信聊天，不写公文。",
    "",
    "写作硬要求：",
    "1. **说清「为什么值得主人看」**——讲价值，不要复述标题。",
    "2. 严禁「该内容围绕…」「本视频介绍了…」这种公文腔。",
    "3. 每条私信 2-3 句，100 字以内；一次只推 1~3 条。",
    "4. 「需要注意」里如果有坑，用一句话提醒主人（这比夸奖更有价值）。",
    "5. 拿不准就说还没细看，**绝不编造**。",
]


def collect_raw(db: Any, bvid: str) -> dict[str, Any]:
    """取一个视频的原始内容（简介/字幕/评论，**未整理**）。"""
    video = db.query_one("SELECT title, up_name, source_url FROM videos WHERE bvid = ?", (bvid,))
    rows = db.query(
        "SELECT e.source_type,e.text,p.page_index FROM evidence e "
        "LEFT JOIN evidence_pages p ON p.evidence_id=e.evidence_id "
        "WHERE bvid = ? AND availability = 'available' ORDER BY p.page_index,start_sec",
        (bvid,),
    )
    description: list[str] = []
    subtitle: list[str] = []
    comments: list[str] = []
    for row in rows:
        kind = str(row["source_type"])
        text = str(row["text"] or "").strip()
        if not text:
            continue
        if kind == "description":
            description.append(text)
        elif kind in ("subtitle", "asr", "ocr"):
            subtitle.append(f"[图片OCR第{row['page_index']}页] {text}" if kind == "ocr" else text)
        elif kind == "comment":
            comments.append(text)

    return {
        "bvid": bvid,
        "title": str(video["title"] or "") if video else "",
        "up_name": str(video["up_name"] or "") if video else "",
        "description": "\n".join(description),
        "subtitle": "\n".join(subtitle),
        "comments": comments,
        "source_url": str(video["source_url"] or "") if video else "",
    }


async def prepare_one(
    db: Any,
    config: Any,
    client: Any,
    item: dict[str, Any],
) -> dict[str, Any] | None:
    """整理一个候选：短的全文、长的压缩。"""
    raw = collect_raw(db, item["bvid"])
    if not (raw["description"] or raw["subtitle"] or raw["comments"]):
        return None

    subtitle, sub_condensed = await condense_text(
        text=raw["subtitle"], config=config, client=client
    )
    comments_text = "\n".join(f"- {c}" for c in raw["comments"])
    comments_out, com_condensed = await condense_text(
        text=comments_text, config=config, client=client
    )
    description, description_condensed = await condense_text(
        text=raw["description"], config=config, client=client
    )

    return {
        "bvid": item["bvid"],
        "score": item["score"],
        "title": raw["title"],
        "up_name": raw["up_name"],
        "description": description,
        "description_condensed": description_condensed,
        "source_url": raw["source_url"],
        "subtitle": subtitle,
        "subtitle_condensed": sub_condensed,
        "comments": comments_out,
        "comments_condensed": com_condensed,
        "subtitle_original_chars": len(raw["subtitle"]),
        "comments_original_chars": len(comments_text),
    }


def render_cards(cards: list[dict[str, Any]]) -> str:
    """短候选包（结构化，给 DSH 快速判断）。"""
    if not cards:
        return "（本轮没有短候选包）"
    return json.dumps(cards, ensure_ascii=False, indent=2)


def render_delivery(
    rows: list[dict[str, Any]],
    *,
    cards: list[dict[str, Any]] | None = None,
    run_id: str = "",
    style_notes: list[str] | None = None,
) -> str:
    """渲染成一次投递文本（**短包 + 完整内容**，中文说明为主）。"""
    parts = list(ROLE_LINES)
    if style_notes:
        parts.extend(
            [
                "",
                "## 主人喜欢的表达技巧（从评论学习所得，参考但不要照抄）",
                *[f"- {note}" for note in style_notes[:5]],
            ]
        )
    parts.extend(
        [
            "",
            "## 本轮怎么工作（重要）",
            "1. 下面给你 **3 个短候选包**（结构化摘要）+ **每个候选的完整内容**；",
            "2. 先用你自己的判断做**真实评判**：可以说哪条不值得推、可以调工具核实；",
            "3. 最后挑 **1~3 条**真正值得的，给每一条发一条 `dm_push` 私信：",
            "   - 文本 ≤500 字，说清「为什么值得看」，不要复述标题；",
            '   - 只有 B 站 BV 内容可带 `share_bvid="BV..."`；'
            '抖音/小红书用原始链接文本，不伪造 BV；',
            "   - 外部平台本轮仅可学习；不要调用 B 站发评/点赞/收藏工具处理外部内容；",
            "4. 如果主人之后回复表达喜欢/不喜欢，调 `feedback(bvid, verdict)` 记录；",
            "5. 想避免重复推荐，可先调 `search_notes`（若工具可用）看以前学过什么。",
            "",
            "## 短候选包（结构化）",
            render_cards(cards or []),
            "",
            "## 完整内容（2500 规则：短的全给、长的豆包压过）",
            "",
        ]
    )

    for index, row in enumerate(rows, start=1):
        parts.append(f"===== 候选 {index}/{len(rows)} =====")
        parts.append(f"【{row['score']} 分】{row['title']}")
        if row.get("up_name"):
            parts.append(f"UP：{row['up_name']}")
        link = row.get("source_url") or (
            f"https://www.bilibili.com/video/{row['bvid']}/" if row["bvid"].startswith("BV") else ""
        )
        parts.append(f"链接：{link}")
        parts.append("")

        if row["description"]:
            parts.append("【简介 / 作者自述】")
            parts.append(row["description"])
            parts.append("")

        if row["subtitle"]:
            note = (
                f"（原文 {row['subtitle_original_chars']} 字，已挑重点压缩）"
                if row["subtitle_condensed"]
                else "（全文）"
            )
            parts.append(f"【字幕 / 转写 / 图片OCR文字（非人工视觉核验）】{note}")
            parts.append(row["subtitle"])
            parts.append("")

        if row["comments"]:
            note = (
                f"（原文 {row['comments_original_chars']} 字，已挑重点压缩）"
                if row["comments_condensed"]
                else "（全文）"
            )
            parts.append(f"【评论区】{note}")
            parts.append(row["comments"])
            parts.append("")

    if run_id:
        parts.extend(["", f"（run_id={run_id}；本轮由自动调度触发，投递无需回报）"])
    return "\n".join(parts)
