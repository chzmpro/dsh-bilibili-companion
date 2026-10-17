"""存储层测试：WAL、可重复迁移、幂等键、``uncertain`` 占额度、模拟与真实分离。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.storage import REPLY_ACTION, TOP_ACTION, Database, now_ts


def test_migrate_is_repeatable(db: Database) -> None:
    """迁移可重复运行（任务书 §5）。"""
    first = db.migrate()
    second = db.migrate()
    assert first == second
    assert db.schema_version() == first


def test_wal_mode_enabled(db: Database) -> None:
    row = db.query_one("PRAGMA journal_mode")
    assert str(row[0]).lower() == "wal"


def test_required_tables_exist(db: Database) -> None:
    """任务书 §5 要求的表必须都在。"""
    expected = {
        "videos", "evidence", "notes", "drafts", "actions",
        "platform_health", "seen_replies", "runs", "text_jobs", "feedback",
        "http_cache", "schema_version",
    }
    rows = db.query("SELECT name FROM sqlite_master WHERE type='table'")
    names = {str(row["name"]) for row in rows}
    assert expected <= names


def test_migration_preserves_data(tmp_path: Path) -> None:
    database = Database(tmp_path / "x.db")
    database.migrate()
    database.upsert_video(bvid="BV1a", aid=1, title="t")
    database.migrate()
    assert database.get_video("BV1a") is not None
    database.close()


# ---- videos 去重 -----------------------------------------------------------


def test_upsert_video_reports_newness(db: Database) -> None:
    assert db.upsert_video(bvid="BV1a", aid=1, title="A") is True
    assert db.upsert_video(bvid="BV1a", aid=1, title="A") is False


def test_upsert_does_not_reset_status(db: Database) -> None:
    """重复发现不能把已处理状态打回 discovered（否则会重复建笔记）。"""
    db.upsert_video(bvid="BV1a", aid=1, title="A")
    db.set_video_status("BV1a", "briefed")
    db.upsert_video(bvid="BV1a", aid=1, title="A")
    assert str(db.get_video("BV1a")["status"]) == "briefed"


def test_upsert_fills_missing_metadata(db: Database) -> None:
    db.upsert_video(bvid="BV1a", aid=1, title="")
    db.upsert_video(bvid="BV1a", aid=1, title="补上的标题", up_name="UP")
    row = db.get_video("BV1a")
    assert str(row["title"]) == "补上的标题"
    assert str(row["up_name"]) == "UP"


# ---- evidence --------------------------------------------------------------


def test_evidence_id_is_deterministic_and_idempotent(db: Database) -> None:
    db.upsert_video(bvid="BV1a", aid=1)
    first = db.add_evidence(bvid="BV1a", source_type="subtitle", text="片段", start_sec=1.0)
    second = db.add_evidence(bvid="BV1a", source_type="subtitle", text="片段", start_sec=1.0)
    assert first == second


def test_evidence_distinguishes_timestamps(db: Database) -> None:
    db.upsert_video(bvid="BV1a", aid=1)
    a = db.add_evidence(bvid="BV1a", source_type="subtitle", text="同样文本", start_sec=1.0)
    b = db.add_evidence(bvid="BV1a", source_type="subtitle", text="同样文本", start_sec=9.0)
    assert a != b


def test_verify_evidence_ids_all_must_belong_to_bvid(db: Database) -> None:
    db.upsert_video(bvid="BV1a", aid=1)
    db.upsert_video(bvid="BV1b", aid=2)
    ev_a = db.add_evidence(bvid="BV1a", source_type="description", text="A 的简介")
    ev_b = db.add_evidence(bvid="BV1b", source_type="description", text="B 的简介")

    assert db.verify_evidence_ids("BV1a", [ev_a]) == (True, "OK")
    assert db.verify_evidence_ids("BV1a", [ev_b]) == (False, "EVIDENCE_BVID_MISMATCH")
    assert db.verify_evidence_ids("BV1a", ["ev_nonexistent"]) == (False, "EVIDENCE_NOT_FOUND")
    assert db.verify_evidence_ids("BV1a", []) == (False, "EVIDENCE_REQUIRED")


def test_excerpt_is_bounded(db: Database) -> None:
    db.upsert_video(bvid="BV1a", aid=1)
    long_text = "长" * 5000
    eid = db.add_evidence(bvid="BV1a", source_type="subtitle", text=long_text)
    excerpt = db.excerpt(eid, 500)
    assert excerpt is not None
    assert len(excerpt["excerpt"]) == 500
    assert excerpt["truncated"] is True
    assert excerpt["excerpt_chars"] == 500


def test_excerpt_missing_returns_none(db: Database) -> None:
    assert db.excerpt("ev_missing", 500) is None


# ---- 幂等键 ----------------------------------------------------------------


def test_idempotency_key_top_and_reply_shapes(db: Database) -> None:
    """任务书 §5 的两种键形状。"""
    top = db.make_idempotency_key(
        agent_mid=1001, action_type=TOP_ACTION, bvid="BV1a", target_rpid=None
    )
    same = db.make_idempotency_key(
        agent_mid=1001, action_type=TOP_ACTION, bvid="BV1a", target_rpid=None
    )
    other_bv = db.make_idempotency_key(
        agent_mid=1001, action_type=TOP_ACTION, bvid="BV1b", target_rpid=None
    )
    assert top == same
    assert top != other_bv

    reply = db.make_idempotency_key(
        agent_mid=1001, action_type=REPLY_ACTION, bvid="BV1a", target_rpid="999"
    )
    reply_same_target_other_bv = db.make_idempotency_key(
        agent_mid=1001, action_type=REPLY_ACTION, bvid="BV1b", target_rpid="999"
    )
    # 回复键只看 agent_mid + target_rpid
    assert reply == reply_same_target_other_bv


def test_idempotency_key_is_unique_per_agent(db: Database) -> None:
    a = db.make_idempotency_key(
        agent_mid=1001, action_type=TOP_ACTION, bvid="BV1a", target_rpid=None
    )
    b = db.make_idempotency_key(
        agent_mid=2002, action_type=TOP_ACTION, bvid="BV1a", target_rpid=None
    )
    assert a != b


def test_unknown_action_type_rejected(db: Database) -> None:
    """未知动作类型必须报错（like/favorite/dynamic 现在是**合法**的）。"""
    with pytest.raises(ValueError, match="未知 action_type"):
        db.make_idempotency_key(
            agent_mid=1, action_type="definitely_not_a_real_action", bvid="BV1a",
            target_rpid=None,
        )


def test_like_favorite_dynamic_keys_are_distinct(db: Database) -> None:
    """★ 点赞/收藏/评论/动态 的幂等键必须互不相同。"""
    keys = {
        action: db.make_idempotency_key(
            agent_mid=1001, action_type=action, bvid="BV1a", target_rpid="x"
        )
        for action in ("top", "reply", "like", "favorite", "dynamic")
    }
    assert len(set(keys.values())) == 5, f"幂等键碰撞：{keys}"


def test_like_key_is_per_video(db: Database) -> None:
    """★ 同一视频只能赞一次：同 agent + 同 bvid → 同键。"""
    first = db.make_idempotency_key(
        agent_mid=1001, action_type="like", bvid="BV1a", target_rpid=None
    )
    again = db.make_idempotency_key(
        agent_mid=1001, action_type="like", bvid="BV1a", target_rpid=None
    )
    other = db.make_idempotency_key(
        agent_mid=1001, action_type="like", bvid="BV1b", target_rpid=None
    )
    assert first == again
    assert first != other


# ---- reserved / uncertain --------------------------------------------------


def test_reserve_then_settle(db: Database) -> None:
    db.upsert_video(bvid="BV1a", aid=1)
    action_id, outcome = db.reserve_action(
        agent_mid=1001, action_type=TOP_ACTION, bvid="BV1a",
        target_rpid=None, draft_id=None, is_simulated=False,
    )
    assert outcome == "reserved"
    row = db.find_action_by_key(
        db.make_idempotency_key(
            agent_mid=1001, action_type=TOP_ACTION, bvid="BV1a", target_rpid=None
        )
    )
    assert str(row["status"]) == "reserved"

    db.settle_action(action_id=action_id, status="sent_verified", remote_rpid="123")
    row = db.find_action_by_key(
        db.make_idempotency_key(
            agent_mid=1001, action_type=TOP_ACTION, bvid="BV1a", target_rpid=None
        )
    )
    assert str(row["status"]) == "sent_verified"
    assert str(row["remote_rpid"]) == "123"


def test_second_reserve_is_duplicate_after_success(db: Database) -> None:
    db.upsert_video(bvid="BV1a", aid=1)
    action_id, _ = db.reserve_action(
        agent_mid=1001, action_type=TOP_ACTION, bvid="BV1a",
        target_rpid=None, draft_id=None, is_simulated=False,
    )
    db.settle_action(action_id=action_id, status="sent_verified", remote_rpid="1")
    _second_id, outcome = db.reserve_action(
        agent_mid=1001, action_type=TOP_ACTION, bvid="BV1a",
        target_rpid=None, draft_id=None, is_simulated=False,
    )
    assert outcome == "duplicate"


def test_stale_reserved_becomes_uncertain_not_retried(db: Database) -> None:
    """预留后进程崩溃 → 再次预留必须转 uncertain 并冻结，**不自动重发**。"""
    db.upsert_video(bvid="BV1a", aid=1)
    db.reserve_action(
        agent_mid=1001, action_type=TOP_ACTION, bvid="BV1a",
        target_rpid=None, draft_id=None, is_simulated=False,
    )
    _action_id, outcome = db.reserve_action(
        agent_mid=1001, action_type=TOP_ACTION, bvid="BV1a",
        target_rpid=None, draft_id=None, is_simulated=False,
    )
    assert outcome == "uncertain_pending"
    row = db.find_action_by_key(
        db.make_idempotency_key(
            agent_mid=1001, action_type=TOP_ACTION, bvid="BV1a", target_rpid=None
        )
    )
    assert str(row["status"]) == "uncertain"


def test_uncertain_stays_frozen_on_repeat(db: Database) -> None:
    db.upsert_video(bvid="BV1a", aid=1)
    action_id, _ = db.reserve_action(
        agent_mid=1001, action_type=TOP_ACTION, bvid="BV1a",
        target_rpid=None, draft_id=None, is_simulated=False,
    )
    db.settle_action(action_id=action_id, status="uncertain", error_code="TIMEOUT")
    for _ in range(3):
        _id, outcome = db.reserve_action(
            agent_mid=1001, action_type=TOP_ACTION, bvid="BV1a",
            target_rpid=None, draft_id=None, is_simulated=False,
        )
        assert outcome == "uncertain_pending"


def test_settle_rejects_illegal_status(db: Database) -> None:
    with pytest.raises(ValueError, match="非法终态"):
        db.settle_action(action_id="x", status="reserved")


# ---- 配额 ------------------------------------------------------------------


def test_uncertain_consumes_quota(db: Database) -> None:
    """★ 任务书 §5：``uncertain`` 占用额度，不靠时间自动释放。"""
    db.upsert_video(bvid="BV1a", aid=1)
    action_id, _ = db.reserve_action(
        agent_mid=1001, action_type=TOP_ACTION, bvid="BV1a",
        target_rpid=None, draft_id=None, is_simulated=False,
    )
    assert db.count_actions_today(action_type=TOP_ACTION, is_simulated=False) == 1
    db.settle_action(action_id=action_id, status="uncertain")
    assert db.count_actions_today(action_type=TOP_ACTION, is_simulated=False) == 1


def test_reserved_consumes_quota(db: Database) -> None:
    db.upsert_video(bvid="BV1a", aid=1)
    db.reserve_action(
        agent_mid=1001, action_type=TOP_ACTION, bvid="BV1a",
        target_rpid=None, draft_id=None, is_simulated=False,
    )
    assert db.count_actions_today(action_type=TOP_ACTION, is_simulated=False) == 1


def test_simulated_actions_counted_separately(db: Database) -> None:
    """★ 模拟与真实**分开计数**。"""
    db.upsert_video(bvid="BV1a", aid=1)
    db.upsert_video(bvid="BV1b", aid=2)

    db.reserve_action(
        agent_mid=1001, action_type=TOP_ACTION, bvid="BV1a",
        target_rpid=None, draft_id=None, is_simulated=True,
    )
    db.reserve_action(
        agent_mid=1001, action_type=TOP_ACTION, bvid="BV1b",
        target_rpid=None, draft_id=None, is_simulated=False,
    )
    assert db.count_actions_today(action_type=TOP_ACTION, is_simulated=True) == 1
    assert db.count_actions_today(action_type=TOP_ACTION, is_simulated=False) == 1


def test_failed_action_does_not_consume_quota(db: Database) -> None:
    db.upsert_video(bvid="BV1a", aid=1)
    action_id, _ = db.reserve_action(
        agent_mid=1001, action_type=TOP_ACTION, bvid="BV1a",
        target_rpid=None, draft_id=None, is_simulated=False,
    )
    db.settle_action(action_id=action_id, status="failed", error_code="COMMENT_CLOSED")
    assert db.count_actions_today(action_type=TOP_ACTION, is_simulated=False) == 0


# ---- 健康状态 --------------------------------------------------------------


def test_health_state_defaults_ready(db: Database) -> None:
    assert db.health_state() == "ready"
    assert db.current_health() is None


def test_health_event_blocks_until_human_recovery(db: Database) -> None:
    db.record_health(state="verification_required", raw_code="-352", category="captcha")
    assert db.health_state() == "verification_required"
    assert db.recover_health(recovered_by="cli:resume") is True
    # 恢复后仍然**保留**历史记录（可审计）
    assert db.current_health() is None
    rows = db.query("SELECT * FROM platform_health")
    assert len(rows) == 1
    assert str(rows[0]["recovered_by"]) == "cli:resume"


def test_recover_without_event_returns_false(db: Database) -> None:
    assert db.recover_health(recovered_by="cli:resume") is False


def test_latest_health_wins(db: Database) -> None:
    db.record_health(state="rate_limited", category="a")
    db.record_health(state="auth_expired", category="b")
    assert db.health_state() == "auth_expired"


# ---- seen_replies ----------------------------------------------------------


def test_mark_seen_reply_is_dedup_by_rpid(db: Database) -> None:
    db.upsert_video(bvid="BV1a", aid=1)
    assert db.mark_seen_reply(remote_rpid="1", bvid="BV1a") is True
    assert db.mark_seen_reply(remote_rpid="1", bvid="BV1a") is False


def test_seen_reply_truncates_excerpt(db: Database) -> None:
    db.upsert_video(bvid="BV1a", aid=1)
    db.mark_seen_reply(remote_rpid="1", bvid="BV1a", text_excerpt="x" * 2000)
    row = db.query_one("SELECT text_excerpt FROM seen_replies WHERE remote_rpid = '1'")
    assert len(str(row["text_excerpt"])) == 500


# ---- runs / text_jobs / feedback -------------------------------------------


def test_run_lifecycle(db: Database) -> None:
    run_id = db.start_run("manual")
    assert db.get_run(run_id) is not None
    db.finish_run(run_id, candidates_n=3, read_n=2, conclusion="ok")
    row = db.get_run(run_id)
    assert row is not None
    assert int(row["candidates_n"]) == 3
    assert str(row["conclusion"]) == "ok"


def test_text_job_cache_key_includes_all_four_parts(db: Database) -> None:
    """缓存键 = bvid + 原文哈希 + 模型 ID + 提示版本。"""
    common = {
        "bvid": "BV1a",
        "input_sha256": "hash1",
        "model_id": "doubao-x",
        "prompt_version": "v1",
        "status": "ok",
    }
    db.record_text_job(**common)
    assert db.find_cached_text_job(
        bvid="BV1a", input_sha256="hash1", model_id="doubao-x", prompt_version="v1"
    ) is not None
    # 原文变了 → 缓存必须失效
    assert db.find_cached_text_job(
        bvid="BV1a", input_sha256="hash2", model_id="doubao-x", prompt_version="v1"
    ) is None
    # 模型变了 → 失效
    assert db.find_cached_text_job(
        bvid="BV1a", input_sha256="hash1", model_id="doubao-y", prompt_version="v1"
    ) is None
    # 提示版本变了 → 失效
    assert db.find_cached_text_job(
        bvid="BV1a", input_sha256="hash1", model_id="doubao-x", prompt_version="v2"
    ) is None


def test_text_job_totals(db: Database) -> None:
    db.record_text_job(
        bvid="BV1a", input_sha256="h", model_id="m", prompt_version="v1",
        status="ok", prompt_tokens=100, completion_tokens=20,
    )
    totals = db.text_job_totals()
    assert totals["prompt_tokens"] == 100
    assert totals["completion_tokens"] == 20
    assert totals["jobs"] == 1


def test_feedback_upsert(db: Database) -> None:
    db.upsert_video(bvid="BV1a", aid=1)
    db.add_feedback(bvid="BV1a", verdict="useful")
    assert db.feedback_map()["BV1a"] == "useful"


def test_feedback_rejects_unknown_verdict_via_schema(db: Database) -> None:
    db.upsert_video(bvid="BV1a", aid=1)
    with pytest.raises(sqlite3.IntegrityError):
        db.add_feedback(bvid="BV1a", verdict="maybe")


# ---- 草稿 ------------------------------------------------------------------


def test_create_and_approve_draft(db: Database) -> None:
    db.upsert_video(bvid="BV1a", aid=1)
    eid = db.add_evidence(bvid="BV1a", source_type="description", text="简介")
    draft_id = db.create_draft(
        draft_type=TOP_ACTION, bvid="BV1a", text="草稿正文", evidence_ids=[eid]
    )
    row = db.get_draft(draft_id)
    assert str(row["status"]) == "pending"
    assert row["approved_at"] is None

    assert db.approve_draft(draft_id) is True
    row = db.get_draft(draft_id)
    assert str(row["status"]) == "approved"
    assert row["approved_at"] is not None


def test_approve_missing_draft_returns_false(db: Database) -> None:
    assert db.approve_draft("draft_nope") is False


def test_draft_status_constraint(db: Database) -> None:
    db.upsert_video(bvid="BV1a", aid=1)
    eid = db.add_evidence(bvid="BV1a", source_type="description", text="简介")
    draft_id = db.create_draft(
        draft_type=TOP_ACTION, bvid="BV1a", text="t", evidence_ids=[eid]
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.set_draft_status(draft_id, "totally-invalid-status")


def test_actions_table_has_unique_idempotency_key(db: Database) -> None:
    db.upsert_video(bvid="BV1a", aid=1)
    key = db.make_idempotency_key(
        agent_mid=1001, action_type=TOP_ACTION, bvid="BV1a", target_rpid=None
    )
    db.execute(
        "INSERT INTO actions (action_id, agent_mid, action_type, bvid, idempotency_key,"
        " status, created_at, updated_at) VALUES ('a1',1001,'top','BV1a',?, 'draft',?,?)",
        (key, now_ts(), now_ts()),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO actions (action_id, agent_mid, action_type, bvid, idempotency_key,"
            " status, created_at, updated_at) VALUES ('a2',1001,'top','BV1a',?, 'draft',?,?)",
            (key, now_ts(), now_ts()),
        )
