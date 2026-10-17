"""密钥解析：``os.environ`` → ``HKCU\\Environment`` 兜底。

## 为什么必须读注册表

本机实测（见 ``docs/compatibility.md`` §7.1）：DSH 由 ``explorer.exe`` 启动，
继承的是 **explorer 的旧环境块**。在「系统属性 → 环境变量」里新设的变量，
**重启 DSH 也读不到**。实测本机豆包密钥只存在于 ``HKCU\\Environment``。

## 为什么要绕开"进程环境变量"这条常规路

MCP stdio 子进程的环境会被官方客户端清洗：删除匹配
``/KEY|PASSWORD|SECRET|TOKEN/i`` 的名称与所有 ``DSH_*`` 名称
（见 ``docs/compatibility.md`` §2.3①）。

→ 结论：**读注册表**同时化解这两个问题——既不依赖环境块继承，
也不受 stdio 清洗影响。

**本模块只返回值给调用方；绝不打印值。** 日志只记来源与长度。
"""

from __future__ import annotations

import os
from typing import Literal

Source = Literal["environment", "registry", "missing"]

_RegistryCache = dict[str, str | None]
_registry_cache: _RegistryCache | None = None

_ENV_KEY = r"Environment"


def _read_user_registry() -> _RegistryCache:
    """读取 ``HKCU\\Environment`` 全部值（仅 Windows）。失败返回空表。"""
    global _registry_cache
    if _registry_cache is not None:
        return _registry_cache

    values: _RegistryCache = {}
    try:  # pragma: no cover - 非 Windows 分支
        import winreg
    except ImportError:
        _registry_cache = values
        return values

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _ENV_KEY) as key:
            index = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                if isinstance(value, str):
                    values[name] = value
                index += 1
    except OSError:
        pass

    _registry_cache = values
    return values


def reset_cache() -> None:
    """清空注册表缓存（测试用）。"""
    global _registry_cache
    _registry_cache = None


def resolve_secret(env_name: str) -> str | None:
    """按 ``环境变量 → HKCU\\Environment`` 顺序解析密钥值。

    返回 ``None`` 表示未配置——调用方必须把它当作"不可用"，
    而不是空字符串继续跑。
    """
    if not env_name:
        return None
    from_env = os.environ.get(env_name)
    if from_env:
        return from_env
    from_registry = _read_user_registry().get(env_name)
    if from_registry:
        return from_registry
    return None


def secret_source(env_name: str) -> Source:
    """报告密钥来源（**只报来源，不报值**）。"""
    if not env_name:
        return "missing"
    if os.environ.get(env_name):
        return "environment"
    if _read_user_registry().get(env_name):
        return "registry"
    return "missing"


def secret_fingerprint(env_name: str) -> str:
    """可安全写进日志的指纹：来源 + 长度。**不含值本身。**"""
    value = resolve_secret(env_name)
    if value is None:
        return f"{env_name}:<absent>"
    return f"{env_name}:{secret_source(env_name)}:len={len(value)}"
