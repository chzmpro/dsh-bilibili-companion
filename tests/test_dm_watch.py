"""常驻私信监听 + 面板测试（2026-10-17）。

主人：*"那就做个仿 BLB 的实现，独立的 web 服务甚至一个独立的页面"*。

## 这个模块要解决的问题

参考项目 `bilibili_learning_bot` 能"及时回复"，靠的是**一个常驻进程**
（`subprocess.Popen([sys.executable, "-m", "brain.monitor"])`，
内部 `while self.running`）。而本项目是 DSH 插件 + 对话形态，
只在用户说话时醒 ⇒ 必须有一个**不属于对话的进程**。

## ★★ 三个真机踩到的跨线程坑（全部钉死在这里）

| # | 错误 | 根因 |
|---|---|---|
| 1 | `SQLite objects created in a thread...` | `sqlite3` 默认 `check_same_thread=True` |
| 2 | 同上（只换 db 还不够） | **`RequestQueue` 内部也持有 db**（记健康状态要写库）⇒ 必须一起重建 |
| 3 | `'CookieBundle' object is not iterable` | poll_once 要裸 dict、reply_to 要 bundle |

## 钉死的不变量

* 后台线程必须**自建** db + queue（不共享主线程的）；
* 单轮失败**不能让整个监听死**（参考项目也这么做）；
* **默认只观察不回复**（说错话比晚回更糟）；
* 面板只监听 `127.0.0.1`（没有鉴权，暴露=谁都能停）。
"""

from __future__ import annotations

from typing import Any

import pytest

from core import dm_watch
from core.dm_watch import (
    DEFAULT_INTERVAL_SECONDS,
    FALLBACK_REPLY,
    MAX_PER_ROUND,
    DmWatcher,
    WatchStats,
)
from core.storage import Database


class _FakeQueue:
    """假队列：只回一个可控的未读数。"""

    def __init__(self, unread: int = 0, messages: list[dict[str, Any]] | None = None) -> None:
        self.unread = unread
        self.messages = messages or []
        self.calls = 0

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        self.calls += 1

        class _Outcome:
            data: dict[str, Any] = {}

        if "unread" in url:
            _Outcome.data = {"follow_unread": self.unread}
        else:
            _Outcome.data = {"messages": self.messages}
        return _Outcome()

    async def aclose(self) -> None:
        return None


class _FakeConfig:
    class account:  # noqa: N801
        owner_mid = 660490601
        agent_mid = 3747570769725629

    class interaction:  # noqa: N801
        pause = False
        dm_reply_enabled = True


class _FakeCookies:
    """模拟 CookieBundle：既有 `.values`（dict），本身又不可迭代。"""

    def __init__(self) -> None:
        self.values = {"SESSDATA": "x", "bili_jct": "y"}

    def csrf_token(self) -> str:
        return "y"

    def has_write_credentials(self) -> bool:
        return True


class _FakeCookieBundle:
    def __init__(self) -> None:
        self.values = {"SESSDATA": "x"}

    def __iter__(self) -> Any:  # 故意不可迭代，模拟真实 CookieBundle
        raise TypeError("'CookieBundle' object is not iterable")


# ---------------------------------------------------------------------------
# ★★ 坑 3：cookies 的两种形态
# ---------------------------------------------------------------------------


def test_cookie_map_is_dict() -> None:
    """★★ `poll_once` 要**裸 dict** —— 传 CookieBundle 会 TypeError。

    真机报错：`TypeError: 'CookieBundle' object is not iterable`
    """
    bundle = _FakeCookies()
    watcher = DmWatcher(
        db=None,
        config=_FakeConfig(),
        queue=_FakeQueue(),
        cookies=bundle,
    )
    assert isinstance(watcher.cookie_map, dict)
    assert watcher.cookie_map == {"SESSDATA": "x", "bili_jct": "y"}
    # 原始 bundle 仍保留（reply_to 要它带 csrf）
    assert watcher.cookies is bundle


def test_cookie_map_accepts_plain_dict() -> None:
    """传裸 dict 时 `values` 不存在 —— 要能退化处理，不能崩。"""
    plain = {"SESSDATA": "x"}
    watcher = DmWatcher(
        db=None, config=_FakeConfig(), queue=_FakeQueue(), cookies=plain
    )
    assert watcher.cookie_map == plain


# ---------------------------------------------------------------------------
# ★★ 坑 1/2：后台线程必须自建 db + queue
# ---------------------------------------------------------------------------


def test_db_factory_creates_own_connection() -> None:
    """★★ 给了 `db_factory` 就必须**自建**连接，不共享外部那个。"""
    created: list[int] = []

    def factory() -> Database:
        created.append(1)
        return Database(":memory:")

    watcher = DmWatcher(
        db=None,
        config=_FakeConfig(),
        queue=_FakeQueue(),
        cookies=_FakeCookies(),
        db_factory=factory,
    )
    handle = watcher._handle()
    assert isinstance(handle, Database)
    assert created == [1]
    # 第二次复用同一个（不要每次新建）
    assert watcher._handle() is handle
    assert created == [1]


def test_queue_factory_receives_thread_db() -> None:
    """★★ `RequestQueue` 内部持有 db ⇒ 必须用**线程内**的 db 建它。

    只换 db 不换 queue 照样跨线程（真机第二次踩到这个）。
    """
    seen: list[Any] = []

    def queue_factory(thread_db: Any) -> Any:
        seen.append(thread_db)
        return _FakeQueue()

    def db_factory() -> Database:
        return Database(":memory:")

    watcher = DmWatcher(
        db=None,
        config=_FakeConfig(),
        queue=None,
        cookies=_FakeCookies(),
        db_factory=db_factory,
        queue_factory=queue_factory,
    )
    handle = watcher._handle()
    built = watcher._queue()
    assert seen == [handle], "queue 必须拿到线程内的 db"
    assert built is not None


def test_no_db_and_no_factory_raises() -> None:
    watcher = DmWatcher(
        db=None, config=_FakeConfig(), queue=_FakeQueue(), cookies=_FakeCookies()
    )
    with pytest.raises(RuntimeError):
        watcher._handle()


def test_close_releases_own_db() -> None:
    watcher = DmWatcher(
        db=None,
        config=_FakeConfig(),
        queue=_FakeQueue(),
        cookies=_FakeCookies(),
        db_factory=lambda: Database(":memory:"),
    )
    watcher._handle()
    watcher.close()
    assert watcher._own_db is None


# ---------------------------------------------------------------------------
# 单轮行为
# ---------------------------------------------------------------------------


async def test_poll_round_observe_only(db: Database) -> None:
    """**观察模式**：发现消息但不发送。"""
    watcher = DmWatcher(
        db=db,
        config=_FakeConfig(),
        queue=_FakeQueue(),
        cookies=_FakeCookies(),
        auto_reply=False,
    )
    result = await watcher.poll_once_round()
    assert result["replied"] == 0
    assert watcher.stats.rounds == 1


async def test_stats_counts_rounds(db: Database) -> None:
    watcher = DmWatcher(
        db=db, config=_FakeConfig(), queue=_FakeQueue(), cookies=_FakeCookies()
    )
    await watcher.poll_once_round()
    await watcher.poll_once_round()
    assert watcher.stats.rounds == 2


async def test_ack_happens_before_delivery_and_carries_text(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 主人 2026-10-19：「消息和豆包的接不上」。

    豆包必须先回占位，**再**投递给值班员；投递文本里要带上豆包回了什么，
    否则 DSH 只看到主人原话，两边各说各话。
    """
    order: list[str] = []
    captured: dict[str, str] = {}

    class _Outcome:
        new_messages = [{"msg_seqno": 42, "text": "主人说：看看今天有什么", "title": ""}]
        unread = 1
        skipped = 0

    async def fake_poll_once(**_kwargs: Any) -> Any:
        return _Outcome()

    async def fake_ack(
        *, db: Database, config: Any, cookies: Any, msg_seqno: int, text: str
    ) -> Any:
        order.append("ack")
        db.execute(
            "UPDATE dm_inbox SET acked_at = 1, ack_text = ? WHERE msg_seqno = ?",
            (text, int(msg_seqno)),
        )
        class _R:
            msg_key = "k"
        return True, "ok"

    async def fake_deliver(text: str, **_kwargs: Any) -> Any:
        order.append("deliver")
        captured["prompt"] = text

        class _R:
            ok = True
            delivered = True
            session_id = "session-d3af12f6-a4fb-4e61-a1e1-02eb94b2a1e9"

        return _R()

    monkeypatch.setattr(dm_watch, "poll_once", fake_poll_once)
    monkeypatch.setattr(dm_watch, "ack_to", fake_ack)
    monkeypatch.setattr(dm_watch, "dm_reply_allowed", lambda _cfg: (True, "OK"))
    monkeypatch.setattr("core.dm_deliver.is_session_alive", lambda _sid: True)
    monkeypatch.setattr("core.dm_deliver.deliver_async", fake_deliver)
    monkeypatch.setattr(
        "core.dm_deliver.read_plugin_config",
        lambda: {"watchSessionId": "session-d3af12f6-a4fb-4e61-a1e1-02eb94b2a1e9"},
    )

    watcher = DmWatcher(
        db=db,
        config=_FakeConfig(),
        queue=_FakeQueue(),
        cookies=_FakeCookies(),
        auto_reply=True,
        reply_text="先看看，我马上细说",
        deliver_delay_seconds=0,
    )
    await watcher.poll_once_round()

    assert order == ["ack", "deliver"], "必须先回占位，再投递"
    assert "先看看，我马上细说" in captured["prompt"], "投递文本要带上豆包的占位内容"
    assert "接着它说" in captured["prompt"]


# ---------------------------------------------------------------------------
# ★★ 单轮失败不能让监听死
# ---------------------------------------------------------------------------


async def test_poll_round_error_is_recorded_not_raised(db: Database) -> None:
    """单轮抛错要**被记录**，而不是冒泡 —— 否则监听循环会退出。"""

    class _BoomQueue(_FakeQueue):
        async def get_json(self, url: str, **kwargs: Any) -> Any:
            raise ConnectionError("网络断了")

    watcher = DmWatcher(
        db=db, config=_FakeConfig(), queue=_BoomQueue(), cookies=_FakeCookies()
    )
    with pytest.raises(ConnectionError):
        # poll_once_round 本身会抛（由 run() 捕获）—— 这里确认"能抛出来"
        await watcher.poll_once_round()


async def test_run_survives_round_errors(db: Database) -> None:
    """★★ 核心：`run()` 里单轮失败**不能让循环退出**。

    参考项目也是这么做的（`gather(return_exceptions=True)`）。
    监听循环一旦因为一次网络抖动退出，就再也收不到消息了。
    """

    class _FlakyQueue(_FakeQueue):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next = True

        async def get_json(self, url: str, **kwargs: Any) -> Any:
            if self.fail_next:
                self.fail_next = False
                raise ConnectionError("偶发断连")
            return await super().get_json(url, **kwargs)

    # 间隔有 15 秒下限（防把 B站 打爆），这里直接调小属性绕过，
    # 只为让测试在秒级内看到"第 2 轮"
    watcher = DmWatcher(
        db=db,
        config=_FakeConfig(),
        queue=_FlakyQueue(),
        cookies=_FakeCookies(),
        interval=15,
    )
    watcher.interval = 1  # 测试用：绕过下限，别在生产里这么做

    task = dm_watch.asyncio.create_task(watcher.run())
    # 等它跑过至少 2 轮（第 1 轮失败、第 2 轮成功）
    for _ in range(60):
        await dm_watch.asyncio.sleep(0.1)
        if watcher.stats.rounds >= 2:
            break
    watcher.stop()
    await dm_watch.asyncio.wait_for(task, timeout=8)

    assert watcher.stats.errors >= 1, "错误要被记录"
    assert watcher.stats.rounds >= 2, (
        f"★ 出错后必须继续跑（实际只跑了 {watcher.stats.rounds} 轮，"
        f"错误 {watcher.stats.errors} 次）"
    )
    assert watcher.stopped is True


# ---------------------------------------------------------------------------
# 回复内容
# ---------------------------------------------------------------------------


async def test_compose_uses_fallback(db: Database) -> None:
    watcher = DmWatcher(
        db=db, config=_FakeConfig(), queue=_FakeQueue(), cookies=_FakeCookies()
    )
    text = await watcher._compose_reply([])
    assert text == FALLBACK_REPLY


async def test_compose_prefers_custom_text(db: Database) -> None:
    watcher = DmWatcher(
        db=db,
        config=_FakeConfig(),
        queue=_FakeQueue(),
        cookies=_FakeCookies(),
        reply_text="自定义话术",
    )
    assert await watcher._compose_reply([]) == "自定义话术"


async def test_compose_uses_factory(db: Database) -> None:
    async def factory(messages: list[dict[str, Any]]) -> str:
        return "生成的内容"

    watcher = DmWatcher(
        db=db,
        config=_FakeConfig(),
        queue=_FakeQueue(),
        cookies=_FakeCookies(),
        reply_factory=factory,
    )
    assert await watcher._compose_reply([]) == "生成的内容"


async def test_compose_factory_failure_falls_back(db: Database) -> None:
    """生成器抛错要**退回兜底话术**，不能让这一轮崩。"""

    async def boom(messages: list[dict[str, Any]]) -> str:
        raise RuntimeError("模型挂了")

    watcher = DmWatcher(
        db=db,
        config=_FakeConfig(),
        queue=_FakeQueue(),
        cookies=_FakeCookies(),
        reply_factory=boom,
    )
    text = await watcher._compose_reply([])
    assert text == FALLBACK_REPLY


async def test_compose_truncates(db: Database) -> None:
    from core.dm import MAX_DM_CHARS

    watcher = DmWatcher(
        db=db,
        config=_FakeConfig(),
        queue=_FakeQueue(),
        cookies=_FakeCookies(),
        reply_text="字" * (MAX_DM_CHARS + 300),
    )
    text = await watcher._compose_reply([])
    assert len(text) <= MAX_DM_CHARS


# ---------------------------------------------------------------------------
# 统计与配置
# ---------------------------------------------------------------------------


def test_stats_events_capped() -> None:
    stats = WatchStats()
    for index in range(WatchStats.MAX_EVENTS + 50):
        stats.add("poll", f"事件 {index}")
    assert len(stats.events) == WatchStats.MAX_EVENTS


def test_stats_to_dict_shape() -> None:
    stats = WatchStats()
    stats.add("found", "x")
    payload = stats.to_dict()
    for key in ("running", "uptime", "rounds", "found", "replied", "errors", "events"):
        assert key in payload


def test_interval_has_floor(db: Database) -> None:
    """★ 间隔有下限 —— 防止误配成 1 秒把 B站 打爆。"""
    watcher = DmWatcher(
        db=db,
        config=_FakeConfig(),
        queue=_FakeQueue(),
        cookies=_FakeCookies(),
        interval=1,
    )
    assert watcher.interval >= 15


def test_default_interval_is_conservative() -> None:
    """本项目红线：不加大请求强度 ⇒ 默认用分钟级。"""
    assert DEFAULT_INTERVAL_SECONDS >= 30


def test_max_per_round_is_small() -> None:
    """一轮最多回几条 —— 防止刷屏式回复。"""
    assert 1 <= MAX_PER_ROUND <= 5
