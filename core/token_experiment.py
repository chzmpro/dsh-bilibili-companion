"""token 对照实验（任务 8 必做项，任务书 §7 任务 8）。

## 要证明什么

任务书原文：*"做同一批公开样本（建议至少 10 个视频…再加至少 5 条互动）的
DSH-only 与'双模型短包'对照；分别记录 DSH 输入/输出 token、豆包输入/输出 token、
DSH 调用次数和人工核对的证据错误。**目标**是同等工作量下 DSH 总 token 降低至少 60%…
这是验收目标而非预先保证。若未达标…如实报告结果。"*

## 做法

两种架构跑**同一批确定性生成的样本**，用**同一套 token 估算器**计量：

* **DSH-only**：把字幕全文 / 评论全文直接放进 DSH 上下文（即"原文出 bridge"）。
* **双模型短包**：原文留在 bridge，DSH 只拿 ≤3 个短候选包，
  必要时按 ``evidence_id`` 取 ≤500 字摘录。

token 用本项目自己的估算器（中文字符 ≈ 0.6 token 的保守系数）——
**不冒充真实厂商 tokenizer**，这是估算；报告里会写明。
豆包侧 token 从 ``text_jobs`` 表的真实 ``usage`` 记账（有真实调用时），
无真实密钥时用同一估算器并明确标注为估算。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.storage import Database
from core.text_worker import CandidateCard, KeyPoint

#: 中文字符 → token 的保守估算系数。
#: 真实 BPE 对中文通常接近 1 字 ≈ 0.6~1 token；取 0.6 是**对双模型不利**的保守取法
#: （即：如果按这个系数都能达标，真实差距只会更大）。
CJK_TOKENS_PER_CHAR = 0.6

#: 固定提示词开销（系统提示 + 工具 schema）估算，单位 token。
DSH_SYSTEM_OVERHEAD_TOKENS = 2500
WORKER_PROMPT_OVERHEAD_TOKENS = 400


def estimate_tokens(text: str) -> int:
    """估算一段文本的 token 数（保守）。"""
    if not text:
        return 0
    return max(1, int(len(text) * CJK_TOKENS_PER_CHAR))


@dataclass
class Sample:
    """一个样本：一个视频的原始材料 + 期望的短包产出。"""

    bvid: str
    title: str
    subtitle_chars: int
    description_chars: int
    comment_count: int
    comment_chars_each: int
    readme_chars: int = 0

    def raw_text(self) -> str:
        """bridge 内部能看到的**全部原文**（DSH-only 模式下这些会进 DSH 上下文）。"""
        return (
            self.title
            + ("字" * self.subtitle_chars)
            + ("简" * self.description_chars)
            + ("评" * (self.comment_count * self.comment_chars_each))
            + ("读" * self.readme_chars)
        )


@dataclass
class Arm:
    """一种架构的计量结果。"""

    name: str
    dsh_input_tokens: int = 0
    dsh_output_tokens: int = 0
    dsh_calls: int = 0
    worker_input_tokens: int = 0
    worker_output_tokens: int = 0
    evidence_errors: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def dsh_total(self) -> int:
        return self.dsh_input_tokens + self.dsh_output_tokens


def build_samples() -> list[Sample]:
    """10 个确定性样本，覆盖无字幕/长字幕/带 README 等形态。"""
    return [
        Sample("BV1s00000001", "DSH 插件机制入门", 3200, 400, 20, 180),
        Sample("BV1s00000002", "MCP 协议实操", 5200, 600, 15, 220),
        Sample("BV1s00000003", "无字幕只有简介", 0, 800, 12, 150),
        Sample("BV1s00000004", "超长字幕技术分享", 12000, 300, 30, 200),
        Sample("BV1s00000005", "带 README 的开源项目讲解", 2400, 500, 18, 160, readme_chars=6000),
        Sample("BV1s00000006", "短字幕快评", 900, 200, 8, 120),
        Sample("BV1s00000007", "评论区讨论热烈", 2800, 350, 45, 240),
        Sample("BV1s00000008", "多 P 长课程第一讲", 7600, 700, 25, 190),
        Sample("BV1s00000009", "工具链配置踩坑", 4100, 450, 22, 170),
        Sample("BV1s00000010", "架构对比与选型", 6400, 900, 35, 210, readme_chars=3000),
    ]


# ---------------------------------------------------------------------------
# DSH-only 架构：原文全部进 DSH 上下文
# ---------------------------------------------------------------------------


def measure_dsh_only(samples: list[Sample], *, max_excerpt_chars: int = 500) -> Arm:
    """模拟"原文出 bridge"：每个视频的全文都进 DSH，且逐视频唤醒。"""
    arm = Arm(name="dsh_only", notes=["每个视频单独唤醒一次 DSH；字幕/评论全文进入上下文"])
    arm.dsh_input_tokens += DSH_SYSTEM_OVERHEAD_TOKENS

    for sample in samples:
        raw = sample.raw_text()
        # 输入：原文 + 一段要求写笔记/评论的指令
        arm.dsh_input_tokens += estimate_tokens(raw)
        arm.dsh_input_tokens += estimate_tokens(
            "请阅读以上材料，判断是否值得推荐，并写出笔记与评论草稿。"
        )
        # 输出：笔记 + 评论草稿（约 600 字）
        arm.dsh_output_tokens += estimate_tokens("字" * 600)
        arm.dsh_calls += 1

    _ = max_excerpt_chars
    return arm


# ---------------------------------------------------------------------------
# 双模型短包架构：原文留在 bridge
# ---------------------------------------------------------------------------


def make_short_card(sample: Sample) -> CandidateCard:
    """构造一个**符合任务书约束**的短候选包（≤3 要点，每点短引文）。"""
    points = [
        KeyPoint(
            claim=f"{sample.title} 的第 {i} 个要点（经证据反查）",
            evidence_id=f"ev_{sample.bvid[2:]}{i}",
            start_sec=float(i * 30),
            short_quote="短" * 20,
            verified=True,
        )
        for i in range(1, 4)
    ]
    return CandidateCard(
        bvid=sample.bvid,
        aid=abs(hash(sample.bvid)) % 10**8,
        title=sample.title,
        source_url=f"https://www.bilibili.com/video/{sample.bvid}/",
        evidence_level="subtitle" if sample.subtitle_chars else "description_only",
        key_points=points,
        relevance_score=80,
        uncertainties=["题材较新，缺少对照"],
        candidate_comment="草稿建议（供上层改写）",
        needs_deep_review=False,
        limitations=[],
    )


def measure_dual_model(
    samples: list[Sample],
    *,
    max_candidates_to_dsh: int = 3,
    max_excerpt_chars: int = 500,
) -> Arm:
    """双模型：豆包吃原文，DSH 只吃短包（每批最多 3 个）。"""
    arm = Arm(
        name="dual_model",
        notes=[
            f"原文留在 bridge；DSH 每批最多 {max_candidates_to_dsh} 个短包",
            f"按 evidence_id 取摘录上限 {max_excerpt_chars} 字",
        ],
    )
    arm.dsh_input_tokens += DSH_SYSTEM_OVERHEAD_TOKENS

    # 豆包侧：把原文分块喂进去（这是**便宜模型**的账，与 DSH 账分开）
    arm.worker_input_tokens += WORKER_PROMPT_OVERHEAD_TOKENS
    for sample in samples:
        arm.worker_input_tokens += estimate_tokens(sample.raw_text())
        arm.worker_output_tokens += estimate_tokens("字" * 400)  # 结构化要点 JSON

    # DSH 侧：按批拿短包。10 个样本、每批 3 个 → 4 批
    batches = [
        samples[i : i + max_candidates_to_dsh]
        for i in range(0, len(samples), max_candidates_to_dsh)
    ]
    for batch in batches:
        payload = {
            "candidates": [make_short_card(s).to_payload() for s in batch],
            "skipped": [],
            "failed": [],
        }
        arm.dsh_input_tokens += estimate_tokens(json.dumps(payload, ensure_ascii=False))
        # 需要核实某句时按证据取摘录：假设每批取 2 次
        arm.dsh_input_tokens += 2 * estimate_tokens("文" * max_excerpt_chars)
        arm.dsh_output_tokens += estimate_tokens("字" * 700)  # 一次写多条笔记+评论
        arm.dsh_calls += 1

    return arm


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


@dataclass
class ComparisonResult:
    samples: int
    dsh_only: Arm
    dual_model: Arm

    @property
    def reduction_pct(self) -> float:
        baseline = self.dsh_only.dsh_total
        if baseline <= 0:
            return 0.0
        return (baseline - self.dual_model.dsh_total) / baseline * 100.0

    @property
    def meets_target(self) -> bool:
        return self.reduction_pct >= 60.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "dsh_only": {
                "dsh_input_tokens": self.dsh_only.dsh_input_tokens,
                "dsh_output_tokens": self.dsh_only.dsh_output_tokens,
                "dsh_total": self.dsh_only.dsh_total,
                "dsh_calls": self.dsh_only.dsh_calls,
                "evidence_errors": self.dsh_only.evidence_errors,
                "notes": self.dsh_only.notes,
            },
            "dual_model": {
                "dsh_input_tokens": self.dual_model.dsh_input_tokens,
                "dsh_output_tokens": self.dual_model.dsh_output_tokens,
                "dsh_total": self.dual_model.dsh_total,
                "dsh_calls": self.dual_model.dsh_calls,
                "worker_input_tokens": self.dual_model.worker_input_tokens,
                "worker_output_tokens": self.dual_model.worker_output_tokens,
                "evidence_errors": self.dual_model.evidence_errors,
                "notes": self.dual_model.notes,
            },
            "dsh_reduction_pct": round(self.reduction_pct, 2),
            # ★ #6（Codex 审查）：这是**架构估算**，不是真实验收。
            #   真实 DSH usage / ≥5 条互动 / 人工证据核对都未采集，
            #   任何下游都不许把 meets_60pct_target 当成"已验收"。
            "meets_60pct_target": self.meets_target,
            "measurement": "estimated",
            "real_dsh_usage": False,
            "evidence_reviewed": False,
            "interactions_included": False,
            "verdict": "estimated_target_met" if self.meets_target else "estimated_below_target",
            "estimator": {
                "kind": "heuristic",
                "cjk_tokens_per_char": CJK_TOKENS_PER_CHAR,
                "dsh_system_overhead_tokens": DSH_SYSTEM_OVERHEAD_TOKENS,
                "disclaimer": (
                    "这是**估算**，不是厂商 tokenizer 的真实计量。"
                    "豆包侧若发生了真实调用，其 usage 以 text_jobs 表为准。"
                ),
            },
        }


def run_experiment(
    *,
    samples: list[Sample] | None = None,
    max_candidates_to_dsh: int = 3,
    max_excerpt_chars: int = 500,
) -> ComparisonResult:
    batch = samples if samples is not None else build_samples()
    return ComparisonResult(
        samples=len(batch),
        dsh_only=measure_dsh_only(batch, max_excerpt_chars=max_excerpt_chars),
        dual_model=measure_dual_model(
            batch,
            max_candidates_to_dsh=max_candidates_to_dsh,
            max_excerpt_chars=max_excerpt_chars,
        ),
    )


def real_worker_usage(db: Database) -> dict[str, int]:
    """从 ``text_jobs`` 读**真实**豆包 usage（有真实调用时用于替换估算）。"""
    return db.text_job_totals()


def render_report(result: ComparisonResult, *, real_usage: dict[str, int] | None = None) -> str:
    data = result.to_dict()
    lines = [
        "# token 对照实验报告",
        "",
        f"样本数：**{result.samples}**（确定性生成，覆盖无字幕/长字幕/带 README 等形态）",
        "",
        "## DSH 侧（同等工作量）",
        "",
        "| 指标 | DSH-only（原文出 bridge） | 双模型短包 | 变化 |",
        "|---|---|---|---|",
        f"| DSH 输入 token | {data['dsh_only']['dsh_input_tokens']} "
        f"| {data['dual_model']['dsh_input_tokens']} | "
        f"{_delta(data['dsh_only']['dsh_input_tokens'], data['dual_model']['dsh_input_tokens'])} |",
        f"| DSH 输出 token | {data['dsh_only']['dsh_output_tokens']} "
        f"| {data['dual_model']['dsh_output_tokens']} | "
        f"{_delta(data['dsh_only']['dsh_output_tokens'],
                  data['dual_model']['dsh_output_tokens'])} |",
        f"| **DSH 合计** | **{data['dsh_only']['dsh_total']}** "
        f"| **{data['dual_model']['dsh_total']}** | "
        f"**{_delta(data['dsh_only']['dsh_total'], data['dual_model']['dsh_total'])}** |",
        f"| DSH 调用次数 | {data['dsh_only']['dsh_calls']} "
        f"| {data['dual_model']['dsh_calls']} | "
        f"{_delta(data['dsh_only']['dsh_calls'], data['dual_model']['dsh_calls'])} |",
        "",
        "## 豆包侧（廉价模型，单独记账）",
        "",
        f"- 输入 token：{data['dual_model']['worker_input_tokens']}",
        f"- 输出 token：{data['dual_model']['worker_output_tokens']}",
        "",
        "## 结论（**估算，不是真实验收**）",
        "",
        f"- **DSH 总 token 估算降低：{data['dsh_reduction_pct']}%**",
        f"- 估算是否达到 ≥60% 目标：**{'是' if data['meets_60pct_target'] else '否'}**"
        "（`measurement=estimated`）",
        "- ❌ 真实 DSH usage / 调用次数：**未采集**",
        "- ❌ ≥5 条互动对照：**未纳入**",
        "- ❌ 人力证据核对：**未做**（`evidence_reviewed=false`）",
        "",
        "## 证据错误（TODO：人工核对）",
        "",
        "> 下面的 0 是**默认值，不是核对结果** —— 未人工逐条核对前不得当成零错误。",
        "",
        f"- DSH-only：{data['dsh_only']['evidence_errors']} 处（未核对）",
        f"- 双模型短包：{data['dual_model']['evidence_errors']} 处（未核对）",
        "",
        "## 计量口径与诚实声明",
        "",
        f"- 估算器：{data['estimator']['kind']}"
        f"（中文字符 ≈ {data['estimator']['cjk_tokens_per_char']} token）",
        f"- DSH 固定开销：{data['estimator']['dsh_system_overhead_tokens']} token/会话",
        f"- ⚠️ {data['estimator']['disclaimer']}",
        "- ⚠️ 这是**架构对照**，样本为确定性生成而非真实抓取；"
        "真实平台上的绝对数值会不同，但**量级差距来自架构本身**（原文是否进 DSH 上下文）。",
    ]
    if real_usage and real_usage.get("jobs"):
        lines += [
            "",
            "## 真实豆包调用记账（来自 text_jobs 表）",
            "",
            f"- 任务数：{real_usage.get('jobs', 0)}",
            f"- prompt_tokens：{real_usage.get('prompt_tokens', 0)}",
            f"- completion_tokens：{real_usage.get('completion_tokens', 0)}",
        ]
    return "\n".join(lines) + "\n"


def _delta(baseline: int, new: int) -> str:
    if baseline <= 0:
        return "—"
    pct = (baseline - new) / baseline * 100.0
    sign = "−" if pct >= 0 else "+"
    return f"{sign}{abs(pct):.1f}%"


def write_report(out_path: Path, *, result: ComparisonResult | None = None) -> Path:
    comparison = result if result is not None else run_experiment()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_report(comparison), encoding="utf-8")
    return out_path
