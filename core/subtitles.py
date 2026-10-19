"""Bounded XHS CDN subtitle reads and SRT/WebVTT cue parsing."""

from __future__ import annotations

import asyncio
import html
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from core.platforms import PlatformError

MAX_SUBTITLE_BYTES = 1024 * 1024
_TIMESTAMP = r"(?:\d{2,}:)?[0-5]\d:[0-5]\d[.,]\d{3}"
_CUE = re.compile(rf"^({_TIMESTAMP})\s*-->\s*({_TIMESTAMP})(?:\s+.*)?$")


def is_xhs_subtitle_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https"
            and (host == "xhscdn.com" or host.endswith(".xhscdn.com"))
            and parsed.port in {None, 443}
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and not any(char in url for char in ("\\", "\r", "\n", "\t"))
        )
    except ValueError:
        return False


def _seconds(value: str) -> float:
    fields = value.replace(",", ".").split(":")
    seconds = float(fields[-1]) + int(fields[-2]) * 60
    return seconds + (int(fields[0]) * 3600 if len(fields) == 3 else 0)


def parse_subtitle_text(text: str, *, source_url: str) -> list[dict[str, Any]]:
    """Return only actual timed cues; metadata, URLs and empty cues are not evidence."""
    results: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", text.lstrip("\ufeff").replace("\r\n", "\n")):
        lines = block.strip().splitlines()
        if not lines or lines[0].startswith(("NOTE", "STYLE", "REGION")):
            continue
        for index, line in enumerate(lines):
            cue = _CUE.fullmatch(line.strip())
            if not cue:
                continue
            start, end = _seconds(cue[1]), _seconds(cue[2])
            body = "\n".join(lines[index + 1 :])
            body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
            body = html.unescape(re.sub(r"<[^>]*>", "", body)).strip()
            if body and end > start:
                results.append(
                    {
                        "source_type": "subtitle",
                        "text": body,
                        "start_sec": start,
                        "end_sec": end,
                        "source_url": source_url,
                    }
                )
            break
    return results


async def fetch_xhs_subtitles(
    client: httpx.AsyncClient, url: str, *, timeout_seconds: float
) -> list[dict[str, Any]]:
    if not is_xhs_subtitle_url(url):
        raise PlatformError("subtitle_url_not_allowed", "字幕地址不属于允许的 HTTPS 小红书 CDN。")
    try:
        async with asyncio.timeout(timeout_seconds):
            # Construct independently of the client's backend credentials/cookies.
            request = httpx.Request(
                "GET",
                url,
                headers={"Accept": "text/vtt,text/plain,*/*"},
                extensions={"timeout": httpx.Timeout(timeout_seconds).as_dict()},
            )
            response = await client.send(request, stream=True, follow_redirects=False, auth=None)
            try:
                if response.status_code == 429:
                    raise PlatformError("rate_limited", "字幕 CDN 返回限流信号。")
                if response.status_code == 401:
                    raise PlatformError("auth_expired", "字幕 CDN 要求重新授权。")
                if 300 <= response.status_code < 400:
                    raise PlatformError("subtitle_redirect_refused", "字幕下载不跟随重定向。")
                declared_size = response.headers.get("Content-Length", "")
                if declared_size.isdigit() and int(declared_size) > MAX_SUBTITLE_BYTES:
                    raise PlatformError("subtitle_too_large", "字幕文本超过 1 MiB 上限。")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    body.extend(chunk)
                    if len(body) > MAX_SUBTITLE_BYTES:
                        raise PlatformError("subtitle_too_large", "字幕文本超过 1 MiB 上限。")
                try:
                    text = body.decode("utf-8-sig")
                except UnicodeDecodeError:
                    raise PlatformError(
                        "subtitle_empty_or_invalid", "字幕不是有效 UTF-8 文本。"
                    ) from None
                cues = parse_subtitle_text(text, source_url=url)
                if not cues and any(
                    signal in text.lower()
                    for signal in (
                        "captcha",
                        "verification required",
                        "请完成验证",
                        "验证码",
                        "安全验证",
                    )
                ):
                    raise PlatformError("verification_required", "字幕 CDN 要求人工验证。")
                if response.status_code != 200:
                    raise PlatformError(
                        f"subtitle_http_{response.status_code}", "字幕 CDN 未返回成功响应。"
                    )
                if "html" in response.headers.get("Content-Type", "").lower() or not cues:
                    raise PlatformError("subtitle_empty_or_invalid", "未读取到有效字幕时间轴。")
                return cues
            finally:
                await response.aclose()
    except (TimeoutError, httpx.TimeoutException):
        raise PlatformError("subtitle_timeout", "字幕文本读取超时。") from None
    except httpx.HTTPError:
        raise PlatformError("subtitle_download_failed", "字幕文本下载失败。") from None
