"""私信「待回复队列」测试（2026-10-17）。

## 主人要求

> 「我的私信要求尽量是让 DSH 进行思考而不是内置的那个低级模型」

## 为什么必须用队列（架构约束，不是设计偏好）

DeepSeek 的凭据在 **DSH 内部**、不落配置文件：

```
provider-snapshots.json → deepseek-official, credential: env
core.secrets.resolve_secret("DEEPSEEK_API_KEY") → None
```

⇒ **常驻监听进程拿不到 key，无法自己调模型**。

正确分工：

```
监听进程：发现新私信 → 写 dm_inbox（pending）
DSH     ：dm_inbox 取出 → 用自己的模型思考 → dm_reply 发出
```

## 钉死的不变量

* **监听只负责入队**，不生成回复（它没有模型可用）；
* **入队幂等**：同一个 `msg_seqno` 不重复入队；
* **回复幂等**：`dm_replies` + `dm_inbox` 双重保险；
* **只回主人**：接收者仍硬编码 `owner_mid`。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from core.dm_watch import DmWatcher
from core.storage import Database


class _FakeConfig:
    class account:  # noqa: N801
        owner_mid = 660490601
        agent_mid = 3747570769725629

    class interaction:  # noqa: N801
        pause = False
        dm_reply_enabled = True


class _FakeCookies:
    def __init__(self) -> None:
        self.values = {"SESSDATA": "x", "bili_jct": "y"}

    def csrf_token(self) -> str:
        return "y"

    def has_write_credentials(self) -> bool:
        return True


def make_watcher(db: Database) -> DmWatcher:
    class _Q:
        async def get_json(self, url: str, **kwargs: Any) -> Any:
            class _O:
                data: dict[str, Any] = {}

            return _O()

        async def aclose(self) -> None:
            return None

    return DmWatcher(
        db=db, config=_FakeConfig(), queue=_Q(), cookies=_FakeCookies()
    )


def test_record_inbox_writes_pending(db: Database) -> None:
    """★ 新消息要落进 `dm_inbox`，状态 `pending`（等 DSH 取走）。"""
    watcher = make_watcher(db)
    written = watcher._record_inbox(
        [
            {
                "msg_seqno": 1001,
                "text": "帮我看看这个插件",
                "title": "",
                "bvid": "",
                "timestamp": int(time.time()),
            }
        ]
    )
    assert written == 1
    row = db.query_one("SELECT * FROM dm_inbox WHERE msg_seqno = 1001")
    assert row is not None
    assert row["status"] == "pending"
    assert row["text"] == "帮我看看这个插件"


def test_record_inbox_is_idempotent(db: Database) -> None:
    """★★ 同一条消息**不能重复入队**（监听每 60 秒查一次，会反复看到同一条）。"""
    watcher = make_watcher(db)
    message = {
        "msg_seqno": 1002,
        "text": "重复的",
        "title": "",
        "bvid": "",
        "timestamp": int(time.time()),
    }
    first = watcher._record_inbox([message])
    second = watcher._record_inbox([message])
    assert first == 1
    assert second == 0, "第二次不该再写一行"
    rows = db.query("SELECT * FROM dm_inbox WHERE msg_seqno = 1002")
    assert len(rows) == 1


def test_record_inbox_skips_bad_seqno(db: Database) -> None:
    """没 seqno 的条目跳过（不能当作有效消息）。"""
    watcher = make_watcher(db)
    written = watcher._record_inbox(
        [{"msg_seqno": 0, "text": "x", "title": "", "bvid": "", "timestamp": 0}]
    )
    assert written == 0


def test_record_inbox_keeps_bvid(db: Database) -> None:
    """★ 分享卡片要留住 bvid —— DSH 可以据此去读视频再回复。"""
    watcher = make_watcher(db)
    watcher._record_inbox(
        [
            {
                "msg_seqno": 1003,
                "text": "看看这个",
                "title": "某视频",
                "bvid": "BV1xx411c7mD",
                "timestamp": int(time.time()),
            }
        ]
    )
    row = db.query_one("SELECT * FROM dm_inbox WHERE msg_seqno = 1003")
    assert row is not None
    assert row["bvid"] == "BV1xx411c7mD"
    assert row["title"] == "某视频"


def test_record_inbox_truncates_long_text(db: Database) -> None:
    from core.dm import MAX_DM_CHARS

    watcher = make_watcher(db)
    watcher._record_inbox(
        [
            {
                "msg_seqno": 1004,
                "text": "字" * (MAX_DM_CHARS + 500),
                "title": "",
                "bvid": "",
                "timestamp": int(time.time()),
            }
        ]
    )
    row = db.query_one("SELECT text FROM dm_inbox WHERE msg_seqno = 1004")
    assert row is not None
    assert len(row["text"]) <= MAX_DM_CHARS


def test_inbox_status_check_constraint(db: Database) -> None:
    """状态只能是三种（数据库 CHECK 约束保证）。"""
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO dm_inbox (msg_seqno, text, status, created_at) VALUES (?,?,?,?)",
            (9999, "x", "乱写的状态", int(time.time())),
        )


def test_pending_query_orders_by_received(db: Database) -> None:
    """待回复按**收到时间升序**（先来的先回）。"""
    watcher = make_watcher(db)
    now = int(time.time())
    watcher._record_inbox(
        [
            {"msg_seqno": 2001, "text": "晚的", "title": "", "bvid": "", "timestamp": now + 100},
            {"msg_seqno": 2002, "text": "早的", "title": "", "bvid": "", "timestamp": now},
        ]
    )
    rows = db.query(
        "SELECT msg_seqno FROM dm_inbox WHERE status='pending' ORDER BY received_at ASC"
    )
    assert [r["msg_seqno"] for r in rows] == [2002, 2001]


def test_answered_excluded_from_pending(db: Database) -> None:
    watcher = make_watcher(db)
    watcher._record_inbox(
        [{"msg_seqno": 3001, "text": "x", "title": "", "bvid": "", "timestamp": int(time.time())}]
    )
    db.execute("UPDATE dm_inbox SET status='answered' WHERE msg_seqno = 3001")
    rows = db.query("SELECT * FROM dm_inbox WHERE status='pending'")
    assert len(rows) == 0


def test_watcher_uses_doubao_by_default(db: Database) -> None:
    """★ 默认回复生成器是**豆包**（主人选的方案 A）。

    背景：主人问「用豆包模型能主动醒来回复吗，或者采用DSH的APIkey」，
    选了豆包。因为：

    * 豆包 key 能拿到（注册表里有）⇒ 常驻进程**自己就能调** ⇒ 能主动回；
    * DSH 的 key 在进程内存里、拿不到，且没有外部调模型的接口。

    ⇒ `DmWatcher` 不传 `reply_factory` 时**自动接豆包**，
    而不是退回那个让主人不满意的固定话术。
    """
    from core.dm_watch import FALLBACK_REPLY

    watcher = make_watcher(db)
    assert watcher.reply_factory is not None, "默认应接豆包，而不是 None"
    assert watcher.reply_factory == watcher._doubao_factory
    # 固定话术仍保留作**降级兜底**（豆包挂了才用）
    assert FALLBACK_REPLY
    # 默认仍然不自动发（要 --reply 才发）
    assert watcher.auto_reply is False
