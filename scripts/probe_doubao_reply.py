"""端到端验证：豆包主动回复（2026-10-17）。

用法：
    ./.venv/Scripts/python.exe -u scripts/probe_doubao_reply.py           # 只看会回什么，不发送
    ./.venv/Scripts/python.exe -u scripts/probe_doubao_reply.py --send    # 真的发出去

★ 它验证的是完整链路：

    从 dm_inbox 取待回复 → 喂给豆包（带历史）→ 生成人设回复 → （可选）真的发送

这个脚本**不启动面板**，只跑一轮，适合验证。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config  # noqa: E402
from core.credentials import CredentialError, load_cookie  # noqa: E402
from core.dm import dm_reply_allowed  # noqa: E402
from core.dm_reply_gen import describe, generate_reply  # noqa: E402
from core.storage import Database  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description="验证豆包主动回复")
    parser.add_argument("--send", action="store_true", help="真的发出（默认只预览）")
    parser.add_argument("--limit", type=int, default=3, help="预览几条")
    args = parser.parse_args()

    config = load_config()
    allowed, reason = dm_reply_allowed(config)
    if not allowed:
        print(f"私信门禁拒绝：{reason}")
        return 1

    print(f"策略：{describe()['strategy']}（上限 {describe()['max_chars']} 字）")

    db = Database()
    db.migrate()
    try:
        rows = db.query(
            "SELECT msg_seqno, text, title, bvid FROM dm_inbox "
            "WHERE status = 'pending' ORDER BY received_at DESC LIMIT ?",
            (args.limit,),
        )
        if not rows:
            print("没有待回复的私信（队列是空的）")
            return 0

        print(f"待回复 {len(rows)} 条，取最新一条生成：\n")

        # 最新一条作为"要回的"，其余当历史
        latest = rows[0]
        history = [
            {"text": str(r["text"]), "from_owner": True, "title": str(r["title"])}
            for r in rows[1:]
        ]

        incoming = str(latest["text"])
        if latest["title"]:
            incoming = f"{incoming}（分享视频：{latest['title']}）"

        print(f"收到：{incoming[:80]}")
        print("生成中…")
        result = await generate_reply(
            incoming=incoming, history=history, config=config
        )

        if not result.ok:
            print(f"\n❌ 生成失败：{result.error_code} {result.message}")
            return 1

        print(f"\n✅ 豆包回复（{len(result.text)} 字）：")
        print("-" * 60)
        print(result.text)
        print("-" * 60)

        if not args.send:
            print(f"\n（预览模式。要真发：加 --send，会回复 seqno={latest['msg_seqno']}）")
            return 0

        from core.dm_live import reply_to

        try:
            bundle = load_cookie()
        except CredentialError as exc:
            print(f"\n❌ 没有凭据：{exc}")
            return 3

        ok, message = await reply_to(
            db=db,
            config=config,
            cookies=bundle,
            msg_seqno=int(latest["msg_seqno"]),
            text=result.text,
        )
        if ok:
            db.execute(
                "UPDATE dm_inbox SET status='answered' WHERE msg_seqno = ?",
                (int(latest["msg_seqno"]),),
            )
            print(f"\n✅ 已发送：{message}")
            return 0
        print(f"\n❌ 发送失败：{message}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
