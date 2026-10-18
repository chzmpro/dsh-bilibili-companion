"""ASR 兜底与 pipeline 的**集成**测试（P1，2026-10-16）。

单元测试在 ``tests/test_asr.py``（子代理写的 42 项）；
这里只验证**接线**：

* 默认**关闭** —— 没开开关时**绝不**调用 ASR；
* 开着且转写成功 → 证据等级升到 ``LEVEL_ASR``，并把文本登记成证据；
* ★ **转写返回空 = 没有语音** → 记 ``ASR_NO_SPEECH``，**仍降级**、**不重试**；
* 缺依赖 / 失败 / 异常 → 只加一条 limitation，**绝不让读取崩**。

**绝不真跑模型**：全部 monkeypatch ``core.asr.transcribe_async``。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.asr import AsrResult
from core.pipeline import LEVEL_ASR, LEVEL_DESCRIPTION_ONLY, Pipeline
from core.storage import Database
from tests.conftest import make_config


class FakeReader:
    """只提供 ``read_video`` 需要的最小读取能力。"""

    def __init__(self, *, has_subtitle: bool = False) -> None:
        self.has_subtitle = has_subtitle

    async def get_video_detail(self, bvid: str) -> Any:
        from core.bilibili import VideoDetail

        return VideoDetail(
            bvid=bvid,
            aid=111,
            cid=222,
            title="测试视频",
            description="这是简介",
            up_mid=2002,
            up_name="某UP",
            published_at=1_700_000_000,
            duration_sec=120,
            reply_count=0,
            source_url=f"https://www.bilibili.com/video/{bvid}/",
        )

    async def get_subtitles(self, bvid: str, cid: int) -> list[Any]:
        return []

    async def fetch_subtitle_body(self, url: str) -> Any:
        raise AssertionError("没字幕时不该调这个")


def make_pipeline(db: Database, *, asr_fallback: bool) -> Pipeline:
    config = make_config(
        text_worker={"asr_fallback": asr_fallback, "model": "test-model"},
        account={"agent_mid": 1001, "owner_mid": 2002},
    )
    reader = FakeReader()
    return Pipeline(db=db, config=config, reader=reader, queue=None, worker=None)  # type: ignore[arg-type]


def _install_fake_asr(monkeypatch, result: AsrResult | Exception) -> list[str]:
    """把 ``core.asr.transcribe_async`` 换成假的；返回调用记录。"""
    calls: list[str] = []

    async def fake(target: str, *, model: str = "small", lang: str = "zh") -> AsrResult:
        calls.append(target)
        if isinstance(result, Exception):
            raise result
        return result

    import core.asr

    monkeypatch.setattr(core.asr, "transcribe_async", fake)
    return calls


async def test_asr_not_called_when_disabled(db: Database, monkeypatch) -> None:
    """★★ 默认关闭：没开开关时**绝不**调用 ASR（不能偷偷下 1GB 依赖）。"""
    calls = _install_fake_asr(monkeypatch, AsrResult(ok=True, text="不该被用到", code="OK"))
    pipeline = make_pipeline(db, asr_fallback=False)

    card, _blocks = await pipeline.read_video("BV1test00001")

    assert calls == [], "关着开关却调用了 ASR"
    assert card.evidence_level == LEVEL_DESCRIPTION_ONLY


async def test_asr_success_upgrades_evidence_level(db: Database, monkeypatch) -> None:
    """转写成功 → 证据等级升到 LEVEL_ASR，并把文本登记为证据。"""
    calls = _install_fake_asr(
        monkeypatch, AsrResult(ok=True, text="这是本机转写出来的字幕文本", code="OK")
    )
    pipeline = make_pipeline(db, asr_fallback=True)

    card, blocks = await pipeline.read_video("BV1test00002")

    assert calls == ["BV1test00002"]
    assert card.evidence_level == LEVEL_ASR
    asr_blocks = [b for b in blocks if b["source_type"] == "asr"]
    assert len(asr_blocks) == 1
    assert "本机转写" in asr_blocks[0]["text"]
    assert "EVIDENCE_FROM_LOCAL_ASR" in card.limitations


async def test_asr_empty_means_no_speech_not_failure(db: Database, monkeypatch) -> None:
    """★★ **空文本 = 没有语音**，不是故障 —— 记 ASR_NO_SPEECH 并降级。

    这条来自竞品的实测教训：把"空"当故障会导致反复重试
    （有用户因此烧掉 27 次工具调用）。所以这里断言**只调用一次**。
    """
    calls = _install_fake_asr(monkeypatch, AsrResult(ok=True, text="", code="NO_SPEECH"))
    pipeline = make_pipeline(db, asr_fallback=True)

    card, _blocks = await pipeline.read_video("BV1test00003")

    assert calls == ["BV1test00003"], "空结果不该触发重试"
    assert card.evidence_level == LEVEL_DESCRIPTION_ONLY
    assert "ASR_NO_SPEECH" in card.limitations
    assert card.evidence_level != LEVEL_ASR


async def test_asr_missing_dependency_degrades_gracefully(db: Database, monkeypatch) -> None:
    """缺依赖 → 只记 limitation，读取照常完成。"""
    _install_fake_asr(
        monkeypatch,
        AsrResult(ok=False, text="", code="NO_DEPENDENCY", message="没装 faster-whisper"),
    )
    pipeline = make_pipeline(db, asr_fallback=True)

    card, _blocks = await pipeline.read_video("BV1test00004")

    assert card.evidence_level == LEVEL_DESCRIPTION_ONLY
    assert any("ASR_NO_DEPENDENCY" in item for item in card.limitations)


async def test_asr_exception_does_not_break_read(db: Database, monkeypatch) -> None:
    """★ ASR 抛异常也不能让整条读取崩（兜底能力失败是允许的）。"""
    _install_fake_asr(monkeypatch, RuntimeError("转写炸了"))
    pipeline = make_pipeline(db, asr_fallback=True)

    card, _blocks = await pipeline.read_video("BV1test00005")

    assert card.evidence_level == LEVEL_DESCRIPTION_ONLY
    assert any("ASR_ERROR_RuntimeError" in item for item in card.limitations)


async def test_asr_download_failed_is_recorded(db: Database, monkeypatch) -> None:
    _install_fake_asr(
        monkeypatch, AsrResult(ok=False, text="", code="DOWNLOAD_FAILED", message="下不动")
    )
    pipeline = make_pipeline(db, asr_fallback=True)

    card, _blocks = await pipeline.read_video("BV1test00006")
    assert any("ASR_DOWNLOAD_FAILED" in item for item in card.limitations)


@pytest.mark.parametrize("asr_fallback", [True, False])
async def test_subtitle_present_never_calls_asr(
    db: Database, monkeypatch, asr_fallback: bool
) -> None:
    """有字幕时**不管开关**都不该走 ASR（字幕优先）。"""
    calls = _install_fake_asr(monkeypatch, AsrResult(ok=True, text="不该用到", code="OK"))

    from core.bilibili import SubtitleBody, SubtitleMeta, SubtitleSegment

    config = make_config(text_worker={"asr_fallback": asr_fallback, "model": "m"})
    reader = FakeReader()

    async def get_subtitles(bvid: str, cid: int) -> list[Any]:
        return [SubtitleMeta(lan="zh-CN", lan_doc="中文", url="https://x/sub.json")]

    async def fetch_subtitle_body(url: str) -> Any:
        return SubtitleBody(
            language="zh-CN",
            label="中文",
            segments=[SubtitleSegment(start_sec=0.0, end_sec=2.0, text="真实字幕")],
        )

    reader.get_subtitles = get_subtitles  # type: ignore[method-assign]
    reader.fetch_subtitle_body = fetch_subtitle_body  # type: ignore[method-assign]

    pipeline = Pipeline(db=db, config=config, reader=reader, queue=None, worker=None)  # type: ignore[arg-type]
    card, _blocks = await pipeline.read_video("BV1test00007")

    assert calls == [], "有字幕时不该调 ASR"
    assert card.evidence_level == "subtitle"
