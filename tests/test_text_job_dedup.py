"""``text_jobs`` 去重回归测试。

## 为什么有这份测试

``idx_text_jobs_cache`` 是**部分唯一索引**：

.. code-block:: sql

    CREATE UNIQUE INDEX idx_text_jobs_cache
        ON text_jobs(cache_key, status) WHERE status = 'ok';

而"重读同一个视频"会命中豆包结果缓存，并**再次**记录一条 ``status='ok'`` 的 job
—— 那时直接 INSERT 会抛：

    IntegrityError: UNIQUE constraint failed: text_jobs.cache_key, text_jobs.status

真实触发路径：``cli.app dm-ingest --force``（重读主人私信里已经读过的视频）。
这类 bug 单测发现不了，因为单测从来只读一次；**必须真的重读一遍**才暴露。
"""

from __future__ import annotations

from core.storage import Database

BVID = "BV1test00001"
SHA = "a" * 64
MODEL = "doubao-test"


def _ok(
    db: Database,
    *,
    prompt_version: str = "v1",
    from_cache: bool = False,
    prompt_tokens: int = 10,
) -> str:
    return db.record_text_job(
        bvid=BVID,
        input_sha256=SHA,
        model_id=MODEL,
        prompt_version=prompt_version,
        status="ok",
        prompt_tokens=prompt_tokens,
        completion_tokens=5,
        from_cache=from_cache,
    )


def _error(db: Database) -> str:
    return db.record_text_job(
        bvid=BVID,
        input_sha256=SHA,
        model_id=MODEL,
        prompt_version="v1",
        status="error",
        error_code="TIMEOUT",
    )


def test_recording_same_ok_job_twice_does_not_raise(db: Database) -> None:
    """★ 核心回归：同一份输入的成功结果重复写，不得抛 IntegrityError。"""
    first = _ok(db)
    second = _ok(db, prompt_tokens=99)

    rows = db.query("SELECT job_id, prompt_tokens FROM text_jobs WHERE status = 'ok'")
    assert len(rows) == 1
    assert second == first
    assert int(rows[0]["prompt_tokens"]) == 99


def test_cached_hit_updates_from_cache_flag(db: Database) -> None:
    _ok(db, from_cache=False)
    _ok(db, from_cache=True)

    rows = db.query("SELECT from_cache FROM text_jobs WHERE status = 'ok'")
    assert len(rows) == 1
    assert int(rows[0]["from_cache"]) == 1


def test_non_ok_statuses_keep_history(db: Database) -> None:
    """错误是过程记录，允许留多条（只有 ok 是唯一的）。"""
    _error(db)
    _error(db)

    rows = db.query("SELECT job_id FROM text_jobs WHERE status = 'error'")
    assert len(rows) == 2


def test_different_prompt_version_is_separate_ok_row(db: Database) -> None:
    """prompt 版本不同 ⇒ cache_key 不同 ⇒ 是两个独立结果。"""
    _ok(db, prompt_version="v1")
    _ok(db, prompt_version="v2")

    rows = db.query("SELECT job_id FROM text_jobs WHERE status = 'ok'")
    assert len(rows) == 2
