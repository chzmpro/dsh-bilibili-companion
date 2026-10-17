"""token 对照实验测试（任务 8）。

这些测试锁住**方法论**，不只是数字：

* 双模型架构必须在同等样本上显著更省 DSH token。
* 原文**不能**出现在 DSH 侧的输入里。
* 估算器与免责声明必须留在报告里（不能把估算说成真实计量）。
"""

from __future__ import annotations

from pathlib import Path

from core.storage import Database
from core.token_experiment import (
    CJK_TOKENS_PER_CHAR,
    DSH_SYSTEM_OVERHEAD_TOKENS,
    build_samples,
    estimate_tokens,
    measure_dsh_only,
    measure_dual_model,
    render_report,
    run_experiment,
    write_report,
)


def test_samples_cover_required_shapes() -> None:
    """样本必须覆盖 无字幕 / 长字幕 / 带 README 等形态（任务书要求）。"""
    samples = build_samples()
    assert len(samples) >= 10, "任务书要求至少 10 个视频样本"
    assert any(s.subtitle_chars == 0 for s in samples), "缺少无字幕样本"
    assert any(s.subtitle_chars > 10000 for s in samples), "缺少长字幕样本"
    assert any(s.readme_chars > 0 for s in samples), "缺少带 README 样本"
    assert any(s.comment_count >= 40 for s in samples), "缺少评论很多的样本"
    assert len({s.bvid for s in samples}) == len(samples), "BV 必须唯一"


def test_estimator_is_monotonic_and_zero_safe() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("字") >= 1
    assert estimate_tokens("字" * 100) > estimate_tokens("字" * 10)


def test_dual_model_reduces_dsh_tokens_substantially() -> None:
    """★ 核心断言：双模型架构在同等样本上显著降低 DSH token。"""
    result = run_experiment()
    assert result.dual_model.dsh_total < result.dsh_only.dsh_total
    assert result.reduction_pct >= 60.0, (
        f"未达任务书 60% 目标：实际 {result.reduction_pct:.1f}%。"
        "若架构正确仍未达标，应检查全文是否误入 DSH。"
    )
    assert result.meets_target is True


def test_dual_model_reduces_dsh_call_count() -> None:
    """任务书 §2.1.3：不要为每个视频单独唤醒一次 DSH。"""
    result = run_experiment()
    assert result.dual_model.dsh_calls < result.dsh_only.dsh_calls
    # 10 个样本 / 每批 3 个 → 4 批
    assert result.dual_model.dsh_calls == 4


def test_dsh_only_scales_with_raw_text() -> None:
    """DSH-only 的输入必须随原文线性增长（这正是"贵"的原因）。"""
    samples = build_samples()
    short = measure_dsh_only(samples[:2])
    long = measure_dsh_only(samples)
    assert long.dsh_input_tokens > short.dsh_input_tokens * 3


def test_dual_model_dsh_input_does_not_scale_with_raw_length() -> None:
    """★ 双模型的 DSH 输入**不应**随原文长度线性增长。"""
    samples = build_samples()
    # 把每个样本的原文放大 5 倍
    fat = [
        type(s)(
            bvid=s.bvid,
            title=s.title,
            subtitle_chars=s.subtitle_chars * 5,
            description_chars=s.description_chars,
            comment_count=s.comment_count,
            comment_chars_each=s.comment_chars_each,
            readme_chars=s.readme_chars,
        )
        for s in samples
    ]
    base = measure_dual_model(samples)
    expanded = measure_dual_model(fat)
    # 短包不含原文，因此 DSH 侧基本不变（允许小幅波动）
    assert expanded.dsh_input_tokens < base.dsh_input_tokens * 1.5, (
        "双模型的 DSH 输入随原文暴涨 —— 说明原文泄漏进了 DSH 上下文"
    )


def test_worker_side_pays_the_cost_instead() -> None:
    """原文的成本转移到**廉价模型**侧，而不是消失（诚实计量）。"""
    result = run_experiment()
    assert result.dual_model.worker_input_tokens > 0
    # 豆包吃下的输入量级应与原文相当
    assert result.dual_model.worker_input_tokens > result.dual_model.dsh_input_tokens


def test_report_states_estimator_and_disclaimer() -> None:
    """★ 报告必须写明"这是估算"，不能冒充真实 tokenizer。"""
    report = render_report(run_experiment())
    assert "估算" in report
    assert "不是厂商 tokenizer" in report
    assert str(CJK_TOKENS_PER_CHAR) in report
    assert str(DSH_SYSTEM_OVERHEAD_TOKENS) in report
    assert "60%" in report or "60" in report


def test_report_has_both_arms_and_delta() -> None:
    report = render_report(run_experiment())
    assert "DSH-only" in report
    assert "双模型" in report
    assert "降低" in report


def test_meets_target_flag_is_honest() -> None:
    """若某天架构退化导致不达标，标志位必须如实变 false（不能硬编码成功）。"""
    result = run_experiment()
    assert result.meets_target == (result.reduction_pct >= 60.0)


def test_write_report_creates_file(tmp_path: Path) -> None:
    target = write_report(tmp_path / "out" / "token-experiment.md")
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert "token 对照实验报告" in text


def test_real_worker_usage_reads_from_db(db: Database) -> None:
    from core.token_experiment import real_worker_usage

    db.record_text_job(
        bvid="BV1x",
        input_sha256="h",
        model_id="doubao-x",
        prompt_version="v1",
        status="ok",
        prompt_tokens=321,
        completion_tokens=123,
    )
    usage = real_worker_usage(db)
    assert usage["prompt_tokens"] == 321
    assert usage["completion_tokens"] == 123


def test_report_includes_real_usage_when_present(db: Database) -> None:
    from core.token_experiment import real_worker_usage

    db.record_text_job(
        bvid="BV1x",
        input_sha256="h",
        model_id="doubao-x",
        prompt_version="v1",
        status="ok",
        prompt_tokens=500,
        completion_tokens=100,
    )
    report = render_report(run_experiment(), real_usage=real_worker_usage(db))
    assert "真实豆包调用记账" in report
    assert "500" in report


def test_no_real_usage_section_when_absent(db: Database) -> None:
    report = render_report(run_experiment(), real_usage=db.text_job_totals())
    assert "真实豆包调用记账" not in report
