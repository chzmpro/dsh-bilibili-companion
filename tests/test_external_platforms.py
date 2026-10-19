"""External backend boundaries: actual upstream shapes, no live services."""

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from core.external_config import ExternalPlatformConfig, ExternalPlatformsConfig
from core.external_platforms import McpBackend, build_external_adapters
from core.platforms import PlatformError, get_adapter, platform_overview


class Backend:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        value = self.responses[name]
        if isinstance(value, Exception):
            raise value
        return value

    async def aclose(self) -> None:
        self.closed = True


def adapter(platform: str, backend: Backend, **kwargs: Any) -> Any:
    config = ExternalPlatformsConfig(
        **{
            platform: ExternalPlatformConfig(
                enabled=True, url="http://localhost:18060/mcp", **kwargs
            )
        }
    )
    return build_external_adapters(config, call_tool_factory=lambda _p, _c: backend)[platform]


def test_disabled_backends_do_not_connect_and_enabled_requires_endpoint() -> None:
    assert build_external_adapters(ExternalPlatformsConfig()) == {}
    with pytest.raises(ValidationError):
        ExternalPlatformConfig(enabled=True)
    with pytest.raises(ValidationError):
        ExternalPlatformConfig(enabled=True, transport="stdio", command="python", env_names=["x=y"])


async def test_xhs_token_survives_search_to_restart_detail_and_images_are_not_ocr() -> None:
    backend = Backend(
        {
            "search_feeds": {
                "feeds": [
                    {
                        "id": "note1",
                        "xsecToken": "access-token",
                        "noteCard": {
                            "displayTitle": "镜头测试",
                            "user": {"userId": "001", "nickname": "作者"},
                        },
                    }
                ]
            },
            "get_feed_detail": {
                "data": {
                    "note": {
                        "noteId": "note1",
                        "title": "镜头测试",
                        "desc": "真实正文",
                        "time": 1700000000000,
                        "user": {"userId": "001", "nickname": "作者"},
                        "imageList": [{"urlDefault": "https://image.example/a.jpg"}],
                        "video": {"subtitles": {"zh": [{"url": "https://example/sub.srt"}]}},
                    },
                    "comments": {
                        "list": [
                            {
                                "id": "c1",
                                "content": "有用",
                                "userInfo": {"userId": "002", "nickname": "读者"},
                            }
                        ]
                    },
                }
            },
        }
    )
    first = adapter("xiaohongshu", backend)
    candidates = await first.search("镜头", limit=1)
    context = candidates[0]["context"]
    assert context["xsec_token"] == "access-token"
    restarted = adapter("xiaohongshu", backend)
    detail = await restarted.get_detail("note1", context=context)
    assert backend.calls[-1] == (
        "get_feed_detail",
        {"feed_id": "note1", "xsec_token": "access-token"},
    )
    assert detail["author_id"] == "001"
    assert detail["published_at"] == 1700000000
    assert detail["images"] == [{"url": "https://image.example/a.jpg", "page_index": 1}]
    assert [b["source_type"] for b in detail["evidence_blocks"]] == ["description"]
    assert detail["evidence_blocks"][0]["text"] == "真实正文"
    assert "image_content_not_read" in detail["limitations"]
    assert "subtitle_url_not_read" in detail["limitations"]
    comments = await restarted.get_comments("note1", context=context)
    assert comments[0]["author_id"] == "002"
    assert comments[0]["text"] == "有用"


async def test_xhs_missing_token_is_actionable_instead_of_guessing() -> None:
    with pytest.raises(PlatformError) as error:
        await adapter("xiaohongshu", Backend({})).get_detail("note1")
    assert error.value.code == "context_required"


async def test_douyin_read_preserves_transcript_timing_and_default_skips_asr() -> None:
    backend = Backend(
        {
            "read_video_content": {
                "success": True,
                "video": {
                    "aweme_id": "123",
                    "title": "说明",
                    "nickname": "作者",
                    "duration_ms": 3500,
                },
                "transcript": {
                    "source": "official_subtitle",
                    "segments": [{"start": 0.5, "end": 2.5, "text": "证据内容"}],
                },
            },
            "get_video_comments": {
                "comments": [
                    {"comment_id": "c2", "nickname": "读者", "text": "评论", "digg_count": 2}
                ]
            },
        }
    )
    reader = adapter("douyin", backend)
    detail = await reader.get_detail("123")
    assert backend.calls[0] == (
        "read_video_content",
        {
            "share_url": "https://www.douyin.com/video/123",
            "no_asr": True,
            "comments": 0,
            "vision": False,
            "chapters": False,
            "related": False,
        },
    )
    assert detail["duration_sec"] == 3.5
    assert detail["evidence_blocks"] == [
        {
            "source_type": "subtitle",
            "text": "证据内容",
            "start_sec": 0.5,
            "end_sec": 2.5,
            "source_url": "https://www.douyin.com/video/123",
        }
    ]
    assert reader.capabilities().subtitles is True
    assert (await reader.get_comments("123"))[0]["id"] == "c2"
    assert backend.calls[-1][1] == {
        "share_url": "https://www.douyin.com/video/123",
        "max_count": 20,
        "cursor": 0,
    }


async def test_douyin_search_normalizes_upstream_video_list() -> None:
    backend = Backend(
        {
            "search_videos": [
                {
                    "aweme_id": "123",
                    "video_title": "标题",
                    "nickname": "作者",
                    "duration_ms": 4000,
                    "create_time": 1700000000,
                }
            ]
        }
    )
    items = await adapter("douyin", backend).search("相机", limit=2)
    assert items[0]["native_id"] == "123"
    assert items[0]["title"] == "标题"
    assert backend.calls == [("search_videos", {"keyword": "相机", "max_count": 2})]


async def test_douyin_retains_caption_as_body_and_author_sec_uid() -> None:
    backend = Backend(
        {
            "search_videos": [
                {
                    "aweme_id": "123",
                    "video_title": "完整作品文案",
                    "sec_uid": "MS4wLjAB-author",
                    "share_url": "https://www.douyin.com/note/123",
                }
            ]
        }
    )
    item = (await adapter("douyin", backend).search("文案"))[0]
    assert item["author_id"] == "MS4wLjAB-author"
    assert item["description"] == "完整作品文案"
    assert item["url"] == "https://www.douyin.com/note/123"


async def test_missing_subtitles_are_explicit_and_do_not_claim_support() -> None:
    backend = Backend(
        {
            "read_video_content": {
                "video": {"aweme_id": "123"},
                "transcript": {"source": "none", "segments": []},
            }
        }
    )
    reader = adapter("douyin", backend)
    with pytest.raises(PlatformError) as error:
        await reader.list_subtitles("123")
    assert error.value.code == "subtitle_unavailable"
    assert reader.capabilities().subtitles is False


async def test_go_backend_null_slices_are_empty_but_missing_keys_are_invalid() -> None:
    backend = Backend(
        {
            "search_feeds": {"feeds": None},
            "get_feed_detail": {
                "data": {"note": {"noteId": "n1", "imageList": None}, "comments": {"list": None}}
            },
        }
    )
    reader = adapter("xiaohongshu", backend)
    assert await reader.search("空结果") == []
    assert (await reader.get_detail("n1", context={"xsec_token": "t"}))["images"] == []
    assert await reader.get_comments("n1", context={"xsec_token": "t"}) == []
    backend.responses["search_feeds"] = {"unknown": None}
    with pytest.raises(PlatformError) as error:
        await reader.search("坏结果")
    assert error.value.code == "invalid_shape"


async def test_explicit_asr_setting_controls_backend_and_retains_asr_evidence() -> None:
    backend = Backend(
        {
            "read_video_content": {
                "video": {"aweme_id": "123"},
                "transcript": {
                    "source": "asr",
                    "segments": [{"start": 1, "end": 3, "text": "真实转写"}],
                },
            }
        }
    )
    reader = adapter("douyin", backend, asr_enabled=True)
    detail = await reader.get_detail("123")
    assert backend.calls[0][1]["no_asr"] is False
    assert detail["evidence_blocks"][0]["source_type"] == "asr"
    assert detail["evidence_blocks"][0]["text"] == "真实转写"


async def test_sdk_text_json_and_structured_list_wrappers_are_decoded() -> None:
    backend = Backend(
        {
            "search_videos": CallToolResult(
                content=[],
                structuredContent={"result": [{"aweme_id": "123", "video_title": "字幕"}]},
            )
        }
    )
    assert (await adapter("douyin", backend).search("字幕"))[0]["native_id"] == "123"
    backend.responses["search_videos"] = CallToolResult(content=[TextContent(text="[]")])
    assert await adapter("douyin", backend).search("字幕") == []
    backend.responses["search_videos"] = CallToolResult(
        isError=True, content=[TextContent(text="需要验证码 token=not-for-output")]
    )
    with pytest.raises(PlatformError) as error:
        await adapter("douyin", backend).search("字幕")
    assert error.value.code == "verification_required"
    assert "not-for-output" not in str(error.value)


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"error": "login expired", "error_code": "auth_expired"}, "auth_expired"),
        ({"error": "需要验证码"}, "verification_required"),
        ({"error": "HTTP 429 rate limit"}, "rate_limited"),
        ({"nonsense": []}, "invalid_shape"),
        (ConnectionError("offline"), "backend_unavailable"),
    ],
)
async def test_backend_failures_are_not_empty_results(payload: Any, code: str) -> None:
    with pytest.raises(PlatformError) as error:
        await adapter("douyin", Backend({"search_videos": payload})).search("相机")
    assert error.value.code == code


async def test_mcp_transport_is_lazy_closes_on_failure_and_forbids_write_tools() -> None:
    opened: list[str] = []

    @asynccontextmanager
    async def session_factory(config: ExternalPlatformConfig) -> Any:
        opened.append("open")
        try:
            yield Backend({"search_videos": ConnectionError("offline")})
        finally:
            opened.append("close")

    backend = McpBackend(
        ExternalPlatformConfig(enabled=True, url="http://localhost/mcp"),
        allowed_tools={"search_videos"},
        session_factory=session_factory,
    )
    assert opened == []
    with pytest.raises(PlatformError) as error:
        await backend.call_tool("publish_comment", {})
    assert error.value.code == "capability_not_allowed"
    assert opened == []
    with pytest.raises(PlatformError):
        await backend.call_tool("search_videos", {"keyword": "相机"})
    assert opened == ["open", "close"]
    await backend.aclose()


async def test_mcp_cancelled_read_closes_transport_without_swallowing_cancel() -> None:
    opened = asyncio.Event()
    closed = asyncio.Event()

    class WaitingBackend:
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            opened.set()
            await asyncio.Event().wait()

    @asynccontextmanager
    async def session_factory(config: ExternalPlatformConfig) -> Any:
        try:
            yield WaitingBackend()
        finally:
            closed.set()

    backend = McpBackend(
        ExternalPlatformConfig(enabled=True, url="http://localhost/mcp"),
        allowed_tools={"search_videos"},
        session_factory=session_factory,
    )
    pending = asyncio.create_task(backend.call_tool("search_videos", {"keyword": "相机"}))
    await asyncio.wait_for(opened.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert closed.is_set()


async def test_injected_adapter_is_reported_enabled_with_write_capabilities_disabled() -> None:
    backend = Backend({})
    reader = adapter("douyin", backend)
    assert get_adapter("douyin", backend=reader) is reader
    row = next(r for r in platform_overview({"douyin": reader}) if r["platform"] == "douyin")
    assert row["implemented"] is True
    assert row["capabilities"]["publish_comment"] is False
    assert row["capabilities"]["publish_reply"] is False
    assert row["capabilities"]["like"] is False
    assert row["capabilities"]["favorite"] is False
    await reader.aclose()
    assert backend.closed
