"""投递给「值班员」会话测试（2026-10-17）。

## 主人要求

> 「难道不能我自己专门开个会话给你用吗，非得你自己调用呢」

## 为什么这个方案最优

| | `createAgent` | **用户自建会话 + followup** |
|---|---|---|
| 冷启动 | 要造 agent | **0**（会话已活） |
| 上下文 | 白纸 | **天然带用户设定** |
| 可视化 | 黑盒 | **用户看得见** |
| 风险 | 不知有无权限 | 只是投递 |

## 真机验证（2026-10-17）

```
POST /api/bili-companion/deliver {"text":"..."}  → {"delivered":true}
值班员会话文件：75245 → 75523 字节（几秒内）
agentCount：1 → 2（agents.get 触发懒加载）
```

## 钉死的不变量

* **端口必须自动发现**（DSH 每次启动会变：43129 → 12660 → 43129）；
* **探针即身份验证**：只有我们的插件会回 `agentsService` 字段；
* **投递失败不能拖垮监听**；
* **空文本拒绝**。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from core import dm_deliver
from core.dm_deliver import (
    DELIVER_PATH,
    KNOWN_PORTS,
    PROBE_PATH,
    DeliverResult,
    discover_port,
    status,
)


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


# ---------------------------------------------------------------------------
# 端口发现
# ---------------------------------------------------------------------------


def test_known_ports_include_dsh_defaults() -> None:
    """DSH 常用的两个端口要在候选里（省得每次扫 netstat）。"""
    assert 43129 in KNOWN_PORTS
    assert 12660 in KNOWN_PORTS


def test_discover_returns_none_when_nothing_responds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 全都探不到时返回 None（调用方据此报"DSH 没开"）。"""
    monkeypatch.setattr(dm_deliver, "_probe", lambda port, timeout=1.5: False)
    monkeypatch.setattr(dm_deliver, "_listening_ports", lambda: [])
    dm_deliver._PORT_CACHE.update({"port": None, "at": 0.0})
    assert discover_port(force=True) is None


def test_discover_finds_responding_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dm_deliver, "_probe", lambda port, timeout=1.5: port == 43129
    )
    monkeypatch.setattr(dm_deliver, "_listening_ports", lambda: [])
    dm_deliver._PORT_CACHE.update({"port": None, "at": 0.0})
    assert discover_port(force=True) == 43129


def test_discover_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存：60 秒内不重复扫（否则每轮监听都扫 netstat 太浪费）。"""
    calls: list[int] = []

    def fake_probe(port: int, timeout: float = 1.5) -> bool:
        calls.append(port)
        return port == 43129

    monkeypatch.setattr(dm_deliver, "_probe", fake_probe)
    monkeypatch.setattr(dm_deliver, "_listening_ports", lambda: [])
    dm_deliver._PORT_CACHE.update({"port": None, "at": 0.0})
    first = discover_port(force=True)
    count_after_first = len(calls)
    second = discover_port()  # 不 force ⇒ 走缓存
    assert first == second == 43129
    assert len(calls) == count_after_first, "第二次不该再探"


def test_probe_validates_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ 探针即**身份验证**：只有我们的插件会回 agentsService 字段。"""

    def fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        # 别的服务回 200 但没有这个字段 ⇒ 不该被认成 DSH
        return _FakeResponse(200, {"ok": True, "something": "else"})

    monkeypatch.setattr(httpx, "get", fake_get)
    assert dm_deliver._probe(12345) is False

    def fake_get_ours(url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(200, {"ok": True, "agentsService": True})

    monkeypatch.setattr(httpx, "get", fake_get_ours)
    assert dm_deliver._probe(12345) is True


def test_probe_rejects_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx, "get", lambda url, **kw: _FakeResponse(404, {"ok": False})
    )
    assert dm_deliver._probe(12345) is False


# ---------------------------------------------------------------------------
# 投递
# ---------------------------------------------------------------------------


def test_deliver_rejects_empty() -> None:
    result = dm_deliver.deliver("   ")
    assert result.ok is False
    assert result.error_code == "EMPTY"


def test_deliver_reports_dsh_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ DSH 不在时要给**明确**错误，不能静默失败。"""
    monkeypatch.setattr(dm_deliver, "discover_port", lambda force=False: None)
    result = dm_deliver.deliver("测试")
    assert result.ok is False
    assert result.error_code == "DSH_NOT_FOUND"
    assert "找不到" in result.message


def test_deliver_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dm_deliver, "discover_port", lambda force=False: 43129)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: _FakeResponse(
            200, {"ok": True, "delivered": True, "sessionId": "session-x"}
        ),
    )
    result = dm_deliver.deliver("测试")
    assert result.ok is True
    assert result.delivered is True
    assert result.session_id == "session-x"


def test_deliver_rejected_by_whitelist(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 插件侧白名单拒绝时，要把原因传出来（不要吞掉）。"""
    monkeypatch.setattr(dm_deliver, "discover_port", lambda force=False: 43129)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: _FakeResponse(
            403, {"ok": False, "error": "拒绝投递：只允许 session-a"}
        ),
    )
    result = dm_deliver.deliver("测试")
    assert result.ok is False
    assert result.error_code == "HTTP_403"
    assert "只允许" in result.message


def test_deliver_network_error_clears_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ 网络失败要**清端口缓存** —— DSH 重启会换端口。"""
    monkeypatch.setattr(dm_deliver, "discover_port", lambda force=False: 43129)
    dm_deliver._PORT_CACHE.update({"port": 43129, "at": 1.0})

    def boom(url: str, **kw: Any) -> Any:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "post", boom)
    result = dm_deliver.deliver("测试")
    assert result.ok is False
    assert result.error_code == "NETWORK"
    assert dm_deliver._PORT_CACHE["port"] is None, "失败后必须清缓存"


def test_deliver_handles_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dm_deliver, "discover_port", lambda force=False: 43129)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _FakeResponse(200, None))
    result = dm_deliver.deliver("测试")
    assert result.ok is False
    assert result.error_code == "BAD_JSON"


def test_deliver_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 任何情况下都不抛异常（监听不能被投递故障拖垮）。"""
    monkeypatch.setattr(dm_deliver, "discover_port", lambda force=False: 43129)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _FakeResponse(500, []))
    result = dm_deliver.deliver("测试")
    assert isinstance(result, DeliverResult)
    assert result.ok is False


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------


def test_status_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dm_deliver, "discover_port", lambda force=False: 43129)
    info = status()
    assert info["reachable"] is True
    assert info["port"] == 43129
    assert PROBE_PATH in info["probe_path"]
    assert DELIVER_PATH in info["deliver_path"]


def test_paths_are_under_our_prefix() -> None:
    """路径必须在 `/api/bili-companion/` 下（不要撞别的插件）。"""
    assert PROBE_PATH.startswith("/api/bili-companion/")
    assert DELIVER_PATH.startswith("/api/bili-companion/")
