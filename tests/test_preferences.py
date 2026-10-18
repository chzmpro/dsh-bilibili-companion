"""主人偏好 + 下一步指引测试（跨对话持久性）。"""

from __future__ import annotations

import pytest

from core.storage import Database

# ---------------------------------------------------------------------------
# 主人偏好：跨对话持久
# ---------------------------------------------------------------------------


def test_add_and_list_preference(db: Database) -> None:
    pid = db.add_owner_preference(kind="rule", content="少点赞", subject="总体")
    rows = db.list_owner_preferences()
    assert len(rows) == 1
    assert str(rows[0]["pref_id"]) == pid
    assert str(rows[0]["content"]) == "少点赞"
    assert bool(rows[0]["active"]) is True


def test_preferences_digest_groups_by_kind(db: Database) -> None:
    db.add_owner_preference(kind="rule", content="少点赞")
    db.add_owner_preference(kind="dislike", content="标题党", subject="某UP")
    digest = db.owner_preferences_digest()
    assert digest["rule"] == ["少点赞"]
    assert digest["dislike"] == ["某UP：标题党"]


def test_deactivate_keeps_record(db: Database) -> None:
    """停用**不删**，保留可追溯。"""
    pid = db.add_owner_preference(kind="note", content="x")
    assert db.deactivate_owner_preference(pid) is True
    assert db.list_owner_preferences() == []
    assert len(db.list_owner_preferences(active_only=False)) == 1


def test_deactivate_unknown_returns_false(db: Database) -> None:
    assert db.deactivate_owner_preference("pref_nope") is False


def test_unknown_kind_rejected(db: Database) -> None:
    with pytest.raises(ValueError, match="未知偏好类型"):
        db.add_owner_preference(kind="whatever", content="x")


def test_empty_content_rejected(db: Database) -> None:
    with pytest.raises(ValueError, match="不能为空"):
        db.add_owner_preference(kind="rule", content="   ")


def test_preference_cannot_grant_publish_authority(db: Database) -> None:
    """★ 关键安全约束：偏好**不得**变成"改发布权限"的旁路。

    scope 只允许 ranking/content/topic；想写 publish 之类的直接报错。
    """
    for bad in ("publish", "authorization", "quota", "mode"):
        with pytest.raises(ValueError, match="非法 scope"):
            db.add_owner_preference(kind="rule", content="x", scope=bad)


def test_preference_scope_whitelist(db: Database) -> None:
    for good in ("ranking", "content", "topic"):
        db.add_owner_preference(kind="rule", content=f"x-{good}", scope=good)
    assert len(db.list_owner_preferences()) == 3


def test_preferences_survive_new_connection(tmp_path: object) -> None:
    """★ 换"对话"（= 换进程/换连接）后偏好仍在。"""
    from pathlib import Path

    path = Path(str(tmp_path)) / "persist.db"
    first = Database(path)
    first.migrate()
    first.add_owner_preference(kind="rule", content="少点赞")
    first.close()

    second = Database(path)
    second.migrate()
    assert second.owner_preferences_digest()["rule"] == ["少点赞"]
    second.close()


def test_migration_v3_to_v4_preserves_data(tmp_path: object) -> None:
    """★ v3 → v4 迁移不得丢数据。"""
    from pathlib import Path

    from core import schema
    from core.storage import Database as DB

    path = Path(str(tmp_path)) / "v3.db"
    # 建一个只到 v3 的库
    db = DB(path)
    cur = db._conn.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
    )
    for version, script in schema.MIGRATIONS:
        if version > 3:
            break
        cur.executescript(script)
        cur.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, 0)", (version,)
        )
    db.upsert_video(bvid="BV1keep00001", aid=1, title="保留我")
    assert db.schema_version() == 3
    db.close()

    # 用当前代码迁移
    db2 = DB(path)
    assert db2.migrate() == schema.SCHEMA_VERSION >= 4
    assert db2.get_video("BV1keep00001") is not None, "迁移把数据弄丢了"
    assert db2.query_one(
        "SELECT name FROM sqlite_master WHERE name='owner_preferences'"
    ) is not None
    db2.close()


def test_schema_version_is_14() -> None:
    """v14 = 发布读回核验状态（``sent_pending_verify``，#4 2026-10-18）。

    ⚠️ **迁移版本号只能追加，不能改已应用的版本**
    —— 把新表塞进已应用过的 v4，既有库不会重跑它（本项目踩过这个坑）。
    """
    from core import schema

    assert schema.SCHEMA_VERSION == 15
    versions = [v for v, _ in schema.MIGRATIONS]
    assert versions == list(range(1, 16)), "迁移版本必须连续"
