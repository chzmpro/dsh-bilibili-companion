"""单实例任务锁测试（任务书 §7 任务 7）。

*"所有唤醒和互动监听共享单实例锁…两次同时唤醒不能让两个进程同时冲击 B 站，
也不能重复发布。"*
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from core.lock import LockError, TaskLock


def test_acquire_and_release(tmp_path: Path) -> None:
    lock = TaskLock(tmp_path / "x.lock", purpose="test")
    info = lock.acquire()
    assert info.pid == os.getpid()
    assert (tmp_path / "x.lock").exists()
    lock.release()
    assert not (tmp_path / "x.lock").exists()


def test_second_acquire_while_held_is_refused(tmp_path: Path) -> None:
    """★ 第二次获取必须被拒绝（不是等待，而是明确失败）。"""
    path = tmp_path / "x.lock"
    first = TaskLock(path, purpose="first")
    first.acquire()
    try:
        second = TaskLock(path, purpose="second", take_over_stale=False)
        with pytest.raises(LockError) as excinfo:
            second.acquire()
        assert excinfo.value.holder is not None
        assert excinfo.value.holder.purpose == "first"
    finally:
        first.release()


def test_context_manager_releases_on_exception(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    with pytest.raises(RuntimeError), TaskLock(path, purpose="t"):
        raise RuntimeError("boom")
    assert not path.exists()


def test_stale_lock_from_dead_pid_is_taken_over(tmp_path: Path) -> None:
    """★ 进程崩溃留下的陈旧锁必须可被接管，否则用户永久卡死。"""
    path = tmp_path / "x.lock"
    # 一个几乎不可能存在的 PID
    path.write_text(
        json.dumps({"pid": 999_999_999, "acquired_at": int(time.time()), "purpose": "crashed"}),
        encoding="utf-8",
    )
    lock = TaskLock(path, purpose="recover")
    info = lock.acquire()
    assert info.pid == os.getpid()
    lock.release()


def test_very_old_lock_is_taken_over(tmp_path: Path) -> None:
    """超过 TTL 的锁视为陈旧（即使 PID 恰好还活着）。"""
    path = tmp_path / "x.lock"
    path.write_text(
        json.dumps(
            {"pid": os.getpid(), "acquired_at": int(time.time()) - 100000, "purpose": "old"}
        ),
        encoding="utf-8",
    )
    lock = TaskLock(path, purpose="new", stale_after_seconds=3600)
    info = lock.acquire()
    assert info.pid == os.getpid()
    lock.release()


def test_stale_takeover_can_be_disabled(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    path.write_text(
        json.dumps({"pid": 999_999_999, "acquired_at": 0, "purpose": "crashed"}),
        encoding="utf-8",
    )
    lock = TaskLock(path, purpose="new", take_over_stale=False)
    with pytest.raises(LockError):
        lock.acquire()


def test_corrupt_lock_file_is_treated_as_stale(tmp_path: Path) -> None:
    """损坏的锁文件不能把程序永久锁死。"""
    path = tmp_path / "x.lock"
    path.write_text("这不是 JSON", encoding="utf-8")
    lock = TaskLock(path, purpose="recover")
    info = lock.acquire()
    assert info.pid == os.getpid()
    lock.release()


def test_release_only_removes_own_lock(tmp_path: Path) -> None:
    """★ 不能误删别人刚抢到的锁。"""
    path = tmp_path / "x.lock"
    lock = TaskLock(path, purpose="mine")
    lock.acquire()
    # 模拟"另一个进程接管了这个锁文件"
    path.write_text(
        json.dumps({"pid": 999_999_999, "acquired_at": int(time.time()), "purpose": "other"}),
        encoding="utf-8",
    )
    lock.release()
    assert path.exists(), "不应删除别人的锁"


def test_lock_dir_created_if_missing(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "x.lock"
    lock = TaskLock(nested, purpose="t")
    lock.acquire()
    assert nested.exists()
    lock.release()


def test_read_holder_returns_info(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    lock = TaskLock(path, purpose="readable")
    lock.acquire()
    holder = lock.read_holder()
    assert holder is not None
    assert holder.purpose == "readable"
    assert holder.pid == os.getpid()
    lock.release()


def test_read_holder_missing_file_returns_none(tmp_path: Path) -> None:
    assert TaskLock(tmp_path / "nope.lock").read_holder() is None
