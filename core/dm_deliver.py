"""把消息投递给 DSH 里已存在的「值班员」会话（2026-10-17）。

## 主人要求

> 「难道不能我自己专门开个会话给你用吗，非得你自己调用呢」

**能，而且这是最优方案**：用户手动开一个专用会话，它的 agent 一直活着。
我们只调 `POST /api/bili-companion/deliver`，插件内部执行
`agents.get(sessionId).followup(message)` —— **零冷启动**。

## 实测（2026-10-17）

```
POST /api/bili-companion/deliver  {"text":"..."}
  → {"ok":true,"delivered":true}

值班员会话文件：75245 → 75523 字节（几秒内）
agentCount：1 → 2（agents.get 触发懒加载，会话被激活）
```

## ★ 端口问题（本模块存在的理由）

DSH web 的端口**每次启动会变**（实测 43129 → 12660 → 43129）。
硬编码必挂，所以这里做**自动发现**：

1. 先试已知端口（缓存 + 常见值）
2. 再从 `netstat` 找所有 `127.0.0.1` 监听端口
3. 逐个打 `/api/bili-companion/probe`，**只有我们的插件会回**
   ⇒ 命中即 DSH 端口（这一步是身份验证，不是猜）

## 安全边界

- 只投递给 `config.watchSessionId` 那一个会话（插件侧另有白名单，双保险）
- **绝不投给别的会话**：投错会污染用户正在用的对话
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import Any

import httpx

#: 探测接口（只有 bili-companion 插件会响应）。
PROBE_PATH = "/api/bili-companion/probe"

#: 投递接口。
DELIVER_PATH = "/api/bili-companion/deliver"

#: 常见端口（先试这些，省得每次扫 netstat）。
KNOWN_PORTS = (43129, 12660, 8080, 3000, 5173)

#: 端口发现结果的缓存（进程内）。
_PORT_CACHE: dict[str, Any] = {"port": None, "at": 0.0}

#: 缓存有效期（秒）。DSH 重启会换端口，所以不能永久缓存。
CACHE_TTL_SECONDS = 60


@dataclass
class DeliverResult:
    """一次投递的结果（**永远不抛异常**）。"""

    ok: bool
    delivered: bool = False
    session_id: str = ""
    error_code: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "delivered": self.delivered,
            "session_id": self.session_id,
            "error_code": self.error_code,
            "message": self.message,
        }


def _listening_ports() -> list[int]:
    """用 netstat 列出本机 `127.0.0.1` 上正在监听的端口（去重、降序）。"""
    try:
        out = subprocess.run(  # noqa: S603, S607 —— netstat 是系统工具
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []

    ports: set[int] = set()
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        addr = parts[1]
        if not addr.startswith("127.0.0.1:"):
            continue
        try:
            ports.add(int(addr.rsplit(":", 1)[1]))
        except (ValueError, IndexError):
            continue
    # 端口大的先试（DSH 常用高位端口）
    return sorted(ports, reverse=True)


def _probe(port: int, *, timeout: float = 1.5) -> bool:
    """打一次 probe，看这个端口是不是我们的插件。"""
    try:
        response = httpx.get(
            f"http://127.0.0.1:{port}{PROBE_PATH}", timeout=timeout, trust_env=False
        )
        if response.status_code != 200:
            return False
        body = response.json()
        return bool(body.get("ok")) and "agentsService" in body
    except (httpx.HTTPError, ValueError):
        return False


def discover_port(*, force: bool = False) -> int | None:
    """找出 DSH web 的端口（**自动发现**，找不到返回 None）。

    ★ 为什么要"发现"而不是"配置"：DSH 每次启动端口会变
    （实测 43129 → 12660 → 43129）。写死必挂。
    """
    now = time.time()
    cached = _PORT_CACHE.get("port")
    if (
        not force
        and cached is not None
        and now - float(_PORT_CACHE.get("at") or 0) < CACHE_TTL_SECONDS
    ):
        return int(cached)

    for port in KNOWN_PORTS:
        if _probe(port):
            _PORT_CACHE.update({"port": port, "at": now})
            return port

    for port in _listening_ports():
        if port in KNOWN_PORTS:
            continue
        if _probe(port):
            _PORT_CACHE.update({"port": port, "at": now})
            return port

    _PORT_CACHE.update({"port": None, "at": now})
    return None


def deliver(text: str, *, session_id: str = "", timeout: float = 15) -> DeliverResult:
    """把一条消息投递给值班员会话（**同步**，由 :func:`deliver_async` 包起来）。"""
    body = (text or "").strip()
    if not body:
        return DeliverResult(ok=False, error_code="EMPTY", message="text 不能为空")

    port = discover_port()
    if port is None:
        return DeliverResult(
            ok=False,
            error_code="DSH_NOT_FOUND",
            message=(
                "找不到 DSH 的接口端口。可能原因：DSH 没启动、"
                "或 bili-companion 插件未加载。"
            ),
        )

    payload: dict[str, Any] = {"text": body}
    if session_id:
        payload["sessionId"] = session_id

    try:
        response = httpx.post(
            f"http://127.0.0.1:{port}{DELIVER_PATH}",
            json=payload,
            timeout=timeout,
            trust_env=False,
        )
    except httpx.HTTPError as exc:
        # 端口可能刚变（DSH 重启）⇒ 清缓存，下次重发现
        _PORT_CACHE.update({"port": None, "at": 0.0})
        return DeliverResult(
            ok=False,
            error_code="NETWORK",
            message=f"投递请求失败：{type(exc).__name__}",
        )

    try:
        data = response.json()
    except ValueError:
        return DeliverResult(
            ok=False, error_code="BAD_JSON", message="接口返回的不是 JSON"
        )

    if not isinstance(data, dict):
        return DeliverResult(ok=False, error_code="BAD_SHAPE", message="返回形状不对")

    if response.status_code != 200 or not data.get("ok"):
        return DeliverResult(
            ok=False,
            error_code=f"HTTP_{response.status_code}",
            delivered=False,
            message=str(data.get("error") or "投递被拒绝")[:200],
        )

    return DeliverResult(
        ok=True,
        delivered=bool(data.get("delivered")),
        session_id=str(data.get("sessionId") or ""),
    )


async def deliver_async(text: str, *, session_id: str = "", timeout: float = 15) -> DeliverResult:
    """**异步**版本（监听器里用）。"""
    import asyncio

    return await asyncio.to_thread(deliver, text, session_id=session_id, timeout=timeout)


def status() -> dict[str, Any]:
    """给面板/CLI 看的当前状态。"""
    port = discover_port()
    return {
        "reachable": port is not None,
        "port": port,
        "probe_path": PROBE_PATH,
        "deliver_path": DELIVER_PATH,
        "note": (
            f"DSH 接口在 127.0.0.1:{port}"
            if port
            else "找不到 DSH 接口（DSH 没开 / 插件没加载）"
        ),
    }


def to_json(payload: Any) -> str:
    """调试用：短 JSON。"""
    try:
        return json.dumps(payload, ensure_ascii=False)[:200]
    except (TypeError, ValueError):
        return str(payload)[:200]
