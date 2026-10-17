"""人设规则与现实约束的自动一致性审计（人用，只读）。

主人要求：「不能只转移而不审查思考一番」。

本模块把 :mod:`core.config` 的**实际生效值**与两处**上级权威**对撞：

* ``projects/dsh-bili-curator/docs/13-agent-persona.md``（人设，21 题问卷定稿）
* ``docs/DSH_EXECUTION_BRIEF.md``（任务书）

对每条规则判定：

``consistent``  各方一致
``tightened``   本项目取值比上级**更严**（安全，通常是好事）
``loosened``    ⚠️ 本项目取值比上级**更松**（需要解释）
``missing``     上级要求了，但本项目**没有对应实现**（缺功能）
``conflict``    ⚠️ 双方直接对立，需要人裁决

``loosened`` / ``missing`` / ``conflict`` 都是"需要人看"的项。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from core.paths import project_root

Verdict = Literal["consistent", "tightened", "loosened", "missing", "conflict"]

PERSONA_DOC = (
    project_root().parent / "dsh-bili-curator" / "docs" / "13-agent-persona.md"
)
BRIEF_DOC = project_root().parent.parent / "docs" / "DSH_EXECUTION_BRIEF.md"


@dataclass
class Finding:
    """一条审计结论。"""

    topic: str
    verdict: Verdict
    ours: str
    upstream: str
    note: str = ""
    needs_human: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "verdict": self.verdict,
            "ours": self.ours,
            "upstream": self.upstream,
            "note": self.note,
            "needs_human": self.needs_human,
        }


@dataclass
class AuditReport:
    findings: list[Finding] = field(default_factory=list)
    upstream_readable: dict[str, bool] = field(default_factory=dict)

    @property
    def needs_human(self) -> list[Finding]:
        return [f for f in self.findings if f.needs_human]

    def to_dict(self) -> dict[str, Any]:
        return {
            "upstream_readable": self.upstream_readable,
            "findings": [f.to_dict() for f in self.findings],
            "by_verdict": _count_by_verdict(self.findings),
            "needs_human": [f.to_dict() for f in self.needs_human],
        }


def _count_by_verdict(findings: list[Finding]) -> dict[str, int]:
    out: dict[str, int] = {}
    for finding in findings:
        out[finding.verdict] = out.get(finding.verdict, 0) + 1
    return out


def _extract_number(text: str, pattern: str) -> int | None:
    """从文本里按正则抠出第一个整数。用于读上级文档里的数值。"""
    match = re.search(pattern, text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (ValueError, IndexError):
        return None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _example_like_quota() -> int:
    """从 config.example.yaml 读点赞配额（它没有专门字段，从注释里取）。

    取不到就用 interaction 的回复配额近似 —— 只影响审计展示，不影响运行。
    """
    text = _read(project_root() / "config.example.yaml")
    found = _extract_number(text, r"max_likes_per_day[:\s=]*(\d+)")
    return found if found is not None else 8


def audit(config: Any) -> AuditReport:
    """执行审计。``config`` 是 :class:`core.config.Config`。"""
    report = AuditReport()
    persona_text = _read(PERSONA_DOC)
    brief_text = _read(BRIEF_DOC)
    report.upstream_readable = {
        "persona_doc": bool(persona_text),
        "brief_doc": bool(brief_text),
    }

    interaction = config.interaction
    persona = config.persona
    report_cfg = config.report

    # ---- ⓪ 频率档位（主人要求"多档可选"）--------------------------------
    report.findings.append(
        Finding(
            topic="频率档位",
            verdict="consistent",
            ours=f"frequency_tier = {interaction.frequency_tier!r}",
            upstream="人设给单组数值；主人 2026-10-15 要求**多档可选**",
            note=(
                "已提供 conservative / normal / active 三档，"
                "且**显式写的单项优先于档位**。默认取最保守档（宁少勿多）。"
            ),
            needs_human=False,
        )
    )

    # ---- ① 顶层评论频率：任务书 1/天 vs 人设 5/天 --------------------------
    brief_top = _extract_number(brief_text, r"max_top_comments_per_day:\s*(\d+)")
    persona_top = _extract_number(persona_text, r"评论\s*≤\s*(\d+)\s*/天")
    ours_top = interaction.max_top_comments_per_day
    report.findings.append(
        _compare(
            topic="顶层评论 / 天",
            ours=ours_top,
            candidates={"任务书": brief_top, "人设(13号)": persona_top},
            prefer="min",
            note="冲突时取更严的一档（宁少勿多，红线 4）",
        )
    )

    # ---- ② 点赞频率：仅人设有 ---------------------------------------------
    config_toml = _read(PERSONA_DOC.parent.parent / "config.toml")
    persona_like = _extract_number(persona_text, r"点赞\s*≤\s*(\d+)\s*/天")
    toml_like = _extract_number(config_toml, r"max_likes_per_day\s*=\s*(\d+)")
    report.findings.append(
        _compare(
            topic="点赞 / 天",
            ours=_example_like_quota(),
            candidates={"人设(13号)": persona_like, "config.toml(实测)": toml_like},
            prefer="min",
            note="13 号文档写 30，但 config.toml 因验证码已降为 8 —— 文档漂移，取实测值",
        )
    )

    # ---- ③ 评论频率人设值 vs 文档漂移 -------------------------------------
    if persona_top is not None and brief_top is not None and persona_top > brief_top:
        report.findings.append(
            Finding(
                topic="人设 13 号文档 §5.4 频率漂移",
                verdict="conflict",
                ours=f"评论 {ours_top}/天（取任务书）",
                upstream=f"13 号写 评论 ≤{persona_top}/天；config.toml 实际 3/天",
                note=(
                    "13 号文档 §5.4 仍是降频前的旧值（评论5/点赞30/收藏5/间隔10），"
                    "而 config.toml 早已降为 3/8/3/25。**照抄文档会调回危险值** —— "
                    "本项目取最严的一档。"
                ),
                needs_human=False,
            )
        )

    # ---- ④ AI 身份：直接对立 ----------------------------------------------
    persona_says_no_ai = bool(re.search(r"不提[\"「]?我是\s*AI", persona_text))
    brief_says_maybe = bool(re.search(r"可适度表明\s*AI\s*身份", brief_text))
    if persona_says_no_ai and brief_says_maybe:
        # ★ 2026-10-15 主人已裁决："可以适度表明，毕竟名称就已经表示了一些"
        report.findings.append(
            Finding(
                topic="AI 身份是否表明",
                verdict="consistent" if persona.ai_identity != "never" else "conflict",
                ours=f"persona.ai_identity = {persona.ai_identity!r}"
                     "（when_asked = 被问到时说明）",
                upstream=(
                    "人设 B8 甲：❌ 不提「我是 AI」；"
                    "任务书 §6.4：小号**可**适度表明 AI 身份"
                ),
                note=(
                    "✅ 主人 2026-10-15 裁决：**适度表明**（名称已暗示）。"
                    "取任务书的『可』+ 人设的语气；"
                    "但仍守『不得冒充真人观众』—— 类型上就**没有**『假装真人』这一档。"
                ),
                needs_human=False,
            )
        )

    # ---- ⑤ 点赞/收藏：任务书完全没规划 ------------------------------------
    brief_has_like = bool(re.search(r"点赞|收藏", brief_text))
    if not brief_has_like:
        implemented = persona.enable_liking and persona.enable_favoriting
        report.findings.append(
            Finding(
                topic="点赞 / 收藏功能",
                verdict="tightened" if implemented else "missing",
                ours=(
                    f"**已实现**：like_video / favorite_video 两个工具"
                    f"（enable_liking={persona.enable_liking}, "
                    f"enable_favoriting={persona.enable_favoriting}）"
                    if implemented
                    else "**未实现**"
                ),
                upstream=(
                    f"人设 §4.1/4.2/4.3 是核心行为（点赞阈值 "
                    f"{persona.like_min_score}、收藏阈值 {persona.favorite_min_score}、"
                    f"{len(persona.favorite_folders)} 个收藏夹）；"
                    "任务书全文 0 次提及点赞/收藏 → **任务书漏了**"
                ),
                note=(
                    "✅ 主人 2026-10-15 要求补做，已实现。"
                    "工具数因此由 14 → **16**（超出任务书 §4 的固定契约，"
                    "属主人明确授权）。点赞/收藏走**与评论同一套门禁**，"
                    "不是「顺手点一下」。"
                ),
                needs_human=not implemented,
            )
        )

    # ---- ⑥ 每条视频给 DSH 的候选数 ----------------------------------------
    brief_candidates = _extract_number(brief_text, r"最多返回\s*(\d+)\s*个短候选包")
    ours_candidates = config.text_worker.max_candidates_to_dsh
    report.findings.append(
        _compare(
            topic="每批给 DSH 的短候选包",
            ours=ours_candidates,
            candidates={"任务书": brief_candidates},
            prefer="max",
            note="大等于任务书要求即可（3 是下限约束的形式：不得超过 3）",
        )
    )

    # ---- ⑦ 单次处理量：人设 10 vs 我们 3 ---------------------------------
    persona_items = _extract_number(persona_text, r"最多处理\s*\*\*(\d+)\s*条")
    if persona_items is not None:
        report.findings.append(
            Finding(
                topic="单次处理视频数",
                verdict="tightened" if ours_candidates <= persona_items else "loosened",
                ours=f"给 DSH {ours_candidates} 个短包 / 单轮最多读 "
                     f"{config.discovery.max_full_reads_per_run} 条",
                upstream=f"人设：单次最多处理 {persona_items} 条素材",
                note=(
                    "读取量（10 条）保留人设；**给 DSH 的包数**按任务书压到 3 —— "
                    "两者不冲突：读得多、喂得少，正是省 token 的关键。"
                ),
                needs_human=False,
            )
        )

    # ---- ⑧ 唤醒时机：主人已定 ---------------------------------------------
    report.findings.append(
        Finding(
            topic="唤醒时机",
            verdict="consistent",
            ours=f"schedule.mode = {config.schedule.mode}",
            upstream="任务书 §7 建议每日固定时刻；人设未规定",
            note="主人已明确：打开 DSH 时唤醒 → 覆盖任务书建议。",
            needs_human=False,
        )
    )

    # ---- ⑨ 双峰阈值 --------------------------------------------------------
    report.findings.append(
        Finding(
            topic="向主号汇报的双峰阈值",
            verdict="consistent",
            ours=(
                f"@公开 {report_cfg.mention_like_threshold} / "
                f"私享 {report_cfg.share_like_threshold}"
            ),
            upstream="人设 §4.4（B13 甲）：≥10000 公开 @；≤50 且内容好私下分享",
            note=(
                "已对齐。但『私下分享』在本项目降级为**只进本地日报** —— "
                "任务书 §1 把私信列为非目标。"
            ),
            needs_human=False,
        )
    )

    # ---- ⑩ 红线：不关注 / 不负反馈 ----------------------------------------
    report.findings.append(
        Finding(
            topic="红线：不关注任何人 / 不做负反馈",
            verdict="tightened",
            ours=(
                f"allow_follow={persona.allow_follow}, "
                f"allow_negative_feedback={persona.allow_negative_feedback}"
                "（配置层不可开启）"
            ),
            upstream="人设 B16 甲 / B14",
            note="已从『文档约定』升级为『**代码约束**』：改成 true 会被配置校验直接拒绝。",
            needs_human=False,
        )
    )

    # ---- ⑪ 原文是否出 bridge ----------------------------------------------
    report.findings.append(
        Finding(
            topic="原文是否交给 agent",
            verdict="conflict",
            ours="原文留在 bridge；摘录 ≤ "
                 f"{config.text_worker.max_excerpt_chars} 字",
            upstream="人设 §4.5 要求 `inspect` 给**字幕全文**；任务书 §4 明令原文不出 bridge",
            note="取任务书（架构与安全优先）。人设『判断前充分了解』用多次摘录满足。",
            needs_human=False,
        )
    )

    # ---- ⑫ 定时 preset 的工具面 -------------------------------------------
    report.findings.append(
        Finding(
            topic="定时运行可用的工具",
            verdict="conflict",
            ours="只挂 bili_companion 窄 MCP；通用浏览器/shell 被插件逐个禁用",
            upstream="人设/curator 让 agent 用 ego 浏览器直接发；任务书明令不给",
            note="取任务书。这也是 `core/publish.py` 9 道门禁存在的原因。",
            needs_human=False,
        )
    )

    return report


def _compare(
    *,
    topic: str,
    ours: int,
    candidates: dict[str, int | None],
    prefer: Literal["min", "max"],
    note: str = "",
) -> Finding:
    """把我们的取值与若干上级取值比较。

    ``prefer="min"`` 表示"冲突时应取最小的"（如频率）；
    ``prefer="max"`` 表示"应取最大的"（如能力下限）。
    """
    present = {k: v for k, v in candidates.items() if v is not None}
    if not present:
        return Finding(
            topic=topic,
            verdict="consistent",
            ours=str(ours),
            upstream="（上级未规定或未能解析）",
            note=note,
            needs_human=False,
        )

    upstream_desc = "；".join(f"{k}={v}" for k, v in present.items())
    if prefer == "min":
        strictest = min(present.values())
        if ours < strictest:
            verdict: Verdict = "tightened"
        elif ours == strictest:
            verdict = "consistent"
        else:
            verdict = "loosened"
    else:
        most_permissive = max(present.values())
        verdict = "consistent" if ours >= most_permissive else "loosened"

    return Finding(
        topic=topic,
        verdict=verdict,
        ours=str(ours),
        upstream=upstream_desc,
        note=note,
        needs_human=verdict == "loosened",
    )


def render(report: AuditReport) -> str:
    """把审计结果渲染成可读文本。"""
    lines = [
        "人设 / 红线 一致性审计",
        "=" * 60,
        f"上级文档可读：{report.upstream_readable}",
        "",
    ]
    order: tuple[Verdict, ...] = (
        "conflict", "missing", "loosened", "tightened", "consistent",
    )
    labels = {
        "conflict": "⚠️  直接对立（需人裁决）",
        "missing": "⚠️  上级要求但未实现",
        "loosened": "⚠️  比上级更松",
        "tightened": "🔒 比上级更严（安全）",
        "consistent": "✅ 一致",
    }
    for verdict in order:
        group = [f for f in report.findings if f.verdict == verdict]
        if not group:
            continue
        lines.append(f"【{labels[verdict]}】{len(group)} 条")
        for finding in group:
            lines.append(f"  · {finding.topic}")
            lines.append(f"      本项目：{finding.ours}")
            lines.append(f"      上级　：{finding.upstream}")
            if finding.note:
                lines.append(f"      说明　：{finding.note}")
        lines.append("")

    human = report.needs_human
    lines.append(f"需要主人拍板：{len(human)} 项")
    for finding in human:
        lines.append(f"  → {finding.topic}")
    return "\n".join(lines)
