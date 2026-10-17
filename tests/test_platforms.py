"""平台抽象层测试（2026-10-17）。

对应主人要求：*"抖音和小红书……走正规渠道，但是这两个你先预留框架"*。

核心不变量（都在这里钉死）：

1. **未实现的平台必须"明确拒绝"** —— 抛 :class:`CapabilityNotImplemented`，
   **绝不返回空列表**（空列表会被上层误解成"有这能力但这次没内容"）；
2. 拒绝信息里要写清**为什么没做**与**规划中的接入方式**；
3. 未知平台**抛错**（不是返回 None），让拼写错误立刻暴露；
4. B 站 adapter 是**薄包装**：不复制读取逻辑，仍走 reader；
5. 能力差异要**显式声明**（小红书没字幕这种）；
6. 抽象层里**没有**任何"绕过风控"的入口。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.platforms import (
    KNOWN_PLATFORMS,
    PLATFORM_BILIBILI,
    PLATFORM_DOUYIN,
    PLATFORM_XIAOHONGSHU,
    BilibiliAdapter,
    CapabilityNotImplemented,
    PlatformAdapter,
    PlatformCapabilities,
    PlatformError,
    UnsupportedPlatform,
    get_adapter,
    platform_overview,
)

# ---------------------------------------------------------------------------
# ★★ 最重要的：未实现平台必须明确拒绝
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", [PLATFORM_DOUYIN, PLATFORM_XIAOHONGSHU])
async def test_unimplemented_platform_search_rejects(platform: str) -> None:
    """★★ 未接的平台**抛错**，绝不返回空列表。

    这是本模块存在的意义：空列表会让上层以为"读到了，只是没内容"，
    从而走进完全错误的降级路径（重试、记 NO_CONTENT、误判"今天没值得看的"）。
    """
    adapter = get_adapter(platform)

    with pytest.raises(CapabilityNotImplemented) as excinfo:
        await adapter.search("任何关键词")

    assert excinfo.value.code == "NOT_IMPLEMENTED"
    message = str(excinfo.value)
    assert platform in message, "错误信息要点明是哪个平台"
    assert "官方授权通道" in message, "要说明规划中的接入方式"


@pytest.mark.parametrize("platform", [PLATFORM_DOUYIN, PLATFORM_XIAOHONGSHU])
async def test_unimplemented_platform_all_methods_reject(platform: str) -> None:
    """每个能力都要拒绝 —— 不能有的拒绝、有的偷偷返回空。"""
    adapter = get_adapter(platform)

    with pytest.raises(CapabilityNotImplemented):
        await adapter.get_detail("x")
    with pytest.raises(CapabilityNotImplemented):
        await adapter.list_subtitles("x")
    with pytest.raises(CapabilityNotImplemented):
        await adapter.get_comments("x")


async def test_douyin_rejection_explains_signature_reason() -> None:
    """抖音的拒绝信息必须点明"逆向签名属于绕过技术保护措施"。"""
    adapter = get_adapter(PLATFORM_DOUYIN)
    with pytest.raises(CapabilityNotImplemented) as excinfo:
        await adapter.search("测试")
    message = str(excinfo.value)
    assert "签名" in message
    assert "绕过" in message or "逆向" in message


def test_xiaohongshu_reason_mentions_legal_cases() -> None:
    """小红书的拒绝理由要提到已有判决（这是"不做"的依据，不是随口说的）。"""
    adapter = get_adapter(PLATFORM_XIAOHONGSHU)
    assert "判决" in adapter.reason or "违法" in adapter.reason


@pytest.mark.parametrize("platform", [PLATFORM_DOUYIN, PLATFORM_XIAOHONGSHU])
def test_unimplemented_capabilities_declare_nothing_available(platform: str) -> None:
    """未接的平台：能力表里**一个 True 都不该有**（不能假装有能力）。"""
    caps = get_adapter(platform).capabilities()
    declared = caps.to_dict()
    booleans = [v for k, v in declared.items() if isinstance(v, bool)]
    assert not any(booleans), f"{platform} 不该声明任何可用能力：{declared}"
    assert caps.access_mode == "none"


# ---------------------------------------------------------------------------
# 未知平台
# ---------------------------------------------------------------------------


def test_unknown_platform_raises_not_none() -> None:
    """★ 拼错平台名要**抛错**，不是返回 None。

    返回 None 会一路传下去，等到"怎么什么都没读到"才发现，排查成本高得多。
    """
    with pytest.raises(UnsupportedPlatform) as excinfo:
        get_adapter("weibo")
    assert excinfo.value.code == "UNKNOWN_PLATFORM"
    assert "bilibili" in str(excinfo.value), "错误信息里要列出已规划的平台"


def test_platform_error_hierarchy() -> None:
    assert issubclass(CapabilityNotImplemented, PlatformError)
    assert issubclass(UnsupportedPlatform, PlatformError)


# ---------------------------------------------------------------------------
# B 站 adapter
# ---------------------------------------------------------------------------


def test_bilibili_adapter_requires_reader() -> None:
    """★ 必须传 reader —— 否则会绕开统一请求队列（限速/缓存/健康门禁）。"""
    with pytest.raises(PlatformError) as excinfo:
        get_adapter(PLATFORM_BILIBILI, reader=None)
    assert excinfo.value.code == "READER_REQUIRED"
    assert "统一请求队列" in str(excinfo.value)


def test_bilibili_adapter_declares_capabilities() -> None:
    adapter = get_adapter(PLATFORM_BILIBILI, reader=object())
    caps = adapter.capabilities()

    assert caps.search is True
    assert caps.detail is True
    assert caps.subtitles is True
    assert caps.comments is True
    assert caps.recommendation_feed is True
    # 必须写明许可风险（这是项目的红线之一）
    assert "§5.6" in caps.note or "书面许可" in caps.note


def test_bilibili_adapter_satisfies_protocol() -> None:
    """运行时协议检查：B 站 adapter 必须符合统一接口。"""
    adapter = get_adapter(PLATFORM_BILIBILI, reader=object())
    assert isinstance(adapter, PlatformAdapter)


class _FakeReader:
    """最小假 reader（**绝不联网**）。"""

    def __init__(self) -> None:
        self.search_calls: list[dict[str, Any]] = []
        self.detail_calls: list[str] = []

    async def search_videos(self, keyword: str, **kwargs: Any) -> list[Any]:
        self.search_calls.append({"keyword": keyword, **kwargs})
        from core.bilibili import VideoMeta

        return [
            VideoMeta(
                bvid="BV1test00001",
                aid=1,
                title="标题",
                up_mid=2,
                up_name="UP",
                published_at=1_700_000_000,
                duration_sec=60,
                source_url="https://x/",
            )
        ]

    async def get_video_detail(self, bvid: str) -> Any:
        self.detail_calls.append(bvid)
        from core.bilibili import VideoDetail

        return VideoDetail(
            bvid=bvid,
            aid=1,
            cid=2,
            title="标题",
            description="简介",
            up_mid=2,
            up_name="UP",
            published_at=1_700_000_000,
            duration_sec=60,
            reply_count=0,
            source_url="https://x/",
        )

    async def get_subtitles(self, bvid: str, cid: int) -> list[Any]:
        return []

    async def get_comments(self, aid: int, *, limit: int = 20) -> list[Any]:
        return []


async def test_bilibili_search_normalizes_shape() -> None:
    """B 站 adapter 把结果转成**统一形状**（跨平台一致）。"""
    reader = _FakeReader()
    adapter = get_adapter(PLATFORM_BILIBILI, reader=reader)

    out = await adapter.search("DSH", days=2, limit=10)

    assert len(out) == 1
    item = out[0]
    assert item["platform"] == "bilibili"
    assert item["native_id"] == "BV1test00001"
    assert item["title"] == "标题"
    assert item["author"] == "UP"
    # ★ 必须真的走了 reader（不复制逻辑）
    assert reader.search_calls and reader.search_calls[0]["keyword"] == "DSH"


async def test_bilibili_detail_normalizes_shape() -> None:
    reader = _FakeReader()
    adapter = get_adapter(PLATFORM_BILIBILI, reader=reader)

    out = await adapter.get_detail("BV1test00001")
    assert out["platform"] == "bilibili"
    assert out["native_id"] == "BV1test00001"
    assert out["description"] == "简介"
    assert reader.detail_calls == ["BV1test00001"]


# ---------------------------------------------------------------------------
# 平台一览 / 能力表
# ---------------------------------------------------------------------------


def test_platform_overview_lists_all_known() -> None:
    overview = platform_overview()
    ids = [item["platform"] for item in overview]
    assert ids == list(KNOWN_PLATFORMS)


def test_platform_overview_marks_implemented_flag() -> None:
    overview = {item["platform"]: item for item in platform_overview()}
    assert overview["bilibili"]["implemented"] is True
    assert overview["douyin"]["implemented"] is False
    assert overview["xiaohongshu"]["implemented"] is False
    # 未实现的要给出"为什么"（可审计）
    assert overview["douyin"]["blocked_reason"]
    assert overview["xiaohongshu"]["blocked_reason"]


def test_capabilities_to_dict_is_json_serializable() -> None:
    import json

    caps = PlatformCapabilities(search=True, note="x")
    assert json.loads(json.dumps(caps.to_dict()))["search"] is True


def test_no_bypass_entry_points_in_module() -> None:
    """★★ 红线检查：抽象层里**不该有**任何"绕过风控"的入口。

    真机教训：一旦抽象层留了 `proxy=` / `fake_fingerprint=` 这种口子，
    接新平台时就会有人（或未来的我）顺手用上。
    """
    import inspect

    from core import platforms

    source = inspect.getsource(platforms).lower()
    for forbidden in ("proxy", "fingerprint", "captcha", "bypass", "rotate_ip"):
        assert forbidden not in source, f"抽象层不该出现 {forbidden!r} 相关入口"


def test_adapter_platform_id_is_stable() -> None:
    adapter = BilibiliAdapter(reader=object())
    assert adapter.platform_id == PLATFORM_BILIBILI
