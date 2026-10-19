"""Read-only clients for separately deployed xiaohongshu-mcp and douyin-mcp-server.

Only their documented read tools are exposed. No backend is installed or started
on construction; stdio starts only the explicitly configured local command when
the user invokes a read. Each call owns and closes its MCP session and transport.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from core.external_config import ExternalPlatformConfig, ExternalPlatformsConfig
from core.platforms import (
    PlatformAdapter,
    PlatformCapabilities,
    PlatformError,
)
from core.subtitles import fetch_xhs_subtitles, is_xhs_subtitle_url


class ToolBackend(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...
    async def aclose(self) -> None: ...


SessionFactory = Callable[[ExternalPlatformConfig], AbstractAsyncContextManager[Any]]
BackendFactory = Callable[[str, ExternalPlatformConfig], ToolBackend]


def _backend_error(value: Any) -> PlatformError:
    # Upstream bodies/exceptions can contain credentials. Return stable, sanitized messages.
    text = str(value).lower()
    if any(word in text for word in ("captcha", "verification", "验证码", "验证", "verify")):
        code = "verification_required"
    elif any(word in text for word in ("429", "rate_limit", "rate limit", "频繁", "限流")):
        code = "rate_limited"
    elif any(word in text for word in ("auth", "login", "cookie", "401", "登录", "登陆")):
        code = "auth_expired"
    elif any(word in text for word in ("403", "forbidden", "platform_error")):
        code = "platform_error"
    else:
        code = "backend_unavailable"
    return PlatformError(code, f"外部只读后端读取失败：{code}；请检查后端状态与账户授权。")


@asynccontextmanager
async def _mcp_session(config: ExternalPlatformConfig) -> AsyncIterator[Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client

    required = set(config.env_names) | set(config.headers_env.values())
    if any(name not in os.environ for name in required):
        raise PlatformError("backend_unavailable", "外部后端所需的环境变量尚未设置。")
    if config.transport == "stdio":
        env = {name: os.environ[name] for name in config.env_names}
        params = StdioServerParameters(command=config.command, args=config.args, env=env)
        async with stdio_client(params) as streams, ClientSession(*streams) as session:
            await session.initialize()
            yield session
    else:
        # MCP SDK 2.x uses httpx2 rather than the application's httpx dependency.
        import httpx2

        headers = {header: os.environ[name] for header, name in config.headers_env.items()}
        async with (
            httpx2.AsyncClient(headers=headers, timeout=config.timeout_sec) as client,
            streamable_http_client(config.url, http_client=client) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            yield session


class McpBackend:
    """Lazily open an SDK transport per call so cancellation also closes resources."""

    def __init__(
        self,
        config: ExternalPlatformConfig,
        *,
        allowed_tools: set[str],
        session_factory: SessionFactory = _mcp_session,
    ) -> None:
        self.config = config
        self.allowed_tools = frozenset(allowed_tools)
        self._session_factory = session_factory
        self._closed = False

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in self.allowed_tools:
            raise PlatformError("capability_not_allowed", "外部后端仅允许已声明的只读工具。")
        if self._closed:
            raise PlatformError("backend_unavailable", "外部后端已关闭。")
        try:
            async with asyncio.timeout(self.config.timeout_sec):
                async with self._session_factory(self.config) as session:
                    return await session.call_tool(name, arguments)
        except PlatformError:
            raise
        except Exception as exc:
            raise _backend_error(exc) from None

    async def aclose(self) -> None:
        self._closed = True


def _payload(result: Any) -> Any:
    """Decode SDK CallToolResult, preserving genuine empty lists and surfacing errors."""
    if getattr(result, "is_error", getattr(result, "isError", False)):
        raise _backend_error(getattr(result, "content", result))
    if not isinstance(result, (dict, list)):
        structured = getattr(
            result, "structured_content", getattr(result, "structuredContent", None)
        )
        if structured is not None:
            result = structured
        else:
            content = getattr(result, "content", [])
            texts = [
                getattr(block, "text", "")
                for block in content
                if getattr(block, "type", "") == "text"
            ]
            if not texts:
                raise PlatformError("invalid_shape", "外部后端缺少结构化读取结果。")
            try:
                result = json.loads("\n".join(texts))
            except (ValueError, TypeError):
                raise PlatformError("invalid_shape", "外部后端未返回有效 JSON。") from None
    if isinstance(result, dict):
        if result.get("error") or result.get("success") is False:
            raise _backend_error(result)
        # FastMCP wraps root-list return values in {result:[...]}.
        if set(result) == {"result"}:
            return _payload(result["result"])
    return result


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlatformError("invalid_shape", "外部后端对象形状不匹配。")
    return value


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise PlatformError("invalid_shape", "外部后端列表形状不匹配。")
    return value


def _go_rows(value: dict[str, Any], key: str) -> list[dict[str, Any]]:
    if key not in value:
        raise PlatformError("invalid_shape", "外部后端缺少列表字段。")
    return [] if value[key] is None else _rows(value[key])


def _number(value: Any, default: float = 0) -> float:
    try:
        return float(value or default)
    except (ValueError, TypeError):
        return default


def _timestamp(value: Any) -> int:
    number = _number(value)
    return int(number / 1000 if number > 100_000_000_000 else number)


class ExternalAdapter:
    def __init__(
        self,
        platform: str,
        config: ExternalPlatformConfig,
        backend: ToolBackend,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.platform_id = platform
        self.config = config
        self._backend = backend
        self._contexts: dict[str, dict[str, Any]] = {}
        self._has_subtitles = False
        # The adapter owns this optional client; construction never opens a connection.
        self._http_client = http_client

    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            search=True,
            detail=True,
            comments=True,
            subtitles=self._has_subtitles,
            access_mode="external_mcp_readonly",
            note="仅连接用户配置的外部只读后端；图片 URL 不代表已读取图片内容。",
        )

    async def _call(self, name: str, args: dict[str, Any]) -> Any:
        try:
            async with asyncio.timeout(self.config.timeout_sec):
                return _payload(await self._backend.call_tool(name, args))
        except PlatformError:
            raise
        except Exception as exc:
            raise _backend_error(exc) from None

    def _context(self, native_id: str, context: dict[str, Any] | None) -> dict[str, Any]:
        return dict(context if context is not None else self._contexts.get(native_id, {}))

    def _url(self, native_id: str, context: dict[str, Any]) -> str:
        source_url = str(context.get("url") or "")
        native_is_url = ":" in native_id or "/" in native_id
        candidates = [source_url] if source_url else []
        if native_is_url:
            candidates.append(native_id)
        content_type = "video"
        for candidate in candidates:
            try:
                parsed = urlsplit(candidate)
                host = (parsed.hostname or "").lower()
                domains = (
                    ("xiaohongshu.com",)
                    if self.platform_id == "xiaohongshu"
                    else ("douyin.com", "iesdouyin.com")
                )
                allowed = (
                    parsed.scheme == "https"
                    and parsed.port in {None, 443}
                    and parsed.username is None
                    and parsed.password is None
                    and any(host == domain or host.endswith("." + domain) for domain in domains)
                    and not any(char in candidate for char in ("\\", "\r", "\n", "\t"))
                )
                if not allowed:
                    raise ValueError("invalid content URL")
                match = re.search(
                    r"/(?:video|note|explore|discovery/item)/([A-Za-z0-9_-]+)(?:/|$)", parsed.path
                )
                if native_is_url:
                    if not match:
                        raise ValueError("a canonical content ID is required")
                    native_id = match[1]
                if "/note/" in parsed.path:
                    content_type = "note"
            except ValueError:
                raise PlatformError(
                    "invalid_source_url", "内容链接必须属于当前平台且包含有效作品 ID。"
                ) from None
        if not re.fullmatch(r"[A-Za-z0-9_-]+", native_id):
            raise PlatformError("invalid_source_url", "作品 ID 形状无效。")
        if self.platform_id == "douyin":
            return f"https://www.douyin.com/{content_type}/{native_id}"
        base = f"https://www.xiaohongshu.com/explore/{native_id}"
        token = context.get("xsec_token")
        return base + ("?" + urlencode({"xsec_token": token}) if token else "")

    async def _load_xhs_subtitles(self, note: dict[str, Any], result: dict[str, Any]) -> None:
        video = _object(note.get("video") or {})
        tracks = video.get("subtitles")
        if not tracks:
            return
        if not isinstance(tracks, dict):
            result["limitations"].append("subtitle_empty_or_invalid")
            return
        urls: list[str] = []
        for language in sorted(tracks, key=lambda key: (key not in {"zh", "zh-CN", "source"}, key)):
            entries = tracks[language]
            if not isinstance(entries, list):
                continue
            for entry in entries:
                url = entry.get("url") if isinstance(entry, dict) else None
                if isinstance(url, str) and url and url not in urls:
                    urls.append(url)
        for url in urls[:3]:
            if not is_xhs_subtitle_url(url):
                result["limitations"].append("subtitle_url_not_allowed")
                continue
            if self._http_client is None:
                self._http_client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
            try:
                cues = await fetch_xhs_subtitles(
                    self._http_client, url, timeout_seconds=self.config.timeout_sec
                )
            except PlatformError as exc:
                if exc.code in {"rate_limited", "auth_expired", "verification_required"}:
                    raise
                result["limitations"].append(exc.code)
            else:
                result["evidence_blocks"].extend(cues)
                result["limitations"].remove("subtitle_url_not_read")
                self._has_subtitles = True
                return

    async def search(
        self, keyword: str, *, days: int | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        if self.platform_id == "douyin":
            data = await self._call(
                "search_videos", {"keyword": keyword, "max_count": min(limit, 50)}
            )
            rows = _rows(data)
            results = [self._douyin_meta(row) for row in rows[:limit]]
        else:
            data = _object(await self._call("search_feeds", {"keyword": keyword}))
            rows = _go_rows(data, "feeds")
            results = []
            for row in rows:
                if row.get("modelType") and row["modelType"] != "note":
                    continue
                card = _object(row.get("noteCard"))
                native_id = str(row.get("id") or "")
                if not native_id:
                    raise PlatformError("invalid_shape", "搜索结果缺少笔记 ID。")
                context = {"xsec_token": str(row.get("xsecToken") or "")}
                results.append(self._xhs_meta(card, native_id, context))
            results = results[:limit]
        # Search backends do not expose a reliable publication-date filter. Filter only known dates.
        if days is not None:
            from time import time

            cutoff = time() - days * 86400
            results = [
                row for row in results if not row["published_at"] or row["published_at"] >= cutoff
            ]
        for result in results:
            self._contexts[result["native_id"]] = result["context"]
        return results

    def _base(self, native_id: str, context: dict[str, Any]) -> dict[str, Any]:
        url = self._url(native_id, context)
        return {
            "platform": self.platform_id,
            "native_id": native_id,
            "title": "",
            "description": "",
            "author": "",
            "author_id": "",
            "published_at": 0,
            "duration_sec": 0,
            "url": url,
            "context": {**context, "url": url},
            "evidence_blocks": [],
            "images": [],
            "limitations": [],
        }

    def _douyin_meta(
        self, row: dict[str, Any], context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        native_id = str(row.get("aweme_id") or row.get("video_id") or "")
        if not native_id:
            raise PlatformError("invalid_shape", "抖音结果缺少作品 ID。")
        ctx = dict(context or {})
        source_url = row.get("source_url") or row.get("share_url") or row.get("url")
        if source_url:
            ctx.setdefault("url", str(source_url))
        result = self._base(native_id, ctx)
        result.update(
            title=str(row.get("title") or row.get("video_title") or ""),
            description=str(
                row.get("description") or row.get("desc") or row.get("video_title") or ""
            ),
            author=str(row.get("nickname") or ""),
            author_id=str(row.get("author_id") or row.get("uid") or row.get("sec_uid") or ""),
            published_at=_timestamp(row.get("create_time")),
            duration_sec=_number(row.get("duration_ms")) / 1000,
        )
        result["images"] = self._images(row.get("images", []))
        return result

    def _xhs_meta(
        self, note: dict[str, Any], native_id: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        result = self._base(native_id, context)
        user = _object(note.get("user", {}))
        result.update(
            title=str(note.get("title") or note.get("displayTitle") or ""),
            description=str(note.get("desc") or ""),
            author=str(user.get("nickname") or user.get("nickName") or ""),
            author_id=str(user.get("userId") or ""),
            published_at=_timestamp(note.get("time")),
        )
        video = _object(note.get("video") or {})
        result["duration_sec"] = _number(_object(video.get("capa") or {}).get("duration"))
        result["images"] = self._images(note.get("imageList", []))
        if video.get("subtitles"):
            result["limitations"].append("subtitle_url_not_read")
        return result

    @staticmethod
    def _images(images: Any) -> list[dict[str, Any]]:
        result = []
        for index, image in enumerate([] if images is None else _rows(images), 1):
            url = image.get("url") or image.get("urlDefault") or image.get("urlPre")
            if url:
                result.append({"url": str(url), "page_index": index})
        return result

    async def _xhs_detail(self, native_id: str, context: dict[str, Any]) -> dict[str, Any]:
        canonical_url = self._url(native_id, context)
        if native_id.startswith(("http://", "https://")):
            parsed = urlsplit(native_id)
            native_id = parsed.path.rstrip("/").split("/")[-1]
            context.setdefault("xsec_token", parse_qs(parsed.query).get("xsec_token", [""])[0])
        native_id = urlsplit(canonical_url).path.rstrip("/").split("/")[-1]
        token = context.get("xsec_token")
        if not token:
            raise PlatformError(
                "context_required", "小红书详情需要搜索结果中的 xsec_token 或带 token 的原始链接。"
            )
        raw = _object(
            await self._call("get_feed_detail", {"feed_id": native_id, "xsec_token": token})
        )
        # Go MCP tool returns {feed_id,data:{note,comments}}.
        data = _object(raw.get("data", raw))
        _object(data.get("note"))
        return data

    async def get_detail(
        self, native_id: str, *, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        ctx = self._context(native_id, context)
        if self.platform_id == "xiaohongshu":
            data = await self._xhs_detail(native_id, ctx)
            note = _object(data["note"])
            result = self._xhs_meta(note, str(note.get("noteId") or native_id), ctx)
            await self._load_xhs_subtitles(note, result)
        else:
            raw = _object(
                await self._call(
                    "read_video_content",
                    {
                        "share_url": self._url(native_id, ctx),
                        "no_asr": not self.config.asr_enabled,
                        "comments": 0,
                        "vision": False,
                        "chapters": False,
                        "related": False,
                    },
                )
            )
            result = self._douyin_meta(_object(raw.get("video")), ctx)
            transcript = raw.get("transcript") or {}
            if not isinstance(transcript, dict):
                raise PlatformError("invalid_shape", "后端转写结果形状不匹配。")
            source = (
                "subtitle"
                if transcript.get("source") in {"official_subtitle", "subtitle"}
                else "asr"
            )
            for segment in _rows(transcript.get("segments", [])):
                if segment.get("text"):
                    result["evidence_blocks"].append(
                        {
                            "source_type": source,
                            "text": str(segment["text"]),
                            "start_sec": _number(segment.get("start")),
                            "end_sec": _number(segment.get("end")),
                            "source_url": result["url"],
                        }
                    )
                    self._has_subtitles = self._has_subtitles or source == "subtitle"
            if transcript.get("text") and not result["evidence_blocks"]:
                result["evidence_blocks"].append(
                    {
                        "source_type": source,
                        "text": str(transcript["text"]),
                        "start_sec": None,
                        "end_sec": None,
                        "source_url": result["url"],
                    }
                )
                self._has_subtitles = self._has_subtitles or source == "subtitle"
            if transcript.get("error"):
                result["limitations"].append("transcript_unavailable")
        if result["description"]:
            result["evidence_blocks"].insert(
                0,
                {
                    "source_type": "description",
                    "text": result["description"],
                    "start_sec": None,
                    "end_sec": None,
                    "source_url": result["url"],
                },
            )
        if result["images"]:
            result["limitations"].append("image_content_not_read")
        return result

    async def get_comments(
        self, native_id: str, *, limit: int = 20, context: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        ctx = self._context(native_id, context)
        if self.platform_id == "xiaohongshu":
            data = await self._xhs_detail(native_id, ctx)
            rows = _go_rows(_object(data.get("comments")), "list")
        else:
            raw = _object(
                await self._call(
                    "get_video_comments",
                    {
                        "share_url": self._url(native_id, ctx),
                        "max_count": min(limit, 50),
                        "cursor": 0,
                    },
                )
            )
            rows = _rows(raw.get("comments"))
        results = []
        for row in rows[:limit]:
            user = _object(row.get("userInfo", {}))
            results.append(
                {
                    "platform": self.platform_id,
                    "id": str(row.get("id") or row.get("comment_id") or ""),
                    "text": str(row.get("content") or row.get("text") or ""),
                    "author": str(
                        user.get("nickname") or user.get("nickName") or row.get("nickname") or ""
                    ),
                    "author_id": str(user.get("userId") or row.get("author_id") or ""),
                    "like_count": int(_number(row.get("likeCount", row.get("digg_count")))),
                    "created_at": _timestamp(row.get("createTime", row.get("create_time"))),
                }
            )
        return results

    async def list_subtitles(self, native_id: str) -> list[dict[str, Any]]:
        detail = await self.get_detail(native_id)
        subtitles = [
            block for block in detail["evidence_blocks"] if block["source_type"] == "subtitle"
        ]
        if not subtitles:
            raise PlatformError("subtitle_unavailable", "外部后端未返回可用字幕文本。")
        return subtitles

    async def aclose(self) -> None:
        try:
            await self._backend.aclose()
        finally:
            if self._http_client is not None:
                await self._http_client.aclose()


def build_external_adapters(
    config: ExternalPlatformsConfig, *, call_tool_factory: BackendFactory | None = None
) -> dict[str, PlatformAdapter]:
    adapters: dict[str, PlatformAdapter] = {}
    for platform in ("xiaohongshu", "douyin"):
        settings = getattr(config, platform)
        if not settings.enabled:
            continue
        tools = (
            {"search_feeds", "get_feed_detail"}
            if platform == "xiaohongshu"
            else {"search_videos", "read_video_content", "get_video_comments"}
        )
        backend = (
            call_tool_factory(platform, settings)
            if call_tool_factory
            else McpBackend(settings, allowed_tools=tools)
        )
        adapters[platform] = ExternalAdapter(platform, settings, backend)
    return adapters
