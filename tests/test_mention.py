"""主号汇报（``@`` 提及）测试 —— **纯 API 核验**。

**核心不变量**：``verified`` 只能在读到 ``content.members[]`` 里
确有 ``owner_mid`` 时出现。正文里的 ``@昵称`` / ``@数字`` **不算**证据。

★ 本模块证明：任务书假设"必须用浏览器才能核验提及"**不成立** ——
B站把"这条评论 @ 到了谁"作为服务端事实放在 ``content.members[]`` 里，
因此可以纯 API 核验，与"定时 preset 不给通用浏览器"的约束相容。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.bilibili import AtMember, CommentItem, _parse_at_members
from core.mention import (
    HIGH_VALUE_LIKES,
    NICHE_LIKES,
    ApiMentionVerifier,
    MentionPlan,
    build_mention_text,
    classify_recommendation,
    extract_mention_candidates,
    looks_like_numeric_uid,
    try_mention,
    verify_from_comments,
)
from core.storage import Database

OWNER_MID = 660490601
OWNER_NAME = "沐雨林心"


def comment(
    *,
    rpid: str = "100",
    message: str = "@沐雨林心 好视频",
    at: list[tuple[int, str]] | None = None,
) -> CommentItem:
    return CommentItem(
        rpid=rpid,
        root="0",
        parent="0",
        author_mid=3747570769725629,
        author_name="干饭不干事的小鲸鱼",
        message=message,
        like_count=0,
        created_at=0,
        at_members=tuple(AtMember(mid=m, uname=n) for m, n in (at or [])),
    )


class FakeVerifier:
    """可控的核验器（模拟"读回评论的 members"）。"""

    def __init__(self, members: tuple[AtMember, ...] = (), *, boom: bool = False) -> None:
        self.members = members
        self.boom = boom
        self.calls: list[dict[str, Any]] = []

    async def read_at_members(self, *, bvid: str, rpid: str) -> tuple[AtMember, ...]:
        self.calls.append({"bvid": bvid, "rpid": rpid})
        if self.boom:
            raise RuntimeError("读回失败")
        return self.members


def seed(db: Database, bvid: str = "BV1test00001") -> str:
    db.upsert_video(bvid=bvid, aid=111, title="t")
    return bvid


# ---------------------------------------------------------------------------
# content.members 解析（★ 这是整个方案的基础）
# ---------------------------------------------------------------------------


def test_parse_at_members_reads_server_fact() -> None:
    """★ ``content.members[]`` 是 B站给出的"@ 到了谁"的**服务端事实**。"""
    content = {
        "message": "@MsGsrs @HJQC",
        "members": [
            {"mid": 282831595, "uname": "MsGsrs"},
            {"mid": 351873378, "uname": "HJQC"},
        ],
    }
    members = _parse_at_members(content)
    assert [m.mid for m in members] == [282831595, 351873378]
    assert [m.uname for m in members] == ["MsGsrs", "HJQC"]


@pytest.mark.parametrize(
    "content",
    [None, {}, {"members": None}, {"members": "x"}, {"members": []}, {"members": [None, 1]}],
)
def test_parse_at_members_handles_missing_or_malformed(content: Any) -> None:
    assert _parse_at_members(content) == ()


def test_parse_at_members_skips_bad_mid() -> None:
    content = {"members": [{"mid": "abc"}, {"mid": 0}, {"mid": -1}, {"mid": 5, "uname": "ok"}]}
    assert [m.mid for m in _parse_at_members(content)] == [5]


def test_comment_mentions_helper() -> None:
    c = comment(at=[(OWNER_MID, OWNER_NAME)])
    assert c.mentions(OWNER_MID) is True
    assert c.mentions(999) is False


def test_comment_without_members_mentions_nothing() -> None:
    """★ 正文写了 @昵称，但 members 为空 → **不算**提及成功。"""
    c = comment(message=f"@{OWNER_NAME} 好视频", at=[])
    assert c.mentions(OWNER_MID) is False


# ---------------------------------------------------------------------------
# 判定规则（双峰）
# ---------------------------------------------------------------------------


def test_high_like_is_high_value() -> None:
    plan = classify_recommendation(like_count=HIGH_VALUE_LIKES, score_total=60)
    assert plan.should_mention is True
    assert plan.tier == "high_value"


def test_low_like_with_good_score_is_niche() -> None:
    plan = classify_recommendation(like_count=10, score_total=90)
    assert plan.should_mention is True
    assert plan.tier == "niche"


def test_low_like_bad_score_not_recommended() -> None:
    assert classify_recommendation(like_count=10, score_total=30).should_mention is False


def test_mid_range_like_is_normal() -> None:
    plan = classify_recommendation(like_count=500, score_total=90)
    assert plan.should_mention is False
    assert plan.tier == "normal"


def test_boundary_values() -> None:
    assert classify_recommendation(like_count=NICHE_LIKES, score_total=75).should_mention
    assert not classify_recommendation(like_count=NICHE_LIKES + 1, score_total=75).should_mention
    assert classify_recommendation(like_count=HIGH_VALUE_LIKES, score_total=0).should_mention


def test_plan_is_frozen() -> None:
    import dataclasses

    plan = classify_recommendation(like_count=1, score_total=1)
    assert isinstance(plan, MentionPlan)
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.should_mention = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 文本构造
# ---------------------------------------------------------------------------


def test_build_mention_text_uses_nickname() -> None:
    """★ B站按**昵称**匹配提及，必须用昵称而不是数字 UID。"""
    text = build_mention_text(owner_name=OWNER_NAME, body="这个视频讲得很清楚")
    assert text.startswith(f"@{OWNER_NAME}")
    assert "这个视频讲得很清楚" in text


def test_build_mention_text_strips_extra_at() -> None:
    assert build_mention_text(owner_name="@沐雨林心", body="x").startswith("@沐雨林心")


def test_build_mention_text_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="owner_name"):
        build_mention_text(owner_name="   ", body="x")


def test_extract_candidates_is_diagnostic_only() -> None:
    """★ 抽取到的候选只是**诊断信息**，不能当成功依据。"""
    assert extract_mention_candidates("谢谢 @2002 和 @小鲸鱼") == ["2002", "小鲸鱼"]
    assert extract_mention_candidates("没有提及") == []


def test_numeric_uid_shape() -> None:
    assert looks_like_numeric_uid("2002") is True
    assert looks_like_numeric_uid("小鲸鱼") is False


# ---------------------------------------------------------------------------
# verify_from_comments（离线核验）
# ---------------------------------------------------------------------------


def test_verify_verified_when_members_contains_owner() -> None:
    status, mid, _ = verify_from_comments(
        comments=[comment(rpid="7", at=[(OWNER_MID, OWNER_NAME)])],
        rpid="7",
        owner_mid=OWNER_MID,
    )
    assert status == "verified"
    assert mid == OWNER_MID


def test_verify_failed_when_mentions_someone_else() -> None:
    """★ 提到了**别人**（同名不同 UID）→ failed，并记录实际 UID。"""
    status, mid, detail = verify_from_comments(
        comments=[comment(rpid="7", at=[(999, "沐雨林心")])],
        rpid="7",
        owner_mid=OWNER_MID,
    )
    assert status == "failed"
    assert mid == 999
    assert "不含" in detail


def test_verify_uncertain_when_no_members() -> None:
    """★ 正文有 @昵称 但 members 为空 → 未产生提醒，不确认成功。"""
    status, mid, _ = verify_from_comments(
        comments=[comment(rpid="7", message=f"@{OWNER_NAME}", at=[])],
        rpid="7",
        owner_mid=OWNER_MID,
    )
    assert status == "uncertain"
    assert mid is None


def test_verify_uncertain_when_rpid_missing() -> None:
    status, mid, detail = verify_from_comments(
        comments=[comment(rpid="8")], rpid="7", owner_mid=OWNER_MID
    )
    assert status == "uncertain"
    assert mid is None
    assert "没找到" in detail


# ---------------------------------------------------------------------------
# try_mention：诚实状态机（纯 API）
# ---------------------------------------------------------------------------


async def test_verified_when_server_confirms(db: Database) -> None:
    """★ 服务端 members 含 owner_mid → verified。**不需要浏览器**。"""
    seed(db)
    verifier = FakeVerifier((AtMember(mid=OWNER_MID, uname=OWNER_NAME),))
    outcome = await try_mention(
        db=db, bvid="BV1test00001", owner_mid=OWNER_MID, owner_name=OWNER_NAME,
        text="好视频", rpid="100", verifier=verifier,
    )
    assert outcome.status == "verified"
    assert outcome.observed_mid == OWNER_MID
    assert verifier.calls, "必须真的读回评论"
    row = db.get_owner_notification(outcome.notification_id)
    assert str(row["status"]) == "verified"


async def test_failed_when_mentions_wrong_person(db: Database) -> None:
    """★ 提及了错误对象 → failed（必须如实报告）。"""
    seed(db)
    outcome = await try_mention(
        db=db, bvid="BV1test00001", owner_mid=OWNER_MID, owner_name=OWNER_NAME,
        text="x", rpid="100",
        verifier=FakeVerifier((AtMember(mid=999, uname="同名的人"),)),
    )
    assert outcome.status == "failed"
    assert outcome.observed_mid == 999
    row = db.get_owner_notification(outcome.notification_id)
    assert str(row["error_code"]) == "MENTION_TARGET_MISMATCH"


async def test_uncertain_when_members_empty(db: Database) -> None:
    """★ members 为空 → 正文的 @ 只是文字，未产生提醒 → uncertain。"""
    seed(db)
    outcome = await try_mention(
        db=db, bvid="BV1test00001", owner_mid=OWNER_MID, owner_name=OWNER_NAME,
        text=f"@{OWNER_NAME}", rpid="100", verifier=FakeVerifier(()),
    )
    assert outcome.status == "uncertain"
    row = db.get_owner_notification(outcome.notification_id)
    assert str(row["error_code"]) == "NO_AT_MEMBERS"


async def test_uncertain_when_not_yet_posted(db: Database) -> None:
    """评论还没发 → 无法核验，不能预先标成功。"""
    seed(db)
    outcome = await try_mention(
        db=db, bvid="BV1test00001", owner_mid=OWNER_MID, owner_name=OWNER_NAME,
        text="x", rpid=None, verifier=FakeVerifier((AtMember(OWNER_MID, OWNER_NAME),)),
    )
    assert outcome.status == "uncertain"
    row = db.get_owner_notification(outcome.notification_id)
    assert str(row["error_code"]) == "NOT_YET_POSTED"


async def test_uncertain_when_verifier_explodes(db: Database) -> None:
    seed(db)
    outcome = await try_mention(
        db=db, bvid="BV1test00001", owner_mid=OWNER_MID, owner_name=OWNER_NAME,
        text="x", rpid="100", verifier=FakeVerifier(boom=True),
    )
    assert outcome.status == "uncertain"
    row = db.get_owner_notification(outcome.notification_id)
    assert str(row["error_code"]) == "RuntimeError"


async def test_unsupported_when_no_owner_mid(db: Database) -> None:
    seed(db)
    outcome = await try_mention(
        db=db, bvid="BV1test00001", owner_mid=0, owner_name=OWNER_NAME, text="x", rpid="1"
    )
    assert outcome.status == "unsupported"


async def test_unsupported_when_no_owner_name(db: Database) -> None:
    """★ 没有昵称就无法构造提及（数字 UID 不产生提醒）→ unsupported。"""
    seed(db)
    outcome = await try_mention(
        db=db, bvid="BV1test00001", owner_mid=OWNER_MID, owner_name="  ",
        text="x", rpid="1", verifier=FakeVerifier(),
    )
    assert outcome.status == "unsupported"
    row = db.get_owner_notification(outcome.notification_id)
    assert str(row["error_code"]) == "OWNER_NAME_REQUIRED"


async def test_uncertain_when_no_verifier(db: Database) -> None:
    seed(db)
    outcome = await try_mention(
        db=db, bvid="BV1test00001", owner_mid=OWNER_MID, owner_name=OWNER_NAME,
        text="x", rpid="1", verifier=None,
    )
    assert outcome.status == "uncertain"
    row = db.get_owner_notification(outcome.notification_id)
    assert str(row["error_code"]) == "NO_VERIFIER"


async def test_mention_is_idempotent_per_bvid(db: Database) -> None:
    seed(db)
    verifier = FakeVerifier((AtMember(OWNER_MID, OWNER_NAME),))
    first = await try_mention(
        db=db, bvid="BV1test00001", owner_mid=OWNER_MID, owner_name=OWNER_NAME,
        text="x", rpid="1", verifier=verifier,
    )
    second = await try_mention(
        db=db, bvid="BV1test00001", owner_mid=OWNER_MID, owner_name=OWNER_NAME,
        text="x", rpid="1", verifier=verifier,
    )
    assert first.notification_id == second.notification_id
    assert len(db.list_owner_notifications()) == 1


async def test_all_non_verified_paths_warn(db: Database) -> None:
    """★ 非 verified 的结果都必须带"不得声称已通知"的提示。"""
    cases: list[tuple[int, str, str | None, FakeVerifier, str]] = [
        (0, OWNER_NAME, "1", FakeVerifier(), "unsupported"),
        (OWNER_MID, "  ", "1", FakeVerifier(), "unsupported"),
        (OWNER_MID, OWNER_NAME, None, FakeVerifier(), "uncertain"),
        (OWNER_MID, OWNER_NAME, "1", FakeVerifier(()), "uncertain"),
        (OWNER_MID, OWNER_NAME, "1", FakeVerifier((AtMember(1, "x"),)), "failed"),
    ]
    for index, (mid, name, rpid, verifier, expected) in enumerate(cases):
        bvid = f"BV1case{index:05d}"
        seed(db, bvid)
        outcome = await try_mention(
            db=db, bvid=bvid, owner_mid=mid, owner_name=name,
            text="x", rpid=rpid, verifier=verifier,
        )
        assert outcome.status == expected, (index, outcome.status, expected)
        assert "不得声称已通知主人" in str(outcome.to_dict()["note"])


# ---------------------------------------------------------------------------
# 存储层第二道断言
# ---------------------------------------------------------------------------


def test_settle_verified_rejects_mismatched_mid(db: Database) -> None:
    """★ 存储层**独立**再断言一次。"""
    seed(db)
    nid = db.plan_owner_notification(bvid="BV1test00001", owner_mid=OWNER_MID)
    with pytest.raises(ValueError, match="observed_mid 等于 owner_mid"):
        db.settle_owner_notification(notification_id=nid, status="verified")
    with pytest.raises(ValueError, match="observed_mid 等于 owner_mid"):
        db.settle_owner_notification(
            notification_id=nid, status="verified", observed_mid=999
        )
    db.settle_owner_notification(
        notification_id=nid, status="verified", observed_mid=OWNER_MID
    )
    assert str(db.get_owner_notification(nid)["status"]) == "verified"


def test_settle_rejects_illegal_status(db: Database) -> None:
    seed(db)
    nid = db.plan_owner_notification(bvid="BV1test00001", owner_mid=OWNER_MID)
    with pytest.raises(ValueError, match="非法投递状态"):
        db.settle_owner_notification(notification_id=nid, status="ok")


def test_settle_unknown_notification_raises(db: Database) -> None:
    with pytest.raises(ValueError, match="通知不存在"):
        db.settle_owner_notification(notification_id="notif_nope", status="uncertain")


def test_summary_counts_by_status(db: Database) -> None:
    seed(db, "BV1a0000001")
    seed(db, "BV1b0000002")
    n1 = db.plan_owner_notification(bvid="BV1a0000001", owner_mid=OWNER_MID)
    db.plan_owner_notification(bvid="BV1b0000002", owner_mid=OWNER_MID)
    db.settle_owner_notification(notification_id=n1, status="verified", observed_mid=OWNER_MID)
    summary = db.owner_notification_summary()
    assert summary.get("verified") == 1
    assert summary.get("planned") == 1


# ---------------------------------------------------------------------------
# 迁移（v1 → v2）
# ---------------------------------------------------------------------------


def test_migration_upgrades_existing_v1_database(tmp_path: Any) -> None:
    """★ 回归：把新表塞进**已应用过的** v1 不会在既有库上执行。"""
    from pathlib import Path

    from core import schema

    path = Path(tmp_path) / "old.db"
    v1_only = tuple(m for m in schema.MIGRATIONS if m[0] == 1)
    assert v1_only

    import sqlite3

    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
    )
    conn.executescript(v1_only[0][1])
    conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (1, 0)")
    conn.commit()
    has_before = conn.execute(
        "SELECT name FROM sqlite_master WHERE name='owner_notifications'"
    ).fetchone()
    conn.close()
    assert has_before is None, "v1 不应包含新表"

    db = Database(path)
    version = db.migrate()
    assert version == schema.SCHEMA_VERSION >= 2
    assert db.query_one(
        "SELECT name FROM sqlite_master WHERE name='owner_notifications'"
    ) is not None
    db.close()


def test_migrations_are_append_only(db: Database) -> None:
    from core import schema

    versions = [version for version, _ in schema.MIGRATIONS]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    assert versions[0] == 1
    assert versions[-1] == schema.SCHEMA_VERSION


def test_api_verifier_does_not_need_browser() -> None:
    """★ 核验器是**纯 API** 的（不需要浏览器）。

    这正是本项目对任务书"可用隔离浏览器处理该步"的替代方案 ——
    也是它与"定时 preset 不给通用浏览器"相容的原因。
    """
    import inspect

    source = inspect.getsource(ApiMentionVerifier)
    assert "reader" in source
    assert "AtMember" in source
    for forbidden in ("ego_", "playwright", "selenium"):
        assert forbidden not in source.lower(), f"核验器不该依赖 {forbidden}"
