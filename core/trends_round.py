"""推荐流关键词轮（只读 + 可选自动回填）—— 供 MCP 工具与自动调度共用。

## 为什么单独抽出来

以前"跑一次推荐流"的逻辑只写在 ``bridge/server.py`` 的 MCP 工具里，
于是自动学习轮（``core/cycle.py``）**根本不会调用它** ——
主人要的"DSH 自动调整搜索词"就永远没有触发点。

抽到这里之后：

* MCP 工具 ``trends_suggest`` 调它（人在对话里让 DSH 跑）；
* 自动调度在 ``discovery.auto_accept_suggestions=true`` 时也调它
  （每次自动轮最多一轮，仍受每日轮次上限约束）。

## 安全边界

* 推荐流是**个性化算法喂的**；候选词只当"词库扩展器"；
* ``auto_accept_suggestions=false``（默认）时只进候选池，不改关键词；
* 无论开关如何，``quota_check`` 的每日轮次上限都先生效。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.bilibili import BilibiliReadError
from core.ratelimit import RateLimitExceeded, RequestsBlocked
from core.trends import (
    DEFAULT_MAX_ITEMS,
    RCMD_PATH,
    extract_candidates,
    parse_feed,
    quota_check,
    record_adoption,
    record_round,
    record_suggestions,
    rounds_summary,
)
from core.yamlkeywords import add_keyword, read_keywords

#: 自动回填时一次最多写入几个词（防止一轮就把关键词撑爆）。
MAX_BACKFILL_PER_ROUND = 3


@dataclass
class TrendsRoundResult:
    """一轮推荐流的结果（字段都可直接序列化给人看）。"""

    ok: bool
    code: str = "OK"
    detail: str = ""
    fetched: int = 0
    new_in_pool: int = 0
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    backfilled: list[str] = field(default_factory=list)
    #: 因**关键词总数超上限**而没回填的词（如实报告，不静默丢弃）
    backfill_skipped: list[str] = field(default_factory=list)
    quota: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "new_in_pool": self.new_in_pool,
            "suggestions": self.suggestions,
            "auto_backfilled": self.backfilled,
            "backfill_skipped": self.backfill_skipped,
            "quota": self.quota,
            "code": self.code,
            "detail": self.detail,
        }


async def run_trends_round(
    runtime: Any,
    *,
    limit: int | None = None,
    min_hits: int | None = None,
    auto_backfill: bool | None = None,
    run_id: str = "",
) -> TrendsRoundResult:
    """跑一轮推荐流 → 候选词池（可选回填关键词）。

    :param runtime: 至少有 ``db`` / ``config`` / ``reader`` 的运行时。
    :param auto_backfill: ``None`` = 读 ``discovery.auto_accept_suggestions``。
    :param run_id: 触发它的主流程 run（写进采纳账本，便于追溯是哪一轮加的）。
    """
    db = runtime.db
    config = runtime.config

    allowed, reason = quota_check(db)
    if not allowed:
        return TrendsRoundResult(
            ok=False,
            code="RATE_LIMITED",
            detail=reason,
            quota=rounds_summary(db),
        )

    cap = DEFAULT_MAX_ITEMS if limit is None else max(1, min(int(limit), 12))
    hits_floor = 2 if min_hits is None else max(1, min(int(min_hits), 10))

    params = {"fresh_type": 3, "ps": cap, "fresh_idx": 1, "fresh_idx_1h": 1}
    try:
        payload = await runtime.reader._get_signed(  # noqa: SLF001 —— 复用现成签名链路
            f"https://api.bilibili.com{RCMD_PATH}",
            params,
            kind="metadata",
            use_cache=False,
        )
    except RequestsBlocked as exc:
        record_round(db, conclusion="blocked")
        return TrendsRoundResult(
            ok=False, code="BLOCKED", detail=f"{exc.state}：{exc.human_action}"
        )
    except RateLimitExceeded as exc:
        record_round(db, conclusion="rate_limited")
        return TrendsRoundResult(ok=False, code="RATE_LIMITED", detail=str(exc))
    except BilibiliReadError as exc:
        record_round(db, conclusion="error")
        return TrendsRoundResult(
            ok=False, code="PLATFORM_ERROR", detail=f"{exc.code}: {exc}"
        )

    items = parse_feed(payload)
    if not items:
        record_round(db, conclusion="no_content")
        return TrendsRoundResult(
            ok=True,
            code="NO_CONTENT",
            detail="推荐流这次没返回内容（不是故障；也可能该接口对你这个账号不适用）。",
            quota=rounds_summary(db),
        )

    suggestions = extract_candidates(
        items,
        known_keywords=list(config.discovery.keywords),
        limit=10,
        min_hits=hits_floor,
    )
    added = record_suggestions(db, suggestions, source="rcmd")

    if auto_backfill is None:
        auto_backfill = bool(getattr(config.discovery, "auto_accept_suggestions", False))
    backfilled: list[str] = []
    backfill_skipped: list[str] = []
    if auto_backfill:
        # ★★ 三道护栏（2026-10-19，C 项）：没有它们，"自动回填"会失控。
        #   ① 一轮最多写 MAX_BACKFILL_PER_ROUND 个（防止一轮灌入十个词）；
        #   ② 关键词**总数**不超过 discovery.max_keywords（防止无限膨胀，
        #      每多一个词就多一路搜索请求 ⇒ 请求量与风控暴露面线性上涨）；
        #   ③ 每个写入都落 keyword_adoptions 账本（可审计、可撤销）。
        max_total = int(getattr(config.discovery, "max_keywords", 12) or 12)
        current = read_keywords()
        room = max(0, max_total - len(current))
        for suggestion in suggestions[:MAX_BACKFILL_PER_ROUND]:
            keyword = str(suggestion.keyword)
            if room <= 0:
                backfill_skipped.append(keyword)
                record_adoption(
                    db, keyword=keyword, run_id=run_id, state="skipped",
                    note=f"关键词总数已达上限 {max_total}",
                )
                continue
            ok, _ = add_keyword(keyword)
            if ok:
                backfilled.append(keyword)
                room -= 1
                record_adoption(db, keyword=keyword, run_id=run_id, state="active")
            else:
                backfill_skipped.append(keyword)
                record_adoption(
                    db, keyword=keyword, run_id=run_id, state="skipped", note="写入失败"
                )

    record_round(db, fetched_n=len(items), added_n=added, conclusion="ok")

    return TrendsRoundResult(
        ok=True,
        code="OK",
        fetched=len(items),
        new_in_pool=added,
        suggestions=[s.to_dict() for s in suggestions],
        backfilled=backfilled,
        backfill_skipped=backfill_skipped,
        quota=rounds_summary(db),
    )
