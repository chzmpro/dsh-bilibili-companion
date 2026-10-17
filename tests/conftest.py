"""测试基线：**网络拦截** + 固定时钟 + 隔离数据目录。

网络拦截是硬要求：任何测试打真网都是 bug。
所有测试通过 ``COMPANION_DATA_DIR`` 指向 tmp_path，**绝不碰真实数据目录**。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from core.config import Config
from core.storage import Database

# ---------------------------------------------------------------------------
# 隔离
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """每个测试都有独立数据目录，绝不写真实 %LOCALAPPDATA%。"""
    from core import paths

    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("COMPANION_DATA_DIR", str(data))
    # ★ 必须同时隔离**配置**：否则测试会读到开发者本机的真实 config.yaml，
    #   "零配置只读启动"这类断言就会失效（本项目踩过）。
    monkeypatch.setenv("COMPANION_CONFIG_PATH", str(tmp_path / "no-such-config.yaml"))
    # core.paths 缓存了解析结果，必须清掉否则会串到别的测试
    paths.reset_cache()
    yield data
    paths.reset_cache()


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """兜底网络拦截。

    即使某个测试忘了注入 MockTransport，也**不能**真的发出请求。
    """
    original = httpx.AsyncClient.send

    async def guarded(self: httpx.AsyncClient, request: httpx.Request, **kwargs: Any) -> Any:
        transport = self._transport
        if isinstance(transport, httpx.MockTransport):
            return await original(self, request, **kwargs)
        raise AssertionError(
            f"测试中禁止真实网络请求：{request.method} {request.url}. "
            "请注入 httpx.MockTransport。"
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", guarded)
    yield


# ---------------------------------------------------------------------------
# 固定时钟
# ---------------------------------------------------------------------------


class FakeClock:
    """可控时钟。``t`` 是 Unix 秒。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# ---------------------------------------------------------------------------
# 数据库与配置
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "companion.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def config() -> Config:
    """默认配置：observe + dry_run + 关闭发布 + 无 UID。"""
    return Config.model_validate(
        {
            "discovery": {"keywords": ["DeepSeek Harness"]},
            "bilibili_requests": {"min_interval_seconds": 1.0},
        }
    )


def make_config(**overrides: Any) -> Config:
    """构造一个测试配置。``overrides`` 走 pydantic 校验，非法值会直接失败。"""
    base: dict[str, Any] = {
        "discovery": {"keywords": ["DeepSeek Harness"]},
        "bilibili_requests": {"min_interval_seconds": 1.0},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return Config.model_validate(base)


# ---------------------------------------------------------------------------
# Mock B 站
# ---------------------------------------------------------------------------

BILI_BASE = "https://api.bilibili.com"


def json_response(payload: dict[str, Any], status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def ok_payload(data: Any) -> dict[str, Any]:
    return {"code": 0, "message": "0", "data": data}


def nav_payload(mid: int = 1001, is_login: bool = True) -> dict[str, Any]:
    return ok_payload(
        {
            "isLogin": is_login,
            "uname": "小鲸鱼测试号",
            "mid": mid,
            "wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.png",
            },
        }
    )


def search_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    return ok_payload({"result": items})


def make_search_item(
    bvid: str = "BV1test00001",
    aid: int = 111,
    title: str = "DSH 插件入门",
    pubdate: int = 1_700_000_000,
    duration: str | int = "12:34",
) -> dict[str, Any]:
    return {
        "bvid": bvid,
        "aid": aid,
        "title": title,
        "author": "某UP",
        "mid": 2002,
        "pubdate": pubdate,
        "duration": duration,
        "description": "简介文本",
        "pic": "//i0.hdslb.com/bfs/archive/cover.jpg",
    }


def route_map_response(request: httpx.Request) -> httpx.Response:
    """一个"什么都没配"的默认响应：让漏配的路径明显失败。"""
    return httpx.Response(404, json={"code": -404, "message": "no route for tests"})


def build_client(handler: Any) -> httpx.AsyncClient:
    """构造**不会**被网络拦截拦下的客户端（MockTransport）。"""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)


@pytest.fixture
def captured_requests() -> list[httpx.Request]:
    return []


def response_json(response: httpx.Response) -> dict[str, Any]:
    return json.loads(response.content.decode("utf-8"))
