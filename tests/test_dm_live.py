"""私信实时回复测试（2026-10-17）。

主人要求：「我想在B站私信你并且你能随时回复」。
核心风险是**刷屏** —— 未读数在"已回复"后不会立刻归零
（B 站要等对端读了才变），所以同一个 ``msg_seqno`` 会在连续几次轮询里
反复出现。**没有幂等就会同一句话回 N 遍。**

这里钉死的不变量：

* **同一个 seqno 只回一次**（幂等）；
* 只认 ``follow_unread``（**主人**发来的），不认 ``unfollow_unread``（陌生人）；
* 只筛 ``sender_uid == owner_mid`` 的消息（不是"最新一条"）；
* 超长正文**拒绝**（不截断后照发）；
* **绝不联网**（全部用假 queue / 假消息）。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.dm import MAX_DM_CHARS, DmMessage
from core.dm_live import (
    pick_owner_messages,
    record_reply,
    replied_seqnos,
    unread_count,
    unseen_messages,
)
from core.storage import Database

OWNER = 660490601
AGENT = 3747570769725629


def msg(seqno: int, *, sender: int, text: str = "hi", bvid: str = "") -> DmMessage:
    return DmMessage(
        msg_seqno=seqno,
        sender_uid=sender,
        receiver_id=AGENT if sender == OWNER else OWNER,
        msg_type=1,
        text=text,
        timestamp=1_700_000_000 + seqno,
        bvid=bvid,
        title="",
        author="",
    )


# ---------------------------------------------------------------------------
# 未读数解析
# ---------------------------------------------------------------------------


def test_unread_count_reads_follow_unread() -> None:
    """★ 只认 ``follow_unread``（我关注的人 = 主人）。"""
    assert unread_count({"follow_unread": 3, "unfollow_unread": 99}) == 3


def test_unread_count_ignores_strangers() -> None:
    """★★ ``unfollow_unread``（陌生人）**不算** —— 我们不回陌生人。"""
    assert unread_count({"follow_unread": 0, "unfollow_unread": 42}) == 0


def test_unread_count_tolerates_garbage() -> None:
    """形状不可信（None / 字符串 / 缺字段）→ 0，不崩。"""
    assert unread_count(None) == 0
    assert unread_count({}) == 0
    assert unread_count({"follow_unread": "abc"}) == 0
    assert unread_count({"follow_unread": None}) == 0
    assert unread_count({"follow_unread": -5}) == 0


# ---------------------------------------------------------------------------
# 消息筛选
# ---------------------------------------------------------------------------


def test_pick_owner_messages_only_owner() -> None:
    """只筛主人发的 —— **不能**把"最新一条"当主人（那可能是我们自己的回复）。"""
    messages = [
        msg(1, sender=OWNER, text="主人的话"),
        msg(2, sender=AGENT, text="我的回复"),
        msg(3, sender=OWNER, text="主人又说"),
        msg(4, sender=AGENT, text="我又回"),
    ]
    picked = pick_owner_messages(messages, owner_mid=OWNER)
    assert [m.msg_seqno for m in picked] == [1, 3]
    assert all(m.sender_uid == OWNER for m in picked)


def test_pick_owner_messages_empty() -> None:
    assert pick_owner_messages([], owner_mid=OWNER) == []


# ---------------------------------------------------------------------------
# ★★ 幂等（这是防刷屏的核心）
# ---------------------------------------------------------------------------


def test_unseen_excludes_replied(db: Database) -> None:
    """★★ 回复过的 seqno **必须**被排除 —— 否则轮询会刷屏。

    真机场景：回复后未读数不会立刻归零（要等对端读），
    所以同一批消息会在**连续几次轮询**里反复出现。
    """
    messages = [msg(100, sender=OWNER), msg(101, sender=OWNER)]
    record_reply(db, msg_seqno=100, reply_text="回过啦")

    fresh = unseen_messages(db, messages, owner_mid=OWNER)
    assert [m.msg_seqno for m in fresh] == [101], "100 已回过，不该再出现"


def test_unseen_all_fresh_when_none_replied(db: Database) -> None:
    messages = [msg(200, sender=OWNER), msg(201, sender=OWNER)]
    fresh = unseen_messages(db, messages, owner_mid=OWNER)
    assert [m.msg_seqno for m in fresh] == [200, 201]


def test_record_reply_is_idempotent(db: Database) -> None:
    """重复记同一个 seqno 不该炸（INSERT OR REPLACE）。"""
    record_reply(db, msg_seqno=300, reply_text="第一次")
    record_reply(db, msg_seqno=300, reply_text="第二次")
    assert replied_seqnos(db) == {300}
    row = db.query_one("SELECT reply_text FROM dm_replies WHERE msg_seqno = ?", (300,))
    assert row is not None and row["reply_text"] == "第二次"


def test_record_reply_truncates_long_text(db: Database) -> None:
    """账本里的文本截到上限（不该因为记账把库撑坏）。"""
    record_reply(db, msg_seqno=400, reply_text="字" * (MAX_DM_CHARS + 200))
    row = db.query_one("SELECT reply_text FROM dm_replies WHERE msg_seqno = ?", (400,))
    assert row is not None
    assert len(row["reply_text"]) == MAX_DM_CHARS


def test_replied_seqnos_orders_by_time(db: Database) -> None:
    for seqno in (501, 502, 503):
        record_reply(db, msg_seqno=seqno, reply_text=f"回{seqno}")
    assert replied_seqnos(db) == {501, 502, 503}


# ---------------------------------------------------------------------------
# 端到端（假 queue，**不联网**）
# ---------------------------------------------------------------------------


class _FakeQueue:
    def __init__(self, unread: dict[str, Any], messages: list[DmMessage]) -> None:
        self._unread = unread
        self._messages = messages
        self.calls: list[str] = []

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        self.calls.append(url)

        class _Outcome:
            data = self._unread if "unread" in url else {"messages": []}

        if "fetch_session_msgs" in url:
            _Outcome.data = {
                "messages": [
                    {
                        "msg_seqno": m.msg_seqno,
                        "sender_uid": m.sender_uid,
                        "receiver_id": m.receiver_id,
                        "msg_type": m.msg_type,
                        "content": f'{{"content":"{m.text}"}}',
                        "timestamp": m.timestamp,
                    }
                    for m in self._messages
                ]
            }
        return _Outcome()


class _FakeConfig:
    class account:  # noqa: N801
        owner_mid = OWNER
        agent_mid = AGENT


async def test_poll_once_returns_fresh_messages(db: Database) -> None:
    from core.dm_live import poll_once

    queue = _FakeQueue(
        {"follow_unread": 2},
        [msg(601, sender=OWNER, text="第一条"), msg(602, sender=OWNER, text="第二条")],
    )
    outcome = await poll_once(
        db=db, config=_FakeConfig(), queue=queue, cookies={}
    )
    assert outcome.unread == 2
    assert [m["msg_seqno"] for m in outcome.new_messages] == [601, 602]
    assert outcome.new_messages[0]["text"] == "第一条"


async def test_poll_once_skips_already_replied(db: Database) -> None:
    """★★ 端到端幂等：已回复过的那条不该再出现在 new_messages 里。"""
    from core.dm_live import poll_once

    record_reply(db, msg_seqno=701, reply_text="回过")
    queue = _FakeQueue(
        {"follow_unread": 2},
        [msg(701, sender=OWNER, text="旧的"), msg(702, sender=OWNER, text="新的")],
    )
    outcome = await poll_once(db=db, config=_FakeConfig(), queue=queue, cookies={})
    assert [m["msg_seqno"] for m in outcome.new_messages] == [702]
    assert outcome.skipped == 1


async def test_poll_once_no_unread_skips_fetch(db: Database) -> None:
    """★ 没有未读时**不该**去拉消息正文（省请求）。"""
    from core.dm_live import poll_once

    queue = _FakeQueue({"follow_unread": 0}, [msg(801, sender=OWNER)])
    outcome = await poll_once(db=db, config=_FakeConfig(), queue=queue, cookies={})
    assert outcome.unread == 0
    assert outcome.new_messages == []
    assert not any("fetch_session_msgs" in url for url in queue.calls), (
        "没未读却拉了正文（浪费请求）"
    )


async def test_poll_once_without_owner_mid(db: Database) -> None:
    """owner_mid 没配 → 明确说明，不瞎猜。"""
    from core.dm_live import poll_once

    class _NoOwner:
        class account:  # noqa: N801
            owner_mid = 0
            agent_mid = AGENT

    queue = _FakeQueue({"follow_unread": 1}, [])
    outcome = await poll_once(db=db, config=_NoOwner(), queue=queue, cookies={})
    assert "owner_mid" in outcome.note


# ---------------------------------------------------------------------------
# 回复长度限制
# ---------------------------------------------------------------------------


async def test_reply_to_rejects_empty_text(db: Database) -> None:
    from core.dm_live import reply_to

    ok, message = await reply_to(
        db=db, config=_FakeConfig(), cookies={}, msg_seqno=1, text="   "
    )
    assert ok is False
    assert "空" in message


async def test_reply_to_rejects_too_long_text(db: Database) -> None:
    """★ 超长**拒绝**，不是截断后照发（截断会让意思变形）。"""
    from core.dm_live import reply_to

    ok, message = await reply_to(
        db=db,
        config=_FakeConfig(),
        cookies={},
        msg_seqno=2,
        text="字" * (MAX_DM_CHARS + 1),
    )
    assert ok is False
    assert "超过上限" in message


async def test_reply_to_is_idempotent(db: Database) -> None:
    """★★ 同一个 seqno 第二次回复**直接拒绝**（连发送都不发）。"""
    from core.dm_live import reply_to

    record_reply(db, msg_seqno=900, reply_text="已经回过")
    ok, message = await reply_to(
        db=db, config=_FakeConfig(), cookies={}, msg_seqno=900, text="再回一次"
    )
    assert ok is False
    assert "已回复过" in message


@pytest.mark.parametrize("unread_payload", [{}, {"follow_unread": 0}, None])
def test_unread_zero_variants(unread_payload: Any) -> None:
    assert unread_count(unread_payload) == 0
