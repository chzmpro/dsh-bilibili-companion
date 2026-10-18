"""#5（Codex 审查）：写请求的风控 code 必须传播到**全局**健康状态。

写请求走独立 httpx 客户端、不经过读队列。旧实现里 ``-412`` 只把当前动作
置 ``uncertain``，``platform_health`` 仍是 ``ready`` ⇒ 其它 BV / 其它互动
类型的后续动作会继续发出去。这里锁定三条：

1. 写执行器把稳定错误码和 HTTP 状态一起抛出来；
2. 发布侧把 ``BILI_CODE_-412`` / ``HTTP_429`` / ``-101`` 交给现有健康分类器；
3. 一旦进入阻断态，**下一次写动作在发网前就被拒绝**，且网络错误不误判成风控。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from core.bilibili_write import HttpWriteExecutor, WriteError
from core.credentials import CookieBundle
from core.publish import REJECT_PLATFORM_BLOCKED, PublishGate
from core.storage import TOP_ACTION, Database
from tests.test_publish_gates import (
    FakeReader,
    RecordingExecutor,
    publish_ready_config,
    seed_draft,
)


def _executor(handler: Any) -> HttpWriteExecutor:
    return HttpWriteExecutor(
        cookies=CookieBundle({"SESSDATA": "sess", "bili_jct": "csrf"}),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False),
    )


def _draft_for(db: Database, *, bvid: str, aid: int) -> str:
    db.upsert_video(bvid=bvid, aid=aid, title=f"视频 {bvid}")
    eid = db.add_evidence(
        bvid=bvid, source_type="subtitle", text=f"{bvid} 的真实字幕", start_sec=1.0
    )
    draft_id = db.create_draft(
        draft_type=TOP_ACTION, bvid=bvid, text="这是一条评论", evidence_ids=[eid]
    )
    db.approve_draft(draft_id)
    return draft_id


# ---------------------------------------------------------------------------
# 执行器：保留稳定错误码 + HTTP 状态
# ---------------------------------------------------------------------------


async def test_write_executor_keeps_bili_code_and_http_status() -> None:
    executor = _executor(
        lambda request: httpx.Response(412, json={"code": -412, "message": "请求被拦截"})
    )
    with pytest.raises(WriteError) as info:
        await executor.like_video(bvid="BV1test00001")
    assert info.value.code == "BILI_CODE_-412"
    assert info.value.http_status == 412
    await executor.aclose()


async def test_write_executor_maps_non_json_http_error() -> None:
    executor = _executor(lambda request: httpx.Response(429, text="too many"))
    with pytest.raises(WriteError) as info:
        await executor.like_video(bvid="BV1test00001")
    assert info.value.code == "HTTP_429"
    assert info.value.http_status == 429
    await executor.aclose()


# ---------------------------------------------------------------------------
# 发布侧：传播到全局健康 + 阻止后续动作
# ---------------------------------------------------------------------------


def _gate_with_failure(db: Database, exc: Exception) -> tuple[PublishGate, RecordingExecutor]:
    executor = RecordingExecutor(fail_with=exc)
    gate = PublishGate(
        db=db,
        config=publish_ready_config(
            max_top_comments_per_day=10,
            max_likes_per_day=10,
            max_favorites_per_day=10,
        ),
        reader=FakeReader(),
        executor=executor,
    )
    return gate, executor


async def test_write_risk_control_updates_global_health_and_blocks_next(
    db: Database,
) -> None:
    draft = seed_draft(db)
    gate, executor = _gate_with_failure(db, WriteError("BILI_CODE_-412", "疑似风控"))

    result = await gate.publish(draft_id=draft)
    assert result.code == "PUBLISH_UNCERTAIN"
    assert db.health_state() == "platform_error"
    row = db.query_one("SELECT error_code FROM actions WHERE draft_id = ?", (draft,))
    assert row is not None and str(row["error_code"]) == "BILI_CODE_-412"

    # 另一个 BV 的下一次写动作必须在**发网前**被拦下
    second = _draft_for(db, bvid="BV1test00002", aid=222)
    calls_before = len(executor.calls)

    blocked = await gate.publish(draft_id=second)
    assert blocked.code == REJECT_PLATFORM_BLOCKED
    assert len(executor.calls) == calls_before, "阻断态下不得再发写请求"


async def test_rate_limit_and_auth_codes_keep_their_own_state(db: Database) -> None:
    first = _draft_for(db, bvid="BV1test00011", aid=311)
    gate, _executor_obj = _gate_with_failure(
        db, WriteError("HTTP_429", "HTTP 429", http_status=429)
    )
    await gate.publish(draft_id=first)
    assert db.health_state() == "rate_limited"

    # 429 不是阻断态，允许继续；-101 必须升级成 auth_expired（阻断）
    second = _draft_for(db, bvid="BV1test00012", aid=312)
    gate2, _ = _gate_with_failure(
        db, WriteError("BILI_CODE_-101", "账号未登录", http_status=200)
    )
    await gate2.publish(draft_id=second)
    assert db.health_state() == "auth_expired"


async def test_network_error_does_not_become_platform_risk(db: Database) -> None:
    draft = _draft_for(db, bvid="BV1test00021", aid=321)
    gate, _executor_obj = _gate_with_failure(
        db, WriteError("WRITE_NETWORK", "网络错误：结果未知")
    )
    result = await gate.publish(draft_id=draft)
    assert result.code == "PUBLISH_UNCERTAIN"
    assert db.health_state() == "ready", "网络错误不能升级成平台风控"


class _LikeExecutor:
    def __init__(self, exc: Exception) -> None:
        self.calls = 0
        self.exc = exc

    async def like_video(self, *, bvid: str, undo: bool = False) -> Any:
        self.calls += 1
        raise self.exc


async def test_like_risk_control_also_updates_global_health(db: Database) -> None:
    """点赞与评论共用同一套健康状态 —— 不能只在评论路径修。"""
    db.upsert_video(bvid="BV1test00031", aid=331, title="点赞目标")
    executor = _LikeExecutor(WriteError("BILI_CODE_-509", "请求过于频繁"))
    gate = PublishGate(
        db=db,
        config=publish_ready_config(max_likes_per_day=10),
        reader=FakeReader(),
        executor=executor,
    )
    result = await gate.publish_like(bvid="BV1test00031")
    assert result.code == "PUBLISH_UNCERTAIN"
    assert executor.calls == 1, "只能提交一次，不能重试"
    assert db.health_state() == "rate_limited"
