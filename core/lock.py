"""单实例任务锁。

任务书 §5 / §7 任务 7：唤醒与互动监听**共享单实例锁**——
两次同时唤醒不能让两个进程同时冲击 B 站，也不能重复发布。

实现：``O_CREAT|O_EXCL`` 锁文件 + 内容记 PID/时间。
进程崩溃留下的**陈旧锁**（PID 已不存在或超过 TTL）可被接管，
否则用户会永久卡死。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from core.paths import lock_path as default_lock_path

DEFAULT_STALE_AFTER_SECONDS = 3600


@dataclass
class LockInfo:
    pid: int
    acquired_at: int
    purpose: str


class LockError(RuntimeError):
    """锁被别的进程持有。"""

    def __init__(self, holder: LockInfo | None) -> None:
        self.holder = holder
        detail = "未知持有者"
        if holder is not None:
            detail = f"pid={holder.pid} since={holder.acquired_at} purpose={holder.purpose}"
        super().__init__(f"已有任务在运行（{detail}）")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - Windows 分支
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class TaskLock:
    """上下文管理器形式的单实例锁。"""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        purpose: str = "run",
        stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
        take_over_stale: bool = True,
    ) -> None:
        self.path = Path(path) if path is not None else default_lock_path()
        self.purpose = purpose
        self.stale_after_seconds = stale_after_seconds
        self.take_over_stale = take_over_stale
        self._fd: int | None = None

    # ---- 读 ----------------------------------------------------------------

    def read_holder(self) -> LockInfo | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        try:
            return LockInfo(
                pid=int(data.get("pid", 0)),
                acquired_at=int(data.get("acquired_at", 0)),
                purpose=str(data.get("purpose", "")),
            )
        except (TypeError, ValueError):
            return None

    def _is_stale(self, holder: LockInfo | None) -> bool:
        if holder is None:
            # 锁文件损坏或为空 → 视为陈旧
            return True
        if not _pid_alive(holder.pid):
            return True
        return (time.time() - holder.acquired_at) > self.stale_after_seconds

    # ---- 获取/释放 ---------------------------------------------------------

    def acquire(self) -> LockInfo:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"pid": os.getpid(), "acquired_at": int(time.time()), "purpose": self.purpose}
        )
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = self.read_holder()
            if self.take_over_stale and self._is_stale(holder):
                # 接管陈旧锁：先删再抢
                try:
                    self.path.unlink()
                except OSError as exc:  # pragma: no cover - 权限异常
                    raise LockError(holder) from exc
                return self.acquire()
            raise LockError(holder) from None

        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        self._fd = fd
        return LockInfo(pid=os.getpid(), acquired_at=int(time.time()), purpose=self.purpose)

    def release(self) -> None:
        try:
            if self.path.exists():
                holder = self.read_holder()
                # 只删自己持有的锁，避免误删别人刚抢到的
                if holder is None or holder.pid == os.getpid():
                    self.path.unlink()
        except OSError:
            pass
        self._fd = None

    def __enter__(self) -> LockInfo:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()
