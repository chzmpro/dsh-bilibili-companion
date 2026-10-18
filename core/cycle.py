"""自动学习轮（R1）：发现 → 阅读 → 豆包 → 短包+完整内容 → DSH → 私信。

## 一轮做什么

```
run_cycle(trigger_source)
  1. 任务锁（同一时刻只允许一轮）
  2. runs 表 start_run
  3. Pipeline.prepare_batch（发现/去重/阅读/豆包打分，返回 ≤3 短包）
  4. 给这 ≤3 条补评论（get_discussion），让"完整内容"真的有评论区
  5. core.cycle_content：短包 + 2500 规则整理后的完整内容
  6. 投递给值班员会话（core.dm_deliver）
  7. 轮询会话 turn/end（core.session_status）判断 DSH 是否真的跑完
  8. 写本地日报 + finish_run（conclusion 如实）
```

## 边界

* **DSH 是决策者**：这里只投递材料，不写推荐、不发私信；
* 投递成功 ≠ 完成：只有最后 ``turn/end.reason.kind == "completed"`` 才算完成；
* 出现验证码/登录失效/平台故障 → 本轮不再发新请求，如实记 conclusion；
* 不自动公开写入（评论/点赞/收藏仍由 DSH 走既有门禁）。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from core import paths
from core.cycle_content import MAX_PER_DELIVERY, prepare_one, render_delivery
from core.dm_deliver import deliver_async, read_plugin_config
from core.lock import LockError, TaskLock
from core.ratelimit import RateLimitExceeded, RequestsBlocked
from core.report import DigestInput, DigestWriter
from core.session_status import read_turn_status

#: 投递后最多等多久让 DSH 跑完（秒）。DSH 要读完整内容再写推荐，给足。
DEFAULT_WAIT_SECONDS = 600

#: 轮询会话状态的间隔（秒）。
DEFAULT_POLL_SECONDS = 10

#: 一次补多少条评论（与竞品/交付口径一致：热评 8 条）。
COMMENTS_PER_VIDEO = 8


@dataclass
class CycleResult:
    """一轮的结果（给人/日志看，字段都**如实**）。"""

    run_id: str = ""
    skipped: bool = False
    reason: str = ""
    candidates: int = 0
    delivered: bool = False
    session_id: str = ""
    conclusion: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "skipped": self.skipped,
            "reason": self.reason,
            "candidates": self.candidates,
            "delivered": self.delivered,
            "session_id": self.session_id,
            "conclusion": self.conclusion,
            "detail": self.detail,
        }


def _session_id(session_id: str = "") -> str:
    if session_id.strip():
        return session_id.strip()
    config = read_plugin_config()
    return str(config.get("watchSessionId") or "").strip()


def _recommended(cards: list[Any], db: Any) -> list[dict[str, Any]]:
    recommended: list[dict[str, Any]] = []
    for card in cards:
        evidence = db.evidence_for_bvid(card.bvid)
        recommended.append(
            {
                "bvid": card.bvid,
                "title": card.title,
                "source_url": card.source_url,
                "evidence_level": card.evidence_level,
                "score": card.relevance_score,
                "evidence_ids": [str(row["evidence_id"]) for row in evidence],
                "key_points": [
                    {"claim": point.claim, "evidence_id": point.evidence_id}
                    for point in card.key_points
                ],
            }
        )
    return recommended


def _write_digest(
    *,
    db: Any,
    run_id: str,
    trigger_source: str,
    batch: Any,
    cards: list[Any],
    keywords: list[str],
) -> Path | None:
    """写本地日报（失败不抛；返回路径或 None）。"""
    try:
        writer = DigestWriter(db=db, out_dir=paths.data_dir() / "digests")
        writer.write(
            DigestInput(
                run_id=run_id,
                trigger_source=trigger_source,
                keywords=list(keywords),
                candidates_n=len(cards),
                read_n=len(batch.read_bvids),
                recommended=_recommended(cards, db),
                errors=[f"{item['keyword_or_bvid']}:{item['reason']}" for item in batch.failed],
                pending=[f"{item['bvid']}:{item['reason']}" for item in batch.skipped],
                cache_stats=batch.cache_stats,
                health_state=db.health_state(),
                simulated=False,
                next_steps=[
                    "DSH 会通过 dm_push 给主人发 1~3 条推荐",
                    "若值班员会话不在线，本轮只写日报、不发私信",
                ],
            )
        )
        return paths.data_dir() / "digests"
    except Exception:  # noqa: BLE001 - 日报失败不该掩盖主流程
        return None


async def _wait_for_completion(
    session_id: str,
    *,
    turns_before: int,
    wait_seconds: float,
    poll_seconds: float,
) -> tuple[str, dict[str, Any]]:
    """等会话出现**新的** turn/end，并返回 ``(conclusion, detail)``。"""
    if not session_id:
        return "no_session", {"reason": "没有配置值班员会话"}
    if wait_seconds <= 0:
        return "delivered_unverified", {"reason": "wait_seconds<=0"}

    deadline = time.monotonic() + wait_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status = read_turn_status(session_id)
        last = {
            "found": status.found,
            "turns": status.turns,
            "last_kind": status.last_kind,
            "error": status.error,
        }
        if status.found and status.turns > turns_before:
            if status.completed:
                return "ok", last
            if status.last_kind == "error":
                return "dsh_error", last
        await asyncio.sleep(poll_seconds)

    return "delivered_unverified", {**last, "reason": "等待 DSH 完成超时"}


async def run_cycle(
    *,
    trigger_source: str = "host_start",
    session_id: str = "",
    runtime: Any = None,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> CycleResult:
    """跑一轮自动学习。**不公开写入**；失败如实返回，不抛给监听循环。"""
    owned = runtime is None
    if runtime is None:
        from core.runtime import build_runtime

        runtime = await build_runtime(with_worker=True)

    db = runtime.db
    config = runtime.config
    sid = _session_id(session_id)
    result = CycleResult(session_id=sid)

    try:
        lock = TaskLock(purpose="cycle")
        with lock:
            run_id = db.start_run(trigger_source)
            result.run_id = run_id
            try:
                batch = await runtime.pipeline.prepare_batch()
            except RequestsBlocked as exc:
                db.finish_run(run_id, errors_n=1, conclusion=f"BLOCKED:{exc.state}")
                result.conclusion = f"BLOCKED:{exc.state}"
                return result
            except RateLimitExceeded as exc:
                db.finish_run(run_id, errors_n=1, conclusion="RATE_LIMIT_EXCEEDED")
                result.conclusion = "RATE_LIMIT_EXCEEDED"
                result.reason = str(exc)[:120]
                return result

            cards = list(batch.candidates)[:MAX_PER_DELIVERY]
            result.candidates = len(cards)
            _write_digest(
                db=db,
                run_id=run_id,
                trigger_source=trigger_source,
                batch=batch,
                cards=cards,
                keywords=list(config.discovery.keywords),
            )

            if not cards:
                db.finish_run(
                    run_id,
                    candidates_n=0,
                    read_n=len(batch.read_bvids),
                    errors_n=len(batch.failed),
                    conclusion="no_candidates",
                    detail={"limitations": batch.limitations[:20]},
                )
                result.conclusion = "no_candidates"
                return result

            # ★ 补评论：prepare_batch 只读简介/字幕；评论要单独拉，
            #   否则"完整内容"里没有评论区，DSH 的判断会偏。
            comment_notes: list[str] = []
            for card in cards:
                try:
                    _page, registered = await runtime.pipeline.get_discussion(
                        bvid=card.bvid, limit=COMMENTS_PER_VIDEO
                    )
                    comment_notes.append(f"{card.bvid}:{registered}")
                except RequestsBlocked as exc:
                    db.finish_run(
                        run_id,
                        candidates_n=len(cards),
                        read_n=len(batch.read_bvids),
                        errors_n=len(batch.failed) + 1,
                        conclusion=f"BLOCKED:{exc.state}",
                        detail={"comments": comment_notes},
                    )
                    result.conclusion = f"BLOCKED:{exc.state}"
                    return result
                except RateLimitExceeded:
                    db.finish_run(
                        run_id,
                        candidates_n=len(cards),
                        read_n=len(batch.read_bvids),
                        errors_n=len(batch.failed) + 1,
                        conclusion="RATE_LIMIT_EXCEEDED",
                        detail={"comments": comment_notes},
                    )
                    result.conclusion = "RATE_LIMIT_EXCEEDED"
                    return result
                except Exception as exc:  # noqa: BLE001 - 单条评论失败不致命
                    comment_notes.append(f"{card.bvid}:ERR:{type(exc).__name__}")

            rows: list[dict[str, Any]] = []
            async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
                for card in cards:
                    prepared = await prepare_one(
                        db,
                        config,
                        client,
                        {"bvid": card.bvid, "score": card.relevance_score},
                    )
                    if prepared is not None:
                        rows.append(prepared)

            if not rows:
                db.finish_run(
                    run_id,
                    candidates_n=len(cards),
                    read_n=len(batch.read_bvids),
                    errors_n=len(batch.failed),
                    conclusion="no_content",
                    detail={"comments": comment_notes},
                )
                result.conclusion = "no_content"
                return result

            style_notes: list[str] = []
            try:
                style_notes = list(db.owner_preferences_digest().get("note", []))
            except Exception:  # noqa: BLE001 - 偏好读不到不影响本轮
                style_notes = []
            text = render_delivery(
                rows,
                cards=[card.to_payload() for card in cards],
                run_id=run_id,
                style_notes=style_notes,
            )
            turns_before = read_turn_status(sid).turns if sid else 0
            delivered = await deliver_async(text, session_id=sid)
            result.delivered = bool(delivered.ok and delivered.delivered)
            if not result.delivered:
                db.finish_run(
                    run_id,
                    candidates_n=len(cards),
                    read_n=len(batch.read_bvids),
                    errors_n=len(batch.failed) + 1,
                    conclusion="delivery_failed",
                    detail={
                        "delivery": delivered.to_dict(),
                        "comments": comment_notes,
                    },
                )
                result.conclusion = "delivery_failed"
                result.reason = delivered.error_code or delivered.message
                return result

            # ★ 投递已成功 ⇒ 提前释放任务锁：等 DSH 跑完可能几分钟，
            #   不能让"手动 prepare_batch"在这段时间里被 LOCKED 挡住。
            #   （调度器另有"未结束 run"防重入，不会因此并发开第二轮。）
            lock.release()
            conclusion, turn_detail = await _wait_for_completion(
                sid,
                turns_before=turns_before,
                wait_seconds=wait_seconds,
                poll_seconds=poll_seconds,
            )
            result.conclusion = conclusion
            db.finish_run(
                run_id,
                candidates_n=len(cards),
                read_n=len(batch.read_bvids),
                errors_n=len(batch.failed),
                conclusion=conclusion,
                detail={
                    "session": sid,
                    "turn": turn_detail,
                    "comments": comment_notes,
                    "chars": len(text),
                },
            )
            return result
    except LockError as exc:
        result.skipped = True
        result.reason = f"LOCKED:{exc}"
        return result
    except Exception as exc:  # noqa: BLE001 - 一轮异常不能让调度/监听死
        # ★ 兜底：把 run 标成错误结束，别留一个永远 unfinished 的行
        #   （调度器的"未结束 run"检查会被它挡住 2 小时）。
        if result.run_id:
            with contextlib.suppress(Exception):
                db.finish_run(
                    result.run_id,
                    errors_n=1,
                    conclusion=f"ERROR:{type(exc).__name__}",
                    detail={"error": str(exc)[:300]},
                )
        result.conclusion = f"ERROR:{type(exc).__name__}"
        result.reason = str(exc)[:200]
        return result
    finally:
        if owned:
            await runtime.aclose()
