"""受控发布测试（任务书 §7 任务 5 的完整测试清单）。

覆盖：
``dry_run`` / ``observe`` / ``review`` 未批准与已批准 / ``auto`` / 错号 / 未登录 /
配额满 / 并发任务 / 点击后超时 / 进程重启 / 审核中 / 同一 BV 两次发言 /
同一回复 ID 两次回复 / 验证码出现后仍有排队任务。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.bilibili import NavInfo
from core.publish import (
    REJECT_AGENT_MID_NOT_CONFIGURED,
    REJECT_DRAFT_NOT_APPROVED,
    REJECT_DUPLICATE,
    REJECT_IDENTITY_MISMATCH,
    REJECT_MODE_OBSERVE,
    REJECT_PAUSED,
    REJECT_PLATFORM_BLOCKED,
    REJECT_PUBLISH_DISABLED,
    REJECT_QUOTA_REPLY,
    REJECT_QUOTA_THREAD,
    REJECT_QUOTA_TOP,
    REJECT_TARGET_FROZEN,
    REJECT_THIRD_PARTY_DISABLED,
    PublishGate,
)
from core.storage import REPLY_ACTION, TOP_ACTION, Database
from tests.conftest import make_config

AGENT_MID = 1001
OWNER_MID = 2002


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class FakeReader:
    """假读取器：可控的登录身份。"""

    def __init__(self, mid: int = AGENT_MID, is_login: bool = True) -> None:
        self.mid = mid
        self.is_login = is_login
        self.calls = 0

    async def get_nav(self) -> NavInfo:
        self.calls += 1
        return NavInfo(is_login=self.is_login, uname="测试号", mid=self.mid)


class RecordingExecutor:
    """记录所有真实写调用——**测试里必须始终为空或受控**。"""

    def __init__(self, *, fail_with: Exception | None = None, rpid: str = "1234567890") -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_with = fail_with
        self.rpid = rpid

    async def send_top_comment(self, *, aid: int, bvid: str, text: str) -> dict[str, Any]:
        self.calls.append({"kind": "top", "aid": aid, "bvid": bvid, "text": text})
        if self.fail_with is not None:
            raise self.fail_with
        return {"remote_rpid": self.rpid, "remote_url": f"https://b/{self.rpid}"}

    async def send_reply(
        self, *, aid: int, bvid: str, root: str, parent: str, text: str
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "kind": "reply",
                "aid": aid,
                "bvid": bvid,
                "root": root,
                "parent": parent,
                "text": text,
            }
        )
        if self.fail_with is not None:
            raise self.fail_with
        return {"remote_rpid": self.rpid, "remote_url": f"https://b/{self.rpid}"}


def publish_ready_config(**interaction_overrides: Any) -> Any:
    """一个"所有发布前提都满足"的配置，供各测试按需破坏其中一条。"""
    interaction = {
        "mode": "auto",
        "dry_run": False,
        "publish_enabled": True,
        "pause": False,
        **interaction_overrides,
    }
    return make_config(
        account={"agent_mid": AGENT_MID, "owner_mid": OWNER_MID},
        interaction=interaction,
    )


def seed_draft(
    db: Database, *, draft_type: str = TOP_ACTION, target_rpid: str | None = None,
    target_root: str | None = None, status: str = "approved",
) -> str:
    db.upsert_video(bvid="BV1test00001", aid=111, title="t")
    eid = db.add_evidence(
        bvid="BV1test00001", source_type="subtitle", text="字幕原文片段", start_sec=1.0
    )
    draft_id = db.create_draft(
        draft_type=draft_type,
        bvid="BV1test00001",
        text="这是一条评论",
        evidence_ids=[eid],
        target_rpid=target_rpid,
        target_root=target_root,
    )
    if status != "pending":
        db.set_draft_status(draft_id, status)
    return draft_id


def make_gate(
    db: Database, config: Any, *, reader: Any = None, executor: Any = None
) -> PublishGate:
    return PublishGate(
        db=db,
        config=config,
        reader=reader if reader is not None else FakeReader(),
        executor=executor,
    )


# ---------------------------------------------------------------------------
# dry_run：绝不真实发
# ---------------------------------------------------------------------------


async def test_dry_run_never_touches_platform(db: Database) -> None:
    """★ 任务书 §8：``dry_run`` 绝不真实发。"""
    draft_id = seed_draft(db)
    executor = RecordingExecutor()
    config = publish_ready_config(dry_run=True)
    gate = make_gate(db, config, executor=executor)

    result = await gate.publish(draft_id=draft_id)
    assert result.ok is True
    assert result.simulated is True
    assert result.code == "SIMULATED"
    assert executor.calls == [], "dry_run 下绝不能调用真实执行器"
    assert result.remote_rpid is None, "★ 模拟 ID 不得当作远端 rpid"


async def test_dry_run_counts_simulated_separately(db: Database) -> None:
    draft_id = seed_draft(db)
    gate = make_gate(db, publish_ready_config(dry_run=True), executor=RecordingExecutor())
    await gate.publish(draft_id=draft_id)
    assert db.count_actions_today(action_type=TOP_ACTION, is_simulated=True) == 1
    assert db.count_actions_today(action_type=TOP_ACTION, is_simulated=False) == 0


async def test_dry_run_marks_own_record_status(db: Database) -> None:
    """模拟成功**不等于**远端成功，但草稿要落一个明确可查的状态。"""
    draft_id = seed_draft(db)
    gate = make_gate(db, publish_ready_config(dry_run=True), executor=RecordingExecutor())
    await gate.publish(draft_id=draft_id)
    row = db.query_one("SELECT * FROM actions WHERE is_simulated = 1")
    assert row is not None
    assert row["remote_rpid"] is None


# ---------------------------------------------------------------------------
# 模式门禁
# ---------------------------------------------------------------------------


async def test_observe_mode_blocks_public_write(db: Database) -> None:
    """★ 任务书 §8：有登录但 ``observe`` 不发。"""
    draft_id = seed_draft(db)
    executor = RecordingExecutor()
    config = make_config(
        account={"agent_mid": AGENT_MID, "owner_mid": OWNER_MID},
        interaction={"mode": "observe", "dry_run": False, "publish_enabled": True},
    )
    gate = make_gate(db, config, executor=executor)
    result = await gate.publish(draft_id=draft_id)
    assert result.ok is False
    assert result.code == REJECT_MODE_OBSERVE
    assert executor.calls == []


async def test_review_unapproved_does_not_publish(db: Database) -> None:
    """★ 任务书 §8：``review`` 未批准不发。"""
    draft_id = seed_draft(db, status="pending")
    executor = RecordingExecutor()
    config = publish_ready_config(mode="review")
    gate = make_gate(db, config, executor=executor)

    result = await gate.publish(draft_id=draft_id)
    assert result.ok is False
    assert result.code == REJECT_DRAFT_NOT_APPROVED
    assert executor.calls == []


async def test_review_approved_then_publishes(db: Database) -> None:
    """批准（人用入口）之后可以发布。"""
    draft_id = seed_draft(db, status="pending")
    executor = RecordingExecutor()
    config = publish_ready_config(mode="review")
    gate = make_gate(db, config, executor=executor)

    db.approve_draft(draft_id)
    result = await gate.publish(draft_id=draft_id)
    assert result.ok is True
    assert result.remote_rpid == "1234567890"
    assert len(executor.calls) == 1


async def test_auto_mode_allows_pending_draft(db: Database) -> None:
    """``auto`` 模式下 pending 草稿可直接发（前提是其余门禁全过）。"""
    draft_id = seed_draft(db, status="pending")
    executor = RecordingExecutor()
    gate = make_gate(db, publish_ready_config(mode="auto"), executor=executor)
    result = await gate.publish(draft_id=draft_id)
    assert result.ok is True
    assert len(executor.calls) == 1


async def test_publish_disabled_blocks(db: Database) -> None:
    draft_id = seed_draft(db)
    executor = RecordingExecutor()
    gate = make_gate(db, publish_ready_config(publish_enabled=False), executor=executor)
    result = await gate.publish(draft_id=draft_id)
    assert result.ok is False
    assert result.code == REJECT_PUBLISH_DISABLED
    assert executor.calls == []


async def test_pause_blocks_new_public_actions(db: Database) -> None:
    draft_id = seed_draft(db)
    executor = RecordingExecutor()
    gate = make_gate(db, publish_ready_config(pause=True), executor=executor)
    result = await gate.publish(draft_id=draft_id)
    assert result.ok is False
    assert result.code == REJECT_PAUSED
    assert executor.calls == []


# ---------------------------------------------------------------------------
# 身份（每次从实际登录态读）
# ---------------------------------------------------------------------------


async def test_identity_mismatch_blocks(db: Database) -> None:
    """★ 错号：实际登录 UID 与配置不符 → 拒绝。"""
    draft_id = seed_draft(db)
    executor = RecordingExecutor()
    gate = make_gate(db, publish_ready_config(), reader=FakeReader(mid=9999), executor=executor)
    result = await gate.publish(draft_id=draft_id)
    assert result.ok is False
    assert result.code == REJECT_IDENTITY_MISMATCH
    assert executor.calls == []


async def test_not_logged_in_blocks(db: Database) -> None:
    """★ 未登录发不出去。"""
    draft_id = seed_draft(db)
    executor = RecordingExecutor()
    gate = make_gate(
        db, publish_ready_config(), reader=FakeReader(mid=0, is_login=False), executor=executor
    )
    result = await gate.publish(draft_id=draft_id)
    assert result.ok is False
    assert result.code in (REJECT_IDENTITY_MISMATCH, "IDENTITY_UNVERIFIED")
    assert executor.calls == []


async def test_identity_read_every_time(db: Database) -> None:
    """★ 不能只信配置或上次缓存——每次发布都要重读登录态。"""
    first = seed_draft(db)
    reader = FakeReader(mid=AGENT_MID)
    executor = RecordingExecutor()
    gate = make_gate(db, publish_ready_config(), reader=reader, executor=executor)
    await gate.publish(draft_id=first)
    assert reader.calls >= 1

    # 第二次换一个草稿（不同幂等键），必须再次读取身份
    db.upsert_video(bvid="BV1test00002", aid=222)
    eid = db.add_evidence(bvid="BV1test00002", source_type="subtitle", text="另一段")
    second = db.create_draft(
        draft_type=TOP_ACTION, bvid="BV1test00002", text="第二条", evidence_ids=[eid]
    )
    db.set_draft_status(second, "approved")
    before = reader.calls
    await gate.publish(draft_id=second)
    assert reader.calls > before


async def test_agent_mid_not_configured_blocks(db: Database) -> None:
    draft_id = seed_draft(db)
    executor = RecordingExecutor()
    config = make_config(
        account={"agent_mid": 0, "owner_mid": OWNER_MID},
        interaction={"mode": "auto", "dry_run": False, "publish_enabled": True},
    )
    gate = make_gate(db, config, executor=executor)
    result = await gate.publish(draft_id=draft_id)
    assert result.ok is False
    assert result.code == REJECT_AGENT_MID_NOT_CONFIGURED
    assert executor.calls == []


# ---------------------------------------------------------------------------
# 平台健康门禁（验证码等）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state", ["verification_required", "auth_expired", "platform_error"]
)
async def test_blocking_health_state_prevents_publish(db: Database, state: str) -> None:
    """★ 验证码/认证失效/平台错误 → 直接拒绝，且**不自动恢复**。"""
    draft_id = seed_draft(db)
    executor = RecordingExecutor()
    db.record_health(state=state, category="test")
    gate = make_gate(db, publish_ready_config(), executor=executor)

    result = await gate.publish(draft_id=draft_id)
    assert result.ok is False
    assert result.code == REJECT_PLATFORM_BLOCKED
    assert executor.calls == []
    assert db.health_state() == state, "不得因发布尝试而自动恢复健康状态"


async def test_queued_task_after_captcha_also_blocked(db: Database) -> None:
    """★ 任务书 §7 任务 5：验证码出现后仍有排队任务 → 也不得发出。"""
    first = seed_draft(db)
    db.upsert_video(bvid="BV1test00002", aid=222)
    eid = db.add_evidence(bvid="BV1test00002", source_type="subtitle", text="别的")
    second = db.create_draft(
        draft_type=TOP_ACTION, bvid="BV1test00002", text="排在后面的", evidence_ids=[eid]
    )
    db.set_draft_status(second, "approved")

    executor = RecordingExecutor()
    db.record_health(state="verification_required", category="captcha", raw_code="-352")
    gate = make_gate(db, publish_ready_config(), executor=executor)

    for draft_id in (first, second):
        result = await gate.publish(draft_id=draft_id)
        assert result.ok is False
        assert result.code == REJECT_PLATFORM_BLOCKED
    assert executor.calls == []


# ---------------------------------------------------------------------------
# 幂等：同一 BV / 同一回复 ID 不重复
# ---------------------------------------------------------------------------


async def test_same_bvid_twice_does_not_double_post(db: Database) -> None:
    """★ 任务书 §7 任务 5：同一 BV 两次发言 → 第二次被幂等键挡住。"""
    first = seed_draft(db)
    executor = RecordingExecutor()
    gate = make_gate(db, publish_ready_config(), executor=executor)

    r1 = await gate.publish(draft_id=first)
    assert r1.ok is True

    # 新建一个同 BV 的顶层草稿（不同 draft_id，但幂等键相同）
    db.upsert_video(bvid="BV1test00001", aid=111)
    eid = db.add_evidence(bvid="BV1test00001", source_type="subtitle", text="另一个片段")
    second = db.create_draft(
        draft_type=TOP_ACTION, bvid="BV1test00001", text="想再说一次", evidence_ids=[eid]
    )
    db.set_draft_status(second, "approved")

    r2 = await gate.publish(draft_id=second)
    assert r2.ok is False
    assert r2.code == REJECT_DUPLICATE
    assert len(executor.calls) == 1, "平台侧只能有一次真实提交"


async def test_same_reply_target_twice_does_not_double_post(db: Database) -> None:
    """★ 同一回复 ID 两次回复 → 第二次被挡。"""
    target = "987654321"
    db.upsert_video(bvid="BV1test00001", aid=111)
    db.mark_seen_reply(remote_rpid=target, bvid="BV1test00001", author_mid=AGENT_MID)

    eid = db.add_evidence(bvid="BV1test00001", source_type="comment", text="别人的评论")
    first = db.create_draft(
        draft_type=REPLY_ACTION, bvid="BV1test00001", text="第一次回复",
        evidence_ids=[eid], target_rpid=target, target_root=target,
    )
    db.set_draft_status(first, "approved")

    executor = RecordingExecutor()
    gate = make_gate(db, publish_ready_config(), executor=executor)
    r1 = await gate.publish(draft_id=first)
    assert r1.ok is True

    second = db.create_draft(
        draft_type=REPLY_ACTION, bvid="BV1test00001", text="第二次回复",
        evidence_ids=[eid], target_rpid=target, target_root=target,
    )
    db.set_draft_status(second, "approved")
    r2 = await gate.publish(draft_id=second)
    assert r2.ok is False
    assert r2.code == REJECT_DUPLICATE
    assert len(executor.calls) == 1


# ---------------------------------------------------------------------------
# uncertain：冻结、不重发、占额度
# ---------------------------------------------------------------------------


async def test_timeout_after_click_becomes_uncertain(db: Database) -> None:
    """★ 点击后超时 → ``uncertain``，不得标成功、不得自动重发。"""
    draft_id = seed_draft(db)
    executor = RecordingExecutor(fail_with=TimeoutError("no response"))
    gate = make_gate(db, publish_ready_config(), executor=executor)

    result = await gate.publish(draft_id=draft_id)
    assert result.ok is False
    assert result.code == "PUBLISH_UNCERTAIN"

    row = db.find_action_by_key(
        db.make_idempotency_key(
            agent_mid=AGENT_MID, action_type=TOP_ACTION, bvid="BV1test00001", target_rpid=None
        )
    )
    assert str(row["status"]) == "uncertain"
    assert str(db.get_draft(draft_id)["status"]) == "uncertain"


async def test_uncertain_freezes_same_target(db: Database) -> None:
    """★ 同目标后续动作被冻结，**不自动重发**。"""
    draft_id = seed_draft(db)
    executor = RecordingExecutor(fail_with=TimeoutError("no response"))
    gate = make_gate(db, publish_ready_config(), executor=executor)
    await gate.publish(draft_id=draft_id)

    # 再建同 BV 顶层草稿并尝试
    eid = db.add_evidence(bvid="BV1test00001", source_type="subtitle", text="再来一段")
    another = db.create_draft(
        draft_type=TOP_ACTION, bvid="BV1test00001", text="重试一下", evidence_ids=[eid]
    )
    db.set_draft_status(another, "approved")

    result = await gate.publish(draft_id=another)
    assert result.ok is False
    assert result.code == REJECT_TARGET_FROZEN, "uncertain 冻结必须优先于普通重复"
    assert len(executor.calls) == 1, "绝不能真的再提交一次"


async def test_uncertain_consumes_quota(db: Database) -> None:
    """★ uncertain 占用额度，进程崩溃后不超额。"""
    draft_id = seed_draft(db)
    gate = make_gate(
        db, publish_ready_config(), executor=RecordingExecutor(fail_with=TimeoutError("x"))
    )
    await gate.publish(draft_id=draft_id)
    assert db.count_actions_today(action_type=TOP_ACTION, is_simulated=False) == 1


async def test_process_restart_after_reserved_freezes(db: Database) -> None:
    """★ 进程重启：预留后没有结论 → 再次尝试转 uncertain 并冻结。"""
    draft_id = seed_draft(db)
    # 模拟"点击前已预留，然后进程死了"
    db.reserve_action(
        agent_mid=AGENT_MID, action_type=TOP_ACTION, bvid="BV1test00001",
        target_rpid=None, draft_id=draft_id, is_simulated=False,
    )
    executor = RecordingExecutor()
    gate = make_gate(db, publish_ready_config(), executor=executor)
    result = await gate.publish(draft_id=draft_id)
    assert result.ok is False
    assert result.code == REJECT_TARGET_FROZEN, "预留后无结论 → 冻结，不重发"
    assert executor.calls == []


async def test_no_remote_rpid_is_not_verified(db: Database) -> None:
    """★ 未核验到 rpid 时不得标记 ``sent_verified``。"""
    draft_id = seed_draft(db)
    executor = RecordingExecutor(rpid="")
    gate = make_gate(db, publish_ready_config(), executor=executor)
    result = await gate.publish(draft_id=draft_id)
    assert result.ok is False
    assert result.code == "PUBLISH_UNVERIFIED"
    row = db.find_action_by_key(
        db.make_idempotency_key(
            agent_mid=AGENT_MID, action_type=TOP_ACTION, bvid="BV1test00001", target_rpid=None
        )
    )
    assert str(row["status"]) == "uncertain"


async def test_executor_unavailable_freezes_not_fakes_success(db: Database) -> None:
    """没有真实执行器时**绝不假装成功**。"""
    draft_id = seed_draft(db)
    gate = make_gate(db, publish_ready_config(), executor=None)
    result = await gate.publish(draft_id=draft_id)
    assert result.ok is False
    assert result.code == "EXECUTOR_UNAVAILABLE"
    assert str(db.get_draft(draft_id)["status"]) == "uncertain"


# ---------------------------------------------------------------------------
# 配额
# ---------------------------------------------------------------------------


async def test_top_comment_quota_enforced(db: Database) -> None:
    """★ 配额满 → 拒绝。"""
    config = publish_ready_config(max_top_comments_per_day=1)
    executor = RecordingExecutor()
    gate = make_gate(db, config, executor=executor)

    first = seed_draft(db)
    assert (await gate.publish(draft_id=first)).ok is True

    db.upsert_video(bvid="BV1test00002", aid=222)
    eid = db.add_evidence(bvid="BV1test00002", source_type="subtitle", text="别的")
    second = db.create_draft(
        draft_type=TOP_ACTION, bvid="BV1test00002", text="第二条", evidence_ids=[eid]
    )
    db.set_draft_status(second, "approved")

    result = await gate.publish(draft_id=second)
    assert result.ok is False
    assert result.code == REJECT_QUOTA_TOP
    assert len(executor.calls) == 1


async def test_reply_quota_enforced(db: Database) -> None:
    config = publish_ready_config(max_replies_per_day=1)
    executor = RecordingExecutor()

    db.upsert_video(bvid="BV1test00001", aid=111)
    db.upsert_video(bvid="BV1test00002", aid=222)
    for bvid, target in (("BV1test00001", "111"), ("BV1test00002", "222")):
        db.mark_seen_reply(remote_rpid=target, bvid=bvid, author_mid=AGENT_MID)

    gate = make_gate(db, config, executor=executor)
    eid1 = db.add_evidence(bvid="BV1test00001", source_type="comment", text="评1")
    d1 = db.create_draft(
        draft_type=REPLY_ACTION, bvid="BV1test00001", text="回1",
        evidence_ids=[eid1], target_rpid="111", target_root="111",
    )
    db.set_draft_status(d1, "approved")
    assert (await gate.publish(draft_id=d1)).ok is True

    eid2 = db.add_evidence(bvid="BV1test00002", source_type="comment", text="评2")
    d2 = db.create_draft(
        draft_type=REPLY_ACTION, bvid="BV1test00002", text="回2",
        evidence_ids=[eid2], target_rpid="222", target_root="222",
    )
    db.set_draft_status(d2, "approved")
    result = await gate.publish(draft_id=d2)
    assert result.ok is False
    assert result.code == REJECT_QUOTA_REPLY


async def test_per_thread_quota_enforced(db: Database) -> None:
    """单串回复上限。"""
    config = publish_ready_config(max_replies_per_day=10, max_replies_per_thread_per_day=1)
    db.upsert_video(bvid="BV1test00001", aid=111)
    root = "555"
    db.mark_seen_reply(remote_rpid=root, bvid="BV1test00001", author_mid=AGENT_MID)
    gate = make_gate(db, config, executor=RecordingExecutor())

    eid = db.add_evidence(bvid="BV1test00001", source_type="comment", text="评论")
    d1 = db.create_draft(
        draft_type=REPLY_ACTION, bvid="BV1test00001", text="回1",
        evidence_ids=[eid], target_rpid=root, target_root=root,
    )
    db.set_draft_status(d1, "approved")
    assert (await gate.publish(draft_id=d1)).ok is True

    # 同串不同 rpid（例如楼中楼里的另一条）
    other = "556"
    db.mark_seen_reply(remote_rpid=other, bvid="BV1test00001", author_mid=AGENT_MID)
    d2 = db.create_draft(
        draft_type=REPLY_ACTION, bvid="BV1test00001", text="回2",
        evidence_ids=[eid], target_rpid=other, target_root=root,
    )
    db.set_draft_status(d2, "approved")
    result = await gate.publish(draft_id=d2)
    assert result.ok is False
    assert result.code == REJECT_QUOTA_THREAD


# ---------------------------------------------------------------------------
# 目标校验
# ---------------------------------------------------------------------------


async def test_unknown_rpid_rejected(db: Database) -> None:
    """★ 对未知 ``rpid`` 返回拒绝——即便第三方开关开着也不行。"""
    config = publish_ready_config(allow_third_party_threads=True)
    db.upsert_video(bvid="BV1test00001", aid=111)
    gate = make_gate(db, config, executor=RecordingExecutor())
    ok, code = await gate.validate_reply_target(bvid="BV1test00001", target_rpid="never-seen")
    assert ok is False
    assert code == "TARGET_UNKNOWN_RPID"


async def test_unknown_rpid_rejected_when_third_party_off(db: Database) -> None:
    """第三方开关关闭时，未知目标一律拒绝（不泄露"它存在与否"之外的信息）。"""
    config = publish_ready_config(allow_third_party_threads=False)
    db.upsert_video(bvid="BV1test00001", aid=111)
    db.mark_seen_reply(remote_rpid="888", bvid="BV1test00001", author_mid=7777)
    gate = make_gate(db, config, executor=RecordingExecutor())
    ok, code = await gate.validate_reply_target(bvid="BV1test00001", target_rpid="888")
    assert ok is False
    assert code == REJECT_THIRD_PARTY_DISABLED


async def test_third_party_requires_switch(db: Database) -> None:
    """第三方评论串默认关；开启后才允许。"""
    db.upsert_video(bvid="BV1test00001", aid=111)
    db.mark_seen_reply(remote_rpid="777", bvid="BV1test00001", author_mid=8888)

    gate_off = make_gate(db, publish_ready_config(allow_third_party_threads=False))
    ok, code = await gate_off.validate_reply_target(bvid="BV1test00001", target_rpid="777")
    assert ok is False
    assert code == REJECT_THIRD_PARTY_DISABLED

    gate_on = make_gate(db, publish_ready_config(allow_third_party_threads=True))
    ok2, code2 = await gate_on.validate_reply_target(bvid="BV1test00001", target_rpid="777")
    assert ok2 is True
    assert code2 == "OK"


async def test_own_comment_thread_allowed(db: Database) -> None:
    gate = make_gate(db, publish_ready_config())
    ok, code = await gate.validate_reply_target(
        bvid="BV1test00001", target_rpid="123", own_comment_rpids={"123"}
    )
    assert ok is True
    assert code == "OK"


async def test_target_from_other_bvid_rejected(db: Database) -> None:
    db.upsert_video(bvid="BV1test00001", aid=111)
    db.upsert_video(bvid="BV1test00002", aid=222)
    db.mark_seen_reply(remote_rpid="999", bvid="BV1test00002", author_mid=AGENT_MID)
    gate = make_gate(db, publish_ready_config(allow_third_party_threads=True))
    ok, code = await gate.validate_reply_target(bvid="BV1test00001", target_rpid="999")
    assert ok is False
    assert code == "TARGET_BVID_MISMATCH"


# ---------------------------------------------------------------------------
# 草稿状态
# ---------------------------------------------------------------------------


async def test_missing_draft(db: Database) -> None:
    gate = make_gate(db, publish_ready_config(), executor=RecordingExecutor())
    result = await gate.publish(draft_id="draft_nope")
    assert result.ok is False
    assert result.code == "DRAFT_NOT_FOUND"


async def test_already_sent_draft_does_not_resend(db: Database) -> None:
    """★ 已发送的草稿不能重发。"""
    draft_id = seed_draft(db)
    executor = RecordingExecutor()
    gate = make_gate(db, publish_ready_config(), executor=executor)

    assert (await gate.publish(draft_id=draft_id)).ok is True
    second = await gate.publish(draft_id=draft_id)
    assert second.ok is False
    assert second.code == REJECT_DUPLICATE
    assert len(executor.calls) == 1


async def test_rejected_draft_not_publishable(db: Database) -> None:
    draft_id = seed_draft(db, status="rejected")
    executor = RecordingExecutor()
    gate = make_gate(db, publish_ready_config(), executor=executor)
    result = await gate.publish(draft_id=draft_id)
    assert result.ok is False
    assert executor.calls == []


async def test_uncertain_draft_rejected_at_gate(db: Database) -> None:
    draft_id = seed_draft(db, status="uncertain")
    executor = RecordingExecutor()
    gate = make_gate(db, publish_ready_config(), executor=executor)
    result = await gate.publish(draft_id=draft_id)
    assert result.ok is False
    assert executor.calls == []


# ---------------------------------------------------------------------------
# 证据
# ---------------------------------------------------------------------------


async def test_draft_without_evidence_cannot_publish(db: Database) -> None:
    """★ 证据必须真实可反查。"""
    db.upsert_video(bvid="BV1test00001", aid=111)
    draft_id = db.create_draft(
        draft_type=TOP_ACTION, bvid="BV1test00001", text="没有证据的评论", evidence_ids=[]
    )
    db.set_draft_status(draft_id, "approved")
    executor = RecordingExecutor()
    gate = make_gate(db, publish_ready_config(), executor=executor)
    result = await gate.publish(draft_id=draft_id)
    assert result.ok is False
    assert result.code == "EVIDENCE_MISSING"
    assert executor.calls == []


async def test_draft_with_foreign_evidence_cannot_publish(db: Database) -> None:
    db.upsert_video(bvid="BV1test00001", aid=111)
    db.upsert_video(bvid="BV1test00002", aid=222)
    foreign = db.add_evidence(bvid="BV1test00002", source_type="subtitle", text="别的视频的")
    draft_id = db.create_draft(
        draft_type=TOP_ACTION, bvid="BV1test00001", text="张冠李戴", evidence_ids=[foreign]
    )
    db.set_draft_status(draft_id, "approved")
    executor = RecordingExecutor()
    gate = make_gate(db, publish_ready_config(), executor=executor)
    result = await gate.publish(draft_id=draft_id)
    assert result.ok is False
    assert result.code == "EVIDENCE_MISSING"
    assert executor.calls == []


# ---------------------------------------------------------------------------
# 门禁结果可审计
# ---------------------------------------------------------------------------


async def test_gate_result_records_all_checks(db: Database) -> None:
    """门禁必须留下可审计的检查项（失败时也返回）。"""
    draft_id = seed_draft(db)
    config = publish_ready_config()
    gate = make_gate(db, config, executor=RecordingExecutor(fail_with=TimeoutError("x")))
    result = await gate.publish(draft_id=draft_id)
    assert result.gates
    for key in ("mode", "dry_run", "publish_enabled", "pause", "platform_health"):
        assert key in result.gates, key
