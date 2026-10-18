"""私信实时回复（2026-10-17，主人要求）。

## 主人原话

> 「如果我想在B站私信你并且你能随时回复，这个功能可以实现吗？
>  我觉得应该是很好实现的，毕竟你也不确定别人什么时候评论你，你再回复，
>  我私信你应该效果一样」

**对，效果一样，而且更简单** —— 评论要轮询全站评论串，
而私信只要查一个数字：`follow_unread`（未读数）。

## 设计（三条约束）

1. **只查未读数，不轮询整个会话** —— `single_unread` 返回一个数字，
   成本极低；只有**数字变化**时才去拉消息正文。
2. **只回主人** —— 接收者硬编码 `account.owner_mid`，代码层无法发给别人。
3. **走 `dm_reply_enabled` 独立开关** —— 与公开发布的 `publish_enabled` 无关
   （回复主人私信不是公开动作）。

## 为什么用"未读数变化"而不是"时间轮询"

主人说「B站私信不是会有消息弹窗吗，只要持续监测监测就行了」——
`follow_unread` 就是那个弹窗背后的数字。监测它等于监测弹窗，
而且**不需要读消息正文**（省请求、少碰数据）。

## 幂等

已回复过的 `msg_seqno` 记在 `dm_replies` 表（迁移 v8），
**同一个 seqno 绝不回第二次** —— 否则轮询会变成刷屏。
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from typing import Any

from core.dm import (
    MAX_DM_CHARS,
    DmMessage,
    fetch_messages,
    fetch_unread,
    send_to_owner,
)
from core.storage import Database


@dataclass(frozen=True)
class PollOutcome:
    """一次轮询的结果。"""

    unread: int = 0
    #: 这次发现的新消息（**只含主人发来的**）
    new_messages: list[dict[str, Any]] = None  # type: ignore[assignment]
    #: 已经回复过的 seqno（跳过的）
    skipped: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "unread": self.unread,
            "new_messages": self.new_messages or [],
            "skipped": self.skipped,
            "note": self.note,
        }


def unread_count(payload: Any) -> int:
    """从未读数响应里取"主人发来的未读数"。

    ★ 只认 ``follow_unread``（**我关注的人**发来的）——
    主人是小号唯一关注的人，所以这个数字就是"主人给我发了几条"。

    ``unfollow_unread`` 是"陌生人发来的"，**不算** ——
    我们不做陌生人私信回复（那是另一套风控，且不在本项目范围内）。
    """
    if not isinstance(payload, dict):
        return 0
    try:
        return max(0, int(payload.get("follow_unread", 0) or 0))
    except (TypeError, ValueError):
        return 0


def pick_owner_messages(
    messages: list[DmMessage], *, owner_mid: int
) -> list[DmMessage]:
    """筛出**主人发来的**消息（按 seqno 升序）。

    判据是 ``sender_uid == owner_mid`` —— 不是"最新一条"，
    因为拉回来的最后一条可能是我们自己发的回复。
    """
    return [m for m in messages if int(m.sender_uid) == int(owner_mid)]


def unseen_messages(
    db: Database, messages: list[DmMessage], *, owner_mid: int
) -> list[DmMessage]:
    """去掉**已经回复过**的（按 seqno 查 ``dm_replies``）。"""
    out: list[DmMessage] = []
    for message in pick_owner_messages(messages, owner_mid=owner_mid):
        already = db.query_one(
            "SELECT msg_seqno FROM dm_replies WHERE msg_seqno = ?",
            (int(message.msg_seqno),),
        )
        if already is None:
            out.append(message)
    return out


def record_reply(
    db: Database, *, msg_seqno: int, reply_text: str, remote_msg_key: str = ""
) -> None:
    """记一条已回复（**幂等的关键**）。"""
    import time

    db.execute(
        "INSERT OR REPLACE INTO dm_replies "
        "(msg_seqno, reply_text, remote_msg_key, replied_at) VALUES (?,?,?,?)",
        (int(msg_seqno), str(reply_text)[:MAX_DM_CHARS], str(remote_msg_key), int(time.time())),
    )


def replied_seqnos(db: Database, *, limit: int = 200) -> set[int]:
    """最近回复过的 seqno 集合（调试/测试用）。"""
    rows = db.query(
        "SELECT msg_seqno FROM dm_replies ORDER BY replied_at DESC LIMIT ?", (limit,)
    )
    return {int(row["msg_seqno"]) for row in rows}


async def poll_once(
    *,
    db: Database,
    config: Any,
    queue: Any,
    cookies: dict[str, str],
) -> PollOutcome:
    """**查一次**未读，返回发现的新消息（**不发送**）。

    刻意把"查"与"回"分开：
    发送是不可逆的公开动作，应该由调用方（人用 CLI / agent）明确决定，
    而不是藏在轮询函数里。
    """
    owner_mid = int(config.account.owner_mid)
    if owner_mid <= 0:
        return PollOutcome(note="owner_mid 未配置，无法判断谁是主人")

    payload = await fetch_unread(queue=queue, cookies=cookies)
    unread = unread_count(payload)
    if unread <= 0:
        return PollOutcome(unread=0, new_messages=[], note="没有未读")

    messages = await fetch_messages(
        queue=queue, cookies=cookies, talker_id=owner_mid, size=20
    )
    fresh = unseen_messages(db, messages, owner_mid=owner_mid)
    skipped = len(pick_owner_messages(messages, owner_mid=owner_mid)) - len(fresh)
    return PollOutcome(
        unread=unread,
        new_messages=[
            {
                "msg_seqno": m.msg_seqno,
                "text": m.text[:MAX_DM_CHARS],
                "timestamp": m.timestamp,
                "bvid": m.bvid,
                "title": m.title,
            }
            for m in fresh
        ],
        skipped=max(0, skipped),
        note="有新消息" if fresh else "未读数>0，但都是已回复过的",
    )


async def reply_to(
    *,
    db: Database,
    config: Any,
    cookies: Any,
    msg_seqno: int,
    text: str,
) -> tuple[bool, str]:
    """回复**一条**主人消息（幂等：同 seqno 只回一次）。

    刻意**不要** ``queue`` 参数：发送走 :func:`core.dm.send_to_owner` 自己的链路
    （那里已经处理了 csrf 与"写请求不重试"），传进来只会让人误以为
    "发送也走统一读队列"。

    Args:
        cookies: :class:`core.credentials.CookieBundle`（不是裸 dict ——
            ``send_to_owner`` 需要 bundle 里的 csrf）。

    Returns:
        ``(成功, 说明)``。
    """
    if not text.strip():
        return False, "回复正文为空"
    if len(text) > MAX_DM_CHARS:
        return False, f"正文 {len(text)} 字，超过上限 {MAX_DM_CHARS}"

    already = db.query_one(
        "SELECT msg_seqno FROM dm_replies WHERE msg_seqno = ?", (int(msg_seqno),)
    )
    if already is not None:
        return False, f"seqno={msg_seqno} 已回复过（幂等跳过）"

    result = await send_to_owner(config=config, cookies=cookies, text=text, db=db)
    record_reply(
        db,
        msg_seqno=int(msg_seqno),
        reply_text=text,
        remote_msg_key=str(getattr(result, "msg_key", "") or ""),
    )

    # ★★ 把 `dm_inbox` 的状态同步成 answered（2026-10-17 值班员发现的真 bug）
    #
    # 原来这条路径**只写 `dm_replies`**，没动 `dm_inbox.status` ⇒ 状态永远停在
    # `pending`。后果：
    #   ① 值班员每次被唤醒都把这些消息当成"新私信"要求回复；
    #   ② 但 `dm_replies` 里已有记录 ⇒ 幂等规则挡掉（BLOCKED）；
    #   ③ 于是那条 pending 永远回不了，链路看着像坏了。
    #
    # 放在 `reply_to` 里（而不是各个调用点）是因为：
    #   自动回复（dm_watch）、MCP 的 dm_reply、CLI 都在走这个函数 ⇒
    #   一处修好，全部受益，不会再出现"某条路径漏更新"。
    #
    # 影响 0 行是正常的（那条私信可能不在 inbox 里，例如历史消息）。
    with contextlib.suppress(Exception):
        db.execute(
            "UPDATE dm_inbox SET status = 'answered' WHERE msg_seqno = ?",
            (int(msg_seqno),),
        )
        db.execute(
            "UPDATE dm_inbox SET delivered_at = COALESCE(delivered_at, ?) "
            "WHERE msg_seqno = ?",
            (int(time.time()), int(msg_seqno)),
        )

    return True, f"已回复（msg_key={getattr(result, 'msg_key', '')}）"


async def ack_to(
    *,
    db: Database,
    config: Any,
    cookies: Any,
    msg_seqno: int,
    text: str,
) -> tuple[bool, str]:
    """豆包**先回一条占位**：只发消息，不占 DSH 的正式回复名额（R3）。

    与 :func:`reply_to` 的差别（这是主人 2026-10-18 的明确要求）：

    * **不写** ``dm_replies`` ⇒ 同一条私信之后仍可由 DSH 用 ``dm_reply``
      给出正式回答；
    * **不改** ``dm_inbox.status``（仍是 pending）⇒ DSH 的 ``dm_inbox``
      还能看到、还能回复；
    * 只写 ``dm_inbox.acked_at`` / ``ack_text`` ⇒ 防止监听进程重复回同一条。

    Returns:
        ``(成功, 说明)``。发送失败**不**写 acked_at，下一轮还能再试。
    """
    body = (text or "").strip()
    if not body:
        return False, "占位回复正文为空"
    if len(body) > MAX_DM_CHARS:
        return False, f"正文 {len(body)} 字，超过上限 {MAX_DM_CHARS}"

    result = await send_to_owner(config=config, cookies=cookies, text=body, db=db)
    with contextlib.suppress(Exception):
        db.execute(
            "UPDATE dm_inbox SET acked_at = ?, ack_text = ? WHERE msg_seqno = ?",
            (int(time.time()), body[:MAX_DM_CHARS], int(msg_seqno)),
        )
    return True, f"已先回占位（msg_key={getattr(result, 'msg_key', '')}）"
