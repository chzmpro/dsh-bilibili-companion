"""**所有** B 站读取共用的请求队列、缓存、限速、有限重试与健康门禁。

任务书 §4 原文：*"上述所有可能访问 B 站的工具都经过统一请求队列、缓存、限速和
平台健康状态检查，**不能由 DSH 绕过而直接调用上游动作函数**"*。

因此本模块是唯一出口：任何读取都必须经由 :meth:`RequestQueue.get_json`。
写入（发布）不在这里——它走 :mod:`core.publish` 的门禁，且**只读重试**规则不适用于它。

要点：

* **先查缓存再请求**，并记录 ``hit / expired / miss / refresh`` 状态。
* 只读**暂态失败**最多重试 ``max_read_retries`` 次。
* 连续限流达 ``max_consecutive_rate_limits`` → 转为人工处理，**不靠加大延迟熬过去**。
* ``verification_required`` / ``auth_expired`` / ``platform_error`` → **立即停止一切请求**。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from core import health
from core import throttle as throttle_mod
from core.storage import Database

CacheStatus = Literal["hit", "expired", "miss", "refresh", "bypass"]

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)

# 缓存分类 → 表名/键前缀
CACHE_KINDS: dict[str, str] = {
    "search": "search_cache_ttl_seconds",
    "metadata": "metadata_cache_ttl_seconds",
    "subtitle": "subtitle_cache_ttl_seconds",
    "comment": "metadata_cache_ttl_seconds",
}


class RequestsBlocked(RuntimeError):
    """平台健康状态不允许发起请求。"""

    def __init__(self, state: str, human_action: str) -> None:
        self.state = state
        self.human_action = human_action
        super().__init__(f"平台状态 {state}：已停止所有新的自动请求。{human_action}")


class RateLimitExceeded(RuntimeError):
    """连续限流达阈值 → 转人工处理。"""


@dataclass
class RequestOutcome:
    """一次读取的结果与观测信息。"""

    status: int
    data: Any
    cache_status: CacheStatus
    code: str | int | None = None
    message: str = ""
    attempts: int = 1
    health_state: str = health.READY


@dataclass
class QueueStats:
    """一轮运行内的请求统计（用于日报区分"实读/缓存/模拟"）。"""

    requests_made: int = 0
    cache_hits: int = 0
    cache_expired: int = 0
    retries: int = 0
    rate_limits: int = 0
    blocked: int = 0
    per_kind: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, int]:
        return {
            "requests_made": self.requests_made,
            "cache_hits": self.cache_hits,
            "cache_expired": self.cache_expired,
            "retries": self.retries,
            "rate_limits": self.rate_limits,
            "blocked": self.blocked,
        }


class RequestQueue:
    """统一的 B 站请求队列。

    :param db: 用于缓存与健康状态的存储
    :param config: :class:`core.config.Config`
    :param client: 可注入的 httpx 客户端（测试用 MockTransport）
    :param clock: 可注入的时钟（测试用固定时钟）
    """

    def __init__(
        self,
        *,
        db: Database,
        config: Any,
        client: httpx.AsyncClient | None = None,
        clock: Any = None,
        sleep: Any = None,
    ) -> None:
        self.db = db
        self.config = config
        self.req_cfg = config.bilibili_requests
        self.stats = QueueStats()
        self._clock = clock or time.time
        self._sleep = sleep or _default_sleep
        self._last_request_at: float = 0.0
        self._consecutive_rate_limits = 0
        # ★ 分频道节流状态（2026-10-17，照抄参考项目）
        self._throttle_state = throttle_mod.Throttle()
        self._owns_client = client is None
        # ★ trust_env=False：避免走系统代理（交接包实测要求）
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": DEFAULT_UA, "Referer": "https://www.bilibili.com/"},
            trust_env=False,
            follow_redirects=True,
        )

    # ---- 生命周期 ----------------------------------------------------------

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---- 健康门禁 ----------------------------------------------------------

    def ensure_allowed(self) -> None:
        """发起任何请求之前的健康检查。"""
        state = self.db.health_state()
        if state in health.BLOCKING_STATES:
            self.stats.blocked += 1
            raise RequestsBlocked(state, health.human_action_for(health.as_state(state)))

    def record_response(
        self, *, code: str | int | None, http_status: int | None, message: str = ""
    ) -> health.Classification:
        """按分类结果更新健康状态。"""
        verdict = health.classify(code=code, http_status=http_status, message=message)

        if verdict.state == health.READY:
            self._consecutive_rate_limits = 0
            return verdict

        if verdict.state == health.RATE_LIMITED:
            self._consecutive_rate_limits += 1
            self.stats.rate_limits += 1
            if self._consecutive_rate_limits >= self.req_cfg.max_consecutive_rate_limits:
                self.db.record_health(
                    state=health.PLATFORM_ERROR,
                    raw_code=str(code) if code is not None else str(http_status),
                    category="consecutive_rate_limits",
                    detail=(
                        f"连续限流 {self._consecutive_rate_limits} 次"
                        f"（阈值 {self.req_cfg.max_consecutive_rate_limits}）"
                        "→ 暂停自动访问并通知主人"
                    ),
                )
                raise RateLimitExceeded(
                    f"连续限流已达阈值 {self.req_cfg.max_consecutive_rate_limits}，"
                    "已暂停自动访问，等待人工处理"
                )

        # ★ ``not_found`` 是**单个资源的确定性失败**，不是平台健康问题。
        #   绝不能写进 platform_health —— 否则一个被删的视频 / 一个不存在的
        #   端点会变成"当前健康状态"，把后续**所有**请求都冻住。
        #   （真实踩到过：探测不存在的端点 → 全线 RequestsBlocked。）
        if verdict.state == health.NOT_FOUND:
            return verdict

        self.db.record_health(
            state=verdict.state,
            raw_code=str(code) if code is not None else (str(http_status) if http_status else None),
            category=verdict.category,
            detail=verdict.human_action,
        )
        return verdict

    # ---- 缓存 --------------------------------------------------------------

    @staticmethod
    def cache_key(kind: str, url: str, params: dict[str, Any] | None) -> str:
        payload = json.dumps(
            {"kind": kind, "url": url, "params": params or {}}, sort_keys=True, ensure_ascii=False
        )
        return f"{kind}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"

    def _ttl(self, kind: str) -> int:
        attr = CACHE_KINDS.get(kind, "metadata_cache_ttl_seconds")
        return int(getattr(self.req_cfg, attr))

    def read_cache(self, key: str, ttl: int) -> tuple[Any | None, CacheStatus]:
        """先查缓存。返回 ``(data, status)``。"""
        row = self.db.query_one(
            "SELECT payload, fetched_at FROM http_cache WHERE cache_key = ?", (key,)
        )
        if row is None:
            return None, "miss"
        age = self._clock() - int(row["fetched_at"])
        if age > ttl:
            return None, "expired"
        try:
            return json.loads(str(row["payload"])), "hit"
        except json.JSONDecodeError:
            return None, "expired"

    def write_cache(self, key: str, kind: str, payload: Any) -> None:
        # ★ 必须用注入的时钟，不能混用 time.time()：否则测试里的固定时钟
        #   与写入时的真实时间会有几十年差距，缓存永远不判过期。
        self.db.execute(
            "INSERT OR REPLACE INTO http_cache (cache_key, kind, payload, fetched_at) "
            "VALUES (?,?,?,?)",
            (key, kind, json.dumps(payload, ensure_ascii=False), int(self._clock())),
        )

    # ---- 限速 --------------------------------------------------------------

    async def _throttle(self, *, channel: str = "") -> None:
        """请求前节流。

        ★ 2026-10-17 增强（照抄参考项目 `bilibili_learning_bot`）：

        * **分频道**：`channel` 不同则各算各的间隔（评论查得勤不该拖慢私信）；
        * **全局冷却**：命中 `-799` 后所有频道一起停（见 :meth:`_backoff_if_limited`）；
        * **抖动**：间隔加随机量，避免固定节奏（固定 0.3s 一次太像爬虫）。

        仍保留原来的**全局最小间隔**（`min_interval_seconds`）作为兜底 ——
        新节流器是"更细一层"，不是替代。
        """
        interval = float(self.req_cfg.min_interval_seconds)
        elapsed = self._clock() - self._last_request_at
        if self._last_request_at > 0 and elapsed < interval:
            await self._sleep(interval - elapsed)

        # ★ 分频道节流（`channel` 为空时退化成全局，行为与旧版一致）
        wait = self._throttle_state.wait_seconds(
            channel or "metadata", now=self._clock()
        )
        if wait > 0:
            await self._sleep(wait)

        self._last_request_at = self._clock()
        self._throttle_state.mark_call(channel or "metadata", now=self._clock())

    async def _backoff_if_limited(self, *, channel: str, code: Any, message: str) -> bool:
        """命中 B站限流码时设置冷却。返回是否命中。

        * `-509` → **该频道**退避 10 秒
        * `-799` → **全局**冷却 90~180 秒（`-799` 是全站级限流）

        ★ 为什么 `-799` 要全局停：它表示"这个账号/IP 被限流了"，
        继续请求**任何**接口只会加重。参考项目 `api/throttle.py` 就是这么做的。
        """
        hit = throttle_mod.is_rate_limit_error(code=code, message=message)
        if hit is None:
            return False
        waited = self._throttle_state.back_off(
            channel or "metadata", code=hit, now=self._clock()
        )
        if waited > 0:
            await self._sleep(min(waited, float(self.req_cfg.backoff_cap_seconds)))
        return True

    # ---- 主入口 ------------------------------------------------------------

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        kind: str = "metadata",
        use_cache: bool = True,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> RequestOutcome:
        """带缓存 + 限速 + 有限重试 + 健康门禁的 GET。

        **这是访问 B 站的唯一读取出口。**
        """
        ttl = self._ttl(kind)
        key = self.cache_key(kind, url, params)

        if use_cache:
            cached, status = self.read_cache(key, ttl)
            if status == "hit":
                self.stats.cache_hits += 1
                return RequestOutcome(status=200, data=cached, cache_status="hit")
            if status == "expired":
                self.stats.cache_expired += 1

        attempts = 0
        max_attempts = 1 + int(self.req_cfg.max_read_retries)
        last_message = ""

        while attempts < max_attempts:
            attempts += 1
            self.ensure_allowed()
            # ★ `kind` 直接当频道用（metadata/search/comments/messages/mentions）
            await self._throttle(channel=kind)
            self.stats.requests_made += 1
            self.stats.per_kind[kind] = self.stats.per_kind.get(kind, 0) + 1

            try:
                response = await self._client.get(
                    url, params=params, cookies=cookies, headers=headers
                )
            except httpx.HTTPError as exc:
                last_message = f"{type(exc).__name__}: {exc}"
                if attempts >= max_attempts:
                    verdict = self.record_response(
                        code=None, http_status=None, message=last_message
                    )
                    return RequestOutcome(
                        status=0,
                        data=None,
                        cache_status="bypass",
                        message=last_message,
                        attempts=attempts,
                        health_state=verdict.state,
                    )
                self.stats.retries += 1
                await self._sleep(
                    health.backoff_seconds(
                        attempts,
                        base=float(self.req_cfg.backoff_base_seconds),
                        cap=float(self.req_cfg.backoff_cap_seconds),
                    )
                    or 0.0
                )
                continue

            payload: Any
            try:
                payload = response.json()
            except ValueError:
                payload = None

            # ★ 只有**真的**是 B 站 API 信封（含 code 字段）时才拆 data。
            #   字幕正文来自 CDN（如 aisubtitle.hdslb.com），返回的是
            #   {"body":[...]} 这类裸结构，没有信封；若无条件 .get("data")
            #   会把整个正文丢掉，表现为"字幕永远不可用"。
            is_envelope = isinstance(payload, dict) and "code" in payload
            code = payload.get("code") if is_envelope else None
            message = ""
            if is_envelope:
                message = str(payload.get("message") or payload.get("msg") or "")

            verdict = self.record_response(
                code=code,
                http_status=response.status_code,
                message=f"{message} {last_message}".strip(),
            )

            # ★ 命中 B站限流码（-509 / -799）时设置冷却（2026-10-17）
            #   `-509` 只停该频道；`-799` 全站停（它是账号/IP 级限流）
            await self._backoff_if_limited(
                channel=kind, code=code, message=f"{message} {last_message}"
            )

            if verdict.state == health.READY:
                data = payload.get("data") if is_envelope else payload
                if use_cache:
                    self.write_cache(key, kind, data)
                    return RequestOutcome(
                        status=response.status_code,
                        data=data,
                        cache_status="refresh",
                        code=code,
                        message=message,
                        attempts=attempts,
                    )
                return RequestOutcome(
                    status=response.status_code,
                    data=data,
                    cache_status="bypass",
                    code=code,
                    message=message,
                    attempts=attempts,
                )

            # 非 ready：判断是否可重试
            if not verdict.retryable or attempts >= max_attempts:
                return RequestOutcome(
                    status=response.status_code,
                    data=None,
                    cache_status="bypass",
                    code=code,
                    message=message or verdict.category,
                    attempts=attempts,
                    health_state=verdict.state,
                )

            self.stats.retries += 1
            await self._sleep(
                health.backoff_seconds(
                    attempts,
                    base=float(self.req_cfg.backoff_base_seconds),
                    cap=float(self.req_cfg.backoff_cap_seconds),
                    state=verdict.state,
                )
            )

        return RequestOutcome(
            status=0,
            data=None,
            cache_status="bypass",
            message=last_message or "超出重试次数",
            attempts=attempts,
        )


async def _default_sleep(seconds: float) -> None:
    import asyncio

    if seconds > 0:
        await asyncio.sleep(seconds)
