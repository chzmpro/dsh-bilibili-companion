"""文件系统布局。

## 数据放哪里

**不放** BiliLearn 的 ``Data/``，**不放** git 追踪范围。
优先级（第一个**可写**的胜出）：

1. 环境变量 ``COMPANION_DATA_DIR``（测试与诊断用；**显式指定即视为权威**）
2. ``%LOCALAPPDATA%\\dsh-bilibili-companion\\``（Windows 常规位置）
3. ``%APPDATA%\\dsh-bilibili-companion\\``（漫游配置位置）
4. ``<项目根>/.local-data/``（项目内兜底，已 gitignore）

## 为什么需要"探测可写性"

本机实测（2026-10-15）：**``%LOCALAPPDATA%`` 下无法创建任何新目录/文件**
（``PermissionError [WinError 5]``，即使用 ``pwsh`` 也一样，
而该目录的 ACL 显示用户是 ``FullControl`` —— 属于工具side 的限制，不是 ACL 问题）。
实测可写的是 ``<项目根>/.local-data``。

→ 因此**不能**写死路径，必须**先探测再决定**，
并把最终选中的目录**报告给使用者**（见 :func:`data_dir_report`），
避免出现"数据写到了你不知道的地方"这种静默行为。

可用 ``COMPANION_DATA_DIR`` 覆盖；**显式指定时不做可写性回退**
（否则会静默忽略使用者的明确选择，反而更糟）。
"""

from __future__ import annotations

import os
import tempfile
from functools import lru_cache
from pathlib import Path

APP_DIR_NAME = "dsh-bilibili-companion"

_DB_NAME = "companion.db"
_COOKIE_NAME = "cookie.txt"
_LOG_NAME = "companion.log"
_LOCK_NAME = "companion.lock"


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _probe_writable(directory: Path) -> bool:
    """真写一个临时文件来判断目录可用（不只看 exists()）。"""
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".probe-", delete=True):
            pass
    except OSError:
        return False
    return True


def _candidate_dirs() -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(("localappdata", Path(local) / APP_DIR_NAME))
    roaming = os.environ.get("APPDATA")
    if roaming:
        candidates.append(("appdata", Path(roaming) / APP_DIR_NAME))
    candidates.append(("project-local", project_root() / ".local-data"))
    return candidates


@lru_cache(maxsize=1)
def _resolve() -> tuple[Path, str]:
    """解析出实际数据目录，返回 ``(路径, 来源标签)``。结果被缓存。"""
    override = os.environ.get("COMPANION_DATA_DIR")
    if override:
        # 显式指定：尊重使用者的选择，不做回退
        return Path(override).expanduser(), "env:COMPANION_DATA_DIR"

    for label, candidate in _candidate_dirs():
        if _probe_writable(candidate):
            return candidate, label

    # 全都不行：至少退到临时目录，保证程序不崩（并会被 data_dir_report 报出来）
    fallback = Path(tempfile.gettempdir()) / APP_DIR_NAME
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback, "temp-fallback"


def reset_cache() -> None:
    """清空解析缓存（测试用）。"""
    _resolve.cache_clear()


def data_dir() -> Path:
    """返回实际数据目录（已确保存在且可写）。"""
    return _resolve()[0]


def data_dir_source() -> str:
    """返回数据目录的**来源标签**，用于诊断输出。"""
    return _resolve()[1]


def data_dir_report() -> dict[str, object]:
    """可安全输出的数据目录诊断（写进 ``companion_status``）。"""
    directory, source = _resolve()
    return {
        "path": str(directory),
        "source": source,
        "writable": _probe_writable(directory),
    }


def ensure_data_dir() -> Path:
    return data_dir()


def db_path() -> Path:
    return data_dir() / _DB_NAME


def cookie_path() -> Path:
    """凭据文件位置。**内容永不进日志/上下文/MCP 返回。**"""
    return data_dir() / _COOKIE_NAME


def log_path() -> Path:
    return data_dir() / _LOG_NAME


def lock_path() -> Path:
    """单实例任务锁文件位置。"""
    return data_dir() / _LOCK_NAME


def default_config_path() -> Path:
    """真实配置路径。

    默认 ``<项目根>/config.yaml``（gitignored）。
    可用 ``COMPANION_CONFIG_PATH`` 覆盖 —— **测试必须用它**，
    否则会读到开发者本机的真实配置，导致"零配置"类断言失效。
    """
    override = os.environ.get("COMPANION_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    return project_root() / "config.yaml"


def example_config_path() -> Path:
    return project_root() / "config.example.yaml"
