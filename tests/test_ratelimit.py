"""统一请求队列测试：缓存、限速、有限重试、健康门禁。

这是任务书 §4/§8 的核心："搜索/视频/字幕请求共用限速、缓存和有限只读重试"。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from core import health
from core.ratelimit import RateLimitExceeded, RequestQueue, RequestsBlocked
from core.storage import Database
from tests.conftest import FakeClock, build_client, make_config, ok_payload

URL = "https://api.bilibili.com/x/web-interface/view"


def make_queue(
    db: Database,
    handler: Any,
    *,
    clock: FakeClock,
    min_interval: float = 5.0,
    max_retries: int = 2,
    max_consecutive_rate_limits: int = 2,
) -> RequestQueue:
    config = make_config(
        bilibili_requests={
            "min_interval_seconds": min_interval,
            "max_read_retries": max_retries,
            "max_consecutive_rate_limits": max_consecutive_rate_limits,
            "backoff_base_seconds": 5.0,
            "backoff_cap_seconds": 60.0,
        }
    )
    return RequestQueue(
        db=db,
        config=config,
        client=build_client(handler),
        clock=clock,
        sleep=clock.sleep,
    )


# ---- 缓存 ------------------------------------------------------------------


async def test_cache_miss_then_hit_avoids_second_request(
    db: Database, clock: FakeClock
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=ok_payload({"bvid": "BV1a"}))

    queue = make_queue(db, handler, clock=clock)

    first = await queue.get_json(URL, params={"bvid": "BV1a"}, kind="metadata")
    assert first.cache_status == "refresh"
    assert len(calls) == 1

    second = await queue.get_json(URL, params={"bvid": "BV1a"}, kind="metadata")
    assert second.cache_status == "hit"
    assert len(calls) == 1, "缓存命中时不得再发请求"
    assert queue.stats.cache_hits == 1


async def test_cache_expires_and_refreshes(db: Database, clock: FakeClock) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=ok_payload({"n": len(calls)}))

    config = make_config(
        bilibili_requests={"min_interval_seconds": 1.0, "metadata_cache_ttl_seconds": 300}
    )
    queue = RequestQueue(
        db=db, config=config, client=build_client(handler), clock=clock, sleep=clock.sleep
    )

    await queue.get_json(URL, kind="metadata")
    clock.advance(301)
    outcome = await queue.get_json(URL, kind="metadata")
    assert outcome.cache_status == "refresh"
    assert len(calls) == 2
    assert queue.stats.cache_expired == 1


async def test_different_params_are_different_cache_entries(
    db: Database, clock: FakeClock
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=ok_payload({}))

    queue = make_queue(db, handler, clock=clock)
    await queue.get_json(URL, params={"bvid": "BV1a"}, kind="metadata")
    await queue.get_json(URL, params={"bvid": "BV1b"}, kind="metadata")
    assert len(calls) == 2


async def test_cache_can_be_bypassed(db: Database, clock: FakeClock) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=ok_payload({}))

    queue = make_queue(db, handler, clock=clock)
    await queue.get_json(URL, kind="metadata", use_cache=False)
    await queue.get_json(URL, kind="metadata", use_cache=False)
    assert len(calls) == 2


# ---- 限速 ------------------------------------------------------------------


async def test_shared_throttle_enforced_across_calls(db: Database, clock: FakeClock) -> None:
    """所有读取共用最小间隔——第二次请求必须等到间隔过去。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_payload({}))

    queue = make_queue(db, handler, clock=clock, min_interval=5.0)
    await queue.get_json("https://api.bilibili.com/a", kind="metadata", use_cache=False)
    clock.advance(1.0)  # 只过了 1 秒
    await queue.get_json("https://api.bilibili.com/b", kind="metadata", use_cache=False)

    assert clock.sleeps, "第二次请求必须被限速"
    assert clock.sleeps[0] == pytest.approx(4.0)


async def test_first_request_not_delayed(db: Database, clock: FakeClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_payload({}))

    queue = make_queue(db, handler, clock=clock)
    await queue.get_json(URL, kind="metadata")
    assert clock.sleeps == []


# ---- 有限重试 --------------------------------------------------------------


async def test_retryable_error_is_retried_up_to_limit(db: Database, clock: FakeClock) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"code": -509, "message": "too fast"})

    # 阈值放宽到 5，单独验证"有限重试"这一条语义（连续限流停机另有专门测试）
    queue = make_queue(db, handler, clock=clock, max_retries=2, max_consecutive_rate_limits=5)
    outcome = await queue.get_json(URL, kind="metadata", use_cache=False)
    assert len(calls) == 3, "1 次原始 + 2 次重试"
    assert outcome.data is None
    assert queue.stats.retries == 2


async def test_non_retryable_error_not_retried(db: Database, clock: FakeClock) -> None:
    """``-400``（参数错误）是本程序的 bug，重试没意义。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"code": -400, "message": "bad request"})

    queue = make_queue(db, handler, clock=clock, max_retries=2)
    outcome = await queue.get_json(URL, kind="metadata", use_cache=False)
    assert len(calls) == 1
    assert outcome.health_state == health.PLATFORM_ERROR


async def test_network_error_is_retried(db: Database, clock: FakeClock) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("boom", request=request)

    queue = make_queue(db, handler, clock=clock, max_retries=2)
    outcome = await queue.get_json(URL, kind="metadata", use_cache=False)
    assert len(calls) == 3
    assert outcome.data is None


async def test_error_is_not_swallowed_into_empty_list(db: Database, clock: FakeClock) -> None:
    """任务书 §4：不能把异常吞成空列表。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -403, "message": "forbidden"})

    queue = make_queue(db, handler, clock=clock)
    outcome = await queue.get_json(URL, kind="metadata", use_cache=False)
    assert outcome.data is None
    assert outcome.code == -403
    assert outcome.health_state == health.AUTH_EXPIRED


# ---- 连续限流停机 ----------------------------------------------------------


async def test_consecutive_rate_limits_stop_automatic_access(
    db: Database, clock: FakeClock
) -> None:
    """★ 任务书 §5：连续限流达阈值 → 暂停自动访问并通知主人（不靠加大延迟熬过去）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -509, "message": "slow down"})

    queue = make_queue(db, handler, clock=clock, max_retries=5, max_consecutive_rate_limits=2)

    with pytest.raises(RateLimitExceeded):
        await queue.get_json(URL, kind="metadata", use_cache=False)

    assert db.health_state() == health.PLATFORM_ERROR
    row = db.current_health()
    assert row is not None
    assert str(row["category"]) == "consecutive_rate_limits"


# ---- 健康门禁 --------------------------------------------------------------


async def test_blocking_state_stops_all_new_requests(db: Database, clock: FakeClock) -> None:
    """验证码 / 认证失效 / 平台错误 → 立即停止**所有**新请求。"""
    for state in (health.VERIFICATION_REQUIRED, health.AUTH_EXPIRED, health.PLATFORM_ERROR):
        database = Database(db.path)
        database.migrate()
        database.record_health(state=state, category="test")

        calls: list[int] = []

        def handler(
            request: httpx.Request, _calls: list[int] = calls
        ) -> httpx.Response:
            _calls.append(1)
            return httpx.Response(200, json=ok_payload({}))

        queue = make_queue(database, handler, clock=FakeClock())
        with pytest.raises(RequestsBlocked) as excinfo:
            await queue.get_json(URL, kind="metadata", use_cache=False)
        assert excinfo.value.state == state
        assert calls == [], "阻断状态下不得发出任何请求"
        assert queue.stats.blocked == 1
        database.close()


async def test_cache_hit_still_works_when_blocked(db: Database, clock: FakeClock) -> None:
    """阻断只针对**新请求**；已缓存内容仍可读（避免把人卡死）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_payload({"n": 1}))

    queue = make_queue(db, handler, clock=clock)
    await queue.get_json(URL, kind="metadata")

    db.record_health(state=health.VERIFICATION_REQUIRED, category="captcha")
    outcome = await queue.get_json(URL, kind="metadata")
    assert outcome.cache_status == "hit"


async def test_verification_message_blocks(db: Database, clock: FakeClock) -> None:
    """响应里出现验证码特征 → 即使 code=0 也进入阻断状态。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "message": "请完成验证", "data": {}})

    queue = make_queue(db, handler, clock=clock)
    await queue.get_json(URL, kind="metadata", use_cache=False)
    assert db.health_state() == health.VERIFICATION_REQUIRED


async def test_412_blocks_and_does_not_retry(db: Database, clock: FakeClock) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(412, json={"code": -412, "message": "banned"})

    queue = make_queue(db, handler, clock=clock, max_retries=3)
    outcome = await queue.get_json(URL, kind="metadata", use_cache=False)
    assert len(calls) == 1, "412 绝不重试"
    assert outcome.health_state == health.PLATFORM_ERROR
    assert db.health_state() == health.PLATFORM_ERROR


# ---- 非信封响应（CDN 裸结构）----------------------------------------------


async def test_non_envelope_response_is_not_unwrapped(
    db: Database, clock: FakeClock
) -> None:
    """★ 回归：字幕正文来自 CDN，是 `{"body":[...]}` 裸结构，没有 code/data 信封。

    若无条件 `payload.get("data")`，整个正文会被丢掉，
    表现为"字幕永远不可用"——这是一个真实出现过的 bug。
    """
    subtitle_url = "https://aisubtitle.hdslb.com/bfs/ai_subtitle/prod/x.json"
    body = {"body": [{"from": 1.0, "to": 3.0, "content": "字幕文本"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    queue = make_queue(db, handler, clock=clock)
    outcome = await queue.get_json(subtitle_url, kind="subtitle")

    assert outcome.data == body, "非信封响应必须原样返回，不能被拆掉 data"
    assert outcome.code is None
    assert outcome.health_state == health.READY


async def test_non_envelope_response_is_cached(db: Database, clock: FakeClock) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"body": [{"from": 0, "to": 1, "content": "x"}]})

    queue = make_queue(db, handler, clock=clock)
    first = await queue.get_json("https://aisubtitle.hdslb.com/a.json", kind="subtitle")
    second = await queue.get_json("https://aisubtitle.hdslb.com/a.json", kind="subtitle")
    assert first.cache_status == "refresh"
    assert second.cache_status == "hit"
    assert len(calls) == 1
    assert second.data == {"body": [{"from": 0, "to": 1, "content": "x"}]}


async def test_envelope_response_still_unwrapped(db: Database, clock: FakeClock) -> None:
    """带了 code 的信封仍然要拆 data（不能因为上面那条修过头）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_payload({"inner": 1}))

    queue = make_queue(db, handler, clock=clock)
    outcome = await queue.get_json(URL, kind="metadata")
    assert outcome.data == {"inner": 1}
    assert outcome.code == 0


# ---- 统计 ------------------------------------------------------------------


async def test_stats_distinguish_real_vs_cache(db: Database, clock: FakeClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_payload({}))

    queue = make_queue(db, handler, clock=clock)
    await queue.get_json(URL, kind="metadata")
    await queue.get_json(URL, kind="metadata")
    stats = queue.stats.to_dict()
    assert stats["requests_made"] == 1
    assert stats["cache_hits"] == 1


# ---- 404 不得冻结全局（真实踩到的坑）--------------------------------------


async def test_not_found_does_not_block_subsequent_requests(
    db: Database, clock: FakeClock
) -> None:
    """★ 回归：一个 404 不能把后续**所有**请求冻住。

    真实踩到过：探测一个不存在的端点返回 404 → 被当成 platform_error 写进
    platform_health → 之后每一个请求都 RequestsBlocked，整个系统停摆。
    单个资源不存在是**确定性失败**（视频被删/评论区关闭），应当跳过它继续干活。
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "gone" in url:
            return httpx.Response(404, json={"code": -404, "message": "啥都木有"})
        return httpx.Response(200, json=ok_payload({"ok": 1}))

    queue = make_queue(db, handler, clock=clock)

    missing = await queue.get_json(
        "https://api.bilibili.com/gone", kind="metadata", use_cache=False
    )
    assert missing.health_state == health.NOT_FOUND
    assert missing.data is None

    # ★ 关键：健康状态仍是 ready，后续请求正常发出
    assert db.health_state() == health.READY, "404 不得写入 platform_health"
    assert db.current_health() is None

    after = await queue.get_json(
        "https://api.bilibili.com/fine", kind="metadata", use_cache=False
    )
    assert after.data == {"ok": 1}, "404 之后必须还能正常请求"
    assert len(calls) == 2


async def test_business_404_also_does_not_block(db: Database, clock: FakeClock) -> None:
    """业务码 -404（评论已删除等）同样不阻断。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -404, "message": "没有该评论"})

    queue = make_queue(db, handler, clock=clock)
    outcome = await queue.get_json(URL, kind="metadata", use_cache=False)
    assert outcome.health_state == health.NOT_FOUND
    assert db.health_state() == health.READY


async def test_not_found_is_not_retried(db: Database, clock: FakeClock) -> None:
    """确定性失败不重试（省时间也省风控暴露）。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404, json={"code": -404, "message": "nope"})

    queue = make_queue(db, handler, clock=clock, max_retries=3)
    await queue.get_json(URL, kind="metadata", use_cache=False)
    assert len(calls) == 1, "404 不应重试"
