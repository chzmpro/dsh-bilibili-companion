"""配置校验测试（任务书 §3「配置校验」逐条）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from core.config import (
    MAX_CANDIDATES_TO_DSH_CEILING,
    MAX_EXCERPT_CHARS_CEILING,
    Config,
    ConfigError,
    load_config,
)
from tests.conftest import make_config


@pytest.fixture(autouse=True)
def audit_upstream_documents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """审计规则使用可控输入，不读取作者机器上的另一个项目。"""
    from core import audit

    persona = tmp_path / "curator" / "docs" / "persona.md"
    persona.parent.mkdir(parents=True)
    persona.write_text('评论 ≤5/天\n点赞 ≤30/天\n不提「我是 AI」\n', encoding="utf-8")
    (persona.parent.parent / "config.toml").write_text(
        "max_likes_per_day = 8\n", encoding="utf-8"
    )
    brief = tmp_path / "brief.md"
    brief.write_text(
        "max_top_comments_per_day: 1\n可适度表明 AI 身份\n最多返回 3 个短候选包\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(audit, "PERSONA_DOC", persona)
    monkeypatch.setattr(audit, "BRIEF_DOC", brief)


def test_default_config_is_readonly_and_safe() -> None:
    """默认必须是 observe + dry_run + 关闭发布 + 无 UID。"""
    config = Config()
    assert config.interaction.mode == "observe"
    assert config.interaction.dry_run is True
    assert config.interaction.publish_enabled is False
    assert config.interaction.pause is False
    assert config.account.agent_mid == 0
    assert config.account.owner_mid == 0
    assert config.can_publish() == (False, "AGENT_MID_NOT_CONFIGURED")


def test_readonly_search_allows_zero_mids() -> None:
    """只读公开搜索允许 agent_mid=0、owner_mid=0。"""
    config = make_config(account={"agent_mid": 0, "owner_mid": 0})
    assert config.account.agent_mid == 0
    assert config.discovery.keywords


def test_keywords_are_stripped_and_deduped() -> None:
    config = make_config(discovery={"keywords": ["  DSH  ", "DSH", "MCP ", "", "  "]})
    assert config.discovery.keywords == ["DSH", "MCP"]


def test_keywords_require_at_least_one() -> None:
    with pytest.raises(ValidationError):
        make_config(discovery={"keywords": ["  ", ""]})


def test_identical_mids_rejected() -> None:
    with pytest.raises(ValidationError, match="不得相同"):
        make_config(account={"agent_mid": 1001, "owner_mid": 1001})


def test_publish_requires_distinct_positive_mids() -> None:
    config = make_config(
        account={"agent_mid": 1001, "owner_mid": 2002},
        interaction={"mode": "auto", "dry_run": False, "publish_enabled": True},
    )
    assert config.can_publish() == (True, "OK")


def test_each_publish_precondition_alone_blocks() -> None:
    """任一门禁不满足都必须拒绝，且给出稳定 reason code。"""
    cases = [
        ({"account": {"agent_mid": 0, "owner_mid": 2002}}, "AGENT_MID_NOT_CONFIGURED"),
        ({"account": {"agent_mid": 1001, "owner_mid": 0}}, "OWNER_MID_NOT_CONFIGURED"),
        ({"interaction": {"pause": True}}, "PAUSED"),
        ({"interaction": {"mode": "observe"}}, "MODE_OBSERVE"),
        ({"interaction": {"dry_run": True}}, "DRY_RUN"),
        ({"interaction": {"publish_enabled": False}}, "PUBLISH_DISABLED"),
    ]
    for override, expected in cases:
        merged = {
            "account": {"agent_mid": 1001, "owner_mid": 2002},
            "interaction": {"mode": "auto", "dry_run": False, "publish_enabled": True},
        }
        merged.update(override)
        if "interaction" in override and "account" not in override:
            merged["interaction"] = {
                "mode": "auto",
                "dry_run": False,
                "publish_enabled": True,
                **override["interaction"],
            }
        config = make_config(**merged)
        assert config.can_publish()[1] == expected, override


def test_hard_ceiling_candidates_to_dsh() -> None:
    """任务书 §4：prepare_batch 最多 3 个短候选包——配置层就挡住。"""
    make_config(text_worker={"max_candidates_to_dsh": MAX_CANDIDATES_TO_DSH_CEILING})
    with pytest.raises(ValidationError, match="不得超过 3"):
        make_config(text_worker={"max_candidates_to_dsh": 4})


def test_hard_ceiling_excerpt_chars() -> None:
    """任务书 §4：read_evidence_excerpt 单次摘录上限——配置层就挡住。"""
    make_config(text_worker={"max_excerpt_chars": MAX_EXCERPT_CHARS_CEILING})
    with pytest.raises(ValidationError, match="不得超过 500"):
        make_config(text_worker={"max_excerpt_chars": 501})


def test_bounded_ranges_reject_extremes() -> None:
    """请求间隔/缓存/单轮上限/重试次数都必须有有限范围。"""
    with pytest.raises(ValidationError):
        make_config(bilibili_requests={"min_interval_seconds": 0.1})
    with pytest.raises(ValidationError):
        make_config(bilibili_requests={"max_read_retries": 99})
    with pytest.raises(ValidationError):
        make_config(bilibili_requests={"search_cache_ttl_seconds": 10})
    with pytest.raises(ValidationError):
        make_config(discovery={"max_candidates_per_run": 1000})


def test_backoff_cap_must_not_be_below_base() -> None:
    with pytest.raises(ValidationError, match="backoff_cap_seconds"):
        make_config(
            bilibili_requests={"backoff_base_seconds": 30, "backoff_cap_seconds": 10}
        )


def test_unknown_field_rejected() -> None:
    """extra=forbid：拼错的字段必须报错，而不是被静默忽略。"""
    with pytest.raises(ValidationError):
        make_config(unknown_section={"x": 1})


def test_text_worker_status_codes() -> None:
    assert make_config(text_worker={"enabled": False}).text_worker_status() == (
        False,
        "TEXT_WORKER_DISABLED",
    )
    assert make_config(text_worker={"model": ""}).text_worker_status() == (
        False,
        "TEXT_WORKER_NOT_CONFIGURED",
    )
    assert make_config(text_worker={"model": "doubao-x"}).text_worker_status() == (True, "OK")


def test_load_config_missing_file_returns_defaults(tmp_path: Path) -> None:
    """没配置文件也要能只读启动。"""
    config = load_config(tmp_path / "nope.yaml")
    assert config.interaction.mode == "observe"
    assert config.account.agent_mid == 0


def test_load_config_bad_yaml_raises_configerror(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("account: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML"):
        load_config(bad)


def test_load_config_non_mapping_raises(tmp_path: Path) -> None:
    bad = tmp_path / "list.yaml"
    bad.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(bad)


def test_load_config_valid_file(tmp_path: Path) -> None:
    good = tmp_path / "good.yaml"
    good.write_text(
        "account:\n  agent_mid: 1001\n  owner_mid: 2002\n"
        "discovery:\n  keywords:\n    - DSH\n",
        encoding="utf-8",
    )
    config = load_config(good)
    assert config.account.agent_mid == 1001
    assert config.discovery.keywords == ["DSH"]


def test_example_config_matches_schema() -> None:
    """交付的 config.example.yaml 必须能被自己的 schema 接受（防文档漂移）。"""
    from core.paths import example_config_path

    config = load_config(example_config_path())
    assert config.interaction.mode == "observe"
    assert config.interaction.dry_run is True
    assert config.text_worker.enabled is True


# ---------------------------------------------------------------------------
# 人设与内容规则（来源：curator docs/13-agent-persona.md 21 题问卷定稿）
# ---------------------------------------------------------------------------


def test_persona_defaults_match_curator_decisions() -> None:
    """★ 人设数值必须与 curator 的 13 号文档一致（防漂移）。"""
    persona = Config().persona
    assert persona.display_name == "干饭不干事的小鲸鱼"
    assert "小肥鱼" in persona.nicknames
    assert persona.like_min_score == 8.0
    assert persona.like_min_not_hype == 8.0
    assert persona.like_max_per_author_per_day == 2
    assert persona.favorite_min_score == 8.0
    assert persona.favorite_folders == [
        "工具与技术", "方法论", "案例与实操", "观点与洞察", "待深挖",
    ]
    assert persona.allow_create_folders is True
    assert persona.allow_reply_others is True
    assert persona.max_items_per_run == 5


def test_persona_redlines_cannot_be_enabled() -> None:
    """★ 红线在**配置层**就不可打开：不关注任何人、不做负反馈。"""
    with pytest.raises(ValidationError, match="不关注任何人"):
        make_config(persona={"allow_follow": True})
    with pytest.raises(ValidationError, match="不做任何负反馈"):
        make_config(persona={"allow_negative_feedback": True})


def test_persona_needs_at_least_one_folder() -> None:
    with pytest.raises(ValidationError):
        make_config(persona={"favorite_folders": []})


def test_double_peak_thresholds() -> None:
    """★ 主人定的"双峰"：≥10000 公开 @；≤50 私下分享。"""
    report = Config().report
    assert report.mention_like_threshold == 10000
    assert report.share_like_threshold == 50
    assert report.share_like_threshold < report.mention_like_threshold


def test_double_peak_thresholds_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="share_like_threshold"):
        make_config(report={"mention_like_threshold": 100, "share_like_threshold": 500})


def test_push_times_validated() -> None:
    assert make_config(report={"push_times": ["10:00", "17:00"]}).report.push_times == [
        "10:00", "17:00",
    ]
    with pytest.raises(ValidationError, match="push_times"):
        make_config(report={"push_times": ["25:00"]})


def test_schedule_mode_defaults_to_host_start() -> None:
    """★ 主人定：打开 DSH 时唤醒。"""
    assert Config().schedule.mode == "on_host_start"
    assert make_config(schedule={"mode": "daily"}).schedule.mode == "daily"
    with pytest.raises(ValidationError):
        make_config(schedule={"mode": "hourly"})


def test_schedule_min_gap_bounded() -> None:
    with pytest.raises(ValidationError):
        make_config(schedule={"min_gap_minutes": 5000})


# ---------------------------------------------------------------------------
# 人设一致性审计（★ 主人要求"不能只转移而不审查"）
# ---------------------------------------------------------------------------


def test_audit_runs_and_reports_findings() -> None:
    """审计必须能跑，并把每条规则都判出结论。

    ★ 注意：主人 2026-10-15 已就"AI 身份"与"点赞/收藏"作出裁决，
    所以这两项**不再**是 conflict/missing。但**架构类冲突仍然存在且必须报出**
    （原文是否出 bridge、定时 preset 是否给浏览器）—— 那些是任务书与旧设计的
    对立，本项目一律取任务书，属"已裁决但仍是冲突"。
    """
    from core.audit import audit

    report = audit(Config())
    verdicts = {f.verdict for f in report.findings}
    assert "conflict" in verdicts, "架构类冲突（原文/浏览器）必须被报出"
    assert "tightened" in verdicts, "比上级更严的项要被标出来"
    assert len(report.findings) >= 10, "审计项太少，可能漏判"


def test_audit_reports_like_favorite_as_implemented() -> None:
    """★ 点赞/收藏：任务书漏了，但主人 2026-10-15 要求补做 → 现已实现。

    审计必须**如实反映当前状态**（不再报 missing），
    并说明"工具数 14 → 16"是主人明确授权的偏离。
    """
    from core.audit import audit

    report = audit(Config())
    # ⚠️ 注意用**精确**匹配："点赞 / 天"是另一条（配额），不要选错
    like = [f for f in report.findings if f.topic == "点赞 / 收藏功能"]
    assert like, "点赞/收藏必须被审计到"
    finding = like[0]
    assert finding.verdict == "tightened", f"实现后应为 tightened，实得 {finding.verdict}"
    assert "已实现" in finding.ours
    assert not finding.needs_human, "已实现就不该再要求人拍板"


def test_audit_ai_identity_resolved_by_owner() -> None:
    """★ AI 身份：主人 2026-10-15 裁决"适度表明" → 不再是 conflict。

    默认 ``when_asked`` 即"被问到时说明"。
    """
    from core.audit import audit

    report = audit(Config())
    ai = [f for f in report.findings if "AI 身份" in f.topic]
    assert ai, "AI 身份必须被审计到"
    assert ai[0].verdict == "consistent", f"裁决后应为 consistent，实得 {ai[0].verdict}"
    assert "when_asked" in ai[0].ours
    assert not ai[0].needs_human


def test_audit_has_no_open_questions_by_default() -> None:
    """★ 主人已就全部冲突做出裁决 → 默认配置下**不应再有待拍板项**。"""
    from core.audit import audit

    report = audit(Config())
    assert report.needs_human == [], (
        "还有待拍板项：" + ", ".join(f.topic for f in report.needs_human)
    )


def test_audit_frequency_tier_is_reported() -> None:
    """频率档位要出现在审计里（主人要求"多档可选"）。"""
    from core.audit import audit

    report = audit(Config())
    assert any("频率档位" in f.topic for f in report.findings)


def test_audit_detects_curator_frequency_drift() -> None:
    """★ 13 号文档的 §5.4 频率是**降频前的旧值** —— 照抄会调回危险值。"""
    from core.audit import audit

    report = audit(Config())
    assert any("漂移" in f.topic for f in report.findings), "文档漂移未被发现"


def test_audit_reports_tightened_items() -> None:
    """比上级更严的项（红线代码化、包数压缩）要被标 tightened。"""
    from core.audit import audit

    report = audit(Config())
    assert any(f.verdict == "tightened" for f in report.findings)


def test_audit_never_silently_drops_unreadable_upstream() -> None:
    """上级文档读不到时要**明说**，不能假装一切正常。"""
    from core.audit import audit

    report = audit(Config())
    assert set(report.upstream_readable) == {"persona_doc", "brief_doc"}
    assert all(
        isinstance(v, bool) for v in report.upstream_readable.values()
    )


def test_audit_is_json_serializable() -> None:
    import json

    from core.audit import audit

    json.dumps(audit(Config()).to_dict(), ensure_ascii=False)


def test_audit_render_marks_human_items() -> None:
    from core.audit import audit, render

    text = render(audit(Config()))
    assert "需要主人拍板" in text
