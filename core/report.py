"""日报：本地 Markdown（可靠留存）+ 固定结构（任务书 §6.7）。

日报**固定包含**：日期、搜索词、候选数、完整阅读数、推荐视频列表、证据链接、
评论/回复链接、失败与待处理项、下一步。

无候选时也生成简短的"今日无值得推荐内容"，**不发空评论**。
日报必须**区分**"实际平台读取 / 缓存命中 / 模拟响应"——
**不能把缓存内容称为本轮新抓取**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from core.storage import Database

NO_CONTENT_LINE = "今日无值得推荐内容。"


@dataclass
class DigestInput:
    run_id: str
    trigger_source: str
    keywords: list[str] = field(default_factory=list)
    candidates_n: int = 0
    read_n: int = 0
    drafts_n: int = 0
    sent_n: int = 0
    sim_sent_n: int = 0
    errors: list[str] = field(default_factory=list)
    recommended: list[dict[str, Any]] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    cache_stats: dict[str, int] = field(default_factory=dict)
    next_steps: list[str] = field(default_factory=list)
    health_state: str = "ready"
    health_note: str = ""
    simulated: bool = False


class DigestWriter:
    def __init__(self, *, db: Database, out_dir: Path | str) -> None:
        self.db = db
        self.out_dir = Path(out_dir)

    def render(self, data: DigestInput) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        stats = data.cache_stats or {}
        lines: list[str] = [
            f"# B 站小号日报 · {today}",
            "",
            f"- run_id：`{data.run_id}`",
            f"- 触发来源：{data.trigger_source}",
            f"- 平台健康状态：**{data.health_state}**"
            + (f"（{data.health_note}）" if data.health_note else ""),
            f"- 本次为模拟：{'是' if data.simulated else '否'}",
            "",
            "## 搜索词",
            "",
            *([f"- {kw}" for kw in data.keywords] or ["- （未使用关键词）"]),
            "",
            "## 计数",
            "",
            f"- 候选：{data.candidates_n}",
            f"- 完整阅读：{data.read_n}",
            f"- 生成的草稿：{data.drafts_n}",
            f"- 真实发送：{data.sent_n}",
            f"- 模拟发送：{data.sim_sent_n}",
            "",
            "## 平台读取 vs 缓存 vs 模拟（**必须区分**）",
            "",
            f"- 本轮真实请求数：{stats.get('requests_made', 0)}",
            f"- 缓存命中：{stats.get('cache_hits', 0)}",
            f"- 缓存过期后刷新：{stats.get('cache_expired', 0)}",
            f"- 只读重试：{stats.get('retries', 0)}",
            f"- 限流次数：{stats.get('rate_limits', 0)}",
        ]

        if stats.get("cache_hits"):
            lines.append(
                "- ⚠️ 本轮有缓存命中，这些内容**不是本轮新抓取**，不得称为新读取。"
            )

        lines += ["", "## 推荐视频", ""]
        if data.recommended:
            for item in data.recommended:
                lines.append(f"### {item.get('title', '(无标题)')}（{item.get('bvid', '')}）")
                lines.append("")
                lines.append(f"- 链接：{item.get('source_url', '')}")
                lines.append(f"- 证据等级：{item.get('evidence_level', 'unavailable')}")
                lines.append(f"- 总分：{item.get('score', 0)}")
                evidence_ids = item.get("evidence_ids") or []
                if evidence_ids:
                    lines.append(f"- 证据：{', '.join(f'`{e}`' for e in evidence_ids)}")
                for point in item.get("key_points", [])[:5]:
                    lines.append(
                        f"  - {point.get('claim', '')}（证据 `{point.get('evidence_id', '')}`）"
                    )
                if item.get("comment_url"):
                    lines.append(f"- 评论链接：{item['comment_url']}")
                lines.append("")
        else:
            lines += [NO_CONTENT_LINE, ""]

        lines += ["## 评论 / 回复链接", ""]
        sent = self.db.query(
            """
            SELECT action_type, bvid, remote_rpid, remote_url, status, is_simulated
              FROM actions WHERE status IN ('sent_verified','uncertain')
             ORDER BY updated_at DESC LIMIT 20
            """
        )
        if sent:
            for row in sent:
                tag = "（模拟）" if row["is_simulated"] else ""
                lines.append(
                    f"- {row['action_type']} {row['bvid']} → "
                    f"{row['remote_url'] or row['remote_rpid'] or '无远端 ID'}"
                    f" [{row['status']}]{tag}"
                )
        else:
            lines.append("- （无）")
        lines.append("")

        lines += ["## 失败与待处理", ""]
        if data.errors:
            lines += [f"- 失败：{err}" for err in data.errors]
        if data.pending:
            lines += [f"- 待处理：{item}" for item in data.pending]
        uncertain = self.db.query(
            "SELECT bvid, target_rpid FROM actions WHERE status = 'uncertain' LIMIT 20"
        )
        if uncertain:
            lines.append("- ⚠️ **不确定动作（已冻结，不自动重发，需人工核对）**：")
            for row in uncertain:
                lines.append(f"  - {row['bvid']} target={row['target_rpid'] or '-'}")
        health = self.db.current_health()
        if health is not None:
            lines.append(
                f"- ⚠️ 平台健康事件：{health['state']}（{health['category']}）"
                f" 需要人工动作：{health['detail']}"
            )
        if not data.errors and not data.pending and not uncertain and health is None:
            lines.append("- （无）")
        lines.append("")

        lines += ["## 主号汇报（@ 提及）状态", ""]
        notif_rows = self.db.list_owner_notifications(limit=20)
        if notif_rows:
            for row in notif_rows:
                observed = row["observed_mid"]
                extra = f" observed_mid={observed}" if observed is not None else ""
                lines.append(
                    f"- {row['bvid']} [{row['status']}] channel={row['channel']}"
                    f"{extra}"
                )
                if row["status"] != "verified":
                    lines.append(f"  - ⚠️ 未确认成功：{row['detail']}")
        else:
            lines.append("- （本轮没有需要 @ 主号的视频）")
        lines.append("")
        lines.append(
            "> ⚠️ `@数字UID` 或纯文本 `@昵称` **不算**提及成功；"
            "只有核验到页面渲染后的 UID 才记 `verified`。"
            "非 verified 时不得声称已通知主人。"
        )
        lines.append("")

        lines += ["## 下一步", ""]
        lines += [f"- {step}" for step in data.next_steps] or ["- （无）"]
        lines += [
            "",
            "---",
            "",
            "> 本日报由本项目 `runs`/`actions` 表生成。"
            "**DSH 定时提醒的投递记录不证明 Agent 已完成任务**，以本表为最终事实来源。",
            "",
        ]
        return "\n".join(lines)

    def write(self, data: DigestInput) -> Path:
        text = self.render(data)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        target = self.out_dir / f"digest-{stamp}.md"
        target.write_text(text, encoding="utf-8")

        history = self.out_dir / "digest-history.jsonl"
        with history.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "run_id": data.run_id,
                        "written_at": datetime.now().isoformat(timespec="seconds"),
                        "candidates_n": data.candidates_n,
                        "read_n": data.read_n,
                        "sent_n": data.sent_n,
                        "sim_sent_n": data.sim_sent_n,
                        "recommended_n": len(data.recommended),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        return target

    # ---- 保底产物（P1，2026-10-16） -----------------------------------------

    def write_stub(
        self,
        *,
        reason: str,
        exit_code: int | None = None,
        log_tail: str = "",
        run_id: str = "",
        extra: list[str] | None = None,
        overwrite: bool = False,
    ) -> Path | None:
        """**不管这一轮发生什么，都要留下"发生了什么"**。

        为什么需要（来自竞品精读 §2.4，任务书也要求"不能悄悄什么都没写"）：

        如果这一轮崩在写日报之前，``out/`` 就是空的 ——
        使用者**完全看不到这一轮发生了什么**，也不知道白烧了钱。

        本方法写一份**占位日报**：说明这一轮没跑成、给出退出码与日志尾部。
        与竞品的差异：**日志尾一定过** :func:`core.redact.redact_text`
        （它没有脱敏这一步）。

        :param overwrite: 默认**不覆盖**已有日报（真日报优先）；
            只有调用方明确要求时才覆盖。
        :returns: 写入路径；若已有日报且 ``overwrite=False`` 则返回 ``None``
        """
        self.out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        target = self.out_dir / f"digest-{stamp}.md"
        if target.exists() and not overwrite:
            return None

        from core.redact import redact

        safe_tail = redact(log_tail or "")[-1500:]

        lines: list[str] = [
            f"# B 站小号日报 · {datetime.now().strftime('%Y-%m-%d')} —— 这一轮没写出正式日报",
            "",
            "> ⚠️ 这是一份**占位日报**：这一轮在写到正常日报之前就结束了。",
            "> 留它是为了让你知道「发生过什么」，而不是打开文件夹看到一片空白。",
            "",
            "## 发生了什么",
            "",
            f"- 原因：{reason}",
        ]
        if exit_code is not None:
            lines.append(f"- 退出码：{exit_code}")
        if run_id:
            lines.append(f"- run_id：`{run_id}`")
        for item in extra or []:
            lines.append(f"- {item}")
        lines += [
            "",
            "## 下一步",
            "",
            "- 看下面这段日志尾，或者到项目目录跑一次 `cli.app status` 看最近运行记录。",
            "- 如果反复失败，请把这段日志尾发给主人/开发者。",
            "",
        ]
        if safe_tail.strip():
            lines += ["## 日志尾部（已脱敏）", "", "```text", safe_tail.rstrip(), "```", ""]
        else:
            lines += ["（没有可用的日志尾部。）", ""]

        target.write_text("\n".join(lines), encoding="utf-8")
        return target

    @staticmethod
    def log_tail(*, log_file: Path | str, limit: int = 1500) -> str:
        """读日志尾部（**给 ``write_stub`` 用**；不做脱敏，脱敏在那边做）。

        刻意做成 ``staticmethod``：它不依赖 writer 实例，
        调用方常常是"还没建 writer 就想先抓日志"的失败路径。
        """
        path = Path(log_file)
        if not path.exists():
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-limit:]


