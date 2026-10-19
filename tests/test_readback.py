"""#4（Codex 审查）：发布读回核验与 ``sent_pending_verify``。

旧实现拿到非空 ``rpid`` 就把动作标成 ``sent_verified``，既没读回作者/正文/BV，
也把“平台接受了请求”和“内容确实是我们发的那条”混为一谈。这里锁定：

1. 匹配（作者 + 正文 + 所属 BV/root）才升 ``sent_verified``；
2. 查不到 / 不匹配 / 审核中 → 保留 ``sent_pending_verify``、占额度、锁幂等键；
3. 进程崩溃或审核延迟后，可用只读 ``readback_pending`` 重试，且**从不重发**。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.bilibili import NavInfo
from core.publish import REJECT_DUPLICATE, PublishGate
from core.storage import TOP_ACTION, Database, now_ts
from core.verify import readback_comment, readback_pending
from tests.test_publish_gates import (
    AGENT_MID,
    RecordingExecutor,
    publish_ready_config,
    seed_draft,
)


class StubQueue:
    """只实现 ``get_json`` 的读取队列（供 verify._fetch_page 使用）。"""

    def __init__(self, db: Database, payloads: list[dict[str, Any]]) -> None:
        self.db = db
        self.payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        kind: str = "metadata",
        use_cache: bool = True,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        from core.ratelimit import RequestOutcome

        self.calls.append({"url": url, "params": params, "use_cache": use_cache})
        data = self.payloads.pop(0) if self.payloads else {"replies": []}
        return RequestOutcome(status=200, data=data, cache_status="miss")


class StubReader:
    def __init__(self, db: Database, payloads: list[dict[str, Any]]) -> None:
        self.queue = StubQueue(db, payloads)
        self.cookies: dict[str, str] = {}

    async def get_nav(self) -> NavInfo:
        return NavInfo(is_login=True, uname="小号", mid=AGENT_MID)


def _comment_payload(
    *, rpid: str = "1234567890", mid: int = AGENT_MID,
    text: str = "这是一条评论", root: str = "0",
) -> dict[str, Any]:
    return {
        "replies": [
            {
                "rpid_str": rpid,
                "root_str": root,
                "parent_str": "0",
                "member": {"mid": mid, "uname": "小号"},
                "content": {"message": text},
                "like": 0,
                "ctime": 1_700_000_000,
                "rcount": 0,
            }
        ],
        "cursor": {"next": 0, "is_end": True, "all_count": 1},
    }


def _gate(db: Database, reader: Any, executor: Any) -> PublishGate:
    return PublishGate(
        db=db,
        config=publish_ready_config(max_top_comments_per_day=10),
        reader=reader,
        executor=executor,
    )


def _draft_for(db: Database, *, bvid: str, aid: int) -> str:
    db.upsert_video(bvid=bvid, aid=aid, title=f"视频 {bvid}")
    eid = db.add_evidence(
        bvid=bvid, source_type="subtitle", text=f"{bvid} 的字幕", start_sec=1.0
    )
    draft_id = db.create_draft(
        draft_type=TOP_ACTION, bvid=bvid, text="这是一条评论", evidence_ids=[eid]
    )
    db.approve_draft(draft_id)
    return draft_id


async def test_matching_readback_marks_verified(db: Database) -> None:
    draft = seed_draft(db)
    reader = StubReader(db, [_comment_payload()])
    executor = RecordingExecutor(rpid="1234567890")
    gate = _gate(db, reader, executor)

    result = await gate.publish(draft_id=draft)
    assert result.ok is True and result.code == "OK"
    row = db.query_one("SELECT status, remote_rpid FROM actions WHERE draft_id = ?", (draft,))
    assert row is not None and row["status"] == "sent_verified"
    draft_row = db.get_draft(draft)
    assert draft_row is not None and draft_row["status"] == "sent_verified"
    assert reader.queue.calls[0]["use_cache"] is False, "读回不能吃缓存"


async def test_wrong_author_stays_pending_and_never_reposts(db: Database) -> None:
    draft = seed_draft(db)
    reader = StubReader(db, [_comment_payload(mid=999999)])
    executor = RecordingExecutor(rpid="1234567890")
    gate = _gate(db, reader, executor)

    result = await gate.publish(draft_id=draft)
    assert result.ok is True and result.code == "OK_PENDING_VERIFY"
    row = db.query_one("SELECT status, error_code FROM actions WHERE draft_id = ?", (draft,))
    assert row is not None
    assert row["status"] == "sent_pending_verify"
    assert row["error_code"] == "AUTHOR_MISMATCH"
    assert len(executor.calls) == 1

    again = await gate.publish(draft_id=draft)
    assert again.ok is False and again.code == REJECT_DUPLICATE
    assert len(executor.calls) == 1, "待核验动作不得再次提交"


async def test_wrong_content_and_not_found_stay_pending(db: Database) -> None:
    draft = seed_draft(db)
    reader = StubReader(db, [_comment_payload(text="被平台换掉的正文")])
    gate = _gate(db, reader, RecordingExecutor(rpid="1234567890"))
    first = await gate.publish(draft_id=draft)
    assert first.code == "OK_PENDING_VERIFY"
    row = db.query_one("SELECT status, error_code FROM actions WHERE draft_id = ?", (draft,))
    assert row is not None and row["status"] == "sent_pending_verify"
    assert row["error_code"] == "CONTENT_MISMATCH"

    # 查不到（审核延迟 / 翻页）同样只能待核验
    draft2 = _draft_for(db, bvid="BV1test00002", aid=222)
    reader2 = StubReader(db, [{"replies": []}])
    gate2 = _gate(db, reader2, RecordingExecutor(rpid="2234567890"))
    second = await gate2.publish(draft_id=draft2)
    assert second.code == "OK_PENDING_VERIFY"
    row2 = db.query_one("SELECT status, error_code FROM actions WHERE draft_id = ?", (draft2,))
    assert row2 is not None and row2["status"] == "sent_pending_verify"
    assert row2["error_code"] == "READBACK_NOT_FOUND"


async def test_pending_actions_occupy_quota(db: Database) -> None:
    draft = seed_draft(db)
    reader = StubReader(db, [{"replies": []}])
    gate = _gate(db, reader, RecordingExecutor(rpid="1234567890"))
    await gate.publish(draft_id=draft)
    used = db.count_actions_today(action_type=TOP_ACTION, is_simulated=False)
    assert used == 1, "待核验的评论已经发出去了，必须占额度"


async def test_readback_pending_promotes_after_review_delay(db: Database) -> None:
    draft = seed_draft(db)
    reader = StubReader(db, [{"replies": []}])
    gate = _gate(db, reader, RecordingExecutor(rpid="1234567890"))
    await gate.publish(draft_id=draft)

    # 审核通过后，远端能查到同一条
    reader.queue.payloads = [_comment_payload()]
    promoted = await readback_pending(db, reader, limit=10)
    assert len(promoted) == 1 and promoted[0][1].ok is True
    row = db.query_one("SELECT status FROM actions WHERE draft_id = ?", (draft,))
    assert row is not None and row["status"] == "sent_verified"
    draft_row = db.get_draft(draft)
    assert draft_row is not None and draft_row["status"] == "sent_verified"
    assert len(gate.executor.calls) == 1, "重试核验不得再发一次评论"


async def test_reply_readback_checks_root(db: Database) -> None:
    db.upsert_video(bvid="BV1reply00001", aid=444, title="回复目标")
    reader = StubReader(db, [_comment_payload(rpid="9876543210", root="777")])
    result = await readback_comment(
        reader,
        bvid="BV1reply00001",
        aid=444,
        rpid="9876543210",
        agent_mid=AGENT_MID,
        expected_text="这是一条评论",
        root="777",
    )
    assert result.ok is True
    assert reader.queue.calls[0]["params"].get("root") == "777"

    reader.queue.payloads = [_comment_payload(rpid="9876543210", root="777")]
    mismatch = await readback_comment(
        reader, bvid="BV1reply00001", aid=444, rpid="9876543210",
        agent_mid=AGENT_MID, expected_text="这是一条评论", root="888",
    )
    assert mismatch.ok is False and mismatch.reason == "ROOT_MISMATCH"


def test_schema_v14_upgrade_preserves_rows(tmp_path: Any) -> None:
    """既有 v13 库升级到 v14：旧数据保留，且允许 ``sent_pending_verify``。"""
    import sqlite3

    from core import schema

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
    )
    for version, script in schema.MIGRATIONS[:13]:
        conn.executescript(script)
        conn.execute("INSERT INTO schema_version VALUES (?, 0)", (version,))
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 13
    conn.commit()
    conn.close()

    db = Database(path)
    assert db.migrate() == 16
    db.upsert_video(bvid="BV1mig000001", aid=1, title="迁移测试")
    db.execute(
        "INSERT INTO actions (action_id, agent_mid, action_type, bvid, idempotency_key,"
        " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("act_mig", AGENT_MID, TOP_ACTION, "BV1mig000001", "k-mig",
         "sent_pending_verify", now_ts(), now_ts()),
    )
    row = db.query_one("SELECT status FROM actions WHERE action_id = 'act_mig'")
    assert row is not None and row["status"] == "sent_pending_verify"
    db.close()


@pytest.mark.parametrize("reason", ["READBACK_BLOCKED:platform_error"])
async def test_health_blocked_readback_stays_pending(db: Database, reason: str) -> None:
    db.upsert_video(bvid="BV1block00001", aid=555, title="阻断")
    db.record_health(state="platform_error", category="test")
    reader = StubReader(db, [])
    result = await readback_comment(
        reader, bvid="BV1block00001", aid=555, rpid="1234567890",
        agent_mid=AGENT_MID, expected_text="x",
    )
    assert result.ok is False and result.reason == reason
