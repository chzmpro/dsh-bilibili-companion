"""把高分候选交给值班员——**短的全文、长的压缩**（2026-10-18）。

## 主人定的口径（重要）

> 「字幕、评论 2500 以下全文发送，2500 以上豆包拣选有用的截断形成 2500 以下发送」

**2500 是分界线，不是截断线**：

| 单项长度 | 处理 |
|---|---|
| **≤ 2500 字** | **全文原样**发送，**不调模型**、一个字节都不动 |
| **> 2500 字** | 豆包挑出有用部分（做法/踩坑/数字/同类项目），压到 2500 以下 |

之前我**一律提炼**，是错的：短内容本来就看得完，提炼只会白花调用还可能丢细节。

## 链路

```
豆包初筛打分 → 本脚本按 2500 规则整理内容 → 投递给值班员
                                                  ↓
                                    值班员用人设写推荐 → dm_push 发私信
```

## 展示格式（主人要求）

**中文解释为主，英文代码作为小字放在下面** —— 否则一堆英文看不懂是干啥的。

所以投递文本里每个候选都按这个结构：

```
【96 分】<中文标题>
  为什么值得看：（中文一句话）
  字幕要点：（中文）
  注意事项：（中文）
  --- 以下为原始信息 ---
  <英文/代码/参数原文，作为小字补充>
```

用法：

    ./.venv/Scripts/python.exe -u scripts/deliver_to_duty.py            # 预览
    ./.venv/Scripts/python.exe -u scripts/deliver_to_duty.py --send     # 投递
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from core.candidate_digest import condense_text  # noqa: E402
from core.config import load_config  # noqa: E402
from core.dm_deliver import deliver_async  # noqa: E402
from core.storage import Database  # noqa: E402

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
    "3. 每个视频 2-3 句，100 字以内。",
    "4. 「需要注意」里如果有坑，用一句话提醒主人（这比夸奖更有价值）。",
    "5. 拿不准就说还没细看，**绝不编造**。",
    "",
    "写完调 `dm_push` 把推荐发私信给主人。",
]


def collect_raw(db: Database, bvid: str) -> dict[str, Any]:
    """取一个视频的原始内容（简介/字幕/评论，**未整理**）。"""
    video = db.query_one(
        "SELECT title, up_name, source_url FROM videos WHERE bvid = ?", (bvid,)
    )
    rows = db.query(
        "SELECT source_type, text FROM evidence "
        "WHERE bvid = ? AND availability = 'available' ORDER BY start_sec",
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
        elif kind in ("subtitle", "asr"):
            subtitle.append(text)
        elif kind == "comment":
            comments.append(text)

    return {
        "bvid": bvid,
        "title": str(video["title"] or "") if video else "",
        "up_name": str(video["up_name"] or "") if video else "",
        "description": "\n".join(description),
        "subtitle": "\n".join(subtitle),
        "comments": comments,
    }


async def prepare_one(
    db: Database,
    config: Any,
    client: httpx.AsyncClient,
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

    return {
        "bvid": item["bvid"],
        "score": item["score"],
        "title": raw["title"],
        "up_name": raw["up_name"],
        "description": raw["description"],
        "subtitle": subtitle,
        "subtitle_condensed": sub_condensed,
        "comments": comments_out,
        "comments_condensed": com_condensed,
        "subtitle_original_chars": len(raw["subtitle"]),
        "comments_original_chars": len(comments_text),
    }


def render_delivery(rows: list[dict[str, Any]]) -> str:
    """渲染成投递文本（**中文解释为主，原文作为补充**）。"""
    parts = list(ROLE_LINES)
    parts.extend(
        [
            "",
            f"下面 {len(rows)} 个视频，内容已按「短的全给、长的挑重点」处理好：",
            "· 原始内容不长的，**全文给你**；",
            "· 原始内容太长的，已挑出有用部分（做法/踩坑/数字/同类项目）。",
            "",
        ]
    )

    for index, row in enumerate(rows, start=1):
        parts.append(f"===== 候选 {index}/{len(rows)} =====")
        parts.append(f"【{row['score']} 分】{row['title']}")
        if row["up_name"]:
            parts.append(f"UP：{row['up_name']}")
        parts.append(f"链接：https://www.bilibili.com/video/{row['bvid']}/")
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
            parts.append(f"【视频字幕】{note}")
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

    return "\n".join(parts)


async def main() -> int:
    parser = argparse.ArgumentParser(description="把高分候选交给值班员")
    parser.add_argument("--send", action="store_true", help="真的投递（默认预览）")
    parser.add_argument("--limit", type=int, default=MAX_PER_DELIVERY)
    parser.add_argument("--min-score", type=int, default=75)
    args = parser.parse_args()

    config = load_config()
    db = Database()
    db.migrate()
    try:
        items = [
            {"bvid": str(r["bvid"]), "score": int(r["score_total"])}
            for r in db.query(
                "SELECT bvid, score_total FROM notes WHERE score_total >= ? "
                "ORDER BY score_total DESC LIMIT ?",
                (args.min_score, args.limit),
            )
        ]
        if not items:
            print("没有高分候选。")
            return 1

        rows: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            for item in items:
                prepared = await prepare_one(db, config, client, item)
                if prepared is not None:
                    rows.append(prepared)
                    print(
                        f"  {item['score']}分 {item['bvid']}: "
                        f"字幕{prepared['subtitle_original_chars']}字"
                        f"{'（压缩）' if prepared['subtitle_condensed'] else '（全文）'} "
                        f"评论{prepared['comments_original_chars']}字"
                        f"{'（压缩）' if prepared['comments_condensed'] else '（全文）'}"
                    )

        if not rows:
            print("没有可用内容。")
            return 1

        text = render_delivery(rows)
        print(f"\n投递文本 {len(text)} 字")
        print("=" * 60)
        print(text[:1200])
        if len(text) > 1200:
            print(f"…（还有 {len(text) - 1200} 字）")

        if not args.send:
            print("\n（预览模式。要真投递：加 --send）")
            return 0

        result = await deliver_async(text)
        print(f"\n投递结果：{result.to_dict()}")
        return 0 if result.ok else 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
