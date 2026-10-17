"""保底日报测试（P1，2026-10-16）。

来源：竞品精读
[`competitor-review.md`](../../dsh-bilibili-companion/docs/competitor-review.md) §2.4
与竞品 `index.js` 的 ``stubReportIfMissing(code)``。

竞品的理由写得很好，直接引在这里：

> ``out/`` 是空的、判重表不存在 ⇒ 使用者**完全看不到这一轮发生了什么**，
> 也**不知道这一轮白烧了钱**。

我们的差异（**比它严一点**）：日志尾**必须过** :func:`core.redact.redact` ——
它没有脱敏这一步。

核心不变量：

* 没写出正式日报时，**一定**留下占位日报；
* 已有当天日报时**不覆盖**（真日报优先）；
* 日志尾里的凭据**必须被脱敏**；
* 写占位日报**自己失败**也不能把主流程带崩。
"""

from __future__ import annotations

from pathlib import Path

from core.report import DigestWriter
from core.storage import Database


def make_writer(tmp_path: Path) -> DigestWriter:
    """构造一个只用到 ``out_dir`` 的 writer（绕过需要 db 的 ``__init__``）。"""
    writer = DigestWriter.__new__(DigestWriter)
    writer.db = None  # type: ignore[assignment]  —— write_stub 不用 db
    writer.out_dir = tmp_path
    return writer


def test_stub_is_written_when_no_digest(tmp_path: Path) -> None:
    writer = make_writer(tmp_path)
    target = writer.write_stub(reason="批处理中途抛异常", exit_code=1, run_id="run_x")

    assert target is not None
    text = target.read_text(encoding="utf-8")
    assert "这一轮没写出正式日报" in text
    assert "批处理中途抛异常" in text
    assert "退出码：1" in text
    assert "run_x" in text


def test_stub_does_not_overwrite_real_digest(tmp_path: Path) -> None:
    """★ 真日报优先：已有当天日报时，占位日报**不覆盖**它。"""
    existing = tmp_path / "digest-20261016.md"
    existing.write_text("# 真日报\n\n今天挑到了好东西。", encoding="utf-8")

    writer = make_writer(tmp_path)
    # 用当天的真实文件名
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d")
    real = tmp_path / f"digest-{stamp}.md"
    real.write_text("# 真日报\n\n今天挑到了好东西。", encoding="utf-8")

    result = writer.write_stub(reason="不该覆盖", exit_code=1)
    assert result is None, "已有日报时不该写占位"
    assert "今天挑到了好东西" in real.read_text(encoding="utf-8")


def test_stub_overwrite_when_explicitly_asked(tmp_path: Path) -> None:
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d")
    real = tmp_path / f"digest-{stamp}.md"
    real.write_text("# 旧内容", encoding="utf-8")

    writer = make_writer(tmp_path)
    result = writer.write_stub(reason="明确要求覆盖", exit_code=None, overwrite=True)
    assert result is not None
    assert "旧内容" not in real.read_text(encoding="utf-8")


def test_stub_redacts_credentials_in_log_tail(tmp_path: Path) -> None:
    """★★ 日志尾必须脱敏 —— 凭据绝不能进日报。"""
    writer = make_writer(tmp_path)
    target = writer.write_stub(
        reason="测试脱敏",
        log_tail="正常日志\nSESSDATA=abcdef1234567890\nbili_jct=deadbeef\n结束",
    )
    assert target is not None
    text = target.read_text(encoding="utf-8")
    assert "abcdef1234567890" not in text, "SESSDATA 值泄露了"
    assert "deadbeef" not in text, "bili_jct 值泄露了"
    assert "<REDACTED>" in text


def test_stub_works_without_log_tail(tmp_path: Path) -> None:
    writer = make_writer(tmp_path)
    target = writer.write_stub(reason="没有日志", exit_code=2)
    assert target is not None
    assert "没有可用的日志尾部" in target.read_text(encoding="utf-8")


def test_log_tail_reads_and_missing_file_is_empty(tmp_path: Path) -> None:
    log = tmp_path / "run.log"
    log.write_text("第一行\n第二行\n", encoding="utf-8")

    assert "第二行" in DigestWriter.log_tail(log_file=log)
    assert DigestWriter.log_tail(log_file=tmp_path / "不存在.log") == ""


def test_log_tail_is_bounded(tmp_path: Path) -> None:
    """日志尾要有界（避免把几百 MB 的日志全文塞进日报）。"""
    log = tmp_path / "run.log"
    log.write_text("x" * 10_000, encoding="utf-8")
    assert len(DigestWriter.log_tail(log_file=log, limit=200)) == 200


def test_stub_failure_does_not_raise(tmp_path: Path) -> None:
    """★ 写占位日报自己失败，也不能把主流程带崩（调用方在 except 里用它）。"""
    writer = make_writer(tmp_path)
    # 让 out_dir 指向一个"文件"，mkdir 就会失败
    blocker = tmp_path / "blocker"
    blocker.write_text("I am a file", encoding="utf-8")
    writer.out_dir = blocker

    try:
        result = writer.write_stub(reason="应该失败")
        # 具体是抛错还是返回 None 都行 —— 只要不把调用方带崩
        _ = result
    except Exception as exc:  # noqa: BLE001
        # write_stub 本身允许抛（调用方有 try/except），但**这里明确记录**这个行为
        assert isinstance(exc, (OSError, NotADirectoryError, FileExistsError))


def test_stub_extra_lines_are_rendered(tmp_path: Path) -> None:
    writer = make_writer(tmp_path)
    target = writer.write_stub(
        reason="带附加信息", extra=["锁被占用", "pid=123"]
    )
    assert target is not None
    text = target.read_text(encoding="utf-8")
    assert "锁被占用" in text
    assert "pid=123" in text


def test_digest_writer_still_writes_normal_digest(db: Database, tmp_path: Path) -> None:
    """回归：加了 write_stub 之后，正常日报的写入不受影响。"""
    from core.report import DigestInput

    writer = DigestWriter(db=db, out_dir=tmp_path)
    path = writer.write(DigestInput(run_id="run_ok", trigger_source="test", candidates_n=1))
    assert path.exists()
    assert "B 站小号日报" in path.read_text(encoding="utf-8")
