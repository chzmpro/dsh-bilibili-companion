"""私信常驻监听 + 单页面板（2026-10-17，仿 `bilibili_learning_bot` 形态）。

## 主人要求

> 「那就做个仿 BLB 的实现，独立的 web 服务甚至一个独立的页面，开机自启就不要了」

## 为什么必须这么做（对比后才发现的结构性差距）

参考项目 `bilibili_learning_bot` 能"及时回复"，靠的是**一个常驻进程**：

```python
# 它的 web_panel.py
monitor_process = subprocess.Popen([sys.executable, "-m", "brain.monitor"], ...)
# 它的 brain/monitor.py
async def main():
    return await MonitorBot().run()      # 内部 while self.running 无限循环
```

而我们的项目是 **DSH 插件 + 对话**形态 —— 我**只在用户说话时醒**，
MCP 的 22 个工具里也没有任何"监听"能力。

⇒ 要"及时回复"，就必须有一个**不属于对话**的常驻进程。本模块就是它。

## 设计（照抄它的三个组成，但按我们已有代码组装）

| 组成 | 它的做法 | 我们的做法 |
|---|---|---|
| 常驻轮询 | `while self.running` | :class:`DmWatcher` 的 `while` 循环 |
| 单实例锁 | `_acquire_bot_lock()` | 复用 `core/lock.py`（**防两个进程重复回复**） |
| 面板 | Flask + 35 分区 | **单页** `http.server`（标准库，零新依赖） |

## 三条安全约束（与项目一致）

1. **只回主人**：接收者硬编码 `owner_mid`，代码层无法发给别人；
2. **幂等**：按 `msg_seqno` 去重（`dm_replies` 表），同一句只回一次；
3. **可以只观察不回复**：`--observe` 模式只记录，不发任何东西。

## 为什么默认"先只观察"

自动回复说错话比晚回一会儿更糟。所以默认 `observe=True`，
你看日志确认"能及时收到"之后，再用 `--reply` 打开自动回复。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from core.dm import MAX_DM_CHARS, dm_reply_allowed
from core.dm_live import ack_to, poll_once
from core.dm_reply_gen import generate_reply
from core.storage import Database

#: 默认轮询间隔（秒）。
#:
#: ★ 取 60 秒：`single_unread` 只返回一个数字，成本极低。
#: 参考项目用 5 秒，但本项目红线是"不加大请求强度" ⇒ 用分钟级。
DEFAULT_INTERVAL_SECONDS = 60

#: 单轮最多处理几条新消息（防止一次刷屏式回复）。
MAX_PER_ROUND = 3

#: 监听启动后**多少秒内不投递**（默认 60）。
#:
#: ★ 主人要求：「让重启DSH后的插件投递慢一点，比如过1分钟再开始，
#:   免得我还没打开会话就已经投递了」
#:
#: 投递需要值班员会话**已经激活**（在内存里）**并且选过模型**
#: （否则 prompt 里 `{{model}}` 解析不到，每轮都报 error）。
#: 所以刚重启时先等一会儿，别急着投。
DEFAULT_DELIVER_DELAY_SECONDS = 60

#: 自动回复的**固定话术**（还没接豆包时的兜底）。
#:
#: ⚠️ 刻意简短：长篇自动回复最容易"说错话"。
FALLBACK_REPLY = "收到啦～我先记下，稍后详细回你。"


@dataclass
class WatchEvent:
    """一条监听事件（面板用）。"""

    at: float
    kind: str  # poll / found / replied / error / skipped
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "time": time.strftime("%H:%M:%S", time.localtime(self.at)),
            "kind": self.kind,
            "detail": self.detail,
        }


@dataclass
class WatchStats:
    """监听统计（面板展示）。"""

    started_at: float = field(default_factory=time.time)
    rounds: int = 0
    found: int = 0
    replied: int = 0
    errors: int = 0
    last_poll_at: float = 0.0
    last_error: str = ""
    events: list[WatchEvent] = field(default_factory=list)

    #: 面板最多展示多少条事件（防止内存无限涨）
    MAX_EVENTS = 200

    def add(self, kind: str, detail: str) -> None:
        self.events.append(WatchEvent(at=time.time(), kind=kind, detail=detail))
        if len(self.events) > self.MAX_EVENTS:
            del self.events[: len(self.events) - self.MAX_EVENTS]

    @property
    def uptime_seconds(self) -> float:
        return max(0.0, time.time() - self.started_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": True,
            "uptime": _human_duration(self.uptime_seconds),
            "uptime_seconds": round(self.uptime_seconds, 1),
            "rounds": self.rounds,
            "found": self.found,
            "replied": self.replied,
            "errors": self.errors,
            "last_poll": (
                time.strftime("%H:%M:%S", time.localtime(self.last_poll_at))
                if self.last_poll_at
                else ""
            ),
            "last_error": self.last_error,
            "events": [e.to_dict() for e in self.events[-40:]][::-1],
        }


def _human_duration(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


class DmWatcher:
    """常驻私信监听器。

    ★ 与 `bilibili_learning_bot` 的 `MonitorBot` 对应物，
    但**刻意做得更小**：只有私信一个频道，没有评论/@/推荐流。
    理由：先让"及时收到你的消息"这件事可靠，再谈扩展。
    """

    def __init__(
        self,
        *,
        db: Database | None,
        config: Any,
        queue: Any,
        cookies: Any,
        interval: int = DEFAULT_INTERVAL_SECONDS,
        auto_reply: bool = False,
        reply_text: str = "",
        reply_factory: Any = None,
        deliver_delay_seconds: int = DEFAULT_DELIVER_DELAY_SECONDS,
        db_factory: Any = None,
        queue_factory: Any = None,
        auto_cycle: bool = False,
        cycle_start_delay: int = 60,
        cycle_interval: int = 60,
    ) -> None:
        """Args:
        db: 已建好的连接（**仅在调用方与监听在同一线程时**用）。
        db_factory: ``() -> Database`` —— 监听跑在**后台线程**时用这个，
            让线程内**自己建连接**。

            ★ 真机踩到：`sqlite3` 默认 ``check_same_thread=True``，
            跨线程用一个连接会抛
            ``SQLite objects created in a thread can only be used in that same thread``。
            本项目 `Database` 的设计就是"一个连接 = 一个线程"，
            所以后台线程必须自己建 —— **不要**改 `check_same_thread=False` 绕过，
            那会引入不确定的并发写风险。
        """
        self.db = db
        self._db_factory = db_factory
        self._queue_factory = queue_factory
        self._own_db: Database | None = None
        self._own_queue: Any = None
        self.config = config
        self.queue = queue
        self.cookies = cookies
        # ★ `poll_once` 要**裸 dict**（拼 cookie 头），
        #   而 `reply_to` 要 **CookieBundle**（里面带 csrf）。
        #   两者不是同一个东西 —— 传错会 TypeError: not iterable。
        #
        # ⚠️ 坑：不能写 `getattr(cookies, "values", cookies)` ——
        #   裸 dict 也有 `.values`（是个**方法**！），那样会取到
        #   `<built-in method values of dict object>` 而不是 dict 本身。
        if isinstance(cookies, dict):
            self.cookie_map = cookies
        else:
            values = getattr(cookies, "values", None)
            self.cookie_map = values if isinstance(values, dict) else {}
        self.interval = max(15, int(interval))
        #: 是否真的回复（False = 只观察）
        self.auto_reply = bool(auto_reply)
        #: 固定话术；留空用 :data:`FALLBACK_REPLY`
        self.reply_text = (reply_text or "").strip()
        #: 可选：一个 `async (messages) -> str` 的生成器。
        #: ★ 不传时**自动用豆包**（主人选的方案 A）——
        #: 因为常驻进程只能调豆包（DSH 的 key 拿不到，见 dm_reply_gen 的说明）。
        self.reply_factory = reply_factory or self._doubao_factory
        #: 启动后多久内不投递（给用户时间打开值班员会话）
        self.deliver_delay_seconds = max(0, int(deliver_delay_seconds))
        self._started_at = time.time()
        self.stats = WatchStats()
        self._stop = False
        #: R1：是否随 DSH 自动跑学习轮（默认关；插件会显式传 --cycle）
        self.auto_cycle = bool(auto_cycle)
        self.cycle_start_delay = max(0, int(cycle_start_delay))
        self.cycle_interval = max(5, int(cycle_interval))
        self._scheduler: Any = None

    def _handle(self) -> Database:
        """取本次调用该用的 DB 句柄。

        给了 ``db_factory`` 就在**当前线程**建一个（并记住复用），
        否则用传入的那个。
        """
        if self._db_factory is not None:
            if self._own_db is None:
                self._own_db = self._db_factory()
                self._own_db.migrate()
            return self._own_db
        if self.db is None:
            raise RuntimeError("DmWatcher 既没有 db 也没有 db_factory")
        return self.db

    def _record_inbox(self, messages: list[dict[str, Any]]) -> int:
        """把新消息写进 ``dm_inbox``（状态 pending），供 **DSH** 取走思考。

        ★ 为什么不让监听进程自己调模型（主人要求）：
        「私信要求尽量是让 DSH 进行思考而不是内置的那个低级模型」。
        而 DeepSeek 的凭据在 DSH 内部、不落配置 ⇒ 监听进程**拿不到**，
        所以它只负责"发现 + 入队"，**回复由 DSH 用自己的模型生成**。

        Returns:
            新写入的条数。
        """
        db = self._handle()
        written = 0
        now = int(time.time())
        for item in messages:
            seqno = int(item.get("msg_seqno") or 0)
            if seqno <= 0:
                continue
            existing = db.query_one(
                "SELECT msg_seqno FROM dm_inbox WHERE msg_seqno = ?", (seqno,)
            )
            if existing is not None:
                continue
            db.execute(
                "INSERT OR IGNORE INTO dm_inbox "
                "(msg_seqno, text, title, bvid, received_at, status, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    seqno,
                    str(item.get("text") or "")[:MAX_DM_CHARS],
                    str(item.get("title") or "")[:200],
                    str(item.get("bvid") or ""),
                    int(item.get("timestamp") or now),
                    "pending",
                    now,
                ),
            )
            written += 1
        return written

    def _queue(self) -> Any:
        """取本次调用该用的 RequestQueue 句柄。

        ★ 和 db 同理：`RequestQueue` 内部持有 `db` 连接
        （`record_health` / `health_state` 都要写库），
        所以**它也必须在线程内新建** —— 只换 db 不换 queue 照样跨线程。
        """
        if self._queue_factory is not None:
            if self._own_queue is None:
                self._own_queue = self._queue_factory(self._handle())
            return self._own_queue
        return self.queue

    def close(self) -> None:
        """关掉自建的资源（外部传入的不动）。"""
        if self._own_queue is not None:
            closer = getattr(self._own_queue, "aclose", None)
            if closer is not None:
                # 收摊失败不该掩盖主错误
                with contextlib.suppress(Exception):
                    loop = asyncio.new_event_loop()
                    loop.run_until_complete(closer())
                    loop.close()
            self._own_queue = None
        if self._own_db is not None:
            self._own_db.close()
            self._own_db = None

    # ---- 生命周期 ----------------------------------------------------------

    def stop(self) -> None:
        """请求停止（面板的"停止"按钮调它）。"""
        self._stop = True
        if self._scheduler is not None:
            self._scheduler.stop()

    @property
    def stopped(self) -> bool:
        return self._stop

    # ---- 单轮 --------------------------------------------------------------

    async def poll_once_round(self) -> dict[str, Any]:
        """跑**一轮**：查未读 → 发现新消息 → （可选）回复。

        Returns:
            ``{"unread": int, "found": int, "replied": int, "skipped": int}``。
        """
        self.stats.rounds += 1
        self.stats.last_poll_at = time.time()

        outcome = await poll_once(
            db=self._handle(), config=self.config, queue=self._queue(), cookies=self.cookie_map
        )
        found = len(outcome.new_messages)
        self.stats.found += found

        if found:
            # ★ 关键：**先入队**，让 DSH 能取走思考（即使不开自动回复）
            queued = self._record_inbox(outcome.new_messages)
            self.stats.add(
                "found",
                f"发现 {found} 条新消息（入队 {queued}）：{outcome.new_messages[-1]['text'][:30]}",
            )

            # ★ 投递给「值班员」会话（主人自建的常驻会话，零冷启动）
            #
            # 主人：「难道不能我自己专门开个会话给你用吗，非得你自己调用呢」
            # ⇒ 是的，而且更好：`agents.get(sid).followup(msg)` 把私信投进去，
            #   那个 agent 自己思考、自己调 dm_reply，不用起新进程。
            await self._deliver_to_duty(outcome.new_messages[-1])

        # ★ 补投：本轮没发现新消息时，看看有没有**上次没投出去**的
        if not found:
            waiting = self._undelivered()
            if waiting is not None:
                await self._deliver_to_duty(waiting)

        replied = 0
        if found and self.auto_reply:
            allowed, reason = dm_reply_allowed(self.config)
            if not allowed:
                self.stats.add("skipped", f"自动回复被门禁拒绝：{reason}")
            else:
                # 只回**最新一条**（避免一次刷多条，更像人）。
                # 前面的几条作为"历史上下文"喂给生成器 ——
                # 主人说过「可以查看我前面的发言并回复我吗」，只给最后一句会答得莫名其妙。
                target = outcome.new_messages[-1]
                text = await self._compose_reply(outcome.new_messages)
                if text:
                    db = self._handle()
                    seqno = int(target["msg_seqno"])
                    row = db.query_one(
                        "SELECT acked_at FROM dm_inbox WHERE msg_seqno = ?", (seqno,)
                    )
                    if row is not None and row["acked_at"] is not None:
                        self.stats.add("skipped", f"seqno={seqno} 已先回过占位，跳过")
                    else:
                        # ★ R3：豆包这条只是**先回占位**，不写 dm_replies、
                        #   不改 status ⇒ DSH 之后仍能用 dm_reply 正式回答。
                        ok, message = await ack_to(
                            db=db,
                            config=self.config,
                            cookies=self.cookies,
                            msg_seqno=seqno,
                            text=text,
                        )
                        if ok:
                            replied += 1
                            self.stats.add("replied", f"已先回占位 {seqno}")
                        else:
                            self.stats.add("skipped", message)

        self.stats.replied += replied
        return {
            "unread": outcome.unread,
            "found": found,
            "replied": replied,
            "skipped": outcome.skipped,
        }

    def _undelivered(self) -> dict[str, Any] | None:
        """取一条**还没成功投递**的待回复私信（最新的一条）。

        ★ 为什么需要"补投"：投递可能因为
          ① 刚启动的静默期、② 值班员会话还没激活、③ 没选模型 而失败。
        原来只有 `found`（新消息）才触发投递 ⇒ **失败一次就永远不投了**。
        有了 `delivered_at IS NULL` 这个条件，每轮都能补上。

        Returns:
            一条消息 dict（含 msg_seqno/text/title），没有就 None。
        """
        try:
            row = self._handle().query_one(
                "SELECT msg_seqno, text, title FROM dm_inbox "
                "WHERE status = 'pending' AND delivered_at IS NULL "
                "ORDER BY received_at DESC LIMIT 1"
            )
        except Exception:  # noqa: BLE001
            return None
        if row is None:
            return None
        return {
            "msg_seqno": int(row["msg_seqno"]),
            "text": str(row["text"] or ""),
            "title": str(row["title"] or ""),
        }

    def _duty_session_id(self) -> str:
        """取值班员会话 id（从**插件的** config.json 读）。

        ★ 为什么不放项目自己的 config.yaml：那个文件是"小号行为配置"
        （关键词/评分/配额），而值班员会话是**DSH 插件**的概念，
        归 `$DSH_HOME/bili-companion/config.json` 管。
        这里只**读**它，不写 —— 写由插件的 /config 接口负责。

        ★ 插件侧也有白名单（`watchSessionId`，/deliver 会再校验一次），
        两边都限制，防止投错会话污染用户正在用的对话。
        """
        try:
            from core.dm_deliver import read_plugin_config

            return str(read_plugin_config().get("watchSessionId") or "")
        except Exception:  # noqa: BLE001 —— 读不到就当没配置
            return ""

    async def _deliver_to_duty(self, message: dict[str, Any]) -> bool:
        """把新私信**投递**给值班员会话（失败不影响主流程）。

        ★ 为什么这条比 headless 好：值班员会话的 agent 已在内存里，
        `followup` 只是往它的 inbox 塞一条并唤醒 —— **零冷启动**。
        作为对比，`dsh --profile headless` 每轮要 20 秒。

        Returns:
            是否投递成功。
        """
        text = str(message.get("text") or "").strip()
        if not text:
            return False

        # ---- 保护 1：启动后静默期内不投递 --------------------------------
        # 主人要求：刚重启 DSH 时别急着投，等他打开会话。
        elapsed = time.time() - self._started_at
        if elapsed < self.deliver_delay_seconds:
            wait = int(self.deliver_delay_seconds - elapsed)
            self.stats.add(
                "skipped", f"投递静默期（还差 {wait} 秒），先攒着不投"
            )
            return False

        # ---- 保护 2：会话必须**活着** -----------------------------------
        # `agents.get()` 只找内存里的活 agent。DSH 刚重启时它不在 ⇒ 投了也白投。
        try:
            from core.dm_deliver import is_session_alive

            session_id = self._duty_session_id()
            if session_id and not is_session_alive(session_id):
                self.stats.add(
                    "skipped", "值班员会话还没激活（请在 DSH 里打开它），先攒着"
                )
                return False
        except Exception as exc:  # noqa: BLE001 —— 探测失败也先别投
            self.stats.add("skipped", f"探测会话失败：{type(exc).__name__}")
            return False

        title = str(message.get("title") or "").strip()
        if title:
            text = f"{text}（分享视频：{title}）"

        prompt = (
            "【主人的新私信】" + text + chr(10) + chr(10)
            + "请调用 dm_inbox 取出待回复的私信，用你自己的判断写回复，"
            + "然后调 dm_reply 发出。不要用模板。"
            + "（如果豆包已经先回过一条占位，你仍然用 dm_reply 给出正式回答；"
            + "占位不会占用你的回复名额。）"
        )
        try:
            from core.dm_deliver import deliver_async

            result = await deliver_async(prompt)
            if result.ok and result.delivered:
                # ★ 标记已投递：防止下一轮重复投同一条
                seqno = int(message.get("msg_seqno") or 0)
                if seqno > 0:
                    # 标记失败不该影响投递结果
                    with contextlib.suppress(Exception):
                        self._handle().execute(
                            "UPDATE dm_inbox SET delivered_at = ? WHERE msg_seqno = ?",
                            (int(time.time()), seqno),
                        )
                self.stats.add("delivered", f"已投递给值班员（{result.session_id[-8:]}）")
                return True
            self.stats.add(
                "skipped", f"投递失败 {result.error_code}：{result.message[:60]}"
            )
        except Exception as exc:  # noqa: BLE001 —— 投递失败不该让监听崩
            self.stats.add("skipped", f"投递异常：{type(exc).__name__}")
        return False

    async def _doubao_factory(self, messages: list[dict[str, Any]]) -> str:
        """默认回复生成器：**用豆包**（主人选的方案 A）。

        ★ 为什么要带历史：主人说过「可以查看我前面的发言并回复我吗」——
        只给最后一句会让回复显得莫名其妙。

        ★ 失败时抛异常，由 `_compose_reply` 退回固定话术（不让监听崩）。
        """
        incoming = str(messages[-1].get("text") or "") if messages else ""
        if not incoming:
            return ""
        # 把队列里的前几条当历史（同一批消息就是"前面的发言"）
        history = [
            {"text": str(m.get("text") or ""), "from_owner": True,
             "title": str(m.get("title") or "")}
            for m in messages[:-1]
        ]
        # ★ 带上项目上下文（主人要求：「没法给豆包塞上下文让他看懂浅层内容吗」）
        context = ""
        try:
            from core.context_pack import build_context
            from core.paths import data_dir, project_root

            bundle = build_context(
                self._handle(),
                digest_dir=data_dir() / "digests",
                data_dir=data_dir(),
                # ★ 人设全文（8,940 字）—— 主人要求"尽可能了解我的想法"
                persona_path=project_root() / "docs" / "persona.md",
                # AGENTS.md 交给 build_context 自动发现（全局那份才全）
            )
            context = bundle.to_prompt()
        except Exception:  # noqa: BLE001 —— 拿不到上下文也要能回（降级）
            context = ""

        result = await generate_reply(
            incoming=incoming, history=history, config=self.config, context=context
        )
        if not result.ok:
            raise RuntimeError(f"{result.error_code}: {result.message}")
        return result.text

    async def _compose_reply(self, messages: list[dict[str, Any]]) -> str:
        """决定回什么。

        优先级：外部生成器（将来接豆包）> 固定话术 > :data:`FALLBACK_REPLY`。
        """
        if self.reply_factory is not None:
            try:
                generated = await self.reply_factory(messages)
                text = str(generated or "").strip()
                if text:
                    return text[:MAX_DM_CHARS]
            except Exception as exc:  # noqa: BLE001 —— 生成失败就退回兜底
                self.stats.add("error", f"生成回复失败：{type(exc).__name__}: {exc}")
        return (self.reply_text or FALLBACK_REPLY)[:MAX_DM_CHARS]

    # ---- 主循环 ------------------------------------------------------------

    async def _cycle_loop(self) -> None:
        """随 DSH 自动跑学习轮（R1）——与私信监听共用这个常驻进程。"""
        from core.cycle_scheduler import CycleScheduler

        scheduler = CycleScheduler(
            db=self._handle(),
            config=self.config,
            session_id=self._duty_session_id(),
            start_delay=self.cycle_start_delay,
            check_interval=self.cycle_interval,
        )
        self._scheduler = scheduler
        self.stats.add("poll", "自动学习轮已启用（跟随 DSH 启停）")
        try:
            await scheduler.run()
        except asyncio.CancelledError:
            scheduler.stop()
            raise
        except Exception as exc:  # noqa: BLE001 - 调度失败不能让监听死
            self.stats.add("error", f"自动学习轮异常：{type(exc).__name__}: {exc}")

    async def run(self) -> None:
        """**常驻**循环 —— 这就是"能及时回复"的关键。

        参考项目是 `while self.running` + `asyncio.sleep(1)`；
        我们直接按配置的间隔 sleep，省一点空转。
        """
        self.stats.add("poll", f"监听已启动（每 {self.interval} 秒查一次未读）")
        cycle_task = (
            asyncio.create_task(self._cycle_loop()) if self.auto_cycle else None
        )
        while not self._stop:
            try:
                await self.poll_once_round()
            except asyncio.CancelledError:
                self.stats.add("poll", "监听被取消")
                break
            except Exception as exc:  # noqa: BLE001 —— 单轮失败不能让整个监听死
                # ★ 参考项目也这么做：`gather(return_exceptions=True)`。
                #   监听循环一旦因为一次网络抖动退出，就再也收不到消息了。
                self.stats.errors += 1
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                self.stats.add("error", self.stats.last_error)

            # 分片 sleep：让 stop() 能快速生效
            slept = 0.0
            while slept < self.interval and not self._stop:
                await asyncio.sleep(min(1.0, self.interval - slept))
                slept += 1.0

        if cycle_task is not None:
            cycle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await cycle_task
        self.stats.add("poll", "监听已停止")
