"""评论发出后的**公开可见性复核**（移植自竞品 ``dsh-bili-scroll`` 的 ``lib/verify.mjs``）。

## 为什么要有它

**接口回「发送成功」≠ 公开可见** —— 新号 / 风控会让评论进审核，你自己看不见，
而程序以为发出去了、还把它记进判重表 ⇒ 下一轮不再发 ⇒ **一条都没到人**。
还有更坏的一层：不公开时**连通知都不会推**，于是"@ 了但没人收到"。

## 复核靠两条只读接口（竞品 2026-10-16 实测可行，**照抄不换**）

=========================  ===========================================================
顶层评论                   ``GET /x/v2/reply/main?type=1&oid=<aid>&mode=2&next=0&ps=20``
                           ``mode=2`` 是**按时间** —— 刚发出来的通常在**第一页**
楼中楼                     ``GET /x/v2/reply/reply?type=1&oid=<aid>&root=<root>&ps=20&pn=1``
=========================  ===========================================================

⚠️ **看不到 ≠ 没发出**（翻页 / 热度排序都会漏）⇒ 结论一律写「**没查到**」，
**绝不**写「没发出去」—— 观测不到不能反推不存在。
✗ ``/x/v2/reply/detail`` 走不通（回 ``-400 请求错误``），别无谓地换它。

## 与竞品的差异（本项目约定）

* 判重来源：竞品读 ``seen.md``；我们从 SQLite ``actions`` 表取
  ``action_type IN ('top','reply')`` + ``status='sent_verified'`` + ``remote_rpid`` 非空的记录。
* 状态账本：竞品写 ``verify-state.json``；我们落 SQLite 表 ``comment_verifications``
  （迁移 v5，见 :mod:`core.schema`）。**已确认公开可见的 7 天内不重复查。**
* 读取一律经 :class:`core.ratelimit.RequestQueue`（``reader.queue.get_json``），不自己发 HTTP。
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from core import health
from core.bilibili import API_BASE, BilibiliReader, CommentItem, VideoDetail, _parse_comment_page
from core.ratelimit import RequestsBlocked
from core.redact import redact
from core.storage import Database, now_ts

# ---- 竞品实测的端点（**照抄，不要换**）-------------------------------------

#: 顶层评论。``mode=2`` = **按时间**，刚发的在第一页。
TOP_REPLY_PATH = "/x/v2/reply/main"

#: 楼中楼：直接按 ``root`` 拉那一串。
SUB_REPLY_PATH = "/x/v2/reply/reply"

# ⚠️ ``/x/v2/reply/detail`` 走不通（回 -400 请求错误）—— 别换它。

# ---- 竞品实测的节奏与账本 TTL ----------------------------------------------

#: 每条之间 ``sleep(700)``（竞品 ms）。
ITEM_PAUSE_SECONDS = 0.7

#: 取视频信息后 ``sleep(300)``（竞品 ms）。
VIDEO_INFO_PAUSE_SECONDS = 0.3

#: 已确认公开可见的记录，**7 天内**不重复查（竞品 ``7 * 864e5`` ms）。
VISIBLE_TTL_SECONDS = 7 * 86400

#: 一次最多扫多少条"我们发过的评论"（actions 表可能很长，必须有界）。
LEDGER_SCAN_LIMIT = 200

# ---- 措辞（弱断言纪律）-----------------------------------------------------

#: 查不到时的结论词。**只写「没查到」，绝不写「没发出去」。**
MISSING_WORD = "没查到"

#: 顶层评论的依据说明（竞品原文）。
TOP_HOW = "顶层·最新一页"

#: 取不到视频信息时的依据说明（竞品原文）。
VIDEO_INFO_HOW = "取视频信息失败"

#: 平台健康门禁把复核挡住时的依据说明（本项目独有）。
HEALTH_BLOCKED_HOW = "平台健康门禁"

#: 观测不到 ≠ 不存在 —— 附在每条"没查到"后面。
#: ★ 刻意**不用**竞品那句「（不等于没发出去）」：本项目纪律是连这个说法都不写。
NOT_ABSENT = "（**观测不到 ≠ 不存在**）"


@dataclass(frozen=True)
class VerifyResult:
    """一条评论的复核结论。

    :param visible: ``True`` = 已确认公开可见；``None`` = **没查到**
        （⚠️ 不是 ``False`` —— 观测不到不能反推不存在）。
    :param likes: 公开可见时顺便读到的赞数；没查到为 ``None``。
    :param how: 依据，如 ``"顶层·最新一页"`` / ``"楼中楼·root=123"``。
    :param note: 附注（invisible 标记 / 为什么没查到）。
    """

    bvid: str
    rpid: str
    visible: bool | None
    likes: int | None
    how: str
    note: str


@dataclass(frozen=True)
class SentRecord:
    """一条**我们确实发出去了**的评论（来自 ``actions`` 表）。"""

    rpid: str
    bvid: str
    action_type: str = ""
    agent_mid: int = 0
    root: str = ""
    action_id: str = ""


@runtime_checkable
class VerifyRuntime(Protocol):
    """``verify_recent`` 需要的最小运行时（``core.runtime.Runtime`` 天然满足）。"""

    @property
    def db(self) -> Database: ...

    @property
    def reader(self) -> BilibiliReader: ...


async def _pause(seconds: float) -> None:
    """复核之间的礼貌间隔（竞品：每条 700ms、取视频信息后 300ms）。

    单独抽成模块级函数，测试里可以整体替换掉，避免真等。
    """
    await asyncio.sleep(seconds)


# ---- 记录与账本 -------------------------------------------------------------


def _field(record: sqlite3.Row | Mapping[str, Any], key: str, default: str = "") -> str:
    """宽容地读一个字段（``sqlite3.Row`` 与 mapping 都支持，缺字段给默认值）。"""
    try:
        value = record[key]
    except (IndexError, KeyError):
        return default
    if value is None:
        return default
    return str(value)


def _int_field(record: sqlite3.Row | Mapping[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(_field(record, key) or default)
    except ValueError:
        return default


def _coerce_record(
    record: SentRecord | sqlite3.Row | Mapping[str, Any],
) -> SentRecord:
    """把 ``actions`` 表的一行（或 mapping）收成 :class:`SentRecord`。"""
    if isinstance(record, SentRecord):
        return record
    return SentRecord(
        rpid=_field(record, "remote_rpid") or _field(record, "rpid"),
        bvid=_field(record, "bvid"),
        action_type=_field(record, "action_type"),
        agent_mid=_int_field(record, "agent_mid"),
        # 楼中楼要额外记"根评论 rpid"。来源按可靠性排序：
        #   ① ``drafts.target_root`` —— 发回复时显式记下的**根评论**（最准）；
        #   ② ``actions.target_rpid`` —— 被回复的那条评论（次选兜底：
        #      回复根评论时它等价；回复二级评论时它只是"父"，
        #      拿它当 root 去拉整串可能拉不到，但比没有强）；
        #   ③ 显式 ``root`` 字段（测试 / 外部传入用）。
        root=(
            _field(record, "target_root")
            or _field(record, "target_rpid")
            or _field(record, "root")
        ),
        action_id=_field(record, "action_id"),
    )


_SENT_SQL = """
SELECT a.action_id, a.agent_mid, a.action_type, a.bvid, a.target_rpid,
       a.remote_rpid, a.status, a.created_at, a.updated_at, d.target_root
  FROM actions a
  LEFT JOIN drafts d ON d.draft_id = a.draft_id
 WHERE a.action_type IN ('top','reply')
   AND a.status = 'sent_verified'
   AND a.remote_rpid IS NOT NULL
   AND a.remote_rpid <> ''
 ORDER BY a.updated_at DESC, a.created_at DESC, a.action_id DESC
 LIMIT ?
"""


def recent_sent_records(db: Database, *, limit: int = LEDGER_SCAN_LIMIT) -> list[SentRecord]:
    """从 ``actions`` 表取「最近发过的评论」（**最新在前**）。

    条件照本项目约定：``action_type IN ('top','reply')`` 且
    ``status='sent_verified'`` 且 ``remote_rpid`` 非空。
    """
    return [_coerce_record(row) for row in db.query(_SENT_SQL, (limit,))]


def _ledger_get(db: Database, rpid: str) -> sqlite3.Row | None:
    return db.query_one(
        "SELECT * FROM comment_verifications WHERE remote_rpid = ?", (rpid,)
    )


def _ledger_put(db: Database, *, result: VerifyResult, checked_at: int) -> None:
    """把这次复核结论写进账本（同一 rpid 覆盖）。

    ``visible``：``True`` → 1，``False`` → 0，``None``（没查到）→ ``NULL``。
    """
    visible: int | None = None if result.visible is None else (1 if result.visible else 0)
    db.execute(
        """
        INSERT INTO comment_verifications
            (remote_rpid, bvid, visible, likes, how, note, checked_at)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(remote_rpid) DO UPDATE SET
            bvid = excluded.bvid,
            visible = excluded.visible,
            likes = excluded.likes,
            how = excluded.how,
            note = excluded.note,
            checked_at = excluded.checked_at
        """,
        (
            result.rpid,
            result.bvid,
            visible,
            result.likes,
            result.how,
            result.note,
            checked_at,
        ),
    )


def _recently_confirmed_visible(db: Database, rpid: str, *, now: int) -> bool:
    """该 rpid 是否**在 7 天内已确认公开可见**（⇒ 不必再查）。"""
    row = _ledger_get(db, rpid)
    if row is None:
        return False
    visible = row["visible"]
    if visible is None or int(visible) != 1:
        return False
    return now - int(row["checked_at"]) < VISIBLE_TTL_SECONDS


# ---- 单条复核 ---------------------------------------------------------------


@dataclass(frozen=True)
class _Page:
    """一次评论读取的结果（读不到时 ``error`` 非空）。"""

    replies: list[CommentItem]
    invisible: dict[str, bool]
    error: str = ""
    blocked_state: str = ""


def _invisible_map(payload: Any) -> dict[str, bool]:
    """从**原始**响应里取每条评论的 ``invisible`` 标记。

    ⚠️ 为什么要绕过 :class:`core.bilibili.CommentItem`：它不带这个字段，
    而措辞纪律要求"接口标了 invisible"必须如实附注。
    """
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("replies") or []
    if not isinstance(raw, list):
        return {}
    out: dict[str, bool] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        rpid = str(item.get("rpid_str") or item.get("rpid") or "")
        if rpid:
            out[rpid] = bool(item.get("invisible"))
    return out


async def _fetch_page(
    reader: BilibiliReader, path: str, params: dict[str, Any]
) -> _Page:
    """按路径读一页评论。**任何失败都收成状态，不抛异常。**

    ``use_cache=False``：复核要的是**当下**的可见性，
    不能吃 5 分钟前的评论缓存（否则刚发的评论永远"没查到"）。
    """
    try:
        outcome = await reader.queue.get_json(
            f"{API_BASE}{path}",
            params=params,
            kind="comment",
            use_cache=False,
            cookies=reader.cookies or None,
        )
    except RequestsBlocked as exc:
        return _Page([], {}, error=exc.state, blocked_state=exc.state)
    except Exception as exc:  # noqa: BLE001 - 读取失败一律按"没查到"处理，绝不崩
        return _Page([], {}, error=type(exc).__name__)

    if outcome.data is None:
        raw_code = outcome.code
        code = str(raw_code) if raw_code is not None else (outcome.message or "读取失败")
        return _Page([], {}, error=code)

    page = _parse_comment_page(outcome.data)
    return _Page(page.replies, _invisible_map(outcome.data))


def _health_gate(reader: BilibiliReader) -> str:
    """当前是否处于"停止一切自动请求"的健康状态。``""`` 表示可以继续。

    ★ 与 :meth:`RequestQueue.ensure_allowed` 同源（读的是同一份本地状态），
    这里只是**提前**看一眼，好给出比"取视频信息失败"更准的话。
    """
    try:
        state = reader.queue.db.health_state()
    except Exception:  # noqa: BLE001 - 读不到本地状态不该阻断复核
        return ""
    return state if state in health.BLOCKING_STATES else ""


async def _video_detail(
    reader: BilibiliReader,
    bvid: str,
    aid_cache: MutableMapping[str, VideoDetail | None],
) -> VideoDetail | None:
    """取视频信息（``aid`` 等），同一个 bvid 只取一次；取不到记 ``None``。"""
    if bvid in aid_cache:
        return aid_cache[bvid]
    detail: VideoDetail | None
    try:
        detail = await reader.get_video_detail(bvid)
    except Exception:  # noqa: BLE001 - 取不到就是这条跳过，不是"没发出"
        detail = None
    aid_cache[bvid] = detail
    await _pause(VIDEO_INFO_PAUSE_SECONDS)  # 竞品：取视频信息后 sleep(300)
    return detail


def _find_top_hit(replies: list[CommentItem], rec: SentRecord) -> CommentItem | None:
    """顶层命中判定：先按 ``rpid`` 相等；退化用 ``mid`` 匹配自己的 uid（竞品同款兜底）。"""
    for item in replies:
        if item.rpid == rec.rpid:
            return item
    if rec.agent_mid:
        for item in replies:
            if item.author_mid == rec.agent_mid:
                return item
    return None


def _invisible_note(invisible: bool) -> str:
    return "（但接口标了 invisible）" if invisible else ""


def _error_note(error: str) -> str:
    return f"接口没读回来（{error}）—— {MISSING_WORD}，稍后可以再复核{NOT_ABSENT}"


def _result(
    rec: SentRecord,
    *,
    visible: bool | None = None,
    likes: int | None = None,
    how: str = "",
    note: str = "",
) -> VerifyResult:
    return VerifyResult(
        bvid=rec.bvid, rpid=rec.rpid, visible=visible, likes=likes, how=how, note=note
    )


async def verify_one(
    reader: BilibiliReader,
    record: SentRecord | sqlite3.Row | Mapping[str, Any],
    *,
    aid_cache: MutableMapping[str, VideoDetail | None],
) -> VerifyResult:
    """复核**一条**评论是否公开可见。

    先取视频信息（拿 ``aid``），再按有没有 ``root`` 走两条路：

    * 无 ``root`` → 顶层评论（按时间第一页）；
    * 有 ``root`` → 楼中楼，直接拉那一串。

    找不到时 ``visible=None``（**没查到**），绝不写成"没发出去"。
    任何读取失败都不抛异常，收成 :class:`VerifyResult`。

    :param aid_cache: 跨条复用的 ``bvid → VideoDetail | None`` 缓存（调用方持有）。
    """
    rec = _coerce_record(record)

    blocked = _health_gate(reader)
    if blocked:
        return _result(
            rec,
            how=HEALTH_BLOCKED_HOW,
            note=f"平台健康状态 {blocked}：已停止复核，需人工处理后继续",
        )

    detail = await _video_detail(reader, rec.bvid, aid_cache)
    if detail is None:
        if blocked := _health_gate(reader):
            # ★ 取不到视频信息**也可能是被健康门禁挡了**（不是"视频已删"）。
            #   含验证码的提示只经 message 传递，而失败路径不返回 message，
            #   所以这里主动看一眼状态，否则会误报成"视频可能已删"。
            return _result(
                rec,
                how=HEALTH_BLOCKED_HOW,
                note=f"平台健康状态 {blocked}：已停止复核，需人工处理后继续",
            )
        return _result(
            rec,
            how=VIDEO_INFO_HOW,
            note="这条跳过（视频可能已删/不可见）",
        )
    aid = detail.aid

    # ① 顶层：按时间第一页（刚发的就在这儿）
    if not rec.root or rec.root == "0":
        page = await _fetch_page(
            reader,
            TOP_REPLY_PATH,
            {"type": 1, "oid": aid, "mode": 2, "next": 0, "ps": 20},
        )
        if page.blocked_state:
            return _result(
                rec,
                how=HEALTH_BLOCKED_HOW,
                note=f"平台健康状态 {page.blocked_state}：已停止复核，需人工处理后继续",
            )
        if page.error:
            return _result(rec, how=TOP_HOW, note=_error_note(page.error))

        hit = _find_top_hit(page.replies, rec)
        if hit is not None and hit.rpid == rec.rpid:
            return _result(
                rec,
                visible=True,
                likes=hit.like_count,
                how=TOP_HOW,
                note=_invisible_note(page.invisible.get(hit.rpid, False)),
            )
        return _result(
            rec,
            how=TOP_HOW,
            note=(
                f"最新一页里没找到我们这条 —— {MISSING_WORD}，可能还在审核，"
                f"也可能只是翻页/热度排序看不到{NOT_ABSENT}"
            ),
        )

    # ② 楼中楼：按 root 拉那一串（竞品这里**不做** mid 兜底）
    how = f"楼中楼·root={rec.root}"
    page = await _fetch_page(
        reader,
        SUB_REPLY_PATH,
        {"type": 1, "oid": aid, "root": rec.root, "ps": 20, "pn": 1},
    )
    if page.blocked_state:
        return _result(
            rec,
            how=HEALTH_BLOCKED_HOW,
            note=f"平台健康状态 {page.blocked_state}：已停止复核，需人工处理后继续",
        )
    if page.error:
        return _result(rec, how=how, note=_error_note(page.error))

    for item in page.replies:
        if item.rpid == rec.rpid:
            return _result(
                rec,
                visible=True,
                likes=item.like_count,
                how=how,
                note=_invisible_note(page.invisible.get(item.rpid, False)),
            )

    if page.replies:
        note = (
            f"那一串里有 {len(page.replies)} 条，但没有我们这条 —— {MISSING_WORD}，"
            f"可能还在审核{NOT_ABSENT}"
        )
    else:
        note = f"那一串暂时拉不到内容 —— {MISSING_WORD}，可能还在审核{NOT_ABSENT}"
    return _result(rec, how=how, note=note)


# ---- 批量复核 ---------------------------------------------------------------


def _verdict_text(result: VerifyResult) -> str:
    """一条复核结论的人话（竞品原文：``公开可见（赞 N）`` / ``没查到``）。"""
    if result.visible is True:
        likes = 0 if result.likes is None else result.likes
        return f"公开可见（赞 {likes}）"
    return MISSING_WORD


def _render(checked: list[VerifyResult], skipped: int, halted: str) -> str:
    """拼出可直接追加进日报的 Markdown（标题照抄竞品）。"""
    lines = ["", "## 评论复核（宿主自动跑的，不是那一轮自己说的）", ""]
    for item in checked:
        suffix = f" · {item.note}" if item.note else ""
        lines.append(
            f"- `{item.bvid}` rpid **{item.rpid}** —— {_verdict_text(item)}"
            f" · 依据：{item.how}{suffix}"
        )
    if skipped:
        lines.append(f"- （另外 {skipped} 条最近已复核过，没重复查）")
    if halted:
        lines.append(
            f"- ⚠️ 平台健康状态 **{halted}**：已停止本轮复核，"
            "需人工处理（人用 CLI 恢复）后再继续。"
        )
    return redact("\n".join(lines))


async def verify_recent(
    runtime_or_db: Database | VerifyRuntime, *, max_items: int = 6
) -> tuple[list[VerifyResult], int, str]:
    """复核最近发过的一批评论（默认最多 6 条，跳过 7 天内已确认可见的）。

    :param runtime_or_db: :class:`core.runtime.Runtime`（有 ``db`` 与 ``reader``）
        或裸 :class:`core.storage.Database`（后者没有读取器，只能返回提示）。
    :param max_items: 本轮最多真正复核几条（竞品默认 6）。
    :returns: ``(已复核列表, 跳过条数, 可直接追加进日报的 Markdown 文本)``
    """
    if max_items < 1:
        return [], 0, "⚠️ max_items 必须 ≥ 1"

    if isinstance(runtime_or_db, Database):
        return [], 0, "⚠️ 没有可用的读取器（reader），没法复核 —— 复核要读评论区"

    db = runtime_or_db.db
    reader = runtime_or_db.reader

    records = recent_sent_records(db)
    if not records:
        return [], 0, "还没有「我们发过的评论」记录（actions 表里没有已确认发出的顶层评论/回复）"

    now = now_ts()
    fresh: list[SentRecord] = []
    skipped = 0
    for rec in records:  # 最新在前
        if _recently_confirmed_visible(db, rec.rpid, now=now):
            skipped += 1
            continue
        fresh.append(rec)
        if len(fresh) >= max_items:
            break

    if not fresh:
        return [], skipped, f"都复核过了（跳过的旧记录 {skipped} 条）"

    aid_cache: dict[str, VideoDetail | None] = {}
    checked: list[VerifyResult] = []
    halted = ""
    for rec in fresh:
        result = await verify_one(reader, rec, aid_cache=aid_cache)
        checked.append(result)
        _ledger_put(db, result=result, checked_at=now)

        # ★ 验证码 / 认证失效 / 平台故障 → **立即停止**（本项目红线：
        #   出现风控提示不加大重试强度，等人工处理）。
        state = db.health_state()
        if result.how == HEALTH_BLOCKED_HOW or state in health.BLOCKING_STATES:
            halted = state
            break

        await _pause(ITEM_PAUSE_SECONDS)  # 竞品：每条之间 sleep(700)

    return checked, skipped, _render(checked, skipped, halted)


# ---- 追加进日报 -------------------------------------------------------------


def append_verify_to_report(digest_path: Path | str, text: str) -> bool:
    """把复核结果追加到**当天日报**末尾。

    日报可能还不存在 —— **那就别硬造一份**（竞品同款：不存在就返回失败）。
    写进去的文本会过 :func:`core.redact.redact`。

    :returns: 写成功 ``True``；日报不存在或写不了 ``False``。
    """
    target = Path(digest_path)
    try:
        if not target.exists():
            return False
        existing = target.read_text(encoding="utf-8")
        target.write_text(existing.rstrip() + "\n" + redact(text) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class ReadbackResult:
    """一次**发布读回核验**的结论（#4）。

    与 :class:`VerifyResult` 的区别：这里回答的是"刚发出去的那条，
    远端作者 / 正文 / 所属视频对不对"，不是"后来公开可见没有"。
    """

    ok: bool
    reason: str
    bvid: str = ""
    rpid: str = ""
    author_mid: int | None = None
    message: str = ""


_SPACE_RE = re.compile(r"\s+")


def _normalize_comment(text: str) -> str:
    """对比评论正文：去掉空白/换行，其余**逐字**比较。"""
    return _SPACE_RE.sub("", text or "").strip()


async def readback_comment(
    reader: BilibiliReader,
    *,
    bvid: str,
    aid: int,
    rpid: str,
    agent_mid: int,
    expected_text: str,
    root: str = "",
) -> ReadbackResult:
    """读回**刚发出的**评论并核对作者 / 正文 / 所属视频（只读、不重试）。

    顶层按 ``mode=2``（时间序）拉第一页；楼中楼按 ``root`` 拉那一串。
    ``oid=aid`` 来自本地视频记录，因此"查得到这条 rpid"本身就证明了
    它属于该 BV —— 不需要（也没有）按评论反查 bvid 的接口。
    """
    if not rpid:
        return ReadbackResult(False, "NO_REMOTE_RPID", bvid=bvid)

    blocked = _health_gate(reader)
    if blocked:
        return ReadbackResult(False, f"READBACK_BLOCKED:{blocked}", bvid=bvid, rpid=rpid)

    if root and root != "0":
        path = SUB_REPLY_PATH
        params: dict[str, Any] = {"type": 1, "oid": aid, "root": root, "ps": 20, "pn": 1}
    else:
        path = TOP_REPLY_PATH
        params = {"type": 1, "oid": aid, "mode": 2, "next": 0, "ps": 20}

    page = await _fetch_page(reader, path, params)
    if page.blocked_state:
        return ReadbackResult(
            False, f"READBACK_BLOCKED:{page.blocked_state}", bvid=bvid, rpid=rpid
        )
    if page.error:
        return ReadbackResult(False, f"READBACK_ERROR:{page.error}", bvid=bvid, rpid=rpid)

    hit = next((item for item in page.replies if item.rpid == rpid), None)
    if hit is None:
        return ReadbackResult(False, "READBACK_NOT_FOUND", bvid=bvid, rpid=rpid)
    if int(hit.author_mid) != int(agent_mid):
        return ReadbackResult(
            False, "AUTHOR_MISMATCH", bvid=bvid, rpid=rpid,
            author_mid=hit.author_mid, message=hit.message,
        )
    if root and root != "0" and str(hit.root) != str(root):
        return ReadbackResult(
            False, "ROOT_MISMATCH", bvid=bvid, rpid=rpid,
            author_mid=hit.author_mid, message=hit.message,
        )
    if _normalize_comment(hit.message) != _normalize_comment(expected_text):
        return ReadbackResult(
            False, "CONTENT_MISMATCH", bvid=bvid, rpid=rpid,
            author_mid=hit.author_mid, message=hit.message,
        )
    return ReadbackResult(
        True, "OK", bvid=bvid, rpid=rpid,
        author_mid=hit.author_mid, message=hit.message,
    )


async def readback_pending(
    db: Database, reader: BilibiliReader, *, limit: int = 10
) -> list[tuple[str, ReadbackResult]]:
    """重试**待核验**动作的只读读回；匹配才升 ``sent_verified``（#4）。

    只读、不重发；查不到 / 不匹配保持 ``sent_pending_verify`` 并记原因。
    """
    if limit < 1:
        return []
    return await _readback_pending_impl(db, reader, limit=limit)


async def _readback_pending_impl(
    db: Database, reader: BilibiliReader, *, limit: int
) -> list[tuple[str, ReadbackResult]]:
    rows = db.query(
        """
        SELECT a.action_id, a.bvid, a.remote_rpid, a.action_type, a.agent_mid,
               a.target_rpid, d.draft_id AS draft_id, d.text AS draft_text,
               d.target_root AS draft_root
          FROM actions a
          LEFT JOIN drafts d ON d.draft_id = a.draft_id
         WHERE a.status = 'sent_pending_verify'
           AND a.remote_rpid IS NOT NULL AND a.remote_rpid <> ''
         ORDER BY a.updated_at ASC, a.created_at ASC
         LIMIT ?
        """,
        (limit,),
    )
    results: list[tuple[str, ReadbackResult]] = []
    for row in rows:
        action_id = str(row["action_id"])
        bvid = str(row["bvid"])
        video = db.get_video(bvid)
        aid = int(video["aid"]) if video is not None and video["aid"] is not None else 0
        if aid <= 0:
            result = ReadbackResult(
                ok=False, reason="VIDEO_AID_UNKNOWN", bvid=bvid,
                rpid=str(row["remote_rpid"] or ""),
            )
        else:
            draft_text = str(row["draft_text"] or "")
            if not draft_text:
                result = ReadbackResult(
                    ok=False, reason="NO_DRAFT_TEXT", bvid=bvid,
                    rpid=str(row["remote_rpid"] or ""),
                )
            else:
                root = (
                    str(row["draft_root"] or "")
                    if str(row["action_type"]) == "reply"
                    else ""
                )
                result = await readback_comment(
                    reader,
                    bvid=bvid,
                    aid=aid,
                    rpid=str(row["remote_rpid"] or ""),
                    agent_mid=int(row["agent_mid"] or 0),
                    expected_text=draft_text,
                    root=root,
                )
        results.append((action_id, result))

        if result.ok:
            db.settle_action(action_id=action_id, status="sent_verified")
            draft_id = str(row["draft_id"] or "")
            if draft_id:
                db.set_draft_status(draft_id, "sent_verified")
        else:
            db.settle_action(
                action_id=action_id,
                status="sent_pending_verify",
                error_code=result.reason,
            )

        if db.health_state() in health.BLOCKING_STATES:
            break
        await _pause(ITEM_PAUSE_SECONDS)

    return results


__all__ = [
    "HEALTH_BLOCKED_HOW",
    "ITEM_PAUSE_SECONDS",
    "LEDGER_SCAN_LIMIT",
    "MISSING_WORD",
    "NOT_ABSENT",
    "SUB_REPLY_PATH",
    "TOP_HOW",
    "TOP_REPLY_PATH",
    "VIDEO_INFO_HOW",
    "VIDEO_INFO_PAUSE_SECONDS",
    "VISIBLE_TTL_SECONDS",
    "SentRecord",
    "ReadbackResult",
    "VerifyResult",
    "VerifyRuntime",
    "append_verify_to_report",
    "readback_comment",
    "readback_pending",
    "recent_sent_records",
    "verify_one",
    "verify_recent",
]
