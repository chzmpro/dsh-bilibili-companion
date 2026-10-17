"""自我进化安全框架测试（2026-10-17）。

## 背景

主人：*"我同意你开始，但是你进行实践时也要看看他出现过的问题不要再犯"*。

所以这里的每一道闸都**照抄参考项目**的实测教训
（`bilibili_learning_bot/services/evolution_settings.py`）：

| # | 闸 | 参考项目原文 |
|---|---|---|
| 1 | `auto_apply` 写死 False | `result['auto_apply'] = False` |
| 2 | 参数步长上限 | `max_parameter_step: 0.05` |
| 3 | 参数硬边界 | `PARAMETERS = {...: (min, max, default)}` |
| 4 | 每日调用上限 | `daily_ai_limit: 2` |

外加它的 `hard_boundaries`：
「不修改源码或执行生成代码 / 不改安全/审核/账号/API配置 / 不删除或重写原始知识文件」。

## 钉死的不变量（按重要性排序）

* **`auto_apply` 永远是 False**（连配置项都不暴露）；
* **授权类字段永不进可进化清单**（`interaction.publish_enabled` 等）；
* **越界直接拒绝**，不是夹紧后接受（夹紧会让 AI 以为改成功了）；
* **步长上限**：一次最多动 5%；
* **每日上限**：最多 2 份提案。
"""

from __future__ import annotations

import pytest

from core.evolution import (
    AUTO_APPLY_FORBIDDEN,
    DAILY_PROPOSAL_LIMIT,
    EVOLVABLE_PARAMETERS,
    FORBIDDEN_TARGETS,
    MAX_PARAMETER_STEP,
    EvolutionError,
    Proposal,
    clamp_step,
    describe_framework,
    make_proposal,
    validate_proposal_count,
)

# ---------------------------------------------------------------------------
# ★★ 闸 1：auto_apply 永远 False
# ---------------------------------------------------------------------------


def test_auto_apply_is_hardcoded_false() -> None:
    """★★ 这是最重要的一道闸 —— 提案**永不**自动生效。

    参考项目在 `validate_settings` 里直接 `result['auto_apply'] = False`，
    就算配置文件写了 `true` 也会被强制改回。
    """
    assert AUTO_APPLY_FORBIDDEN is False
    assert describe_framework()["auto_apply"] is False


def test_framework_doc_mentions_no_auto_apply() -> None:
    note = describe_framework()["auto_apply_note"]
    assert "False" in note
    assert "人" in note, "要说明'必须人批准'"


# ---------------------------------------------------------------------------
# ★★ 闸 2：授权类字段永不进清单
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        "interaction.mode",
        "interaction.dry_run",
        "interaction.publish_enabled",
        "interaction.pause",
        "interaction.dm_reply_enabled",
        "account.agent_mid",
        "account.owner_mid",
        "text_worker.api_key_env",
    ],
)
def test_forbidden_targets_rejected(target: str) -> None:
    """★★ 自我进化**绝不许**碰授权/账号/密钥 —— 这只能由人改。"""
    with pytest.raises(EvolutionError) as excinfo:
        make_proposal(parameter=target, old_value=0, new_value=1)
    assert "禁改项" in str(excinfo.value)


def test_forbidden_prefix_cannot_be_bypassed() -> None:
    """★ 用子路径也不能绕过（`interaction.mode.x` 也要被拒）。"""
    with pytest.raises(EvolutionError):
        make_proposal(parameter="interaction.publish_enabled.anything", old_value=0, new_value=1)


def test_evolvable_params_never_overlap_forbidden() -> None:
    """★★ 可进化清单与禁改清单**不能有交集**（结构性保证）。"""
    for name in EVOLVABLE_PARAMETERS:
        assert name not in FORBIDDEN_TARGETS, f"{name} 同时出现在两个清单里"
        for forbidden in FORBIDDEN_TARGETS:
            assert not name.startswith(forbidden + ".")


# ---------------------------------------------------------------------------
# ★★ 闸 3：硬边界（越界**拒绝**，不是夹紧）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("param", "bad"),
    [
        ("score_topic_weight", 1.5),
        ("score_topic_weight", 0.1),
        ("min_score_for_comment", 200),
        ("min_score_for_comment", 10),
        ("published_within_days", 999),
        ("published_within_days", 0),
    ],
)
def test_out_of_bounds_rejected(param: str, bad: float) -> None:
    """★★ 越界要**明确拒绝** —— 夹紧会让 AI 以为"我改成功了"，后续推理全错。"""
    with pytest.raises(EvolutionError) as excinfo:
        make_proposal(parameter=param, old_value=EVOLVABLE_PARAMETERS[param][2], new_value=bad)
    assert "硬边界" in str(excinfo.value)


def test_unknown_parameter_rejected() -> None:
    with pytest.raises(EvolutionError) as excinfo:
        make_proposal(parameter="some.random.thing", old_value=0, new_value=1)
    assert "不在可进化清单" in str(excinfo.value)


def test_nan_rejected() -> None:
    """非有限数字要拒绝（NaN 会让所有比较失效）。"""
    with pytest.raises(EvolutionError):
        make_proposal(parameter="score_topic_weight", old_value=0.8, new_value=float("nan"))


# ---------------------------------------------------------------------------
# ★★ 闸 4：步长上限
# ---------------------------------------------------------------------------


def test_ratio_step_is_capped() -> None:
    """比率型参数：一次最多动 0.05（照抄 `max_parameter_step`）。"""
    clamped = clamp_step("score_topic_weight", 0.80, 0.95)
    assert clamped == pytest.approx(0.80 + MAX_PARAMETER_STEP)


def test_ratio_step_capped_downward() -> None:
    clamped = clamp_step("score_topic_weight", 0.80, 0.60)
    assert clamped == pytest.approx(0.80 - MAX_PARAMETER_STEP)


def test_small_step_passes_through() -> None:
    """小步（在限内）要原样通过，不该被改动。"""
    assert clamp_step("score_topic_weight", 0.80, 0.82) == pytest.approx(0.82)


def test_count_step_uses_relative_ratio() -> None:
    """★ 天数型用**相对比例** —— 0.05 天对 30 天毫无意义。"""
    clamped = clamp_step("published_within_days", 30, 90)
    assert clamped == pytest.approx(31.5, abs=0.01), "30 天 × 5% = 1.5 天"


def test_clamp_never_exceeds_hard_bounds() -> None:
    """夹紧后仍要在硬边界内。"""
    low, high, _ = EVOLVABLE_PARAMETERS["score_topic_weight"]
    for old in (0.51, 0.8, 0.94):
        for new in (0.0, 0.5, 0.95, 1.0):
            clamped = clamp_step("score_topic_weight", old, new)
            assert low <= clamped <= high


def test_proposal_uses_clamped_value() -> None:
    """`make_proposal` 出来的提案，新值必须是**夹紧后**的。"""
    proposal = make_proposal(
        parameter="score_topic_weight", old_value=0.80, new_value=0.95, reason="想大改"
    )
    assert proposal.new_value == pytest.approx(0.85)
    assert proposal.new_value != 0.95


# ---------------------------------------------------------------------------
# ★★ 闸 5：每日上限
# ---------------------------------------------------------------------------


def test_daily_limit_allows_first() -> None:
    ok, message = validate_proposal_count(today_count=0)
    assert ok is True
    assert str(DAILY_PROPOSAL_LIMIT) in message


def test_daily_limit_blocks_when_reached() -> None:
    """★ 达到上限要**明确拒绝**，并说明"这是设计不是故障"。"""
    ok, message = validate_proposal_count(today_count=DAILY_PROPOSAL_LIMIT)
    assert ok is False
    assert "设计" in message or "上限" in message


def test_daily_limit_is_small() -> None:
    """上限必须**保守**（参考项目是 2）—— 防止变成烧 token 机器。"""
    assert 1 <= DAILY_PROPOSAL_LIMIT <= 5


# ---------------------------------------------------------------------------
# 提案结构
# ---------------------------------------------------------------------------


def test_proposal_marks_pending() -> None:
    """提案的 status 必须是 `pending`（**不是** applied）。"""
    proposal = make_proposal(
        parameter="score_topic_weight", old_value=0.80, new_value=0.82, reason="测试"
    )
    payload = proposal.to_dict()
    assert payload["status"] == "pending"
    assert "不会自动生效" in payload["note"]


def test_proposal_carries_evidence() -> None:
    """提案要能带证据（参考项目的 `evidence_ids`）。"""
    proposal = make_proposal(
        parameter="score_topic_weight",
        old_value=0.80,
        new_value=0.82,
        evidence=["ev_1", "ev_2"],
    )
    assert proposal.evidence == ("ev_1", "ev_2")
    assert proposal.to_dict()["evidence"] == ["ev_1", "ev_2"]


def test_proposal_is_frozen() -> None:
    """提案是不可变的（生成后不能被偷偷改）。

    ★ frozen dataclass 抛的是 `FrozenInstanceError`（`AttributeError` 子类）。
    **不能**用 `pytest.raises(Exception)` 那种盲断言 —— 那样写
    任何异常都能让测试通过，等于没测。
    """
    import dataclasses

    proposal = make_proposal(parameter="score_topic_weight", old_value=0.80, new_value=0.82)
    with pytest.raises(dataclasses.FrozenInstanceError):
        proposal.new_value = 0.99  # type: ignore[misc]


def test_delta_is_reported() -> None:
    proposal = make_proposal(
        parameter="score_topic_weight", old_value=0.80, new_value=0.82
    )
    assert proposal.to_dict()["delta"] == pytest.approx(0.02, abs=1e-4)


# ---------------------------------------------------------------------------
# 框架说明
# ---------------------------------------------------------------------------


def test_framework_lists_all_gates() -> None:
    info = describe_framework()
    for key in ("auto_apply", "max_parameter_step", "daily_proposal_limit",
                "evolvable", "forbidden", "hard_boundaries"):
        assert key in info, f"框架说明缺 {key}"


def test_hard_boundaries_copied_from_reference() -> None:
    """★ 三条硬边界照抄参考项目（含"不修改源码"）。"""
    boundaries = describe_framework()["hard_boundaries"]
    joined = " ".join(boundaries)
    assert "源码" in joined, "必须写明不修改/不执行源码"
    assert "账号" in joined or "API" in joined


def test_evolvable_defaults_are_within_config_range() -> None:
    """★ 可进化参数的默认值要落在 config 的**合法范围**内。

    ⚠️ 这里**不能**断言"等于 `Config()` 的默认值" ——
    主人会把 `published_within_days` 改成 30（实际配置），
    而 `Config()` 的**类默认值**是 7。两者本来就可以不同。

    真正要保证的是：提案用的 `old_value` 落在 config 允许的区间里，
    否则 `make_proposal` 会拿一个"config 都不接受的值"当基准。
    """
    from core.config import Config

    cfg = Config()
    assert EVOLVABLE_PARAMETERS["score_topic_weight"][2] == pytest.approx(
        cfg.discovery.score_topic_weight
    )
    assert EVOLVABLE_PARAMETERS["min_score_for_comment"][2] == pytest.approx(
        cfg.discovery.min_score_for_comment
    )
    # 天数：只要落在 pydantic 的 [1, 90] 里就合法（实际值由主人配）
    low, high, default = EVOLVABLE_PARAMETERS["published_within_days"]
    assert 1 <= default <= 90
    assert low >= 1 and high <= 90, "可进化边界不能超出 config 的合法范围"


def test_proposal_log_counts_by_day() -> None:
    """提案历史能按天计数（供每日上限用）。"""
    from core.evolution import ProposalLog

    log = ProposalLog()
    log.add(
        Proposal(
            parameter="score_topic_weight",
            old_value=0.8,
            new_value=0.82,
            reason="2026-10-17 第一份",
        )
    )
    assert log.count_for_day("2026-10-17") == 1
    assert log.count_for_day("2026-10-18") == 0
