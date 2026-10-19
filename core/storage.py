"""SQLite 存储层：WAL、可重复迁移、事务化配额预留。

设计约束（任务书 §5）：

* SQLite 使用 **WAL**，单实例任务锁（锁在 :mod:`core.lock`）。
* 迁移**可重复运行**。
* 每日额度的预留/提交在**事务**中记录；``uncertain`` **占用额度**，不靠时间自动释放，
  进程崩溃后不超额。
* 模拟与真实动作**分开存储/计数**（``actions.is_simulated``）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core import schema
from core.paths import db_path as default_db_path

TOP_ACTION = "top"
REPLY_ACTION = "reply"
LIKE_ACTION = "like"
FAVORITE_ACTION = "favorite"
DYNAMIC_ACTION = "dynamic"

#: 参与**幂等**的动作（like/favorite 用 bvid 维度；dynamic 无 bvid）
IDEMPOTENT_ACTIONS: frozenset[str] = frozenset(
    {TOP_ACTION, REPLY_ACTION, LIKE_ACTION, FAVORITE_ACTION, DYNAMIC_ACTION}
)


def now_ts() -> int:
    return int(time.time())


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ActionRecord:
    action_id: str
    agent_mid: int
    action_type: str
    bvid: str
    target_rpid: str | None
    idempotency_key: str
    draft_id: str | None
    status: str
    remote_rpid: str | None
    remote_url: str | None
    is_simulated: bool
    error_code: str | None


class Database:
    """一个连接 = 一个 :class:`Database` 实例。线程/任务内使用。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._configure()

    # ---- 生命周期 ----------------------------------------------------------

    def _configure(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")

    def close(self) -> None:
        with suppress(sqlite3.Error):
            self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        """显式事务。异常回滚。

        ``isolation_level=None`` 下这是唯一正确的做法：
        绝不让中间态（如 ``reserved``）在崩溃后处于半写状态。
        """
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            yield cur
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    # ---- 迁移 --------------------------------------------------------------

    def migrate(self) -> int:
        """可重复运行的迁移。返回当前 schema 版本。"""
        cur = self._conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
        )
        row = cur.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = int(row["v"]) if row and row["v"] is not None else 0

        for version, script in schema.MIGRATIONS:
            if version <= current:
                continue
            if version != current + 1:
                # ★ 顺序护栏（2026-10-19）：把新版本插在 MIGRATIONS 中间会
                #   让"中间那段"整段被跳过（循环只把 current 推到已执行的版本）。
                #   症状是**静默缺表**：`no such table: trends_rounds`。
                #   宁可在迁移时 loud 报错，也不要让库悄悄缺表。
                raise ValueError(
                    f"迁移必须严格递增：当前 {current}，却要应用 {version}；"
                    "新版本只能**追加到 MIGRATIONS 末尾**"
                )
            with self.transaction() as tx:
                tx.executescript(script)
                tx.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (version, now_ts()),
                )
            current = version
        return current

    def schema_version(self) -> int:
        try:
            row = self._conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row["v"]) if row and row["v"] is not None else 0

    # ---- 通用 --------------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        # sqlite3 的类型标注把 fetchone() 标成 Any；本连接设了 row_factory，
        # 运行时必然是 sqlite3.Row。这里收窄类型。
        row: sqlite3.Row | None = self._conn.execute(sql, tuple(params)).fetchone()
        return row

    # ---- videos ------------------------------------------------------------

    def upsert_video(
        self,
        *,
        bvid: str,
        aid: int | None = None,
        title: str = "",
        up_mid: int | None = None,
        up_name: str = "",
        published_at: int | None = None,
        duration_sec: int | None = None,
        source_url: str = "",
        discovery_keyword: str = "",
    ) -> bool:
        """插入或刷新视频。返回 ``True`` 表示本次是**新发现**。"""
        ts = now_ts()
        existing = self.query_one("SELECT bvid FROM videos WHERE bvid = ?", (bvid,))
        if existing is None:
            self.execute(
                """
                INSERT INTO videos (bvid, aid, title, up_mid, up_name, published_at,
                                    duration_sec, source_url, first_seen_at, last_seen_at,
                                    status, discovery_keyword)
                VALUES (?,?,?,?,?,?,?,?,?,?,'discovered',?)
                """,
                (
                    bvid,
                    aid,
                    title,
                    up_mid,
                    up_name,
                    published_at,
                    duration_sec,
                    source_url,
                    ts,
                    ts,
                    discovery_keyword,
                ),
            )
            return True

        # 已存在：只刷新"最后发现"与可能补全的元数据，**不重置 status**。
        self.execute(
            """
            UPDATE videos
               SET last_seen_at = ?,
                   aid = COALESCE(?, aid),
                   title = CASE WHEN ? <> '' THEN ? ELSE title END,
                   up_mid = COALESCE(?, up_mid),
                   up_name = CASE WHEN ? <> '' THEN ? ELSE up_name END,
                   published_at = COALESCE(?, published_at),
                   duration_sec = COALESCE(?, duration_sec),
                   source_url = CASE WHEN ? <> '' THEN ? ELSE source_url END
             WHERE bvid = ?
            """,
            (
                ts,
                aid,
                title,
                title,
                up_mid,
                up_name,
                up_name,
                published_at,
                duration_sec,
                source_url,
                source_url,
                bvid,
            ),
        )
        return False

    def set_video_status(self, bvid: str, status: str) -> None:
        self.execute(
            "UPDATE videos SET status = ?, last_seen_at = ? WHERE bvid = ?",
            (status, now_ts(), bvid),
        )

    def get_video(self, bvid: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM videos WHERE bvid = ?", (bvid,))

    # ---- evidence ----------------------------------------------------------

    def add_evidence(
        self,
        *,
        bvid: str,
        source_type: str,
        text: str,
        source_url: str = "",
        start_sec: float | None = None,
        end_sec: float | None = None,
        availability: str = "available",
    ) -> str:
        """登记一段**真实读到的原文**并返回 ``evidence_id``。

        同一 bvid + 来源 + 文本内容 → 复用已有 ID（幂等），
        避免重复读取同一片段时产生重复证据。
        """
        text_hash = _sha256(text)
        existing = self.query_one(
            """
            SELECT evidence_id FROM evidence
             WHERE bvid = ? AND source_type = ? AND text_sha256 = ?
               AND COALESCE(start_sec, -1) = COALESCE(?, -1)
             LIMIT 1
            """,
            (bvid, source_type, text_hash, start_sec),
        )
        if existing is not None:
            return str(existing["evidence_id"])

        evidence_id = f"ev_{text_hash[:12]}"
        # 极端哈希碰撞兜底
        if self.query_one("SELECT 1 FROM evidence WHERE evidence_id = ?", (evidence_id,)):
            evidence_id = f"ev_{text_hash[:12]}_{uuid.uuid4().hex[:6]}"

        self.execute(
            """
            INSERT INTO evidence (evidence_id, bvid, source_type, source_url, start_sec,
                                  end_sec, text, text_sha256, fetched_at, availability)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                evidence_id,
                bvid,
                source_type,
                source_url,
                start_sec,
                end_sec,
                text,
                text_hash,
                now_ts(),
                availability,
            ),
        )
        return evidence_id

    def get_evidence(self, evidence_id: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM evidence WHERE evidence_id = ?", (evidence_id,))

    def evidence_for_bvid(self, bvid: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM evidence WHERE bvid = ? ORDER BY start_sec", (bvid,))

    def verify_evidence_ids(self, bvid: str, evidence_ids: Sequence[str]) -> tuple[bool, str]:
        """校验证据 ID 集合：全部存在、全部属于同一 BV、且来源可用。

        任务书 §4：**模型自报"我看过"不能凭空生成证据**。
        返回 ``(ok, reason_code)``。
        """
        if not evidence_ids:
            return False, "EVIDENCE_REQUIRED"
        for evidence_id in evidence_ids:
            row = self.get_evidence(evidence_id)
            if row is None:
                return False, "EVIDENCE_NOT_FOUND"
            if str(row["bvid"]) != bvid:
                return False, "EVIDENCE_BVID_MISMATCH"
            if str(row["availability"]) == "unavailable":
                return False, "EVIDENCE_UNAVAILABLE"
        return True, "OK"

    def excerpt(self, evidence_id: str, max_chars: int) -> dict[str, Any] | None:
        """返回不超过 ``max_chars`` 的摘录（供 ``read_evidence_excerpt``）。"""
        row = self.get_evidence(evidence_id)
        if row is None:
            return None
        text = str(row["text"])
        clipped = len(text) > max_chars
        return {
            "evidence_id": evidence_id,
            "bvid": row["bvid"],
            "source_type": row["source_type"],
            "source_url": row["source_url"],
            "start_sec": row["start_sec"],
            "end_sec": row["end_sec"],
            "excerpt": text[:max_chars],
            "excerpt_chars": min(len(text), max_chars),
            "truncated": clipped,
            "fetched_at": row["fetched_at"],
        }

    # ---- drafts ------------------------------------------------------------

    def create_draft(
        self,
        *,
        draft_type: str,
        bvid: str,
        text: str,
        evidence_ids: Sequence[str],
        target_rpid: str | None = None,
        target_root: str | None = None,
    ) -> str:
        draft_id = new_id("draft")
        ts = now_ts()
        self.execute(
            """
            INSERT INTO drafts (draft_id, draft_type, bvid, target_rpid, target_root, text,
                                evidence_ids, status, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,'pending',?,?)
            """,
            (
                draft_id,
                draft_type,
                bvid,
                target_rpid,
                target_root,
                text,
                json.dumps(list(evidence_ids)),
                ts,
                ts,
            ),
        )
        return draft_id

    def get_draft(self, draft_id: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM drafts WHERE draft_id = ?", (draft_id,))

    def list_drafts(self, status: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        if status:
            return self.query(
                "SELECT * FROM drafts WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        return self.query("SELECT * FROM drafts ORDER BY created_at DESC LIMIT ?", (limit,))

    def set_draft_status(self, draft_id: str, status: str) -> None:
        self.execute(
            "UPDATE drafts SET status = ?, updated_at = ? WHERE draft_id = ?",
            (status, now_ts(), draft_id),
        )

    def approve_draft(self, draft_id: str) -> bool:
        """**人用入口专用**（CLI）。绝不注册为 MCP 工具。"""
        row = self.get_draft(draft_id)
        if row is None:
            return False
        self.execute(
            "UPDATE drafts SET status = 'approved', approved_at = ?, updated_at = ? "
            "WHERE draft_id = ?",
            (now_ts(), now_ts(), draft_id),
        )
        return True

    # ---- actions & 配额 ----------------------------------------------------

    def make_idempotency_key(
        self,
        *,
        agent_mid: int,
        action_type: str,
        bvid: str,
        target_rpid: str | None,
        is_simulated: bool = False,
    ) -> str:
        """任务书 §5 的幂等键。

        * 顶层评论：``agent_mid + bvid + top``
        * 回复：``agent_mid + target_rpid + reply``
        * 点赞 / 收藏：``agent_mid + bvid + like|favorite``

        ## ★ 为什么键里要带 ``is_simulated``

        任务书 §5 明确要求"**模拟与真实动作分开存储/计数**，
        模拟成功不等于远端成功"。

        若模拟与真实共用同一个键，就会出现一个真实存在的 bug：
        **dry_run 演练过一次之后，真实发布会被自己刚才的模拟记录挡住**
        （报 ``ALREADY_LIKED`` / ``DUPLICATE_ACTION``），
        而平台侧其实一次都没发过 —— 本项目实测踩到过。

        因此把模拟标志并入键：两条记录互不干扰，各自幂等。
        """
        scope = "sim" if is_simulated else "real"
        if action_type == TOP_ACTION:
            raw = f"{agent_mid}|{bvid}|top|{scope}"
        elif action_type == REPLY_ACTION:
            raw = f"{agent_mid}|{target_rpid}|reply|{scope}"
        elif action_type == LIKE_ACTION:
            # 点赞按「谁 + 哪个视频」——同一视频只能赞一次
            raw = f"{agent_mid}|{bvid}|like|{scope}"
        elif action_type == FAVORITE_ACTION:
            raw = f"{agent_mid}|{bvid}|favorite|{scope}"
        elif action_type == DYNAMIC_ACTION:
            # 动态没有 bvid；用调用方给的 target_rpid 槽位放指纹
            raw = f"{agent_mid}|{target_rpid}|dynamic|{scope}"
        else:
            raise ValueError(f"未知 action_type: {action_type}")
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def find_action_by_key(self, idempotency_key: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM actions WHERE idempotency_key = ?", (idempotency_key,))

    def reserve_action(
        self,
        *,
        agent_mid: int,
        action_type: str,
        bvid: str,
        target_rpid: str | None,
        draft_id: str | None,
        is_simulated: bool,
    ) -> tuple[str, str]:
        """在**点击前**持久化 ``reserved`` 预留（事务内）。

        返回 ``(action_id, outcome)``，``outcome`` ∈
        ``reserved`` / ``duplicate`` / ``uncertain_pending``。

        * ``duplicate``：同幂等键已有终态记录 → 不重复动作。
        * ``uncertain_pending``：同幂等键处于 ``uncertain`` → **冻结**，不自动重发。
        """
        key = self.make_idempotency_key(
            agent_mid=agent_mid,
            action_type=action_type,
            bvid=bvid,
            target_rpid=target_rpid,
            is_simulated=is_simulated,
        )
        ts = now_ts()
        action_id = new_id("act")

        with self.transaction() as tx:
            existing = tx.execute(
                "SELECT * FROM actions WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing is not None:
                status = str(existing["status"])
                if status == "uncertain":
                    return str(existing["action_id"]), "uncertain_pending"
                if status in ("sent_verified", "sent_pending_verify", "failed"):
                    return str(existing["action_id"]), "duplicate"
                if status == "reserved":
                    # 上一次预留没有结论 → 视为不确定，冻结
                    tx.execute(
                        "UPDATE actions SET status='uncertain', updated_at=? WHERE action_id=?",
                        (ts, existing["action_id"]),
                    )
                    return str(existing["action_id"]), "uncertain_pending"

            tx.execute(
                """
                INSERT INTO actions (action_id, agent_mid, action_type, bvid, target_rpid,
                                     idempotency_key, draft_id, status, is_simulated,
                                     created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,'reserved',?,?,?)
                """,
                (
                    action_id,
                    agent_mid,
                    action_type,
                    bvid,
                    target_rpid,
                    key,
                    draft_id,
                    1 if is_simulated else 0,
                    ts,
                    ts,
                ),
            )
        return action_id, "reserved"

    def settle_action(
        self,
        *,
        action_id: str,
        status: str,
        remote_rpid: str | None = None,
        remote_url: str | None = None,
        error_code: str | None = None,
    ) -> None:
        if status not in ("sent_verified", "sent_pending_verify", "uncertain", "failed"):
            raise ValueError(f"非法终态: {status}")
        self.execute(
            """
            UPDATE actions
               SET status = ?, remote_rpid = COALESCE(?, remote_rpid),
                   remote_url = COALESCE(?, remote_url), error_code = ?,
                   updated_at = ?
             WHERE action_id = ?
            """,
            (status, remote_rpid, remote_url, error_code, now_ts(), action_id),
        )

    def count_actions_today(
        self, *, action_type: str, is_simulated: bool, agent_mid: int | None = None
    ) -> int:
        """今日已占用额度的动作数。

        ★ **``reserved`` / ``uncertain`` / ``sent_pending_verify`` 都计入**——
        任务书 §5："``uncertain`` 占用额度，不靠时间自动释放，进程崩溃后不超额"。
        待核验的评论**已经发出去了**，当然也要占额度。
        """
        start_of_day = _start_of_today()
        sql = (
            "SELECT COUNT(*) AS n FROM actions "
            " WHERE action_type = ? AND is_simulated = ? AND created_at >= ? "
            "   AND status IN ('reserved','sent_verified','sent_pending_verify','uncertain')"
        )
        params: list[Any] = [action_type, 1 if is_simulated else 0, start_of_day]
        if agent_mid is not None:
            sql += " AND agent_mid = ?"
            params.append(agent_mid)
        row = self.query_one(sql, params)
        return int(row["n"]) if row else 0

    def count_replies_to_thread_today(self, *, target_root: str, is_simulated: bool) -> int:
        start_of_day = _start_of_today()
        row = self.query_one(
            """
            SELECT COUNT(*) AS n FROM actions a
              JOIN drafts d ON d.draft_id = a.draft_id
             WHERE a.action_type = 'reply' AND a.is_simulated = ?
               AND a.created_at >= ?
               AND a.status IN ('reserved','sent_verified','sent_pending_verify','uncertain')
               AND d.target_root = ?
            """,
            (1 if is_simulated else 0, start_of_day, target_root),
        )
        return int(row["n"]) if row else 0

    def freeze_target(self, *, bvid: str, target_rpid: str | None) -> bool:
        """同目标是否已被冻结（存在 ``uncertain`` 动作）。"""
        if target_rpid is None:
            return False
        row = self.query_one(
            "SELECT 1 FROM actions WHERE bvid = ? AND target_rpid = ? AND status = 'uncertain' "
            "LIMIT 1",
            (bvid, target_rpid),
        )
        return row is not None

    # ---- platform_health ---------------------------------------------------

    def record_health(
        self,
        *,
        state: str,
        raw_code: str | None = None,
        category: str = "",
        detail: str = "",
        next_check_at: int | None = None,
    ) -> int:
        cur = self.execute(
            """
            INSERT INTO platform_health (state, raw_code, category, detail, occurred_at,
                                         next_check_at)
            VALUES (?,?,?,?,?,?)
            """,
            (state, raw_code, category, detail, now_ts(), next_check_at),
        )
        return int(cur.lastrowid or 0)

    def current_health(self) -> sqlite3.Row | None:
        """最近一条**未恢复**的健康事件，即当前状态。

        按 ``occurred_at`` 与自增 ``event_id`` 双重排序——同一秒内先后发生的
        两个事件必须能区分先后，否则"最新状态"会取错。
        """
        return self.query_one(
            "SELECT * FROM platform_health WHERE recovered_at IS NULL "
            "ORDER BY occurred_at DESC, event_id DESC LIMIT 1"
        )

    def health_state(self) -> str:
        row = self.current_health()
        return str(row["state"]) if row else "ready"

    def recover_health(self, *, recovered_by: str, note: str = "") -> bool:
        """人工恢复。**必须由人用入口调用**（任务书 §5）。

        验证码 / 认证失效 / 反复限流**不得**由模型或计时自动恢复。
        """
        row = self.current_health()
        if row is None:
            return False
        self.execute(
            "UPDATE platform_health SET recovered_at = ?, recovered_by = ?, "
            "detail = CASE WHEN ? <> '' THEN ? ELSE detail END WHERE event_id = ?",
            (now_ts(), recovered_by, note, note, row["event_id"]),
        )
        return True

    # ---- seen_replies ------------------------------------------------------

    def mark_seen_reply(
        self,
        *,
        remote_rpid: str,
        bvid: str,
        root_rpid: str | None = None,
        parent_rpid: str | None = None,
        author_mid: int | None = None,
        author_name: str = "",
        text_excerpt: str = "",
        is_mention: bool = False,
    ) -> bool:
        """登记一条已见回复。返回 ``True`` 表示本次是**新的**。"""
        existing = self.query_one(
            "SELECT remote_rpid FROM seen_replies WHERE remote_rpid = ?", (remote_rpid,)
        )
        if existing is not None:
            return False
        self.execute(
            """
            INSERT INTO seen_replies (remote_rpid, bvid, root_rpid, parent_rpid, author_mid,
                                      author_name, text_excerpt, is_mention, process_state,
                                      first_seen_at)
            VALUES (?,?,?,?,?,?,?,?, 'new', ?)
            """,
            (
                remote_rpid,
                bvid,
                root_rpid,
                parent_rpid,
                author_mid,
                author_name,
                text_excerpt[:500],
                1 if is_mention else 0,
                now_ts(),
            ),
        )
        return True

    def list_seen_replies(
        self, process_state: str | None = None, limit: int = 50
    ) -> list[sqlite3.Row]:
        if process_state:
            return self.query(
                "SELECT * FROM seen_replies WHERE process_state = ? "
                "ORDER BY first_seen_at DESC LIMIT ?",
                (process_state, limit),
            )
        return self.query(
            "SELECT * FROM seen_replies ORDER BY first_seen_at DESC LIMIT ?", (limit,)
        )

    # ---- runs --------------------------------------------------------------

    def start_run(self, trigger_source: str) -> str:
        run_id = new_id("run")
        self.execute(
            "INSERT INTO runs (run_id, trigger_source, started_at) VALUES (?,?,?)",
            (run_id, trigger_source, now_ts()),
        )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        candidates_n: int = 0,
        read_n: int = 0,
        drafts_n: int = 0,
        sent_n: int = 0,
        sim_sent_n: int = 0,
        errors_n: int = 0,
        conclusion: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.execute(
            """
            UPDATE runs SET finished_at = ?, candidates_n = ?, read_n = ?, drafts_n = ?,
                            sent_n = ?, sim_sent_n = ?, errors_n = ?, conclusion = ?,
                            detail_json = ?
             WHERE run_id = ?
            """,
            (
                now_ts(),
                candidates_n,
                read_n,
                drafts_n,
                sent_n,
                sim_sent_n,
                errors_n,
                conclusion,
                json.dumps(detail or {}, ensure_ascii=False),
                run_id,
            ),
        )

    def latest_runs(self, limit: int = 5) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM runs WHERE run_id = ?", (run_id,))

    def runs_today(self) -> int:
        """今天**已开始**的轮次数（本地时区当天）—— 持续跑模式用来判"跑满没有"。

        ★ 与 :meth:`count_actions_today` 同一个"今天"口径（都走
        :func:`_start_of_today`）—— 两份实现早晚会在半夜对不上。

        数的是 ``started_at``（不是 ``finished_at``）：跨零点的长轮次算作
        "那天跑的"，与 :meth:`last_finished_run_at` 的口径一致。
        """
        row = self.query_one(
            "SELECT COUNT(*) AS n FROM runs WHERE started_at >= ?",
            (_start_of_today(),),
        )
        return int(row["n"]) if row is not None else 0

    def last_started_run_at(self) -> int | None:
        """最近一次**开始**的时刻（**含未完成/崩溃**的轮次）。

        与 :meth:`last_finished_run_at` 分工不同：这里用于**最小间隔**，
        挡住"官方不保证恰好投递一次"造成的并发窗口——
        第一次运行还没结束时第二次唤醒不该再跑。
        """
        row = self.query_one(
            "SELECT started_at FROM runs ORDER BY started_at DESC LIMIT 1"
        )
        if row is None or row["started_at"] is None:
            return None
        return int(row["started_at"])

    def last_finished_run_at(self) -> int | None:
        """最近一次**已结束**的运行的开始时刻（Unix 秒）。

        用于 :func:`core.schedule.is_due` 判断"今天跑过没有"。
        取 ``started_at`` 而非 ``finished_at``：跨零点运行时应算作"那天跑的"。
        """
        row = self.query_one(
            "SELECT started_at FROM runs WHERE finished_at IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 1"
        )
        if row is None or row["started_at"] is None:
            return None
        return int(row["started_at"])

    # ---- text_jobs ---------------------------------------------------------

    def record_text_job(
        self,
        *,
        bvid: str | None,
        input_sha256: str,
        model_id: str,
        prompt_version: str,
        status: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        from_cache: bool = False,
        error_code: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> str:
        job_id = new_id("job")
        cache_key = f"{bvid or '-'}|{input_sha256}|{model_id}|{prompt_version}"
        created_at = now_ts()
        result_json = json.dumps(result, ensure_ascii=False) if result is not None else None

        # ★ ``idx_text_jobs_cache`` 是**部分唯一索引**（``WHERE status='ok'``）：
        #   同一份输入 + 同一模型 + 同一 prompt 版本的"成功结果"只允许一行。
        #   而"重读同一个视频"会命中缓存并**再次**写 ok —— 那时直接 INSERT 会抛
        #   ``UNIQUE constraint failed: text_jobs.cache_key, text_jobs.status``
        #   （真实踩到：dm-ingest --force 重读一条已读视频）。
        #   ⇒ ok 走 upsert（复用原 job_id、覆盖结果）；其余状态仍是普通插入
        #     （error / needs_deep_review 是过程记录，允许留多条）。
        if status == "ok":
            existing = self.query_one(
                "SELECT job_id FROM text_jobs WHERE cache_key = ? AND status = 'ok' LIMIT 1",
                (cache_key,),
            )
            if existing is not None:
                job_id = str(existing["job_id"])
                if from_cache:
                    # 缓存读取不是新模型调用，不清空结果或冲掉历史 token 账。
                    self.execute(
                        "UPDATE text_jobs SET from_cache = 1 WHERE job_id = ?", (job_id,)
                    )
                    return job_id
                self.execute(
                    """
                    UPDATE text_jobs
                       SET prompt_tokens = ?, completion_tokens = ?, from_cache = ?,
                           error_code = ?, result_json = ?, created_at = ?
                     WHERE job_id = ?
                    """,
                    (
                        prompt_tokens,
                        completion_tokens,
                        1 if from_cache else 0,
                        error_code,
                        result_json,
                        created_at,
                        job_id,
                    ),
                )
                return job_id

        self.execute(
            """
            INSERT INTO text_jobs (job_id, bvid, input_sha256, model_id, prompt_version,
                                   cache_key, status, prompt_tokens, completion_tokens,
                                   from_cache, error_code, result_json, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job_id,
                bvid,
                input_sha256,
                model_id,
                prompt_version,
                cache_key,
                status,
                prompt_tokens,
                completion_tokens,
                1 if from_cache else 0,
                error_code,
                result_json,
                created_at,
            ),
        )
        return job_id

    def find_cached_text_job(
        self, *, bvid: str | None, input_sha256: str, model_id: str, prompt_version: str
    ) -> sqlite3.Row | None:
        cache_key = f"{bvid or '-'}|{input_sha256}|{model_id}|{prompt_version}"
        return self.query_one(
            "SELECT * FROM text_jobs WHERE cache_key = ? AND status = 'ok' LIMIT 1", (cache_key,)
        )

    def text_job_totals(self) -> dict[str, int]:
        row = self.query_one(
            "SELECT COALESCE(SUM(prompt_tokens),0) AS p, "
            "       COALESCE(SUM(completion_tokens),0) AS c, COUNT(*) AS n FROM text_jobs"
        )
        if row is None:
            return {"prompt_tokens": 0, "completion_tokens": 0, "jobs": 0}
        return {
            "prompt_tokens": int(row["p"]),
            "completion_tokens": int(row["c"]),
            "jobs": int(row["n"]),
        }

    # ---- 视频点赞/收藏状态（主人 2026-10-15 要求）-------------------------

    def get_video_state(self, bvid: str) -> dict[str, Any]:
        """读视频的点赞/收藏状态。没有记录时返回全 0（**不建行**，避免噪声）。"""
        row = self.query_one("SELECT * FROM video_states WHERE bvid = ?", (bvid,))
        if row is None:
            return {"bvid": bvid, "like_state": 0, "fav_state": 0, "fav_media_ids": []}
        try:
            media_ids = json.loads(str(row["fav_media_ids"]))
        except json.JSONDecodeError:
            media_ids = []
        return {
            "bvid": bvid,
            "like_state": int(row["like_state"]),
            "fav_state": int(row["fav_state"]),
            "fav_media_ids": media_ids,
        }

    def set_video_like_state(self, bvid: str, state: int) -> None:
        """记点赞状态。``1`` 已赞 / ``0`` 未赞。"""
        if state not in (0, 1):
            raise ValueError(f"like_state 只能是 0/1，收到 {state}")
        self._upsert_video_state(bvid, like_state=state)

    def set_video_fav_state(
        self, bvid: str, state: int, media_id: int | None = None
    ) -> None:
        """记收藏状态；``media_id`` 非空时追加进 ``fav_media_ids``。"""
        if state not in (0, 1):
            raise ValueError(f"fav_state 只能是 0/1，收到 {state}")
        current = self.get_video_state(bvid)
        media_ids = list(current["fav_media_ids"])
        if media_id is not None and media_id > 0 and media_id not in media_ids:
            media_ids.append(media_id)
        self._upsert_video_state(
            bvid, fav_state=state, fav_media_ids=json.dumps(media_ids)
        )

    def _upsert_video_state(
        self,
        bvid: str,
        *,
        like_state: int | None = None,
        fav_state: int | None = None,
        fav_media_ids: str | None = None,
    ) -> None:
        ts = now_ts()
        existing = self.query_one("SELECT * FROM video_states WHERE bvid = ?", (bvid,))
        if existing is None:
            self.execute(
                """
                INSERT INTO video_states
                    (bvid, like_state, fav_state, fav_media_ids, updated_at)
                VALUES (?,?,?,?,?)
                """,
                (
                    bvid,
                    like_state if like_state is not None else 0,
                    fav_state if fav_state is not None else 0,
                    fav_media_ids if fav_media_ids is not None else "[]",
                    ts,
                ),
            )
            return
        self.execute(
            """
            UPDATE video_states
               SET like_state = COALESCE(?, like_state),
                   fav_state = COALESCE(?, fav_state),
                   fav_media_ids = COALESCE(?, fav_media_ids),
                   updated_at = ?
             WHERE bvid = ?
            """,
            (like_state, fav_state, fav_media_ids, ts, bvid),
        )

    # ---- 收藏夹名 ↔ media_id 映射 -----------------------------------------

    def upsert_favorite_folder(
        self,
        *,
        name: str,
        media_id: int,
        folder_id: int = 0,
        title: str = "",
    ) -> None:
        """缓存「收藏夹名 → media_id」映射（收藏时要用 mlid）。"""
        ts = now_ts()
        self.execute(
            """
            INSERT INTO favorite_folders
                (name, media_id, folder_id, title, created_at, resolved_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET
                media_id = excluded.media_id,
                folder_id = excluded.folder_id,
                title = excluded.title,
                resolved_at = excluded.resolved_at
            """,
            (name, media_id, folder_id, title, ts, ts),
        )

    def get_favorite_folder(self, name: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM favorite_folders WHERE name = ?", (name,))

    def list_favorite_folders(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM favorite_folders ORDER BY name")

    def resolve_favorite_media_id(self, name: str) -> int:
        """从缓存里取 media_id；没有则返回 0（调用方需去 B站 解析）。"""
        row = self.get_favorite_folder(name)
        if row is None or row["media_id"] is None:
            return 0
        return int(row["media_id"])

    # ---- 主人偏好（跨对话持久；**不影响发布授权**）------------------------

    def add_owner_preference(
        self,
        *,
        kind: str,
        content: str,
        subject: str = "",
        scope: str = "ranking",
        pref_id: str | None = None,
    ) -> str:
        """记一条主人偏好。

        ★ ``scope`` **不允许**是 ``publish`` 之类的授权类值 ——
        偏好的作用是"影响排序与内容策略"，**绝不能**变成"改发布权限"的旁路。
        """
        if kind not in ("rule", "like", "dislike", "note"):
            raise ValueError(f"未知偏好类型：{kind}")
        if scope not in ("ranking", "content", "topic"):
            raise ValueError(
                f"非法 scope：{scope}。偏好**不得**影响发布授权"
                "（模式/配额/UID 只能由 config.yaml 决定）"
            )
        text = content.strip()
        if not text:
            raise ValueError("偏好内容不能为空")
        pid = pref_id or new_id("pref")
        ts = now_ts()
        self.execute(
            """
            INSERT INTO owner_preferences
                (pref_id, kind, subject, content, scope, active, created_at, updated_at)
            VALUES (?,?,?,?,?,1,?,?)
            """,
            (pid, kind, subject.strip(), text, scope, ts, ts),
        )
        return pid

    def list_owner_preferences(
        self, *, active_only: bool = True, limit: int = 100
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM owner_preferences"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY updated_at DESC LIMIT ?"
        return self.query(sql, (limit,))

    def deactivate_owner_preference(self, pref_id: str) -> bool:
        """停用一条偏好（**不删**，保留可追溯）。"""
        cur = self.execute(
            "UPDATE owner_preferences SET active = 0, updated_at = ? WHERE pref_id = ?",
            (now_ts(), pref_id),
        )
        return cur.rowcount > 0

    def owner_preferences_digest(self) -> dict[str, list[str]]:
        """给 agent 看的偏好摘要（按类型分组）。"""
        out: dict[str, list[str]] = {"rule": [], "like": [], "dislike": [], "note": []}
        for row in self.list_owner_preferences():
            subject = str(row["subject"] or "")
            content = str(row["content"])
            out[str(row["kind"])].append(
                f"{subject}：{content}" if subject else content
            )
        return out

    # ---- 主号汇报投递状态（任务书 §6.6）----------------------------------

    def plan_owner_notification(
        self,
        *,
        bvid: str,
        owner_mid: int,
        channel: str = "local",
        draft_id: str | None = None,
        detail: str = "",
    ) -> str:
        """登记一条**待投递**的主号汇报。返回 ``notification_id``。

        同 bvid + owner_mid 复用同一条记录（幂等），避免同一视频重复上报。
        """
        existing = self.query_one(
            "SELECT notification_id FROM owner_notifications WHERE bvid = ? AND owner_mid = ?",
            (bvid, owner_mid),
        )
        if existing is not None:
            self.execute(
                "UPDATE owner_notifications SET updated_at = ?, channel = ?, "
                "draft_id = COALESCE(?, draft_id), detail = CASE WHEN ? <> '' THEN ? "
                "ELSE detail END WHERE notification_id = ?",
                (now_ts(), channel, draft_id, detail, detail, existing["notification_id"]),
            )
            return str(existing["notification_id"])

        notification_id = new_id("notif")
        ts = now_ts()
        self.execute(
            """
            INSERT INTO owner_notifications (notification_id, bvid, owner_mid, status,
                                             channel, draft_id, detail, created_at, updated_at)
            VALUES (?,?,?,'planned',?,?,?,?,?)
            """,
            (notification_id, bvid, owner_mid, channel, draft_id, detail, ts, ts),
        )
        return notification_id

    def settle_owner_notification(
        self,
        *,
        notification_id: str,
        status: str,
        observed_mid: int | None = None,
        action_id: str | None = None,
        remote_rpid: str | None = None,
        remote_url: str | None = None,
        error_code: str | None = None,
        detail: str = "",
    ) -> None:
        """落定投递状态。

        ★ ``status='verified'`` **只允许**在 ``observed_mid == owner_mid`` 时使用。
        本方法会**断言**这一点，防止"假称已通知主人"。
        """
        if status not in ("planned", "verified", "uncertain", "unsupported", "failed"):
            raise ValueError(f"非法投递状态: {status}")

        row = self.query_one(
            "SELECT owner_mid FROM owner_notifications WHERE notification_id = ?",
            (notification_id,),
        )
        if row is None:
            raise ValueError(f"通知不存在: {notification_id}")

        if status == "verified":
            owner_mid = int(row["owner_mid"])
            if observed_mid is None or int(observed_mid) != owner_mid:
                raise ValueError(
                    "status='verified' 要求 observed_mid 等于 owner_mid"
                    f"（owner_mid={owner_mid}, observed_mid={observed_mid}）——"
                    "未核验到目标身份时不得声称已通知主人"
                )

        self.execute(
            """
            UPDATE owner_notifications
               SET status = ?, observed_mid = COALESCE(?, observed_mid),
                   action_id = COALESCE(?, action_id),
                   remote_rpid = COALESCE(?, remote_rpid),
                   remote_url = COALESCE(?, remote_url),
                   error_code = ?, detail = CASE WHEN ? <> '' THEN ? ELSE detail END,
                   updated_at = ?
             WHERE notification_id = ?
            """,
            (
                status,
                observed_mid,
                action_id,
                remote_rpid,
                remote_url,
                error_code,
                detail,
                detail,
                now_ts(),
                notification_id,
            ),
        )

    def get_owner_notification(self, notification_id: str) -> sqlite3.Row | None:
        return self.query_one(
            "SELECT * FROM owner_notifications WHERE notification_id = ?", (notification_id,)
        )

    def list_owner_notifications(
        self, status: str | None = None, limit: int = 50
    ) -> list[sqlite3.Row]:
        if status:
            return self.query(
                "SELECT * FROM owner_notifications WHERE status = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        return self.query(
            "SELECT * FROM owner_notifications ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    def owner_notification_summary(self) -> dict[str, int]:
        """各状态的计数（供日报与 companion_status 汇报"到底通知成功没有"）。"""
        rows = self.query(
            "SELECT status, COUNT(*) AS n FROM owner_notifications GROUP BY status"
        )
        return {str(r["status"]): int(r["n"]) for r in rows}

    def verification_summary(self) -> dict[str, int]:
        """评论可见性复核账本的三态计数（P1，2026-10-16）。

        ★ **三态必须分开报**，不能把 NULL 混进 0：

        * ``visible`` —— 已确认**公开可见**；
        * ``invisible`` —— 接口明确标了不可见；
        * ``unknown`` —— **没查到**（翻页 / 热度排序都会漏）。

        ``unknown`` **不等于**"没发出去" —— 观测不到不能反推不存在。
        """
        row = self.query_one(
            "SELECT "
            "  COALESCE(SUM(CASE WHEN visible = 1 THEN 1 ELSE 0 END), 0) AS visible, "
            "  COALESCE(SUM(CASE WHEN visible = 0 THEN 1 ELSE 0 END), 0) AS invisible, "
            "  COALESCE(SUM(CASE WHEN visible IS NULL THEN 1 ELSE 0 END), 0) AS unknown, "
            "  COUNT(*) AS total "
            "FROM comment_verifications"
        )
        if row is None:
            return {"visible": 0, "invisible": 0, "unknown": 0, "total": 0}
        return {
            "visible": int(row["visible"]),
            "invisible": int(row["invisible"]),
            "unknown": int(row["unknown"]),
            "total": int(row["total"]),
        }

    # ---- feedback ----------------------------------------------------------

    def add_feedback(self, *, bvid: str, verdict: str, note: str = "") -> None:
        self.execute(
            "INSERT OR REPLACE INTO feedback (bvid, verdict, note, created_at) VALUES (?,?,?,?)",
            (bvid, verdict, note, now_ts()),
        )

    def search_notes(self, *, query: str, limit: int = 5) -> list[sqlite3.Row]:
        """按关键词搜**知识笔记**（R5，只读）。

        搜索范围：summary / facts / inferences / doubts / knowledge_tags。
        只返回少量字段；完整内容仍在库里，由 DSH 按需再看。
        """
        needle = (query or "").strip()
        if not needle:
            return []
        like = f"%{needle}%"
        return self.query(
            """
            SELECT n.bvid, v.title, n.score_total, n.summary, n.facts_json,
                   n.knowledge_tags, n.evidence_level, n.updated_at
              FROM notes n
              LEFT JOIN videos v ON v.bvid = n.bvid
             WHERE n.summary LIKE ? OR n.facts_json LIKE ?
                OR n.inferences_json LIKE ? OR n.doubts_json LIKE ?
                OR n.knowledge_tags LIKE ?
             ORDER BY n.score_total DESC, n.updated_at DESC
             LIMIT ?
            """,
            (like, like, like, like, like, max(1, min(int(limit), 20))),
        )

    def feedback_map(self) -> dict[str, str]:
        rows = self.query("SELECT bvid, verdict FROM feedback ORDER BY created_at DESC")
        result: dict[str, str] = {}
        for row in rows:
            result.setdefault(str(row["bvid"]), str(row["verdict"]))
        return result

    def feedback_signals(self, *, limit: int = 5) -> dict[str, list[str]]:
        """把反馈聚合成**可解释的偏好信号**（R4，2026-10-18）。

        不额外调模型，只用本地数据：

        * **UP**：同一 UP 被标 useful 多于 not_useful → 喜欢；反之 → 不喜欢；
        * **发现关键词**：``videos.discovery_keyword`` 的净反馈，同理。

        返回 ``{"liked_ups": [...], "disliked_ups": [...],
        "liked_keywords": [...], "disliked_keywords": [...]}``。
        数据不足就是空列表 —— 绝不硬猜。
        """
        rows = self.query(
            "SELECT v.up_name AS up_name, v.discovery_keyword AS keyword, f.verdict AS verdict "
            "  FROM feedback f JOIN videos v ON v.bvid = f.bvid"
        )
        up_score: dict[str, int] = {}
        keyword_score: dict[str, int] = {}
        for row in rows:
            delta = 1 if str(row["verdict"]) == "useful" else -1
            up_name = str(row["up_name"] or "").strip()
            keyword = str(row["keyword"] or "").strip()
            if up_name:
                up_score[up_name] = up_score.get(up_name, 0) + delta
            if keyword:
                keyword_score[keyword] = keyword_score.get(keyword, 0) + delta

        def _pick(scores: dict[str, int], *, positive: bool) -> list[str]:
            items = [
                (name, score)
                for name, score in scores.items()
                if (score > 0 if positive else score < 0)
            ]
            items.sort(key=lambda item: (abs(item[1]), item[0]), reverse=True)
            return [name for name, _score in items[:limit]]

        return {
            "liked_ups": _pick(up_score, positive=True),
            "disliked_ups": _pick(up_score, positive=False),
            "liked_keywords": _pick(keyword_score, positive=True),
            "disliked_keywords": _pick(keyword_score, positive=False),
        }


def _start_of_today() -> int:
    """本地时区当天 00:00 的 Unix 秒。"""
    lt = time.localtime()
    midnight = time.struct_time(
        (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst)
    )
    return int(time.mktime(midnight))
