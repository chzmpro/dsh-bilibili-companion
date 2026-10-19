"""Real subtitle parsing and bounded HTTP reads through an in-memory transport."""

from typing import Any

import httpx
import pytest

from core.external_config import ExternalPlatformConfig
from core.external_platforms import ExternalAdapter
from core.platforms import PlatformError
from core.subtitles import parse_subtitle_text

SUBTITLE_URL = "https://sns-video-bd.xhscdn.com/subtitles/zh.srt?sign=temporary"
SRT = (
    "1\n00:00:01,200 --> 00:00:03,450\n第一行\n第二行\n\n"
    "2\n00:00:03,450 --> 00:00:04,000\n最后一句\n"
)


class Backend:
    def __init__(self, urls: list[str] | None = None) -> None:
        self.urls = [SUBTITLE_URL] if urls is None else urls
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        return {
            "data": {
                "note": {
                    "noteId": "note1",
                    "desc": "作品正文",
                    "video": {
                        "subtitles": {
                            "zh": [
                                {"url": url, "language": "zh", "format": 1, "type": 1}
                                for url in self.urls
                            ]
                        }
                    },
                },
                "comments": {"list": []},
            }
        }

    async def aclose(self) -> None:
        pass


def reader(client: httpx.AsyncClient, urls: list[str] | None = None) -> ExternalAdapter:
    return ExternalAdapter(
        "xiaohongshu",
        ExternalPlatformConfig(enabled=True, url="http://localhost:18060/mcp"),
        Backend(urls),
        http_client=client,
    )


def test_srt_and_vtt_preserve_real_cues_and_ignore_metadata() -> None:
    assert parse_subtitle_text(SRT, source_url=SUBTITLE_URL) == [
        {
            "source_type": "subtitle",
            "text": "第一行\n第二行",
            "start_sec": 1.2,
            "end_sec": 3.45,
            "source_url": SUBTITLE_URL,
        },
        {
            "source_type": "subtitle",
            "text": "最后一句",
            "start_sec": 3.45,
            "end_sec": 4.0,
            "source_url": SUBTITLE_URL,
        },
    ]
    vtt = (
        "\ufeffWEBVTT\n\nNOTE metadata\nnot a cue\n\ncue-1\n"
        "00:01.000 --> 00:02.500 align:start\n<v narrator>真正的 &amp; 字幕</v>\n"
    )
    assert parse_subtitle_text(vtt, source_url=SUBTITLE_URL) == [
        {
            "source_type": "subtitle",
            "text": "真正的 & 字幕",
            "start_sec": 1.0,
            "end_sec": 2.5,
            "source_url": SUBTITLE_URL,
        }
    ]
    assert parse_subtitle_text("https://example/subtitle.srt", source_url=SUBTITLE_URL) == []
    assert (
        parse_subtitle_text("1\n00:00:03,000 --> 00:00:01,000\n倒序", source_url=SUBTITLE_URL) == []
    )


async def test_xhs_subtitles_are_downloaded_as_text_with_actual_source_and_closed() -> None:
    requests: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=SRT, headers={"Content-Type": "application/x-subrip"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(serve),
        follow_redirects=True,
        headers={"Cookie": "should-not-leak", "Authorization": "Bearer private"},
    )
    adapter = reader(client)
    detail = await adapter.get_detail("note1", context={"xsec_token": "access-token"})
    assert detail["evidence_blocks"][1] == {
        "source_type": "subtitle",
        "text": "第一行\n第二行",
        "start_sec": 1.2,
        "end_sec": 3.45,
        "source_url": SUBTITLE_URL,
    }
    assert adapter.capabilities().subtitles is True
    assert "subtitle_url_not_read" not in detail["limitations"]
    assert str(requests[0].url) == SUBTITLE_URL
    assert "cookie" not in requests[0].headers
    assert "authorization" not in requests[0].headers
    await adapter.aclose()
    assert client.is_closed


@pytest.mark.parametrize(
    "url",
    [
        "http://sns-video-bd.xhscdn.com/a.srt",
        "https://localhost/a.srt",
        "https://xhscdn.com.attacker.test/a.srt",
        "https://user:pass@sns-video-bd.xhscdn.com/a.srt",
        "https://sns-video-bd.xhscdn.com:8443/a.srt",
        "file:///tmp/a.srt",
    ],
)
async def test_invalid_subtitle_url_is_not_requested_or_fabricated(url: str) -> None:
    requests: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, text=SRT)

    adapter = reader(httpx.AsyncClient(transport=httpx.MockTransport(serve)), [url])
    detail = await adapter.get_detail("note1", context={"xsec_token": "access-token"})
    assert requests == []
    assert [b["source_type"] for b in detail["evidence_blocks"]] == ["description"]
    assert "subtitle_url_not_read" in detail["limitations"]
    assert "subtitle_url_not_allowed" in detail["limitations"]
    await adapter.aclose()


@pytest.mark.parametrize(
    ("response", "limitation"),
    [
        (
            httpx.Response(302, headers={"Location": "http://localhost/private"}),
            "subtitle_redirect_refused",
        ),
        (httpx.Response(200, text="没有字幕时间轴"), "subtitle_empty_or_invalid"),
        (httpx.Response(200, content=b"x" * (1024 * 1024 + 1)), "subtitle_too_large"),
        (httpx.Response(403, text="signed URL expired"), "subtitle_http_403"),
    ],
)
async def test_unreadable_subtitles_have_explicit_limits(
    response: httpx.Response, limitation: str
) -> None:
    requests: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return response

    adapter = reader(httpx.AsyncClient(transport=httpx.MockTransport(serve), follow_redirects=True))
    detail = await adapter.get_detail("note1", context={"xsec_token": "access-token"})
    assert requests == [SUBTITLE_URL]
    assert "subtitle_url_not_read" in detail["limitations"]
    assert limitation in detail["limitations"]
    assert not any(b["source_type"] == "subtitle" for b in detail["evidence_blocks"])
    await adapter.aclose()


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(429, text="Too many requests"), "rate_limited"),
        (httpx.Response(403, text="<html>请完成验证码</html>"), "verification_required"),
    ],
)
async def test_risk_signals_stop_the_read_instead_of_becoming_optional_limits(
    response: httpx.Response, code: str
) -> None:
    adapter = reader(httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: response)))
    with pytest.raises(PlatformError) as error:
        await adapter.get_detail("note1", context={"xsec_token": "access-token"})
    assert error.value.code == code
    await adapter.aclose()


async def test_subtitle_timeout_is_an_explicit_limitation() -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    adapter = reader(httpx.AsyncClient(transport=httpx.MockTransport(serve)))
    detail = await adapter.get_detail("note1", context={"xsec_token": "access-token"})
    assert "subtitle_timeout" in detail["limitations"]
    await adapter.aclose()


async def test_unadvertised_stream_is_bounded_and_closed_before_consuming_whole_body() -> None:
    class OversizedStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.chunks_read = 0
            self.closed = False

        async def __aiter__(self) -> Any:
            for _ in range(100):
                self.chunks_read += 1
                yield b"x" * 65536

        async def aclose(self) -> None:
            self.closed = True

    stream = OversizedStream()
    adapter = reader(
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=stream))
        )
    )
    detail = await adapter.get_detail("note1", context={"xsec_token": "access-token"})
    assert "subtitle_too_large" in detail["limitations"]
    assert stream.chunks_read == 17
    assert stream.closed
    await adapter.aclose()


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/private",
        "https://www.xiaohongshu.com/explore/x",
        "file:///private",
        "https://www.douyin.com.attacker.test/video/123",
        "https://u:p@www.douyin.com/video/123",
    ],
)
async def test_untrusted_content_url_never_reaches_douyin_backend(url: str) -> None:
    backend = Backend([])
    adapter = ExternalAdapter(
        "douyin", ExternalPlatformConfig(enabled=True, url="http://localhost/mcp"), backend
    )
    with pytest.raises(PlatformError) as error:
        await adapter.get_detail("123", context={"url": url})
    assert error.value.code == "invalid_source_url"
    assert backend.calls == []


async def test_native_id_uses_canonical_content_url_instead_of_backend_shortlink() -> None:
    backend = Backend([])
    adapter = ExternalAdapter(
        "douyin", ExternalPlatformConfig(enabled=True, url="http://localhost/mcp"), backend
    )
    with pytest.raises(PlatformError):
        # This fixture returns an XHS detail shape; only the emitted URL is relevant here.
        await adapter.get_detail("123", context={"url": "https://v.douyin.com/untrusted"})
    assert backend.calls[0][1]["share_url"] == "https://www.douyin.com/video/123"
