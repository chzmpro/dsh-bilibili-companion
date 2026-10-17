"""私信通道测试（主人 2026-10-16 要求"读取 + 回复都做"）。

## 背景

任务书 §1 把"私信机器人"列为**非目标**，但主人明确要求"回复我发给小号的私信"。
于是新增一条**只通向主人**的通道。本文件把它的边界固化成测试：

* 接收者**不是参数**，而是 ``owner_mid`` —— 正文里写谁的 UID 都改不了收件人；
* 它有自己的开关 ``dm_reply_enabled``，**与 publish_enabled 相互独立**：
  打开回复私信**不会**顺带打开视频评论 / 点赞 / 收藏；
* 门禁拒绝时**一个请求都不发**；
* 平台处于阻塞态（验证码/风控）时同样不发，且**不自动恢复**；
* 写请求不重试；凭据不外泄。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from core import health
from core.config import Config
from core.credentials import CookieBundle
from core.dm import (
    MAX_DM_CHARS,
    DmError,
    dm_reply_allowed,
    fetch_messages,
    parse_content,
    send_to_owner,
)
from core.ratelimit import RequestQueue
from core.storage import Database
from tests.conftest import build_client, make_config

AGENT_MID = 1001
OWNER_MID = 2002


def _config(*, dm_enabled: bool = True, **interaction: Any) -> Config:
    merged: dict[str, Any] = {"dm_reply_enabled": dm_enabled, **interaction}
    return make_config(
        account={"agent_mid": AGENT_MID, "owner_mid": OWNER_MID},
        interaction=merged,
    )


def _cookies(*, csrf: bool = True) -> CookieBundle:
    values: dict[str, str] = {"sessdata": "sess-value"}
    if csrf:
        values["bili_jct"] = "csrf-value"
    return CookieBundle(values)


def _ok_handler(captured: list[httpx.Request]) -> Any:
    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200, json={"code": 0, "message": "0", "data": {"msg_key": 42}}
        )

    return handle


# ---------------------------------------------------------------------------
# 内容解析
# ---------------------------------------------------------------------------


def test_parse_content_text() -> None:
    text, meta = parse_content('{"content":"主人好呀"}')
    assert text == "主人好呀"
    assert meta["content"] == "主人好呀"


def test_parse_content_share_card_exposes_bvid() -> None:
    text, meta = parse_content('{"title":"某个视频","bvid":"BV1xx","author":"UP"}')
    assert text == "某个视频"
    assert meta["bvid"] == "BV1xx"


def test_parse_content_non_json_is_returned_as_is() -> None:
    text, meta = parse_content("裸文本")
    assert text == "裸文本"
    assert meta == {}


# ---------------------------------------------------------------------------
# 门禁
# ---------------------------------------------------------------------------


def test_disabled_by_default() -> None:
    allowed, reason = dm_reply_allowed(_config(dm_enabled=False))
    assert (allowed, reason) == (False, "DM_REPLY_DISABLED")


def test_pause_blocks() -> None:
    assert dm_reply_allowed(_config(pause=True)) == (False, "PAUSED")


def test_owner_mid_required() -> None:
    config = make_config(
        account={"agent_mid": AGENT_MID, "owner_mid": 0},
        interaction={"dm_reply_enabled": True},
    )
    assert dm_reply_allowed(config) == (False, "OWNER_MID_NOT_CONFIGURED")


def test_independent_from_public_publish_switch() -> None:
    """★ 核心边界：私信开关与公开写入开关**互相独立**。

    默认姿态（observe + dry_run + 关闭总开关）下**不能**发评论，
    但**可以**回复主人私信 —— 因为私信不是公开动作。
    打开私信开关**不会**让 ``can_publish()`` 变真。
    """
    config = _config(mode="observe", dry_run=True, publish_enabled=False)

    assert config.can_publish() == (False, "MODE_OBSERVE")
    assert dm_reply_allowed(config) == (True, "OK")


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------


async def test_send_targets_owner_and_encodes_content() -> None:
    captured: list[httpx.Request] = []
    result = await send_to_owner(
        config=_config(),
        cookies=_cookies(),
        text="主人好呀",
        client=build_client(_ok_handler(captured)),
    )

    assert result.ok
    assert result.msg_key == "42"
    assert len(captured) == 1

    form = parse_qs(captured[0].content.decode("utf-8"))
    assert form["msg[receiver_id]"] == [str(OWNER_MID)]
    assert form["msg[receiver_type]"] == ["1"]
    assert form["msg[msg_type]"] == ["1"]
    assert form["msg[sender_uid]"] == [str(AGENT_MID)]
    assert json.loads(form["msg[content]"][0])["content"] == "主人好呀"


async def test_receiver_cannot_be_changed_by_text() -> None:
    """接收者**不是参数**：正文里写别人的 UID 也改不了收件人。"""
    captured: list[httpx.Request] = []
    await send_to_owner(
        config=_config(),
        cookies=_cookies(),
        text="这封信其实想发给 999999",
        client=build_client(_ok_handler(captured)),
    )

    form = parse_qs(captured[0].content.decode("utf-8"))
    assert form["msg[receiver_id]"] == [str(OWNER_MID)]


async def test_gate_rejection_sends_nothing() -> None:
    captured: list[httpx.Request] = []
    with pytest.raises(DmError) as excinfo:
        await send_to_owner(
            config=_config(dm_enabled=False),
            cookies=_cookies(),
            text="不该发出去",
            client=build_client(_ok_handler(captured)),
        )

    assert excinfo.value.code == "DM_REPLY_DISABLED"
    assert captured == []


async def test_too_long_text_rejected_before_request() -> None:
    captured: list[httpx.Request] = []
    with pytest.raises(DmError) as excinfo:
        await send_to_owner(
            config=_config(),
            cookies=_cookies(),
            text="字" * (MAX_DM_CHARS + 1),
            client=build_client(_ok_handler(captured)),
        )

    assert excinfo.value.code == "TEXT_TOO_LONG"
    assert captured == []


async def test_empty_text_rejected() -> None:
    captured: list[httpx.Request] = []
    with pytest.raises(DmError) as excinfo:
        await send_to_owner(
            config=_config(),
            cookies=_cookies(),
            text="   ",
            client=build_client(_ok_handler(captured)),
        )

    assert excinfo.value.code == "EMPTY_TEXT"
    assert captured == []


async def test_missing_csrf_rejected_without_request() -> None:
    captured: list[httpx.Request] = []
    with pytest.raises(DmError) as excinfo:
        await send_to_owner(
            config=_config(),
            cookies=_cookies(csrf=False),
            text="没有 csrf",
            client=build_client(_ok_handler(captured)),
        )

    assert excinfo.value.code == "CSRF_MISSING"
    assert captured == []


async def test_platform_blocked_health_stops_send(db: Database) -> None:
    blocking = sorted(health.BLOCKING_STATES)[0]
    db.record_health(state=blocking, raw_code="-352", category="captcha", detail="需人工")

    captured: list[httpx.Request] = []
    with pytest.raises(DmError) as excinfo:
        await send_to_owner(
            config=_config(),
            cookies=_cookies(),
            text="停机期间不该发",
            db=db,
            client=build_client(_ok_handler(captured)),
        )

    assert excinfo.value.code == "PLATFORM_BLOCKED"
    assert captured == []


async def test_platform_error_code_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -400, "message": "请求错误"})

    with pytest.raises(DmError) as excinfo:
        await send_to_owner(
            config=_config(),
            cookies=_cookies(),
            text="会被平台拒绝",
            client=build_client(handler),
        )

    assert excinfo.value.code == "BILI_CODE_-400"


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


async def test_fetch_messages_parses_sorts_and_exposes_card(db: Database) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "0",
                "data": {
                    "messages": [
                        {
                            "msg_seqno": 2,
                            "sender_uid": OWNER_MID,
                            "receiver_id": AGENT_MID,
                            "msg_type": 1,
                            "content": '{"content":"第二条"}',
                            "timestamp": 200,
                        },
                        {
                            "msg_seqno": 1,
                            "sender_uid": OWNER_MID,
                            "receiver_id": AGENT_MID,
                            "msg_type": 7,
                            "content": '{"title":"分享的卡片","bvid":"BV1card"}',
                            "timestamp": 100,
                        },
                    ]
                },
            },
        )

    config = _config()
    queue = RequestQueue(db=db, config=config, client=build_client(handler))
    try:
        messages = await fetch_messages(
            queue=queue, cookies={}, talker_id=OWNER_MID, size=10
        )
    finally:
        await queue.aclose()

    assert [m.msg_seqno for m in messages] == [1, 2]
    assert messages[0].text == "分享的卡片"
    assert messages[0].bvid == "BV1card"
    assert messages[0].from_owner(OWNER_MID)
    assert not messages[0].from_agent(AGENT_MID)
    assert messages[1].text == "第二条"
