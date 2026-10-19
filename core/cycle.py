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
import json
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
RECOVERY_INTERVAL_SECONDS = 300
MAX_DELIVERY_ATTEMPTS = 3


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
    expected_text: str = "",
    wait_seconds: float,
    poll_seconds: float,
) -> tuple[str, dict[str, Any]]:
    """只等待包含本次完整投递消息的那个 turn 完成。"""
    if not session_id:
        return "no_session", {"reason": "没有配置值班员会话"}
    if wait_seconds <= 0:
        return "delivered_unverified", {"reason": "wait_seconds<=0"}

    deadline = time.monotonic() + wait_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status = read_turn_status(session_id, expected_text=expected_text)
        last = {
            "found": status.found,
            "turns": status.turns,
            "last_kind": status.last_kind,
            "error": status.error,
            "matched": status.matched,
            "message_id": status.message_id,
            "turn_id": status.turn_id,
        }
        if expected_text and status.found and status.matched:
            if status.completed:
                return "ok", last
            if status.last_kind == "error":
                return "dsh_error", last
        await asyncio.sleep(poll_seconds)

    return "delivered_unverified", {**last, "reason": "等待 DSH 完成超时"}


async def _resume_delivery(
    runtime: Any,
    item: Any,
    *,
    result: CycleResult,
    wait_seconds: float,
    poll_seconds: float,
    lock: Any = None,
) -> CycleResult:
    """恢复已经选好的材料；发送前写 sending，崩溃后只能核对、不可重发。"""
    db, config = runtime.db, runtime.config
    run_id = str(item["run_id"])
    result.run_id = run_id
    sid = str(item["session_id"] or result.session_id)
    result.session_id = sid
    payload = json.loads(item["payload_json"])
    cards = payload["cards"]
    result.candidates = len(cards)
    state = str(item["state"])
    current = int(time.time())
    if state in {"exhausted", "dsh_error"} or item["next_attempt_at"] > current:
        result.skipped = True
        result.conclusion = state
        result.reason = (
            "RECOVERY_REQUIRES_REVIEW"
            if state in {"exhausted", "dsh_error"}
            else "RECOVERY_BACKOFF"
        )
        return result

    def finish(conclusion: str, **detail: Any) -> CycleResult:
        result.conclusion = conclusion
        result.detail = {"outbox_run_id": run_id, **detail}
        db.finish_run(
            run_id,
            candidates_n=len(cards),
            read_n=payload.get("read_n", 0),
            errors_n=payload.get("errors_n", 0) + int(conclusion not in {"ok", "no_content"}),
            conclusion=conclusion,
            detail={
                "session": sid,
                "comments": payload.get("comments", []),
                "trends": payload.get("trends", {}),
                **result.detail,
            },
        )
        return result

    if payload.get("batch_in_progress"):
        # A process crash can interrupt the batch between completed reads.
        # Reapply only its deterministic selection to the atomically saved
        # cards; never send those videos back through the text worker.
        read_cards = payload.get("read_cards", [])
        minimum = int(payload.get("min_score", config.discovery.min_score_for_comment))
        cap = min(
            MAX_PER_DELIVERY,
            int(payload.get("candidate_cap", config.text_worker.max_candidates_to_dsh)),
        )
        cards = sorted(
            [
                card
                for card in read_cards
                if card["relevance_score"] >= minimum or card.get("needs_deep_review")
            ],
            key=lambda card: card["relevance_score"],
            reverse=True,
        )[:cap]
        result.candidates = len(cards)
        payload.update(
            cards=cards, read_n=len(read_cards), batch_in_progress=False, batch_recovered=True
        )
        db.update_cycle_delivery(run_id, payload_json=json.dumps(payload, ensure_ascii=False))
        if not cards:
            db.update_cycle_delivery(run_id, state="empty")
            return finish("batch_interrupted")

    # A lease also keeps a second scheduler from treating an in-flight wait as a crash.
    db.update_cycle_delivery(
        run_id,
        session_id=sid,
        next_attempt_at=current + max(RECOVERY_INTERVAL_SECONDS, int(wait_seconds) + 60),
    )
    text = str(item["prompt"])
    if not text:
        prepare_attempts = int(payload.get("prepare_attempts", 0))
        if prepare_attempts >= MAX_DELIVERY_ATTEMPTS:
            db.update_cycle_delivery(run_id, state="exhausted")
            return finish("preparation_exhausted")
        payload["prepare_attempts"] = prepare_attempts + 1
        db.update_cycle_delivery(run_id, payload_json=json.dumps(payload, ensure_ascii=False))
        comments = payload.setdefault("comments", [])
        completed_comments = payload.setdefault("comments_done", [])
        for card in cards:
            key = card["bvid"]
            if key in completed_comments:
                continue
            try:
                _page, registered = await runtime.pipeline.get_discussion(
                    bvid=key, limit=COMMENTS_PER_VIDEO
                )
                comments.append(f"{key}:{registered}")
            except RequestsBlocked as exc:
                return finish(f"BLOCKED:{exc.state}")
            except RateLimitExceeded:
                return finish("RATE_LIMIT_EXCEEDED")
            except Exception as exc:  # one comment source may be unavailable
                comments.append(f"{key}:ERR:{type(exc).__name__}")
            completed_comments.append(key)
            db.update_cycle_delivery(run_id, payload_json=json.dumps(payload, ensure_ascii=False))

        prepared_rows = payload.setdefault("rows", {})
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            for card in cards:
                key = card["bvid"]
                if key not in prepared_rows:
                    prepared_rows[key] = await prepare_one(
                        db, config, client, {"bvid": key, "score": card["relevance_score"]}
                    )
                    # Persist every completed condensation, not only the last candidate.
                    db.update_cycle_delivery(
                        run_id, payload_json=json.dumps(payload, ensure_ascii=False)
                    )
        rows = [row for row in prepared_rows.values() if row is not None]
        if not rows:
            db.update_cycle_delivery(run_id, state="empty")
            return finish("no_content")
        style_notes: list[str] = []
        with contextlib.suppress(Exception):
            style_notes = list(db.owner_preferences_digest().get("note", []))
        text = render_delivery(rows, cards=cards, run_id=run_id, style_notes=style_notes)
        db.update_cycle_delivery(run_id, state="ready", prompt=text)
        state = "ready"

    if state in {"preparing", "ready", "retry"}:
        attempts = int(item["attempts"])
        if attempts >= MAX_DELIVERY_ATTEMPTS:
            db.update_cycle_delivery(run_id, state="exhausted")
            return finish("delivery_exhausted")
        db.update_cycle_delivery(
            run_id, state="sending", attempts=attempts + 1, last_attempt_at=int(time.time())
        )
        delivered = await deliver_async(text, session_id=sid)
        result.delivered = bool(delivered.ok and delivered.delivered)
        if not result.delivered:
            safe = bool(delivered.retry_safe)
            next_state = (
                ("exhausted" if attempts + 1 >= MAX_DELIVERY_ATTEMPTS else "retry")
                if safe
                else "uncertain"
            )
            db.update_cycle_delivery(
                run_id,
                state=next_state,
                next_attempt_at=int(time.time()) + RECOVERY_INTERVAL_SECONDS,
            )
            result.reason = delivered.error_code or delivered.message
            return finish(
                "delivery_failed" if safe else "delivery_uncertain", delivery=delivered.to_dict()
            )
        db.update_cycle_delivery(run_id, state="accepted")
        state = "accepted"
    elif state == "sending":
        # The process may have died after followup accepted it, before writing its response.
        db.update_cycle_delivery(run_id, state="uncertain")
        state = "uncertain"

    result.delivered = state == "accepted"
    # DSH can use the normal read tools while processing the saved package.
    # The persisted lease protects against another recovery during this wait.
    if lock is not None:
        lock.release()
    conclusion, detail = await _wait_for_completion(
        sid, expected_text=text, wait_seconds=wait_seconds, poll_seconds=poll_seconds
    )
    if state == "uncertain" and conclusion == "delivered_unverified":
        conclusion = "delivery_uncertain"
    db.update_cycle_delivery(
        run_id,
        state="completed"
        if conclusion == "ok"
        else "dsh_error"
        if conclusion == "dsh_error"
        else state,
        next_attempt_at=int(time.time()) + RECOVERY_INTERVAL_SECONDS,
    )
    if conclusion in {"ok", "dsh_error"}:
        result.delivered = True
    return finish(conclusion, turn=detail, chars=len(text))


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
            pending = db.pending_cycle_delivery()
            if pending is not None:
                return await _resume_delivery(
                    runtime,
                    pending,
                    result=result,
                    wait_seconds=wait_seconds,
                    poll_seconds=poll_seconds,
                    lock=lock,
                )
            run_id = db.start_run(trigger_source)
            result.run_id = run_id

            # ★★ C 项（2026-10-19）：先跑一轮**推荐流抽词**，再发现。
            #   放在最前面，是因为回填的关键词**本轮就该生效** ——
            #   `prepare_batch` 读的是 config，回填后同轮搜索能用到新词。
            #   仅在 auto_accept_suggestions=true 时才跑（默认关）；
            #   无论开关如何，推荐流有自己的每日轮次上限（trends_rounds）。
            trends_detail: dict[str, Any] = {}
            trends_backfilled: list[str] = []
            if bool(getattr(config.discovery, "auto_accept_suggestions", False)):
                try:
                    from core.trends_round import run_trends_round

                    trends = await run_trends_round(runtime, auto_backfill=True, run_id=run_id)
                    trends_detail = trends.to_dict()
                    trends_backfilled = list(trends.backfilled)
                    if trends_backfilled:
                        # 回填改了 config.yaml → 重新读盘，让**本轮**的搜索用上新词。
                        # 只换 runtime.config 不够：pipeline / worker 各自持有
                        # 一份 config 引用，必须一起换，否则仍是旧关键词。
                        with contextlib.suppress(Exception):
                            from core.config import load_config

                            config = load_config()
                            runtime.config = config
                            if getattr(runtime, "pipeline", None) is not None:
                                runtime.pipeline.config = config
                            if getattr(runtime, "worker", None) is not None:
                                runtime.worker.config = config
                except Exception as exc:  # noqa: BLE001 - 抽词失败不该挡住本轮
                    trends_detail = {"ok": False, "detail": f"{type(exc).__name__}"}
            else:
                trends_detail = {"skipped": "auto_accept_suggestions=false"}

            db.save_cycle_delivery(
                run_id,
                session_id=sid,
                payload={
                    "cards": [],
                    "batch_in_progress": True,
                    "trends": trends_detail,
                    "min_score": config.discovery.min_score_for_comment,
                    "candidate_cap": config.text_worker.max_candidates_to_dsh,
                },
            )
            db.update_cycle_delivery(
                run_id, next_attempt_at=int(time.time()) + RECOVERY_INTERVAL_SECONDS
            )
            try:
                batch = await runtime.pipeline.prepare_batch(cycle_run_id=run_id)
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
            saved = db.pending_cycle_delivery()
            payload = json.loads(saved["payload_json"])
            payload.update(
                cards=[card.to_payload() for card in cards],
                read_n=len(batch.read_bvids),
                errors_n=len(batch.failed),
                batch_in_progress=False,
            )
            db.update_cycle_delivery(
                run_id, payload_json=json.dumps(payload, ensure_ascii=False), next_attempt_at=0
            )
            _write_digest(
                db=db,
                run_id=run_id,
                trigger_source=trigger_source,
                batch=batch,
                cards=cards,
                keywords=list(config.discovery.keywords),
            )

            if not cards:
                conclusion = "batch_failed" if batch.failed else "no_candidates"
                db.update_cycle_delivery(run_id, state="empty")
                db.finish_run(
                    run_id,
                    candidates_n=0,
                    read_n=len(batch.read_bvids),
                    errors_n=len(batch.failed),
                    conclusion=conclusion,
                    detail={
                        "limitations": batch.limitations[:20],
                        "trends": trends_detail,
                    },
                )
                result.conclusion = conclusion
                return result

            return await _resume_delivery(
                runtime,
                db.pending_cycle_delivery(),
                result=result,
                wait_seconds=wait_seconds,
                poll_seconds=poll_seconds,
                lock=lock,
            )
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
