"""并发与重跑测试（任务书 §7 任务 5/7 明确要求）。

覆盖任务书点名的边界：

* **并发任务**：两个请求同时想对同一目标发言 → 只能有一个成功。
* **两次同时唤醒**：共享单实例锁，只有一个能跑。
* **重启后重跑 / 停机错过一次**：第二轮不把已处理视频当新内容，也不重复发言。
* **日报重复生成**：不产生互相覆盖或权限误判。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.lock import LockError, TaskLock
from core.publish import PublishGate
from core.storage import TOP_ACTION, Database
from tests.conftest import make_config
from tests.test_publish_gates import (
    AGENT_MID,
    FakeReader,
    RecordingExecutor,
    publish_ready_config,
    seed_draft,
)

# ---------------------------------------------------------------------------
# 并发布同一目标 → 只能一个成功
# ---------------------------------------------------------------------------


async def test_concurrent_publish_of_same_draft_only_one_succeeds(db: Database) -> None:
    """★ 两个协程同时对**同一个草稿**发布 → 只有一个真正提交。"""
    draft_id = seed_draft(db)
    executor = RecordingExecutor()
    gate = make_gate(db, publish_ready_config(), executor)

    results = await asyncio.gather(
        gate.publish(draft_id=draft_id),
        gate.publish(draft_id=draft_id),
        return_exceptions=True,
    )
    oks = [r for r in results if not isinstance(r, BaseException) and r.ok]
    assert len(oks) == 1, f"应当只有一个成功，实际 {len(oks)}"
    assert len(executor.calls) == 1, "平台侧只能有一次真实提交"


async def test_concurrent_publish_of_same_bvid_only_one_succeeds(db: Database) -> None:
    """★ 两个**不同草稿**但同一 BV（幂等键相同）→ 也只能一个成功。"""
    db.upsert_video(bvid="BV1test00001", aid=111, title="t")
    eid1 = db.add_evidence(bvid="BV1test00001", source_type="subtitle", text="片段一")
    eid2 = db.add_evidence(bvid="BV1test00001", source_type="subtitle", text="片段二")

    first = db.create_draft(
        draft_type=TOP_ACTION, bvid="BV1test00001", text="第一条", evidence_ids=[eid1]
    )
    second = db.create_draft(
        draft_type=TOP_ACTION, bvid="BV1test00001", text="第二条", evidence_ids=[eid2]
    )
    db.set_draft_status(first, "approved")
    db.set_draft_status(second, "approved")

    executor = RecordingExecutor()
    gate = make_gate(db, publish_ready_config(), executor)

    results = await asyncio.gather(
        gate.publish(draft_id=first),
        gate.publish(draft_id=second),
        return_exceptions=True,
    )
    oks = [r for r in results if not isinstance(r, BaseException) and r.ok]
    assert len(oks) == 1, "同 BV 的幂等键必须挡住其中一个"
    assert len(executor.calls) == 1


async def test_concurrent_publish_respects_quota(db: Database) -> None:
    """并发也不能超额：配额为 1 时两个不同 BV 只能发一个。"""
    db.upsert_video(bvid="BV1a0000001", aid=1, title="t")
    db.upsert_video(bvid="BV1b0000002", aid=2, title="t")
    eid_a = db.add_evidence(bvid="BV1a0000001", source_type="subtitle", text="a")
    eid_b = db.add_evidence(bvid="BV1b0000002", source_type="subtitle", text="b")

    draft_a = db.create_draft(
        draft_type=TOP_ACTION, bvid="BV1a0000001", text="A", evidence_ids=[eid_a]
    )
    draft_b = db.create_draft(
        draft_type=TOP_ACTION, bvid="BV1b0000002", text="B", evidence_ids=[eid_b]
    )
    db.set_draft_status(draft_a, "approved")
    db.set_draft_status(draft_b, "approved")

    executor = RecordingExecutor()
    config = publish_ready_config(max_top_comments_per_day=1)
    gate = make_gate(db, config, executor)

    results = await asyncio.gather(
        gate.publish(draft_id=draft_a),
        gate.publish(draft_id=draft_b),
        return_exceptions=True,
    )
    oks = [r for r in results if not isinstance(r, BaseException) and r.ok]
    assert len(oks) == 1, f"配额为 1 时并发也只能成功一个，实际 {len(oks)}"
    assert len(executor.calls) == 1


def make_gate(db: Database, config: Any, executor: Any) -> PublishGate:
    return PublishGate(db=db, config=config, reader=FakeReader(mid=AGENT_MID), executor=executor)


# ---------------------------------------------------------------------------
# 单实例锁：两次同时唤醒
# ---------------------------------------------------------------------------


def test_two_simultaneous_wakeups_only_one_acquires(tmp_path: Path) -> None:
    """★ 任务书 §7 任务 7：两次同时唤醒 → 共享锁只能一个进。"""
    path = tmp_path / "run.lock"
    first = TaskLock(path, purpose="wake-1")
    first.acquire()
    try:
        second = TaskLock(path, purpose="wake-2", take_over_stale=False)
        with pytest.raises(LockError):
            second.acquire()
    finally:
        first.release()

    # 第一个释放后，第二个可以进（不永久卡死）
    third = TaskLock(path, purpose="wake-3")
    third.acquire()
    third.release()


# ---------------------------------------------------------------------------
# 重跑 / 停机错过
# ---------------------------------------------------------------------------


def test_restart_after_reserved_does_not_double_post(db: Database) -> None:
    """★ 重启后重跑：上一轮留下的 ``reserved`` 必须冻结，不能重发。"""
    draft_id = seed_draft(db)
    # 模拟"点击前已预留，然后进程死了"
    db.reserve_action(
        agent_mid=AGENT_MID, action_type=TOP_ACTION, bvid="BV1test00001",
        target_rpid=None, draft_id=draft_id, is_simulated=False,
    )
    executor = RecordingExecutor()
    gate = make_gate(db, publish_ready_config(), executor)
    result = asyncio.run(gate.publish(draft_id=draft_id))
    assert result.ok is False
    assert executor.calls == [], "重启后不得重发"


def test_rerun_produces_different_run_ids(db: Database) -> None:
    """★ 验收：连续两轮产生不同 ``run_id``。"""
    first = db.start_run("schedule")
    db.finish_run(first, conclusion="ok")
    second = db.start_run("schedule")
    db.finish_run(second, conclusion="ok")
    assert first != second
    assert len(db.latest_runs(limit=5)) == 2


def test_second_run_does_not_treat_processed_as_new(db: Database) -> None:
    """★ 验收：第二轮不把已处理视频当新内容。"""
    assert db.upsert_video(bvid="BV1test00001", aid=111, title="t") is True
    db.set_video_status("BV1test00001", "briefed")
    # 第二轮再次"发现"同一 BV
    assert db.upsert_video(bvid="BV1test00001", aid=111, title="t") is False
    assert str(db.get_video("BV1test00001")["status"]) == "briefed", "状态不能被重置"


def test_missed_run_does_not_duplicate_publish(db: Database) -> None:
    """★ 停机错过一轮后重跑：已发送的目标不会被再次发送。"""
    draft_id = seed_draft(db)
    executor = RecordingExecutor()
    gate = make_gate(db, publish_ready_config(), executor)

    first = asyncio.run(gate.publish(draft_id=draft_id))
    assert first.ok is True

    # 模拟"停机后重新唤醒，又跑到同一个目标"
    again = asyncio.run(gate.publish(draft_id=draft_id))
    assert again.ok is False
    assert len(executor.calls) == 1


# ---------------------------------------------------------------------------
# 日报重复生成
# ---------------------------------------------------------------------------


def test_repeated_digest_generation_is_safe(db: Database, tmp_path: Path) -> None:
    """同一天多次生成日报：覆盖同一文件 + 追加历史，不报错。"""
    from core.report import DigestInput, DigestWriter

    writer = DigestWriter(db=db, out_dir=tmp_path / "digests")
    for _ in range(3):
        writer.write(DigestInput(run_id="run_same", trigger_source="schedule"))

    files = list((tmp_path / "digests").glob("digest-*.md"))
    assert len(files) == 1, "同一天只应有一个日报文件（覆盖语义）"
    history = (tmp_path / "digests" / "digest-history.jsonl").read_text(encoding="utf-8")
    assert len(history.strip().splitlines()) == 3, "历史必须逐次追加"


def test_digest_after_uncertain_keeps_freeze_visible(db: Database, tmp_path: Path) -> None:
    """重复生成日报时，uncertain 冻结信息不能被丢掉。"""
    from core.report import DigestInput, DigestWriter

    db.upsert_video(bvid="BV1test00001", aid=111)
    action_id, _ = db.reserve_action(
        agent_mid=AGENT_MID, action_type=TOP_ACTION, bvid="BV1test00001",
        target_rpid=None, draft_id=None, is_simulated=False,
    )
    db.settle_action(action_id=action_id, status="uncertain", error_code="TIMEOUT")

    writer = DigestWriter(db=db, out_dir=tmp_path / "digests")
    writer.write(DigestInput(run_id="run_1", trigger_source="schedule"))
    second = writer.write(DigestInput(run_id="run_2", trigger_source="schedule"))
    text = second.read_text(encoding="utf-8")
    assert "不确定动作" in text
    assert "不自动重发" in text


# ---------------------------------------------------------------------------
# 批次中途出现验证码
# ---------------------------------------------------------------------------


async def test_captcha_mid_batch_stops_remaining_work(db: Database) -> None:
    """★ 任务书 §7 任务 7：验证码发生在批次中途 → 后续请求必须立刻停。

    做法：第一个视频正常读完；读**第二个**视频时平台返回验证码提示。
    断言：健康状态立即转为 verification_required，且**不再发出任何新请求**。
    """
    import httpx

    from core import health
    from core.bilibili import BilibiliReader, BilibiliReadError
    from core.pipeline import Pipeline
    from core.ratelimit import RequestQueue
    from tests.conftest import FakeClock, build_client, nav_payload, ok_payload

    calls: list[str] = []
    view_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal view_count
        url = str(request.url)
        calls.append(url)

        if "view" in url:
            view_count += 1
            if view_count >= 2:
                # 第二个视频：平台要求人机验证
                return httpx.Response(
                    200, json={"code": 0, "message": "请完成验证", "data": {}}
                )
            return httpx.Response(
                200,
                json=ok_payload(
                    {
                        "bvid": "BV1test00001", "aid": 111, "cid": 222,
                        "title": "t", "desc": "简介", "pubdate": 1,
                        "duration": 10, "owner": {"mid": 1, "name": "u"},
                        "stat": {"reply": 0}, "pages": [{"cid": 222}],
                    }
                ),
            )
        if "nav" in url:
            # 字幕需要 WBI 签名；先让签名 key 就绪
            return httpx.Response(200, json=nav_payload())
        # 字幕探测：第一个视频无字幕（走降级路径）
        return httpx.Response(200, json=ok_payload({"subtitle": {"subtitles": []}}))

    clock = FakeClock()
    config = make_config(bilibili_requests={"min_interval_seconds": 1.0})
    queue = RequestQueue(
        db=db, config=config, client=build_client(handler), clock=clock, sleep=clock.sleep
    )
    reader = BilibiliReader(queue=queue, cookies={})
    pipeline = Pipeline(db=db, config=config, reader=reader, queue=queue, worker=None)

    # 第一个视频：正常读完（不发验证码）
    card, _blocks = await pipeline.read_video("BV1test00001")
    assert card.evidence_level == "description_only"
    assert db.health_state() == health.READY

    # 第二个视频：撞上验证码。此时平台返回 code=0 但 message 含"请完成验证"，
    # 因此健康状态被置为 verification_required，读取本身失败并给出明确错误码。
    before = len(calls)
    with pytest.raises(BilibiliReadError) as excinfo:
        await pipeline.read_video("BV1test00002")
    assert "VERIFICATION" in excinfo.value.code.upper()

    assert db.health_state() == health.VERIFICATION_REQUIRED, "必须进入人工恢复状态"
    assert len(calls) > before, "确实发生了请求"

    # ★ 关键：进入 verification_required 后，**任何新的请求都被阻止**。
    #   注意：阻断只针对**新请求**；已缓存内容仍可读（这是刻意的，
    #   否则一次验证码会把主人永久卡死，连已经抓到的材料都看不了）。
    from core.ratelimit import RequestsBlocked

    after = len(calls)
    with pytest.raises(RequestsBlocked):
        # 一个从未读过的 BV → 必然要发请求 → 必须被阻断
        await pipeline.read_video("BV1test00003")
    assert len(calls) == after, "阻断状态下不得再发出任何请求"

    # ★ 且**不会**自动恢复（计时结束也不行）
    assert db.health_state() == health.VERIFICATION_REQUIRED

    # 人工入口之外没有别的恢复途径
    assert db.recover_health(recovered_by="test:human") is True
    assert db.health_state() == health.READY
