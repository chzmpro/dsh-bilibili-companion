"""本项目核心层：配置、存储、限流、健康状态、策略与日报。

分层约定（依赖方向单向向下）：

    bridge/  →  core/
    cli/     →  core/

``core`` 不 import ``bridge``，也不 import ``cli``。
所有对 B 站的访问都必须经过 :mod:`core.ratelimit` 的统一请求队列，
不允许绕过它直接调用上游动作函数。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
