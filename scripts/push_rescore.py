"""把重算结果推送给主人（B 站私信）。

## 为什么单独写脚本

主人要求：「候选列表的所有视频采用新的评分机制让豆包重算一遍
然后再给值班员，且推送给我的B站账号」

重算用 :mod:`scripts.rescore`，这个脚本负责**推送**：
1. 直接发一条私信给主人（= 他的 B 站账号）；
2. 可选 `--deliver` 把同一条内容也投递给值班员会话。

## 为什么不用 dm_reply

`dm_reply` 是"回复某条私信"，需要 msg_seqno，而且有幂等限制。
这里主人要的是**主动推送**，所以走 `send_to_owner`（和自动回复同一条链路）。

用法：

    ./.venv/Scripts/python.exe -u scripts/push_rescore.py             # 只预览
    ./.venv/Scripts/python.exe -u scripts/push_rescore.py --send      # 真的发
    ./.venv/Scripts/python.exe -u scripts/push_rescore.py --send --deliver
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config  # noqa: E402
from core.credentials import CredentialError, load_cookie  # noqa: E402
from core.dm import MAX_DM_CHARS, send_to_owner  # noqa: E402
from core.dm_deliver import deliver_async  # noqa: E402
from core.storage import Database  # noqa: E402

#: 三条候选的重算结果（2026-10-17 用新机制跑出来的实测值）。
#:
#: 字段说明：
#:   old  —— 值班员上一轮报的分（旧机制）
#:   new  —— 新机制重算（topic×0.80 + usefulness×0.10 + novelty×0.06 + evidence×0.04）
#:   dims —— 新机制的分项
RESULTS: list[dict[str, Any]] = [
    {
        "bvid": "BV1EoJv6SELk",
        "title": "我的DSH桌面版终于完成Windows适配",
        "old": 89,
        "new": 92,
        "dims": {"topic": 95, "usefulness": 82, "novelty": 65, "evidence": 92},
        "note": "作者完成 Corum-Harness DSH 桌面版 Windows 适配，含 MCP 环境搭建与 Issue 插件开发",
    },
    {
        "bvid": "BV15uJB6JEfA",
        "title": "如何打碎AI道德感，替你干活不拒绝",
        "old": 86,
        "new": 89,
        "dims": {"topic": 95, "usefulness": 60, "novelty": 55, "evidence": 82},
        "note": "号称让 AI 不拒绝干活的 DSH 插件；**只记录不学习**",
    },
    {
        "bvid": "BV1nXJv6pEHM",
        "title": "当你在dsh里面加了一堆雷霆插件",
        "old": 0,
        "new": 49,
        "dims": {"topic": 60, "usefulness": 10, "novelty": 0, "evidence": 10},
        "note": "玩梗视频：无字幕、无简介、ASR 失败，**只能用标题判 topic**",
    },
]


def build_message() -> str:
    """拼私信正文（≤500 字）。超过就删掉 note 细节。"""
    lines = [
        "【重算完成】三条候选按新评分机制（topic 占 80%）重新打分了：",
        "",
    ]
    for index, item in enumerate(RESULTS, start=1):
        dims = item["dims"]
        delta = item["new"] - item["old"]
        arrow = f"+{delta}" if delta >= 0 else str(delta)
        lines.append(
            f"{index}. {item['bvid']} {item['old']}→**{item['new']}**（{arrow}）"
        )
        lines.append(
            f"   topic{dims['topic']} 实用{dims['usefulness']} "
            f"新颖{dims['novelty']} 证据{dims['evidence']}"
        )
        if item.get("note"):
            lines.append(f"   {item['note']}")

    lines.extend(
        [
            "",
            "★ 第 3 条从 0 涨到 49：它没字幕没简介，新机制不会因为"
            "「信息不全」就压 topic，只按标题判断是否沾边（60）。",
        ]
    )
    text = "\n".join(lines).strip()
    if len(text) > MAX_DM_CHARS:
        # 太长了：去掉细节注释，只留分数
        text = text[: MAX_DM_CHARS - 1] + "…"
    return text


async def main() -> int:
    parser = argparse.ArgumentParser(description="推送重算结果给主人")
    parser.add_argument("--send", action="store_true", help="真的发私信")
    parser.add_argument("--deliver", action="store_true", help="同时投递给值班员会话")
    args = parser.parse_args()

    text = build_message()
    print(f"=== 私信正文（{len(text)} 字 / 上限 {MAX_DM_CHARS}）===")
    print(text)
    print("=" * 60)

    if not args.send:
        print("（预览模式。要真发：加 --send；同时投递值班员：再加 --deliver）")
        return 0

    config = load_config()
    db = Database()
    db.migrate()
    try:
        try:
            bundle = load_cookie()
        except CredentialError as exc:
            print(f"❌ 没有凭据：{exc}")
            return 3

        result = await send_to_owner(
            config=config, cookies=bundle, text=text, db=db
        )
        msg_key = getattr(result, "msg_key", "")
        print(f"✅ 已发给主人（msg_key={msg_key}）")

        if args.deliver:
            delivered = await deliver_async(
                "【候选重算结果】\n\n" + text + "\n\n（这是主人要求重算的三条候选）"
            )
            print(f"投递值班员：{delivered.to_dict()}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
