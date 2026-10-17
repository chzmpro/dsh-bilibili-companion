"""把**提炼后的高分候选**交给值班员，让它用人设写推荐发私信（2026-10-18）。

## 主人要求

> 「但凡是主动发给我的都是上层模型查看过的」
> 「我推荐这种实操交给值班员，修改代码这些交给你」

## 链路

```
① 豆包读全文提炼（scripts/digest_candidates.py）
      ↓ core_value / highlights / peers / human_bits / caveats
② 本脚本：把提炼结果投递给**值班员会话**
      ↓
③ 值班员用人设写推荐 → 调 dm_push 发私信给主人
```

## 为什么投递的是「提炼结果」而不是原文

| | 原文 | 提炼 |
|---|---|---|
| 长度 | 3 个视频 × 7500 字 | 3 个视频 × ~600 字 |
| 值班员的负担 | 要自己读几万字 | **读提炼就能写** |
| 信息完整 | 有 | **有**（豆包读的是全文） |

★ 关键：提炼是"读完了再压缩"，不是"没读就截断"。
值班员拿到的每一条，背后都是豆包读过的完整字幕+评论。

## 人设

`docs/persona.md`（16832 字节）里定好的小鲸鱼人设，投递时会明确要求：
少女感、最多一个括号吐槽、**说清为什么值得看**、不许复述标题。

用法：

    ./.venv/Scripts/python.exe -u scripts/deliver_to_duty.py            # 预览
    ./.venv/Scripts/python.exe -u scripts/deliver_to_duty.py --send     # 投递
    ./.venv/Scripts/python.exe -u scripts/deliver_to_duty.py --send --limit 2
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.candidate_digest import Digest, load_digest  # noqa: E402
from core.dm_deliver import deliver_async  # noqa: E402
from core.storage import Database  # noqa: E402

#: 一次投递最多几个候选（对齐参考项目 dive_videos_default=3）。
MAX_PER_DELIVERY = 3

#: 投递时给值班员的角色说明（人设 + 任务）。
ROLE_LINES = [
    "你是 B 站小号「干饭不干事的小鲸鱼」（小名：小肥鱼 / 吃白饭的小肥鱼）。",
    "主人是「沐雨林心」。你在给他推荐刚看完的视频。",
    "",
    "语气要求：少女感、可爱、热心真诚；最多在括号里小小吐槽一句；像微信聊天，**不写公文**。",
    "",
    "写作硬要求：",
    "1. **说清「为什么值得主人看」**——讲价值/用途，**不要复述标题**。",
    "2. 严禁「该内容围绕…」「本视频介绍了…」这类公文腔。",
    "3. 每个视频写 2-3 句，100 字以内。",
    "4. 如果「需要注意」里有坑，用一句提醒主人（这比夸奖更有价值）。",
    "5. 拿不准就说「还没细看」，**绝不编造**。",
    "",
    "写完调用 `dm_push` 把推荐发私信给主人（一个视频一条私信也行，合并也行）。",
]


def pick_candidates(
    db: Database, *, limit: int = MAX_PER_DELIVERY, min_score: int = 75
) -> list[dict[str, Any]]:
    """挑出**已提炼**的高分候选（按分数降序）。"""
    rows = db.query(
        "SELECT n.bvid, n.score_total, v.title "
        "FROM notes n LEFT JOIN videos v ON v.bvid = n.bvid "
        "WHERE n.score_total >= ? "
        "  AND EXISTS (SELECT 1 FROM candidate_digests d WHERE d.bvid = n.bvid) "
        "ORDER BY n.score_total DESC LIMIT ?",
        (min_score, limit),
    )
    return [
        {
            "bvid": str(r["bvid"]),
            "score": int(r["score_total"]),
            "title": str(r["title"] or ""),
        }
        for r in rows
    ]


def render_delivery(items: list[dict[str, Any]], digests: list[Digest]) -> str:
    """拼成投递给值班员的文本。"""
    parts = list(ROLE_LINES)
    parts.append("")
    parts.append(
        f"下面 {len(items)} 个视频的内容**已经由另一个模型读完并提炼**"
        "（字幕全文 + 评论区，含同类项目与真实人类经验）。"
    )
    parts.append("你基于这些提炼写推荐就行，不用再自己去读视频。")
    parts.append("")

    for index, (item, digest) in enumerate(zip(items, digests, strict=False), start=1):
        parts.append(f"===== 候选 {index}/{len(items)} =====")
        parts.append(f"【{item['score']} 分】{item['title']}")
        parts.append(f"链接：https://www.bilibili.com/video/{item['bvid']}/")
        parts.append("")
        parts.append(digest.to_prompt())
        parts.append("")

    return "\n".join(parts)


async def main() -> int:
    parser = argparse.ArgumentParser(description="把提炼后的候选交给值班员")
    parser.add_argument("--send", action="store_true", help="真的投递（默认只预览）")
    parser.add_argument("--limit", type=int, default=MAX_PER_DELIVERY, help="投递几个")
    parser.add_argument("--min-score", type=int, default=75, help="分数下限")
    args = parser.parse_args()

    db = Database()
    db.migrate()
    try:
        items = pick_candidates(db, limit=args.limit, min_score=args.min_score)
        if not items:
            print("没有已提炼的高分候选。先跑 digest_candidates.py。")
            return 1

        digests: list[Digest] = []
        for item in items:
            digest = load_digest(db, item["bvid"])
            if digest is not None:
                digests.append(digest)
        if not digests:
            print("提炼结果读取失败。")
            return 1

        text = render_delivery(items, digests)
        print(f"投递 {len(digests)} 个候选，文本 {len(text)} 字")
        print("=" * 60)
        print(text[:1500])
        if len(text) > 1500:
            print(f"…（还有 {len(text) - 1500} 字）")

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
