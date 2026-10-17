"""只读拉取小号与主人之间的私信（**不发送任何内容**）。

用途：核实"主人给小号发过私信"这件事，并把内容读给 DSH 决策。

* 只读：只调用 GET，不写库、不改配置、不发消息。
* 凭据：复用本项目数据目录里的 cookie，**任何情况下都不打印凭据**。
* 请求：经 ``core.ratelimit.RequestQueue``，与其他读取共用限速与健康门禁。

接口来源：bilibili-API-collect ``docs/message/private_msg.md``
（``fetch_session_msgs`` / ``single_unread``）。

用法::

    ./.venv/Scripts/python.exe scripts/read_owner_dm.py --size 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from core.config import load_config
from core.credentials import CredentialError, load_cookie
from core.ratelimit import RequestQueue
from core.storage import Database

DM_HOST = "https://api.vc.bilibili.com"
UNREAD_PATH = "/session_svr/v1/session_svr/single_unread"
MSGS_PATH = "/svr_sync/v1/svr_sync/fetch_session_msgs"

CST = timezone(timedelta(hours=8))

MSG_TYPE_LABEL: dict[int, str] = {
    1: "文字",
    2: "图片",
    5: "撤回",
    12: "通知",
    13: "通知",
}


def _render_content(raw: Any, *, raw_out: bool = False) -> str:
    """私信 ``content`` 是**嵌套 JSON 字符串**，这里解出可读文字。"""
    if raw_out:
        return raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    obj: Any = raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return raw
    if isinstance(obj, dict):
        for key in ("content", "title", "text"):
            val = obj.get(key)
            if isinstance(val, str) and val:
                return val
        return json.dumps(obj, ensure_ascii=False)
    return str(obj)


def _who(sender_uid: int, agent_mid: int, owner_mid: int) -> str:
    if sender_uid == agent_mid:
        return "小号"
    if sender_uid == owner_mid:
        return "主人"
    return f"uid={sender_uid}"


async def _run(size: int, *, raw_out: bool = False) -> int:
    config = load_config()
    agent_mid = config.account.agent_mid
    owner_mid = config.account.owner_mid
    if owner_mid <= 0:
        print("owner_mid 未配置，无法定位会话")
        return 2

    try:
        cookies = load_cookie().values
    except CredentialError as exc:
        print(f"没有可用凭据：{exc}")
        return 3

    db = Database()
    db.migrate()
    queue = RequestQueue(db=db, config=config)
    try:
        unread = await queue.get_json(
            DM_HOST + UNREAD_PATH, use_cache=False, cookies=cookies
        )
        print("== 未读私信 ==")
        print(
            json.dumps(unread.data, ensure_ascii=False)
            if unread.data is not None
            else f"读不到（code={unread.code} status={unread.status} {unread.message}）"
        )
        print()

        outcome = await queue.get_json(
            DM_HOST + MSGS_PATH,
            params={
                "sender_device_id": 1,
                "talker_id": owner_mid,
                "session_type": 1,
                "size": size,
                "build": 0,
                "mobi_app": "web",
            },
            use_cache=False,
            cookies=cookies,
        )
        payload = outcome.data if isinstance(outcome.data, dict) else {}
        messages = payload.get("messages") or []
        if not messages:
            print(
                f"== 与主人（mid={owner_mid}）的私信 ==\n"
                f"（没有读到消息；code={outcome.code} status={outcome.status} "
                f"{outcome.message}）"
            )
            return 0

        print(f"== 与主人（mid={owner_mid}）的私信：最近 {len(messages)} 条 ==")
        for msg in sorted(messages, key=lambda m: int(m.get("msg_seqno", 0))):
            sender = int(msg.get("sender_uid", 0))
            ts = int(msg.get("timestamp", 0))
            when = datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d %H:%M:%S") if ts else "-"
            mtype = MSG_TYPE_LABEL.get(int(msg.get("msg_type", 0)), f"type={msg.get('msg_type')}")
            text = _render_content(msg.get("content"), raw_out=raw_out)
            print(f"[{when}] {_who(sender, agent_mid, owner_mid)}（{mtype}）：{text}")
        return 0
    finally:
        await queue.aclose()
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="只读拉取小号与主人的私信")
    parser.add_argument("--size", type=int, default=20, help="拉取条数（默认 20）")
    parser.add_argument(
        "--raw", action="store_true", help="打印原始 content JSON（含分享卡片链接）"
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.size, raw_out=args.raw))


if __name__ == "__main__":
    raise SystemExit(main())
