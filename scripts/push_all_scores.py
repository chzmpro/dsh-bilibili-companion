"""把评分结果**全部分批**推送给主人（B 站私信，2026-10-18）。

## 主人要求

> 「这次打分所挑选的候选必须全部发给我」

98 条候选，B 站私信单条上限 **500 字** ⇒ 必须分批。

## 设计

1. 从 `notes` 表读全部有分的视频，按分数降序；
2. 按 `--chunk` 条一组切片，每组拼一条私信（自动控制在 500 字内）；
3. 逐条发送，**每条之间等 `--delay` 秒**（默认 8 秒，别把私信接口打限流）；
4. `--top N` 只发前 N 条（默认全发）。

## 为什么不复用 dm_reply

`dm_reply` 是"回复某条私信"（需要 msg_seqno + 幂等）。
这里是**主动推送**，走 `send_to_owner`（和自动回复同一条链路）。

用法：

    ./.venv/Scripts/python.exe -u scripts/push_all_scores.py              # 预览
    ./.venv/Scripts/python.exe -u scripts/push_all_scores.py --send       # 真发
    ./.venv/Scripts/python.exe -u scripts/push_all_scores.py --send --top 30
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

#: 默认每组几条（会按字数二次收窄）。
DEFAULT_CHUNK = 8


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
        }
        for r in db.query(sql)
    ]


def build_chunks(rows: list[dict[str, Any]], *, chunk: int = DEFAULT_CHUNK) -> list[str]:
    """把结果切成若干条私信（每条严格 ≤ MAX_DM_CHARS）。"""
    total = len(rows)
    messages: list[str] = []
    index = 0
    page = 1
    while index < total:
        header = f"【全量评分 {page}】共 {total} 条，按分数排序："
        lines = [header, ""]
        added = 0
        while index < total and added < chunk:
            item = rows[index]
            line = f"{item['score']} {item['bvid']} {item['title'][:18]}"
            # 预估加上这条会不会超
            if len("\n".join([*lines, line])) > MAX_DM_CHARS - 2:
                break
            lines.append(line)
            index += 1
            added += 1
        if added == 0:
            # 单条就超长（标题特别长）⇒ 硬截断这一条，避免死循环
            item = rows[index]
            lines.append(f"{item['score']} {item['bvid']}")
            index += 1
        messages.append("\n".join(lines))
        page += 1
    return messages


async def main() -> int:
    parser = argparse.ArgumentParser(description="分批推送全部评分结果")
    parser.add_argument("--send", action="store_true", help="真的发（默认只预览）")
    parser.add_argument("--top", type=int, default=0, help="只发前 N 条（默认全发）")
    parser.add_argument("--chunk", type=int, default=DEFAULT_CHUNK, help="每条私信几条")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="私信间隔秒数")
    args = parser.parse_args()

    db = Database()
    db.migrate()
    try:
        rows = load_scores(db, top=args.top)
        if not rows:
            print("notes 表里还没有分数（先跑 rescore_all.py）")
            return 1
        messages = build_chunks(rows, chunk=args.chunk)
        print(f"共 {len(rows)} 条候选，切成 {len(messages)} 条私信")
        for number, text in enumerate(messages, start=1):
            print(f"\n--- 第 {number}/{len(messages)} 条（{len(text)} 字）---")
            print(text)

        if not args.send:
            print("\n（预览模式。要真发：加 --send）")
            return 0

        config = load_config()
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
            key = getattr(result, "msg_key", "")
            print(f"  [{number}/{len(messages)}] ✅ msg_key={key}")
            sent += 1
            if number < len(messages):
                time.sleep(args.delay)
        print(f"\n✅ 共发出 {sent} 条私信")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
