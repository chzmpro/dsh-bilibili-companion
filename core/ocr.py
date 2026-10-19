"""Optional local Tesseract OCR for public platform image attachments.

Image bytes go through stdin: no cloud vision calls or temporary image files.
Only recognized text becomes evidence; every unread image leaves a limitation.
"""

from __future__ import annotations

import asyncio
import shutil
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field


class OcrConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    command: str = Field(default="tesseract", min_length=1, max_length=4096)
    languages: str = Field(default="chi_sim+eng", pattern=r"^[A-Za-z0-9_+]+$", max_length=128)
    max_images: int = Field(default=6, ge=1, le=20)
    timeout_seconds: float = Field(default=20, gt=0, le=120)
    max_image_bytes: int = Field(default=10 * 1024 * 1024, ge=1, le=50 * 1024 * 1024)


_IMAGE_DOMAINS = {
    "xiaohongshu": ("xhscdn.com", "xiaohongshu.com"),
    "douyin": ("douyinpic.com", "byteimg.com", "ibytedtos.com"),
}


def _allowed_url(url: str, platform: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and any(
                host == domain or host.endswith("." + domain)
                for domain in _IMAGE_DOMAINS.get(platform, ())
            )
        )
    except ValueError:
        return False


class _ImageTooLarge(Exception):
    pass


async def _download(client: httpx.AsyncClient, url: str, config: OcrConfig) -> bytes:
    async with client.stream(
        "GET", url, follow_redirects=False, timeout=config.timeout_seconds
    ) as response:
        response.raise_for_status()
        length = response.headers.get("content-length")
        if length is not None and int(length) > config.max_image_bytes:
            raise _ImageTooLarge
        content = bytearray()
        async for chunk in response.aiter_bytes():
            if len(content) + len(chunk) > config.max_image_bytes:
                raise _ImageTooLarge
            content.extend(chunk)
        return bytes(content)


async def _reap(process: asyncio.subprocess.Process) -> None:
    """Stop and drain the child before returning control to a cancelled caller."""
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
    try:
        await asyncio.wait_for(process.communicate(), timeout=2)
    except TimeoutError:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await process.communicate()


async def _recognize(content: bytes, command: str, config: OcrConfig) -> tuple[str, str]:
    process = await asyncio.create_subprocess_exec(
        command, "stdin", "stdout", "-l", config.languages,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        shell=False,
    )
    try:
        stdout, _stderr = await process.communicate(input=content)
    except BaseException:
        # Shield cleanup from the cancellation that interrupted recognition.
        cleanup = asyncio.create_task(_reap(process))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
        raise
    if process.returncode != 0:
        return "", "OCR_RECOGNITION_FAILED"
    text = stdout.decode("utf-8", errors="replace").strip()
    return (text, "") if text else ("", "OCR_NO_TEXT_FOUND")


async def read_images(
    images: list[dict[str, Any]],
    *,
    platform: str,
    config: OcrConfig,
    client: httpx.AsyncClient | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not images:
        return [], []
    if not config.enabled:
        return [], ["OCR_DISABLED_IMAGES_UNREAD"]
    command = shutil.which(config.command)
    if command is None:
        return [], ["OCR_NO_DEPENDENCY_IMAGES_UNREAD"]
    if client is None:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as owned_client:
            return await read_images(images, platform=platform, config=config, client=owned_client)

    blocks: list[dict[str, Any]] = []
    limitations: list[str] = []
    for index, image in enumerate(images[:config.max_images], start=1):
        url = image.get("url")
        if not isinstance(url, str) or not _allowed_url(url, platform):
            limitations.append("OCR_IMAGE_URL_REJECTED")
            continue
        try:
            async with asyncio.timeout(config.timeout_seconds):
                content = await _download(client, url, config)
                text, limitation = await _recognize(content, command, config)
        except _ImageTooLarge:
            limitations.append("OCR_IMAGE_TOO_LARGE")
            continue
        except (TimeoutError, httpx.TimeoutException):
            limitations.append("OCR_TIMEOUT_IMAGES_UNREAD")
            continue
        except (httpx.HTTPError, ValueError):
            limitations.append("OCR_IMAGE_DOWNLOAD_FAILED")
            continue
        except FileNotFoundError:
            limitations.append("OCR_NO_DEPENDENCY_IMAGES_UNREAD")
            continue
        except OSError:
            limitations.append("OCR_RECOGNITION_FAILED")
            continue
        if limitation:
            limitations.append(limitation)
            continue
        page_index = image.get("page_index")
        if not isinstance(page_index, int) or isinstance(page_index, bool) or page_index < 1:
            page_index = index
        blocks.append({
            "source_type": "ocr", "text": text, "source_url": url, "page_index": page_index,
        })
    if len(images) > config.max_images:
        limitations.append("OCR_IMAGE_LIMIT_REACHED")
    return blocks, list(dict.fromkeys(limitations))
