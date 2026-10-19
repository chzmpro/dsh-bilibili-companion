"""Local OCR boundaries: controlled HTTP and real, disposable child processes."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from core import ocr

XHS_IMAGE = "https://sns-img-qc.xhscdn.com/image.jpg"
DOUYIN_IMAGE = "https://p3-pc-sign.douyinpic.com/image.jpg"


@pytest.fixture
def executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr.shutil, "which", lambda _command: sys.executable)


def spawn_python(
    monkeypatch: pytest.MonkeyPatch, script: str
) -> tuple[list[asyncio.subprocess.Process], asyncio.Event]:
    """Substitute only the external executable; process lifecycle remains real."""
    original = asyncio.create_subprocess_exec
    processes: list[asyncio.subprocess.Process] = []
    started = asyncio.Event()

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        assert args[1:3] == ("stdin", "stdout")
        assert kwargs.get("shell", False) is False
        process = await original(sys.executable, "-c", script, **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(ocr.asyncio, "create_subprocess_exec", spawn)
    return processes, started


async def test_disabled_ocr_does_not_download() -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Disabled OCR must never download images")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        result = await ocr.read_images(
            [{"url": XHS_IMAGE}], platform="xiaohongshu", config=ocr.OcrConfig(), client=client
        )
    assert result == ([], ["OCR_DISABLED_IMAGES_UNREAD"])


@pytest.mark.parametrize(
    "field,value",
    [("max_images", 0), ("max_images", 21), ("timeout_seconds", 0),
     ("timeout_seconds", 121), ("max_image_bytes", 0), ("max_image_bytes", 52428801)],
)
def test_config_rejects_unbounded_resource_settings(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        ocr.OcrConfig(**{field: value})


async def test_success_keeps_source_page_and_numbers_unindexed_images(
    executable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn_python(monkeypatch, "import sys; sys.stdin.buffer.read(); print('page text')")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"image bytes"))
    ) as client:
        blocks, limitations = await ocr.read_images(
            [{"url": XHS_IMAGE, "page_index": 7}, {"url": XHS_IMAGE + "?second"}],
            platform="xiaohongshu", config=ocr.OcrConfig(enabled=True), client=client,
        )
    assert limitations == []
    assert blocks == [
        {"source_type": "ocr", "text": "page text", "source_url": XHS_IMAGE, "page_index": 7},
        {"source_type": "ocr", "text": "page text",
         "source_url": XHS_IMAGE + "?second", "page_index": 2},
    ]


@pytest.mark.parametrize(
    "platform,url",
    [("xiaohongshu", "http://sns-img-qc.xhscdn.com/image.jpg"),
     ("xiaohongshu", "https://xhscdn.com.evil.example/image.jpg"),
     ("xiaohongshu", "https://127.0.0.1/image.jpg"),
     ("xiaohongshu", "https://localhost/image.jpg"),
     ("xiaohongshu", "https://user:password@sns-img-qc.xhscdn.com/image.jpg"),
     ("xiaohongshu", "https://sns-img-qc.xhscdn.com:8443/image.jpg"),
     ("douyin", XHS_IMAGE), ("unsupported", DOUYIN_IMAGE)],
)
async def test_disallowed_image_url_is_rejected_before_download(
    executable: None, platform: str, url: str
) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Untrusted image must not be requested")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        blocks, limitations = await ocr.read_images(
            [{"url": url}], platform=platform, config=ocr.OcrConfig(enabled=True), client=client
        )
    assert blocks == []
    assert "OCR_IMAGE_URL_REJECTED" in limitations


async def test_missing_dependency_returns_unread_without_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ocr.shutil, "which", lambda _command: None)
    blocks, limitations = await ocr.read_images(
        [{"url": XHS_IMAGE}], platform="xiaohongshu", config=ocr.OcrConfig(enabled=True)
    )
    assert blocks == []
    assert limitations == ["OCR_NO_DEPENDENCY_IMAGES_UNREAD"]


async def test_redirect_is_not_followed_even_with_redirecting_client(executable: None) -> None:
    def redirect(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == XHS_IMAGE
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(redirect), follow_redirects=True
    ) as client:
        blocks, limitations = await ocr.read_images(
            [{"url": XHS_IMAGE}], platform="xiaohongshu",
            config=ocr.OcrConfig(enabled=True), client=client,
        )
    assert blocks == []
    assert "OCR_IMAGE_DOWNLOAD_FAILED" in limitations


@pytest.mark.parametrize("declared", [True, False])
async def test_oversized_download_never_produces_evidence(executable: None, declared: bool) -> None:
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"x" * 12
            yield b"y" * 12
            yield b"z" * 8

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, stream=Chunks(),
                                 headers={"content-length": "32"} if declared else {})
    )) as client:
        blocks, limitations = await ocr.read_images(
            [{"url": DOUYIN_IMAGE}], platform="douyin",
            config=ocr.OcrConfig(enabled=True, max_image_bytes=16), client=client,
        )
    assert blocks == []
    assert "OCR_IMAGE_TOO_LARGE" in limitations


async def test_slow_download_is_cancelled_and_stream_is_closed(executable: None) -> None:
    class SlowImage(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"header"
            await asyncio.sleep(60)
            yield b"body"

        async def aclose(self) -> None:
            self.closed = True

    stream = SlowImage()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, stream=stream)
    )) as client:
        blocks, limitations = await asyncio.wait_for(ocr.read_images(
            [{"url": XHS_IMAGE}], platform="xiaohongshu", client=client,
            config=ocr.OcrConfig(enabled=True, timeout_seconds=0.05),
        ), timeout=2)
    assert blocks == []
    assert limitations == ["OCR_TIMEOUT_IMAGES_UNREAD"]
    assert stream.closed


@pytest.mark.parametrize("script,code", [
    ("import sys; sys.stdin.buffer.read(); sys.exit(2)", "OCR_RECOGNITION_FAILED"),
    ("import sys; sys.stdin.buffer.read(); print('  ')", "OCR_NO_TEXT_FOUND"),
])
async def test_failed_or_empty_recognition_never_claims_image_read(
    executable: None, monkeypatch: pytest.MonkeyPatch, script: str, code: str
) -> None:
    spawn_python(monkeypatch, script)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"image"))
    ) as client:
        blocks, limitations = await ocr.read_images(
            [{"url": XHS_IMAGE}], platform="xiaohongshu",
            config=ocr.OcrConfig(enabled=True), client=client,
        )
    assert blocks == []
    assert code in limitations


@pytest.mark.parametrize("mode", ["timeout", "cancel"])
async def test_interrupted_process_is_reaped_before_return(
    executable: None, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    processes, started = spawn_python(monkeypatch, "import time; time.sleep(60)")
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"image"))
        ) as client:
            task = asyncio.create_task(ocr.read_images(
                [{"url": XHS_IMAGE}], platform="xiaohongshu", client=client,
                config=ocr.OcrConfig(enabled=True, timeout_seconds=0.1 if mode == "timeout" else 5),
            ))
            await asyncio.wait_for(started.wait(), timeout=2)
            if mode == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                blocks, limitations = await asyncio.wait_for(task, timeout=3)
                assert blocks == []
                assert "OCR_TIMEOUT_IMAGES_UNREAD" in limitations
        assert processes[0].returncode is not None
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.communicate()


async def test_max_images_limits_downloads_and_reports_remaining_unread(
    executable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn_python(monkeypatch, "import sys; sys.stdin.buffer.read(); print('first')")

    def download(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == XHS_IMAGE
        return httpx.Response(200, content=b"image")

    async with httpx.AsyncClient(transport=httpx.MockTransport(download)) as client:
        blocks, limitations = await ocr.read_images(
            [{"url": XHS_IMAGE}, {"url": XHS_IMAGE + "?unread"}], platform="xiaohongshu",
            config=ocr.OcrConfig(enabled=True, max_images=1), client=client,
        )
    assert len(blocks) == 1
    assert "OCR_IMAGE_LIMIT_REACHED" in limitations
