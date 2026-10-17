"""把评分结果按**三档格式**分批推送给主人（B 站私信，2026-10-18）。

## 主人要求

> 「合格的结果按照正常方式进行推荐，其他的发送标题加分数即可，
>   最后再发个分数分布」

## 三档

| 档 | 条件 | 格式 |
|---|---|---|
| **合格推荐** | `score >= min_score_for_comment`（默认 75） | 标题 + 分数 + 分维度 + 链接 |
| **其余** | `< 门槛` | 只有「分数 + BV + 标题」，一行一条 |
| **分数分布** | 最后单独一条 | 分段计数 + 条形图 |

## 为什么分三档

98 条里大部分是低分（跟关注点无关的）。全部按推荐格式发，
既超字数上限，也会把真正值得看的淹没在噪音里。

## 用法

    ./.venv/Scripts/python.exe -u scripts/push_all_scores.py              # 预览
    ./.venv/Scripts/python.exe -u scripts/push_all_scores.py --send       # 真发
    ./.venv/Scripts/python.exe -u scripts/push_all_scores.py --send --threshold 80
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config  # noqa: E402
from core.credentials import CredentialError, load_cookie  # noqa: E402
from core.dm import MAX_DM_CHARS, send_to_owner  # noqa: E402
from core.storage import Database  # noqa: E402

#: 每条私信之间等多久（秒）。太密会被私信接口限流。
DEFAULT_DELAY = 8.0

#: 分数分段的边界（用于最后的分布统计）。
BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("90-100", 90, 100),
    ("75-89", 75, 89),
    ("50-74", 50, 74),
    ("30-49", 30, 49),
    ("0-29", 0, 29),
)


def load_scores(db: Database, *, top: int = 0) -> list[dict[str, Any]]:
    """从 notes 表读全部有分的视频（按分数降序）。"""
    sql = (
        "SELECT n.bvid, n.score_total, n.score_topic, n.score_novelty, "
        "n.score_evidence, v.title, v.source_url "
        "FROM notes n LEFT JOIN videos v ON v.bvid = n.bvid "
        "WHERE n.score_total IS NOT NULL "
        "ORDER BY n.score_total DESC, n.bvid"
    )
    if top > 0:
        sql += f" LIMIT {int(top)}"
    return [
        {
            "bvid": str(r["bvid"]),
            "score": int(r["score_total"]),
            "topic": int(r["score_topic"] or 0),
            "novelty": int(r["score_novelty"] or 0),
            "evidence": int(r["score_evidence"] or 0),
            "title": str(r["title"] or ""),
            "source_url": str(
                r["source_url"] or f"https://www.bilibili.com/video/{r['bvid']}"
            ),
        }
        for r in db.query(sql)
    ]


def _recommend_block(item: dict[str, Any], number: int) -> str:
    """一条「合格推荐」的完整格式。"""
    return "\n".join(
        [
            f"{number}. {item['score']}分 {item['bvid']}",
            f"   {item['title'][:60]}",
            f"   topic{item['topic']} 新颖{item['novelty']} 证据{item['evidence']}",
            f"   {item['source_url']}",
        ]
    )


def _plain_line(item: dict[str, Any]) -> str:
    """一条「其余」的精简格式：分数 + BV + 标题。"""
    return f"{item['score']} {item['bvid']} {item['title'][:22]}"


def build_messages(rows: list[dict[str, Any]], *, threshold: int = 75) -> list[str]:
    """切成若干条私信（每条 ≤ MAX_DM_CHARS）。"""
    qualified = [r for r in rows if r["score"] >= threshold]
    others = [r for r in rows if r["score"] < threshold]

    messages: list[str] = []

    # ---- 第 1 档：合格推荐（详细格式，每条私信最多 4 个）----
    page = 1
    index = 0
    while index < len(qualified):
        header = (
            f"【合格推荐 {page}】≥{threshold} 分，共 {len(qualified)} 条"
            if page == 1
            else f"【合格推荐 {page}（续）】"
        )
        lines = [header, ""]
        added = 0
        while index < len(qualified) and added < 4:
            block = _recommend_block(qualified[index], index + 1)
            if len("\n".join([*lines, block])) > MAX_DM_CHARS - 2:
                break
            lines.append(block)
            lines.append("")
            index += 1
            added += 1
        if added == 0:
            index += 1
            continue
        messages.append("\n".join(lines).rstrip())
        page += 1

    # ---- 第 2 档：其余候选（只有标题+分数）----
    if others:
        page = 1
        index = 0
        while index < len(others):
            header = (
                f"【其余 {len(others)} 条】<{threshold} 分，仅记录："
                if page == 1
                else f"【其余（续 {page}）】"
            )
            lines = [header, ""]
            while index < len(others):
                line = _plain_line(others[index])
                if len("\n".join([*lines, line])) > MAX_DM_CHARS - 2:
                    break
                lines.append(line)
                index += 1
            if len(lines) == 2:  # 一条都没塞进去（标题超长）⇒ 硬加
                lines.append(_plain_line(others[index]))
                index += 1
            messages.append("\n".join(lines))
            page += 1

    # ---- 第 3 档：分数分布 ----
    total = len(rows)
    lines = [f"【分数分布】共 {total} 条：", ""]
    for label, low, high in BUCKETS:
        count = sum(1 for r in rows if low <= r["score"] <= high)
        bar = "█" * min(count, 30)
        lines.append(f"{label:>7}: {count:>3} 条 {bar}")
    lines.append("")
    lines.append(f"合格（≥{threshold}）{len(qualified)} 条 · 其余 {len(others)} 条")
    messages.append("\n".join(lines))

    return messages


async def main() -> int:
    parser = argparse.ArgumentParser(description="三档分批推送评分结果")
    parser.add_argument("--send", action="store_true", help="真的发（默认只预览）")
    parser.add_argument("--top", type=int, default=0, help="只发前 N 条（默认全发）")
    parser.add_argument(
        "--threshold",
        type=int,
        default=0,
        help="合格门槛（默认取 config 的 min_score_for_comment，通常是 75）",
    )
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="私信间隔秒数")
    args = parser.parse_args()

    db = Database()
    db.migrate()
    try:
        config = load_config()
        threshold = args.threshold or int(config.discovery.min_score_for_comment)

        rows = load_scores(db, top=args.top)
        if not rows:
            print("notes 表里还没有分数（先跑 rescore_all.py）")
            return 1

        messages = build_messages(rows, threshold=threshold)
        qualified = [r for r in rows if r["score"] >= threshold]
        print(
            f"共 {len(rows)} 条 | 合格(≥{threshold}) {len(qualified)} 条 "
            f"| 其余 {len(rows) - len(qualified)} 条 | 切成 {len(messages)} 条私信"
        )
        for number, text in enumerate(messages, start=1):
            print(f"\n--- 第 {number}/{len(messages)} 条（{len(text)} 字）---")
            print(text)

        if not args.send:
            print("\n（预览模式。要真发：加 --send）")
            return 0

        try:
            bundle = load_cookie()
        except CredentialError as exc:
            print(f"❌ 没有凭据：{exc}")
            return 3

        sent = 0
        for number, text in enumerate(messages, start=1):
            result = await send_to_owner(
                config=config, cookies=bundle, text=text, db=db
            )
            print(
                f"  [{number}/{len(messages)}] ✅ "
                f"msg_key={getattr(result, 'msg_key', '')}"
            )
            sent += 1
            if number < len(messages):
                time.sleep(args.delay)
        print(f"\n✅ 共发出 {sent} 条私信")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
