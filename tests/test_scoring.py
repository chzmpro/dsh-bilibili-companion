"""打分机制测试（2026-10-17）。

## 背景（主人反馈）

> 「这个打分机制我觉得有问题，为什么我感觉没什么用的能给这么高分」

查出来是**三条叠加**：

1. 提示词里 `relevance_score` **只有一句 "0-100"，没有任何标准**；
2. 系统提示第一句还写着"**不做价值判断**"；
3. `analyze()` **连 keywords 都没传给模型** —— 它压根不知道主人想看什么。

⇒ 模型只能按"内容充实度"打分，于是"评论区扎堆的大肥鱼"
（讲 B 站上一堆 AI 号发一样评论，与 DSH 插件无关）拿了 **100 分**。

## 这里钉死的不变量

* 总分由**代码按权重算**，不听模型自己编；
* **topic 权重最高**（默认 0.8），无关内容不可能靠"内容充实"刷高分；
* 关键词**真的出现在标题/正文**时，topic 有硬下限（不靠模型估）；
* 权重可由 config 调，且**只有 topic 一个旋钮**（防过拟合）；
* 模型给的 `scores` 形状不可信（缺维度/字符串/None）也要能算。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.config import Config
from core.text_worker import (
    KEYWORD_HIT_CEILING,
    KEYWORD_HIT_FLOOR,
    SCORE_WEIGHTS,
    _apply_keyword_floor,
    _clamp_score,
    _keyword_topic_floor,
    _score_from_dimensions,
    weights_for,
)

KEYWORDS = ["DSH插件", "DeepSeek Harness", "DSH 插件"]


# ---------------------------------------------------------------------------
# ★★ 核心：无关内容不能靠"内容充实"刷高分
# ---------------------------------------------------------------------------


def test_irrelevant_content_scores_low_even_if_rich() -> None:
    """★★ 这条就是主人反馈的那个 case。

    "评论区扎堆的大肥鱼"：证据很足（字幕完整）、做得也不差，
    但**跟 DSH 插件完全无关** ⇒ 总分必须低。
    """
    dimensions = {"topic": 0, "novelty": 20, "evidence": 90, "usefulness": 0}
    score = _score_from_dimensions(dimensions)
    assert score < 40, f"无关内容给了 {score} 分，太高了（旧逻辑给 100）"


def test_relevant_content_scores_high() -> None:
    """真正相关的（topic 高）必须能过阈值。"""
    dimensions = {"topic": 95, "novelty": 85, "evidence": 90, "usefulness": 90}
    score = _score_from_dimensions(dimensions)
    assert score >= 75, f"相关内容只有 {score} 分，太低了"


def test_topic_dominates_total() -> None:
    """★ topic 一个维度就能决定生死（权重 0.8）。"""
    high_topic = _score_from_dimensions(
        {"topic": 90, "novelty": 0, "evidence": 0, "usefulness": 0}
    )
    low_topic = _score_from_dimensions(
        {"topic": 0, "novelty": 100, "evidence": 100, "usefulness": 100}
    )
    assert high_topic > low_topic, "topic 高的应该总分更高（哪怕其他维度是 0）"
    assert low_topic < 30, "topic=0 时总分必须很低"


# ---------------------------------------------------------------------------
# 权重
# ---------------------------------------------------------------------------


def test_default_weights_sum_to_one() -> None:
    assert abs(sum(SCORE_WEIGHTS.values()) - 1.0) < 1e-9


def test_topic_weight_is_dominant() -> None:
    """★ topic 必须是最大权重（这是"只看跟我有关的"的保证）。"""
    assert SCORE_WEIGHTS["topic"] >= 0.7


def test_weights_for_scales_others() -> None:
    """调 topic 占比时，其余三维按相对比例分剩下的权重。"""
    weights = weights_for(0.9)
    assert abs(weights["topic"] - 0.9) < 1e-9
    assert abs(sum(weights.values()) - 1.0) < 1e-9
    # 相对比例保持不变：usefulness > novelty > evidence
    assert weights["usefulness"] > weights["novelty"] > weights["evidence"]


def test_weights_for_clamps_out_of_range() -> None:
    """越界值要收敛（不让 1.5 或 -0.3 破坏权重）。"""
    assert weights_for(1.5)["topic"] == 1.0
    assert weights_for(-0.5)["topic"] == 0.0


def test_weights_for_none_returns_default() -> None:
    assert weights_for(None) == SCORE_WEIGHTS


def test_config_exposes_topic_weight() -> None:
    """★ 设置页要能调 —— 所以 config 必须有这个字段且有合理默认。"""
    cfg = Config()
    assert 0.5 <= cfg.discovery.score_topic_weight <= 0.95
    assert cfg.discovery.score_topic_weight == pytest.approx(0.80)


def test_config_rejects_extreme_weight() -> None:
    """★ 太极端要拒绝（0 或 1 会让某一维度完全失效）。"""
    from pydantic import ValidationError

    from core.config import DiscoveryConfig

    with pytest.raises(ValidationError):
        DiscoveryConfig(keywords=["a"], score_topic_weight=0.1)
    with pytest.raises(ValidationError):
        DiscoveryConfig(keywords=["a"], score_topic_weight=1.0)


def test_higher_topic_weight_increases_separation() -> None:
    """★ 用真实样本的形状验证：topic 占比越高，相关/无关区分越大。"""
    relevant = {"topic": 95, "novelty": 60, "evidence": 80, "usefulness": 80}
    irrelevant = {"topic": 5, "novelty": 60, "evidence": 80, "usefulness": 80}

    def gap(topic_weight: float) -> int:
        w = weights_for(topic_weight)
        return _score_from_dimensions(relevant, weights=w) - _score_from_dimensions(
            irrelevant, weights=w
        )

    assert gap(0.85) > gap(0.60), "topic 占比高时区分度应该更大"


# ---------------------------------------------------------------------------
# ★ 关键词下限（代码算的硬事实，不靠模型估）
# ---------------------------------------------------------------------------


def test_keyword_in_title_gives_topic_floor() -> None:
    """标题里真出现关注关键词 → topic 至少 80。"""
    floor = _keyword_topic_floor("我目前用得最多的一款DeepSeek Harness插件", "", KEYWORDS)
    assert floor == KEYWORD_HIT_FLOOR


def test_keyword_in_body_gives_topic_floor() -> None:
    """正文里出现也算（标题可能不含关键词）。"""
    floor = _keyword_topic_floor("随便的标题", "这条讲的是 DeepSeek Harness 的用法", KEYWORDS)
    assert floor == KEYWORD_HIT_FLOOR


def test_multiple_keywords_raise_floor() -> None:
    """命中多个关键词更可信，下限更高（但不封顶 100）。"""
    floor = _keyword_topic_floor("DSH插件 与 DeepSeek Harness", "", KEYWORDS)
    assert floor > KEYWORD_HIT_FLOOR
    assert floor <= KEYWORD_HIT_CEILING


def test_no_keyword_means_zero_floor() -> None:
    """★ 完全没提到关键词 → 下限 0（**不影响**无关内容的低分）。"""
    floor = _keyword_topic_floor("评论区扎堆的大肥鱼", "B站上一堆AI号发一样的评论", KEYWORDS)
    assert floor == 0


def test_keyword_floor_ignores_short_keywords() -> None:
    """★ 太短的词（1 字）不参与命中 —— 否则会到处误命中。"""
    floor = _keyword_topic_floor("标题里有 A 这个字母", "", ["A"])
    assert floor == 0


def test_apply_keyword_floor_only_raises() -> None:
    """★ 下限只**抬**不**压** —— 模型给高分时不该被拉低。"""
    dims = {"topic": 95, "novelty": 50, "evidence": 50, "usefulness": 50}
    result = _apply_keyword_floor(dims, title="DSH插件", text="", focus={"keywords": KEYWORDS})
    assert result["topic"] == 95, "模型给了 95，不该被下限 80 拉低"


def test_apply_keyword_floor_raises_low_topic() -> None:
    """模型保守估低时，下限把它抬起来。"""
    dims = {"topic": 55, "novelty": 20, "evidence": 20, "usefulness": 20}
    result = _apply_keyword_floor(
        dims, title="DeepSeek Harness插件", text="", focus={"keywords": KEYWORDS}
    )
    assert result["topic"] == KEYWORD_HIT_FLOOR


def test_apply_keyword_floor_without_focus() -> None:
    """没给 focus 就原样返回（不崩）。"""
    dims = {"topic": 10, "novelty": 10, "evidence": 10, "usefulness": 10}
    assert _apply_keyword_floor(dims, title="DSH插件", text="", focus=None) == dims


# ---------------------------------------------------------------------------
# 形状容错（模型输出不可信）
# ---------------------------------------------------------------------------


def test_missing_dimension_counts_as_zero() -> None:
    """缺维度按 0 算（**宁可低估，不要高估**）。"""
    score = _score_from_dimensions({"topic": 100})
    assert score == 80, "只有 topic=100，其余缺 → 应该正好是 0.8*100"


def test_string_numbers_are_parsed() -> None:
    """模型可能把数字写成字符串。"""
    score = _score_from_dimensions(
        {"topic": "90", "novelty": "50", "evidence": "50", "usefulness": "50"}
    )
    assert score > 0


def test_out_of_range_values_are_clamped() -> None:
    assert _clamp_score(150) == 100
    assert _clamp_score(-20) == 0
    assert _clamp_score("abc") is None
    assert _clamp_score(None) is None


@pytest.mark.parametrize("bad", [None, "字符串", 123, [], {}])
def test_falls_back_when_no_dimensions(bad: Any) -> None:
    """没有分项时退回旧字段（老缓存 / 模型没按新格式答）。"""
    score = _score_from_dimensions(bad, fallback=88)
    assert score == 88


def test_zero_when_nothing_available() -> None:
    assert _score_from_dimensions(None) == 0


def test_prompt_mentions_keywords_and_rubric() -> None:
    """★ 提示词必须**真的**包含打分标准与关注点（否则等于没改）。"""
    from core.text_worker import TextWorker

    worker = TextWorker.__new__(TextWorker)  # 不跑 __init__（不建 http 客户端）
    system_prompt = TextWorker._system_prompt(worker)
    assert "scores" in system_prompt, "系统提示要要求输出分项分数"

    user_prompt = TextWorker._user_prompt(
        worker,
        title="测试标题",
        evidence_blocks=[{"evidence_id": "e1", "source_type": "subtitle", "text": "内容"}],
        focus={"keywords": ["DSH插件"], "rules": ["优先讲清解决了什么问题"]},
    )
    assert "DSH插件" in user_prompt, "用户提示必须带上关注关键词"
    assert "解决了什么问题" in user_prompt, "用户提示必须带上主人的内容规则"
