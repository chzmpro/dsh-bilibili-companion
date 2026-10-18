"""把高分候选交给值班员——**短包 + 2500 规则完整内容**（2026-10-18）。

渲染逻辑已抽到 [`core/cycle_content.py`](../core/cycle_content.py)，
自动调度（R1）与这个手动脚本共用同一套内容，避免两份实现漂移。

用法：

    ./.venv/Scripts/python.exe -u scripts/deliver_to_duty.py            # 预览
    ./.venv/Scripts/python.exe -u scripts/deliver_to_duty.py --send     # 投递

主人口径：字幕/评论 ≤2500 字全文，>2500 字由豆包挑有用的压到 2500 以下。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from core.config import load_config  # noqa: E402
from core.cycle_content import (  # noqa: E402
    MAX_PER_DELIVERY,
    prepare_one,
    render_delivery,
)
from core.dm_deliver import deliver_async  # noqa: E402
from core.storage import Database  # noqa: E402


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

        text = render_delivery(
            rows, style_notes=list(db.owner_preferences_digest().get("note", []))
        )
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
