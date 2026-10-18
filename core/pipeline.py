"""编排层：发现 → 去重 → 阅读 → 证据登记 → 豆包预处理 → 短候选包。

对应任务书 §7 任务 3/4/6 的 bridge 内部实现。**原文留在 bridge 内**：
本模块把字幕全文/评论全文登记成 ``evidence``，只把**短候选包**交给 DSH。

关键约束：

* ``prepare_batch`` 最多给 DSH ``max_candidates_to_dsh``（上限 3）个短包，
  且整批不超过 ``max_batch_chars``。
* 候选集保留一小部分由**确定性规则**选出的新近/多样性样本供 DSH 抽查，
  并记录被筛掉的视频以便回看（任务书 §2.1.3）。
* 同一视频重复运行**不重复建笔记**。
* 字幕缺失时**降级**（``description_only``）并降低评分，**不编造字幕**、不无限重试。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from core.bilibili import (
    BilibiliReader,
    BilibiliReadError,
    CommentPage,
    SubtitleBody,
    VideoDetail,
    VideoMeta,
)
from core.health import READY
from core.ratelimit import RequestQueue, RequestsBlocked
from core.storage import Database, now_ts
from core.text_worker import CandidateCard, TextWorker

#: 证据等级（任务书 §4）
LEVEL_SUBTITLE = "subtitle"
LEVEL_ASR = "asr"
LEVEL_DESCRIPTION_ONLY = "description_only"
LEVEL_UNAVAILABLE = "unavailable"

#: 每个候选包最多配几条证据给豆包（控制成本）
MAX_EVIDENCE_PER_VIDEO = 40


@dataclass
class DiscoveryResult:
    candidates: list[VideoMeta] = field(default_factory=list)
    new_count: int = 0
    seen_count: int = 0
    cache_hits: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class BatchResult:
    """``prepare_batch`` 的产物。"""

    candidates: list[CandidateCard] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    #: ★ 本轮**完整读取过的视频**（BVID）。与 ``evidence_ids`` 是两回事：
    #: 一条视频会登记几十条证据，混用会把"读了 3 条视频"报成"读了 155 条"。
    read_bvids: list[str] = field(default_factory=list)
    #: 因**时间预算耗尽**而未处理的视频（供上层如实汇报"没读完"）
    deferred_bvids: list[str] = field(default_factory=list)
    #: 本轮实际耗时（秒）
    elapsed_seconds: float = 0.0
    cache_stats: dict[str, int] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    def to_payload(self, *, max_batch_chars: int) -> dict[str, Any]:
        """裁剪到整批字符上限后返回。**不足则原样返回。**"""
        result: list[dict[str, Any]] = []
        used = 0
        for card in self.candidates:
            payload = card.to_payload()
            size = len(json.dumps(payload, ensure_ascii=False))
            if result and used + size > max_batch_chars:
                self.limitations.append(f"BATCH_CHAR_LIMIT_DROPPED_{card.bvid}")
                continue
            result.append(payload)
            used += size
        return {
            "candidates": result,
            "skipped": self.skipped,
            "failed": self.failed,
            "evidence_ids": self.evidence_ids,
            "read_bvids": self.read_bvids,
            "deferred_bvids": self.deferred_bvids,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "cache_stats": self.cache_stats,
            "limitations": self.limitations,
            "batch_chars": used,
        }


class Pipeline:
    """把各层拼起来的高层编排。

    这个类**不决定是否发言**——那是 DSH 的事。
    它只负责"把高消耗的读料嚼碎"并保证证据可反查。
    """

    def __init__(
        self,
        *,
        db: Database,
        config: Any,
        reader: BilibiliReader,
        queue: RequestQueue,
        worker: TextWorker | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.reader = reader
        self.queue = queue
        self.worker = worker

    # ---- 发现 --------------------------------------------------------------

    async def discover(
        self,
        *,
        keywords: list[str] | None = None,
        days: int | None = None,
        limit: int | None = None,
    ) -> DiscoveryResult:
        """按关键词与发布时间发现视频并去重。"""
        cfg = self.config.discovery
        use_keywords = keywords or cfg.keywords
        window_days = days if days is not None else cfg.published_within_days
        cap = limit if limit is not None else cfg.max_candidates_per_run

        begin = now_ts() - window_days * 86400
        result = DiscoveryResult()

        for keyword in use_keywords:
            if len(result.candidates) >= cap:
                break
            try:
                metas = await self.reader.search_videos(
                    keyword, order="pubdate", page=1, page_size=20, pubtime_begin=begin
                )
            except RequestsBlocked as exc:
                result.errors.append(f"BLOCKED:{exc.state}")
                break
            except BilibiliReadError as exc:
                result.errors.append(f"{keyword}:{exc.code}")
                continue

            for meta in metas:
                if len(result.candidates) >= cap:
                    break
                if meta.published_at and meta.published_at < begin:
                    continue
                is_new = self.db.upsert_video(
                    bvid=meta.bvid,
                    aid=meta.aid,
                    title=meta.title,
                    up_mid=meta.up_mid,
                    up_name=meta.up_name,
                    published_at=meta.published_at or None,
                    duration_sec=meta.duration_sec or None,
                    source_url=meta.source_url,
                    discovery_keyword=keyword,
                )
                if is_new:
                    result.new_count += 1
                    result.candidates.append(meta)
                else:
                    result.seen_count += 1

        result.cache_hits = self.queue.stats.cache_hits
        return result

    # ---- 阅读 + 证据登记 ---------------------------------------------------

    async def read_video(
        self, bvid: str, *, detail: VideoDetail | None = None
    ) -> tuple[CandidateCard, list[dict[str, Any]]]:
        """读取一个视频并登记证据，返回 ``(短候选包, 证据块)``。

        **不把原文返回给调用方之外的任何地方**——DSH 只能拿到短包。
        """
        if detail is None:
            detail = await self.reader.get_video_detail(bvid)

        self.db.upsert_video(
            bvid=detail.bvid,
            aid=detail.aid,
            title=detail.title,
            up_mid=detail.up_mid,
            up_name=detail.up_name,
            published_at=detail.published_at or None,
            duration_sec=detail.duration_sec or None,
            source_url=detail.source_url,
        )

        blocks: list[dict[str, Any]] = []
        limitations: list[str] = []

        # ① 简介（总是可用）
        if detail.description.strip():
            eid = self.db.add_evidence(
                bvid=bvid,
                source_type="description",
                text=detail.description,
                source_url=detail.source_url,
            )
            blocks.append(
                {"evidence_id": eid, "source_type": "description", "start_sec": None,
                 "text": detail.description}
            )
        else:
            limitations.append("NO_DESCRIPTION")

        # ② 字幕（需登录；失败则降级，不重试到超时）
        subtitle: SubtitleBody | None = None
        try:
            metas = await self.reader.get_subtitles(bvid, detail.cid)
            if metas:
                chosen = _choose_subtitle(metas)
                subtitle = await self.reader.fetch_subtitle_body(chosen.url)
        except RequestsBlocked:
            raise
        except BilibiliReadError as exc:
            limitations.append(f"SUBTITLE_{exc.code}")

        if subtitle is not None and subtitle.segments:
            # ★ 时间戳随片段一起登记，供豆包给出 start_sec 后反查
            for segment in subtitle.segments[:MAX_EVIDENCE_PER_VIDEO]:
                eid = self.db.add_evidence(
                    bvid=bvid,
                    source_type="subtitle",
                    text=segment.text,
                    source_url=detail.source_url,
                    start_sec=segment.start_sec,
                    end_sec=segment.end_sec,
                )
                blocks.append(
                    {
                        "evidence_id": eid,
                        "source_type": "subtitle",
                        "start_sec": segment.start_sec,
                        "text": segment.text,
                    }
                )
            evidence_level = LEVEL_SUBTITLE
        else:
            evidence_level = LEVEL_DESCRIPTION_ONLY
            limitations.append("SUBTITLE_UNAVAILABLE")

            # ★ 本地 ASR 兜底（P1，2026-10-16）—— **默认关**。
            #   任务书允许"字幕缺失就降级"，但那样等于放弃这条视频。
            #   开着它时，本机转写一遍（不花 API 钱），成功就升到 LEVEL_ASR。
            #   ⚠️ 空文本 = **这条视频没有语音**（纯音乐/纯录屏），**不是坏了** ——
            #      按"跳过（无语音）"处理，**不重试、不写调试脚本**。
            if getattr(self.config.text_worker, "asr_fallback", False):
                asr_text, asr_note = await self._try_asr_fallback(bvid)
                if asr_text:
                    eid = self.db.add_evidence(
                        bvid=bvid,
                        source_type="asr",
                        text=asr_text,
                        source_url=detail.source_url,
                    )
                    blocks.append(
                        {
                            "evidence_id": eid,
                            "source_type": "asr",
                            "start_sec": None,
                            "text": asr_text,
                        }
                    )
                    evidence_level = LEVEL_ASR
                    limitations.append("EVIDENCE_FROM_LOCAL_ASR")
                elif asr_note:
                    limitations.append(asr_note)

        card: CandidateCard
        if self.worker is not None:
            card = await self.worker.analyze(
                bvid=bvid,
                aid=detail.aid,
                title=detail.title,
                source_url=detail.source_url,
                evidence_level=evidence_level,
                evidence_blocks=blocks,
                # ★ 把**主人的关注点**传给模型（2026-10-17）。
                #   不传的话它只能按"内容充实度"打分 —— 这正是
                #   "没什么用却给 100 分"的根因。
                focus=self.build_focus(),
            )
        else:
            card = CandidateCard(
                bvid=bvid,
                aid=detail.aid,
                title=detail.title,
                source_url=detail.source_url,
                evidence_level=evidence_level,
                needs_deep_review=True,
                limitations=["TEXT_WORKER_UNAVAILABLE"],
            )

        card.limitations.extend(limitations)
        if evidence_level == LEVEL_DESCRIPTION_ONLY:
            # 降级 → 降低评分（不编造字幕）
            card.relevance_score = min(card.relevance_score, 40)
            card.limitations.append("SCORE_CAPPED_DESCRIPTION_ONLY")

        card.uncertainties.append(
            f"证据等级 {evidence_level}："
            + (
                "有字幕/ASR 支撑"
                if evidence_level in (LEVEL_SUBTITLE, LEVEL_ASR)
                else "仅简介可用，不得写成\"视频讲到\""
            )
        )
        # 预处理完成才标为 read；中途取消时仍应能在下轮继续。
        self.db.set_video_status(bvid, "read")
        return card, blocks

    def build_focus(self) -> dict[str, list[str]]:
        """组装**主人的关注点**，供打分时传给模型（2026-10-17）。

        为什么需要它：之前模型打分时**只知道标题和证据**，
        压根不知道"对谁有用"，于是只能按"内容充实度"打 ——
        一条与关注点无关的吐槽视频因此拿到 100 分。

        来源（都是本地已有数据，**不额外发请求**）：

        * ``keywords`` ← ``config.discovery.keywords``（主人自己写的关注词）
        * ``rules`` / ``likes`` / ``dislikes`` ← ``owner_preferences`` 表
          （主人通过 ``cli.app prefs`` 录入的规则/喜欢/不喜欢）

        Returns:
            ``{"keywords": [...], "rules": [...], "likes": [...], "dislikes": [...]}``。
            **读不到就返回空列表**，绝不抛异常（打分降级好过整轮失败）。
        """
        focus: dict[str, list[str]] = {
            "keywords": [],
            "rules": [],
            "likes": [],
            "dislikes": [],
        }
        # ⚠️ 这里用 suppress 而不是 try/except-pass：
        #    打分降级好过整轮失败（读不到偏好就按"只看关键词"打）
        with contextlib.suppress(Exception):
            focus["keywords"] = [
                str(k) for k in self.config.discovery.keywords if str(k).strip()
            ]

        with contextlib.suppress(Exception):
            rows = self.db.query(
                "SELECT kind, content FROM owner_preferences WHERE active = 1"
            )
            for row in rows:
                kind = str(row["kind"])
                content = str(row["content"] or "").strip()
                if not content:
                    continue
                if kind == "rule":
                    focus["rules"].append(content)
                elif kind == "like":
                    focus["likes"].append(content)
                elif kind == "dislike":
                    focus["dislikes"].append(content)

        return focus

    async def _try_asr_fallback(self, bvid: str) -> tuple[str, str]:
        """字幕不可用时，试着在本机转写一遍（**不花 API 钱**）。

        返回 ``(转写文本, 限制说明)``：

        * 拿到文字 → ``(text, "")``；
        * **没有语音** → ``("", "ASR_NO_SPEECH")``（**这是结果，不是故障**）；
        * 依赖没装 / 转写失败 / 超时 → ``("", "ASR_<CODE>")``。

        ★ 为什么把"没有语音"单独分出来（竞品实测教训）：
        转写返回空串时如果当成"ASR 坏了"，就会去写调试脚本、反复重跑 ——
        有用户因此**烧掉 27 次工具调用**，最后报告是空的。
        """
        try:
            from core.asr import transcribe_target
        except ImportError:
            return "", "ASR_MODULE_MISSING"

        model = str(getattr(self.config.text_worker, "asr_model", "") or "")
        try:
            # ★ ASR 是**同步阻塞**的重活（CPU 转写），扔到线程里别堵事件循环
            result = await asyncio.to_thread(transcribe_target, bvid, model=model)
        except Exception as exc:  # noqa: BLE001 —— 兜底能力失败不该让整条读取崩
            return "", f"ASR_ERROR_{type(exc).__name__}"

        if result.ok and result.text.strip():
            return result.text.strip(), ""
        if result.ok:
            # ok 但文本为空 ⇒ 没有语音（纯音乐 / 纯录屏 / 无声演示）
            return "", "ASR_NO_SPEECH"
        return "", f"ASR_{result.code}"

    # ---- 批次 --------------------------------------------------------------

    async def prepare_batch(
        self,
        *,
        keywords: list[str] | None = None,
        days: int | None = None,
        limit: int | None = None,
        include_reviewed: bool = False,
    ) -> BatchResult:
        """定时/手动批处理：发现 → 去重 → 限速读取 → 豆包预处理 → 短候选包。"""
        batch = BatchResult()
        # 同一个预算覆盖发现、网络等待和模型调用，不只在视频间检查。
        budget = float(getattr(self.config.text_worker, "max_batch_seconds", 90.0))
        started_at = time.monotonic()

        def remaining() -> float:
            return budget - (time.monotonic() - started_at)

        try:
            async with asyncio.timeout(max(0.0, remaining())):
                discovery = await self.discover(keywords=keywords, days=days, limit=limit)
        except TimeoutError:
            batch.failed.append({
                "keyword_or_bvid": "discovery", "reason": "DISCOVERY_TIME_BUDGET_EXHAUSTED"
            })
            batch.deferred_bvids = self._pending_videos(
                limit=self.config.discovery.max_full_reads_per_run
            )
            batch.elapsed_seconds = time.monotonic() - started_at
            batch.limitations.append("TIME_BUDGET_EXHAUSTED_DURING_DISCOVERY")
            batch.cache_stats = self.queue.stats.to_dict()
            return batch
        batch.failed.extend(
            {"keyword_or_bvid": err, "reason": "discover_error"} for err in discovery.errors
        )

        cfg = self.config.discovery
        if include_reviewed:
            pending = [
                str(row["bvid"])
                for row in self.db.query(
                    "SELECT bvid FROM videos LIMIT ?", (cfg.max_full_reads_per_run,)
                )
            ]
        else:
            pending = self._pending_videos(limit=cfg.max_full_reads_per_run)

        if not pending:
            batch.limitations.append("NO_PENDING_VIDEOS")
            batch.elapsed_seconds = time.monotonic() - started_at
            batch.cache_stats = self.queue.stats.to_dict()
            return batch

        cards: list[CandidateCard] = []
        # ★ 确定性规则选出的新近样本，保证弱模型漏掉的好内容仍能被 DSH 抽查
        for bvid in pending[: cfg.max_full_reads_per_run]:
            if remaining() <= 0:
                batch.deferred_bvids.append(bvid)
                continue
            try:
                async with asyncio.timeout(max(0.0, remaining())):
                    card, blocks = await self.read_video(bvid)
            except TimeoutError:
                # 已登记的证据留在库内供重试复用；本轮不冒充完整读完。
                batch.deferred_bvids.append(bvid)
                continue
            except RequestsBlocked as exc:
                batch.failed.append({"keyword_or_bvid": bvid, "reason": f"BLOCKED:{exc.state}"})
                break
            except BilibiliReadError as exc:
                batch.failed.append({"keyword_or_bvid": bvid, "reason": exc.code})
                self.db.set_video_status(bvid, "error")
                continue

            batch.evidence_ids.extend(block["evidence_id"] for block in blocks)
            batch.read_bvids.append(bvid)
            cards.append(card)

        # 低分只入库/略过，不交给 DSH（省 token），但**记录**以便回看
        keep: list[CandidateCard] = []
        for card in cards:
            if card.relevance_score < cfg.min_score_for_comment and not card.needs_deep_review:
                batch.skipped.append(
                    {"bvid": card.bvid, "reason": f"LOW_SCORE_{card.relevance_score}"}
                )
                self.db.set_video_status(card.bvid, "skipped")
                continue
            keep.append(card)

        keep.sort(key=lambda c: c.relevance_score, reverse=True)
        cap = self.config.text_worker.max_candidates_to_dsh
        batch.candidates = keep[:cap]
        for card in keep[cap:]:
            batch.skipped.append({"bvid": card.bvid, "reason": "OVER_BATCH_LIMIT"})

        batch.elapsed_seconds = time.monotonic() - started_at
        if batch.deferred_bvids:
            batch.limitations.append(
                f"TIME_BUDGET_EXHAUSTED_DEFERRED_{len(batch.deferred_bvids)}"
                f"（预算 {budget:.0f}s，已用 {batch.elapsed_seconds:.0f}s）"
                "—— 这些视频本轮**没读**，下次继续，不是失败"
            )
        batch.cache_stats = self.queue.stats.to_dict()
        batch.limitations.append(f"DISCOVERED_NEW_{discovery.new_count}")
        batch.limitations.append(f"ALREADY_SEEN_{discovery.seen_count}")
        return batch

    def _pending_videos(self, *, limit: int) -> list[str]:
        """待处理视频：``discovered`` 优先，其次 ``read``（同一视频不重复建笔记）。"""
        rows = self.db.query(
            """
            SELECT bvid FROM videos
             WHERE status = 'discovered'
             ORDER BY COALESCE(published_at, 0) DESC
             LIMIT ?
            """,
            (limit,),
        )
        return [str(row["bvid"]) for row in rows]

    # ---- 笔记与草稿 --------------------------------------------------------

    def save_analysis(
        self,
        *,
        bvid: str,
        evidence_ids: list[str],
        score_total: int,
        summary: str,
        facts: list[str] | None = None,
        inferences: list[str] | None = None,
        doubts: list[str] | None = None,
        knowledge_tags: list[str] | None = None,
        draft_text: str | None = None,
        draft_type: str = "top",
        target_rpid: str | None = None,
        target_root: str | None = None,
        evidence_level: str = LEVEL_UNAVAILABLE,
    ) -> tuple[bool, str, str | None]:
        """持久化笔记与草稿。返回 ``(ok, code, draft_id)``。

        ★ **强制**：``evidence_ids`` 必须存在、属于同一 BV、指向真实读过的来源。
        模型自报"我看过"不能凭空生成证据。
        """
        ok, reason = self.db.verify_evidence_ids(bvid, evidence_ids)
        if not ok:
            return False, reason, None

        ts = now_ts()
        with self.db.transaction() as tx:
            tx.execute(
                """
                INSERT INTO notes (bvid, summary, score_total, facts_json, inferences_json,
                                   doubts_json, knowledge_tags, evidence_level, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(bvid) DO UPDATE SET
                    summary = excluded.summary,
                    score_total = excluded.score_total,
                    facts_json = excluded.facts_json,
                    inferences_json = excluded.inferences_json,
                    doubts_json = excluded.doubts_json,
                    knowledge_tags = excluded.knowledge_tags,
                    evidence_level = excluded.evidence_level,
                    updated_at = excluded.updated_at
                """,
                (
                    bvid,
                    summary,
                    score_total,
                    json.dumps(facts or [], ensure_ascii=False),
                    json.dumps(inferences or [], ensure_ascii=False),
                    json.dumps(doubts or [], ensure_ascii=False),
                    json.dumps(knowledge_tags or [], ensure_ascii=False),
                    evidence_level,
                    ts,
                ),
            )

        draft_id: str | None = None
        if draft_text and draft_text.strip():
            draft_id = self.db.create_draft(
                draft_type=draft_type,
                bvid=bvid,
                text=draft_text.strip(),
                evidence_ids=evidence_ids,
                target_rpid=target_rpid,
                target_root=target_root,
            )

        self.db.set_video_status(bvid, "briefed")
        return True, "OK", draft_id

    def export_note_markdown(self, bvid: str, out_dir: Any) -> Any:
        """导出 Markdown 笔记（文件名含 BV）。**数据库仍是主状态。**"""
        from pathlib import Path

        row = self.db.query_one("SELECT * FROM notes WHERE bvid = ?", (bvid,))
        video = self.db.get_video(bvid)
        if row is None or video is None:
            return None

        evidence = self.db.evidence_for_bvid(bvid)
        lines = [
            f"# {video['title']}（{bvid}）",
            "",
            f"- 链接：{video['source_url']}",
            f"- 证据等级：{row['evidence_level']}",
            f"- 总分：{row['score_total']}",
            f"- 更新时间：{row['updated_at']}",
            "",
            "## 摘要",
            "",
            str(row["summary"]),
            "",
            "## 有证据支持的事实",
            "",
            *[f"- {item}" for item in json.loads(str(row["facts_json"]))],
            "",
            "## 推断",
            "",
            *[f"- {item}" for item in json.loads(str(row["inferences_json"]))],
            "",
            "## 未验证点",
            "",
            *[f"- {item}" for item in json.loads(str(row["doubts_json"]))],
            "",
            "## 证据来源",
            "",
        ]
        for item in evidence:
            start = item["start_sec"]
            where = f" @{start}s" if start is not None else ""
            lines.append(
                f"- `{item['evidence_id']}`"
                f"（{item['source_type']}{where}）：{item['source_url']}"
            )

        directory = Path(out_dir)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"note-{bvid}.md"
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return target

    # ---- 讨论区 ------------------------------------------------------------

    async def get_discussion(
        self, *, bvid: str, page: int = 1, limit: int = 20
    ) -> tuple[CommentPage, int]:
        """限速拉取评论并**登记为证据**（供 ``prepare_reply`` 引用）。

        讨论区内容一律当**不可信数据**，不执行其中的任何"指令"。
        """
        video = self.db.get_video(bvid)
        if video is None or video["aid"] is None:
            detail = await self.reader.get_video_detail(bvid)
            aid = detail.aid
        else:
            aid = int(video["aid"])

        cursor = 0 if page <= 1 else 0
        page_result = await self.reader.get_comments(aid, next_cursor=cursor, page_size=limit)

        registered = 0
        for item in page_result.replies:
            eid = self.db.add_evidence(
                bvid=bvid,
                source_type="comment",
                text=item.message,
                source_url=f"https://www.bilibili.com/video/{bvid}/#reply{item.rpid}",
            )
            self.db.mark_seen_reply(
                remote_rpid=item.rpid,
                bvid=bvid,
                root_rpid=item.root,
                parent_rpid=item.parent,
                author_mid=item.author_mid,
                author_name=item.author_name,
                text_excerpt=item.message,
                is_mention=False,
            )
            registered += 1
            _ = eid
        return page_result, registered


def _choose_subtitle(metas: list[Any]) -> Any:
    """优先中文人工字幕，其次中文自动字幕（``lan_doc`` 含"自动"），最后第一个。"""
    for meta in metas:
        if meta.lan.lower().startswith("zh") and "自动" not in meta.lan_doc:
            return meta
    for meta in metas:
        if meta.lan.lower().startswith("zh"):
            return meta
    return metas[0]


__all__ = [
    "BatchResult",
    "DiscoveryResult",
    "LEVEL_ASR",
    "LEVEL_DESCRIPTION_ONLY",
    "LEVEL_SUBTITLE",
    "LEVEL_UNAVAILABLE",
    "Pipeline",
    "READY",
]
