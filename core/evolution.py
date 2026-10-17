"""自我进化的**安全框架**（2026-10-17，照抄参考项目）。

## 主人要求

> 「当然这只是一个项目，我同意你开始，但是你进行实践时也要看看他出现过的
> 问题不要再犯」

所以这个模块的**每一道闸**都来自参考项目的实测教训，
不是我自己想的。

## ★ 照抄的四道硬闸

来源：`bilibili_learning_bot/services/evolution_settings.py`

| # | 闸 | 它的实现 | 为什么必须有 |
|---|---|---|---|
| 1 | **`auto_apply` 写死 False** | 代码里强制 `= False` | "AI 改自己"必须人点头 |
| 2 | **参数步长上限** | `max_parameter_step: 0.05` | 一次最多动 5%，防止"一步改飞" |
| 3 | **参数硬边界** | 每个参数带 `(min, max, default)` | 越界**直接拒绝** |
| 4 | **每日调用上限** | `daily_ai_limit: 2` | 防止自我进化变成"烧 token 机器" |

## ★ 它的三条 `hard_boundaries`（原文照抄，我们同样遵守）

```
不修改源码或执行生成代码
不改安全/审核/账号/API配置
不删除或重写原始知识文件
```

> 第 2 条最关键：**自我进化绝不能碰 `interaction.mode` / `dry_run` /
> `publish_enabled`** —— 那是"授权"，只能由人改。

## 设计取舍

参考项目用 JSON 配置 + 3 层（parameters / knowledge / strategy）。
我们**先只做 parameters 一层**，理由：

* `knowledge` / `strategy` 层要改知识文件与策略文本，**风险更高**；
* 我们还没有"策略说明书"这种东西，做了是空壳；
* 先把**最安全、最可测**的一层做扎实，比铺开三层更负责。

## 提案制（不是自动生效）

流程：**生成提案 → 人看 → 人批准 → 才写进 config**。
这与项目既有的「授权类改动只能由人做」是同一个原则。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

#: ★★ **写死**：自我进化**永不自动应用**。
#:
#: 参考项目在 `validate_settings` 里直接 `result['auto_apply'] = False` ——
#: 就算配置文件写了 `true` 也会被强制改回 False。
#: 我们照抄这条，并且**连配置项都不暴露**。
AUTO_APPLY_FORBIDDEN = False

#: 可进化的参数及其 **硬边界** 与 **默认值**。
#:
#: 格式：`名字: (最小值, 最大值, 默认值)`
#:
#: ★ 这些参数都是**打分/筛选**用的，**不涉及授权**。
#: 边界值是"人拍板"的，AI 只能在区间内微调。
EVOLVABLE_PARAMETERS: dict[str, tuple[float, float, float]] = {
    # 打分权重：topic 占比（越大越只看"跟我有关"）
    "score_topic_weight": (0.50, 0.95, 0.80),
    # 打分阈值：低于这个分不评论
    "min_score_for_comment": (50.0, 95.0, 75.0),
    # 只看最近几天
    "published_within_days": (1.0, 90.0, 30.0),
}

#: 单次提案允许的**最大变化量**（照抄 `max_parameter_step: 0.05`）。
#:
#: ★ 对"比率型"参数（0~1）是 0.05；对"天/分"型参数按**相对比例**限（见 :func:`clamp_step`）。
MAX_PARAMETER_STEP = 0.05

#: 每日最多生成几份提案（照抄 `daily_ai_limit: 2`）
DAILY_PROPOSAL_LIMIT = 2

#: ★ 硬边界（照抄参考项目的 `hard_boundaries`）。
#:
#: 自我进化**永远不许**碰这些：
FORBIDDEN_TARGETS: tuple[str, ...] = (
    "interaction.mode",
    "interaction.dry_run",
    "interaction.publish_enabled",
    "interaction.pause",
    "interaction.dm_reply_enabled",
    "account.agent_mid",
    "account.owner_mid",
    "text_worker.api_key_env",
)


class EvolutionError(RuntimeError):
    """提案不合法（越界 / 步长过大 / 碰了禁改项）。"""


@dataclass(frozen=True)
class Proposal:
    """一份**待审**的进化提案（**还没生效**）。"""

    parameter: str
    old_value: float
    new_value: float
    reason: str = ""
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "parameter": self.parameter,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "delta": round(self.new_value - self.old_value, 4),
            "reason": self.reason,
            "evidence": list(self.evidence),
            "status": "pending",
            "note": "**提案不会自动生效** —— 需要人用 CLI 批准",
        }


@dataclass
class ProposalLog:
    """提案历史（内存版；落库由上层负责）。"""

    items: list[Proposal] = field(default_factory=list)

    def add(self, proposal: Proposal) -> None:
        self.items.append(proposal)

    def count_for_day(self, day: str) -> int:
        """某天（``YYYY-MM-DD``）提了几份 —— 用于每日上限。"""
        return sum(1 for item in self.items if day in item.reason)


def _is_forbidden(parameter: str) -> bool:
    """参数名是否撞上禁改项（含前缀匹配，防 ``interaction.mode.x`` 绕过）。"""
    name = parameter.strip()
    for forbidden in FORBIDDEN_TARGETS:
        if name == forbidden or name.startswith(forbidden + "."):
            return True
    return False


def clamp_step(parameter: str, old: float, new: float) -> float:
    """把 ``new`` 限制在"离 ``old`` 不超过一步"的范围内。

    ★ 步长按**参数类型**分两种（这是参考项目没做、但我们需要的细化）：

    * **比率型**（边界落在 0~1 区间内，如 ``score_topic_weight`` 的 0.5~0.95）：
      绝对步长 :data:`MAX_PARAMETER_STEP`（0.05）—— 与参考项目一致；
    * **计数/天数型**（如 ``published_within_days`` 的 1~90）：用**相对比例**
      （``old × 5%``，至少 1）—— 否则 0.05 天对 30 天毫无意义。

    Returns:
        夹紧后的新值。
    """
    low, high, _ = EVOLVABLE_PARAMETERS[parameter]
    # ★ 判"比率型"看**边界是否落在 0~1 之内**，而不是看 high 是否恰好等于 1
    #   （第一版写成 `high == 1.0`，导致 0.5~0.95 这种区间被当成计数型，
    #    步长退化成 1.0 ⇒ 夹紧形同虚设。这是自查发现的真 bug。）
    is_ratio = low >= 0.0 and high <= 1.0
    max_delta = (
        MAX_PARAMETER_STEP if is_ratio else max(1.0, abs(old) * MAX_PARAMETER_STEP)
    )

    if new > old + max_delta:
        new = old + max_delta
    elif new < old - max_delta:
        new = old - max_delta

    # 再夹进硬边界
    new = max(low, min(high, new))
    return round(new, 4)


def make_proposal(
    *,
    parameter: str,
    old_value: float,
    new_value: float,
    reason: str = "",
    evidence: list[str] | None = None,
) -> Proposal:
    """校验并构造一份提案。**不合法的直接拒绝**（不是夹紧后接受）。

    ★ 为什么"拒绝"而不是"夹紧"：夹紧会让 AI 以为"我改成功了"，
    而实际改的是另一个值 —— 后续推理全错。**明确拒绝**更诚实。
    """
    if _is_forbidden(parameter):
        raise EvolutionError(
            f"「{parameter}」是**禁改项**（授权/账号/密钥），"
            "自我进化永远不许碰 —— 这只能由人在终端改。"
        )
    if parameter not in EVOLVABLE_PARAMETERS:
        allowed = "、".join(EVOLVABLE_PARAMETERS)
        raise EvolutionError(f"「{parameter}」不在可进化清单里（可选：{allowed}）")

    low, high, _ = EVOLVABLE_PARAMETERS[parameter]
    if not math.isfinite(new_value):
        raise EvolutionError("新值不是有限数字")
    if not (low <= new_value <= high):
        raise EvolutionError(
            f"新值 {new_value} 超出硬边界 [{low}, {high}] —— 边界由人定，AI 不能越"
        )

    clamped = clamp_step(parameter, old_value, new_value)
    return Proposal(
        parameter=parameter,
        old_value=round(float(old_value), 4),
        new_value=clamped,
        reason=reason,
        evidence=tuple(evidence or ()),
    )


def validate_proposal_count(*, today_count: int) -> tuple[bool, str]:
    """每日提案上限检查（照抄 `daily_ai_limit: 2`）。"""
    if today_count >= DAILY_PROPOSAL_LIMIT:
        return False, (
            f"今天已生成 {today_count} 份提案（上限 {DAILY_PROPOSAL_LIMIT}）——"
            "**这是设计，不是故障**。明天再来，或先审完已有的。"
        )
    return True, f"今天还可生成 {DAILY_PROPOSAL_LIMIT - today_count} 份提案"


def describe_framework() -> dict[str, Any]:
    """给 status / CLI 用的框架说明（让人一眼看清有哪些闸）。"""
    return {
        "auto_apply": AUTO_APPLY_FORBIDDEN,
        "auto_apply_note": "写死 False —— 提案永不自动生效，必须人批准",
        "max_parameter_step": MAX_PARAMETER_STEP,
        "daily_proposal_limit": DAILY_PROPOSAL_LIMIT,
        "evolvable": {
            name: {"min": low, "max": high, "default": default}
            for name, (low, high, default) in EVOLVABLE_PARAMETERS.items()
        },
        "forbidden": list(FORBIDDEN_TARGETS),
        "hard_boundaries": [
            "不修改源码或执行生成代码",
            "不改安全/审核/账号/API配置",
            "不删除或重写原始知识文件",
        ],
    }
