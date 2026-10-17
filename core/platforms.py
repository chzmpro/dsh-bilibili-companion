"""平台抽象层：一个统一的"视频平台"接口，为多平台预留框架（2026-10-17）。

## 为什么现在就要做（虽然只有 B 站实现了）

主人说：*"抖音和小红书……走正规渠道，但是这两个你先预留框架，
待 B 站基本完成后再对它们进行完善"*。

把接口先定下来，好处是**将来接新平台只是加一个 adapter**，
而不用回头改 pipeline / 门禁 / 日报那一整条链。

## ★★ 三条设计红线（这是本模块存在的意义）

1. **没实现的能力必须"明确拒绝"，绝不"假装能用"**
   抖音/小红书 adapter 的所有方法都抛 :class:`CapabilityNotImplemented`，
   里面写清**为什么没做**（走正规渠道 / 待 B 站完成）。
   绝不允许返回空列表假装成功 —— 那会让上层"以为读到了，只是没内容"。

2. **平台能力差异要显式声明**（:class:`PlatformCapabilities`）
   比如小红书没有"字幕"概念、抖音的评论接口要授权。
   上层据此决定降级策略，而不是猜。

3. **不提供"绕过风控"的入口**
   抽象层里**没有**任何签名伪造 / 指纹伪造 / 代理轮换的位置。
   接新平台只能通过**该平台的官方授权通道**。

## 用法

```python
adapter = get_adapter("bilibili", reader=reader)
if adapter.capabilities().subtitles:
    metas = await adapter.list_subtitles(bvid)
```

:func:`get_adapter` 对未注册平台**抛错**（不是返回 None）——
让"用错了平台"立刻暴露。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, NoReturn, Protocol, runtime_checkable

#: 已注册的平台 id
PLATFORM_BILIBILI = "bilibili"
PLATFORM_DOUYIN = "douyin"
PLATFORM_XIAOHONGSHU = "xiaohongshu"

#: 已知平台（含**尚未实现**的）—— 用于给出人话错误
KNOWN_PLATFORMS: tuple[str, ...] = (PLATFORM_BILIBILI, PLATFORM_DOUYIN, PLATFORM_XIAOHONGSHU)


class PlatformError(RuntimeError):
    """平台层错误基类。``code`` 是稳定语义码。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


class CapabilityNotImplemented(PlatformError):
    """**该平台还没接**（或该能力它没有）。

    ★ 刻意与"读到了空结果"区分开：

    * 返回空列表 ⇒ 上层会理解成"这个平台有这能力，只是这次没内容"；
    * 抛这个错 ⇒ 上层明确知道"**能力不存在**"，可以走完全不同的降级路径。

    真机教训（来自本项目其他模块）：把"没有"和"坏了"混同，
    会导致无意义的重试与调试（有项目因此烧掉 27 次工具调用）。
    """


class UnsupportedPlatform(PlatformError):
    """平台 id 不认识（拼错了 / 还没规划）。"""


@dataclass(frozen=True)
class PlatformCapabilities:
    """**该平台支持什么**（显式声明，不靠猜）。

    上层据此决定降级策略。例如小红书没有字幕 ⇒
    ``subtitles=False``，那就不该走"取字幕 → 失败 → 记 SUBTITLE_UNAVAILABLE"
    那条路，而是直接走图文笔记的正文。
    """

    #: 能否按关键词搜索
    search: bool = False
    #: 能否取单条内容详情
    detail: bool = False
    #: 有没有"字幕"这个概念
    subtitles: bool = False
    #: 能否取评论
    comments: bool = False
    #: 能否取楼中楼
    comment_replies: bool = False
    #: 能否发顶层评论（**公开写入**）
    publish_comment: bool = False
    #: 能否回复评论（**公开写入**）
    publish_reply: bool = False
    #: 能否点赞（**公开写入**）
    like: bool = False
    #: 能否收藏（**公开写入**）
    favorite: bool = False
    #: 有没有"推荐流"
    recommendation_feed: bool = False
    #: 备注（人话，说明限制）
    note: str = ""
    #: 接入方式（``official_api`` / ``web_readonly`` / ``none``）
    access_mode: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "search": self.search,
            "detail": self.detail,
            "subtitles": self.subtitles,
            "comments": self.comments,
            "comment_replies": self.comment_replies,
            "publish_comment": self.publish_comment,
            "publish_reply": self.publish_reply,
            "like": self.like,
            "favorite": self.favorite,
            "recommendation_feed": self.recommendation_feed,
            "note": self.note,
            "access_mode": self.access_mode,
        }


@dataclass(frozen=True)
class ContentRef:
    """跨平台的内容标识（不同平台的 id 形状不同，统一包一层）。"""

    platform: str
    #: 平台内的主键（B 站是 BV 号；抖音是 aweme_id；小红书是 note_id）
    native_id: str
    #: 可读链接
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"platform": self.platform, "native_id": self.native_id, "url": self.url}


@runtime_checkable
class PlatformAdapter(Protocol):
    """一个平台要实现的接口。

    ⚠️ **只读方法与写入方法分开**：写入方法在上层要过发布门禁
    （见 :mod:`core.publish`）—— adapter 自己**不判断该不该发**。
    """

    #: 平台 id
    platform_id: str

    def capabilities(self) -> PlatformCapabilities:
        """该平台支持什么。**必须显式声明**。"""
        ...

    async def search(
        self, keyword: str, *, days: int | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """按关键词搜索，返回标准化的候选列表。"""
        ...

    async def get_detail(self, native_id: str) -> dict[str, Any]:
        """取单条内容详情。"""
        ...

    async def list_subtitles(self, native_id: str) -> list[dict[str, Any]]:
        """列出可用字幕（没有这能力的平台应抛 :class:`CapabilityNotImplemented`）。"""
        ...

    async def get_comments(self, native_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """取评论。"""
        ...


# ---------------------------------------------------------------------------
# 已实现：B 站（薄适配器，**不重复实现逻辑**，只做形状转换）
# ---------------------------------------------------------------------------


@dataclass
class BilibiliAdapter:
    """B 站适配器 —— 把现有 :class:`core.bilibili.BilibiliReader` 包成统一形状。

    ★ 它**不复制**任何读取逻辑：所有请求仍走 reader（进而走统一请求队列）。
    这样"多平台"不会绕开既有的限速 / 缓存 / 健康门禁。
    """

    reader: Any
    platform_id: str = PLATFORM_BILIBILI

    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            search=True,
            detail=True,
            subtitles=True,
            comments=True,
            comment_replies=True,
            publish_comment=True,
            publish_reply=True,
            like=True,
            favorite=True,
            recommendation_feed=True,
            access_mode="web_readonly",
            note=(
                "B 站走的是网页只读接口（含 WBI 签名）。"
                "⚠️ 用户协议 §5.6(3) 禁止未经书面许可用自动程序获取内容 ——"
                "「能运行」不等于「已许可」。"
            ),
        )

    async def search(
        self, keyword: str, *, days: int | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        from core.bilibili import BilibiliReadError
        from core.storage import now_ts

        begin = None if days is None else now_ts() - int(days) * 86400
        metas = await self.reader.search_videos(
            keyword, order="pubdate", page=1, page_size=limit, pubtime_begin=begin
        )
        _ = BilibiliReadError  # 保持 import 语义清晰（异常由 reader 抛）
        return [
            {
                "platform": self.platform_id,
                "native_id": meta.bvid,
                "title": meta.title,
                "author": meta.up_name,
                "author_id": meta.up_mid,
                "published_at": meta.published_at,
                "duration_sec": meta.duration_sec,
                "url": meta.source_url,
            }
            for meta in metas
        ]

    async def get_detail(self, native_id: str) -> dict[str, Any]:
        detail = await self.reader.get_video_detail(native_id)
        return {
            "platform": self.platform_id,
            "native_id": detail.bvid,
            "title": detail.title,
            "description": detail.description,
            "author": detail.up_name,
            "author_id": detail.up_mid,
            "published_at": detail.published_at,
            "duration_sec": detail.duration_sec,
            "url": detail.source_url,
        }

    async def list_subtitles(self, native_id: str) -> list[dict[str, Any]]:
        detail = await self.reader.get_video_detail(native_id)
        metas = await self.reader.get_subtitles(native_id, detail.cid)
        return [{"language": m.lan, "label": m.lan_doc, "url": m.url} for m in metas]

    async def get_comments(self, native_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        detail = await self.reader.get_video_detail(native_id)
        items = await self.reader.get_comments(detail.aid, limit=limit)
        return [
            {
                "platform": self.platform_id,
                "id": item.rpid,
                "author": item.author_name,
                "author_id": item.author_mid,
                "text": item.message,
                "like_count": item.like_count,
                "created_at": item.created_at,
            }
            for item in items
        ]


# ---------------------------------------------------------------------------
# 未实现：抖音 / 小红书（**只预留框架**）
# ---------------------------------------------------------------------------


@dataclass
class _UnimplementedAdapter:
    """尚未接入的平台 —— **所有方法都明确拒绝**。

    ★★ 这是本模块最重要的设计：

    它**绝不**返回空列表。因为空列表会被上层理解成
    "平台有这能力，只是这次没内容"，从而走进完全错误的降级路径
    （重试、记 `NO_CONTENT`、甚至误判成"今天没值得看的"）。

    抛错则让上层立刻知道"**这平台还没接**"，并给出可执行的下一步。
    """

    platform_id: str
    reason: str
    planned_access_mode: str = "official_api"
    capabilities_declared: PlatformCapabilities = field(
        default_factory=lambda: PlatformCapabilities(
            access_mode="none", note="尚未接入"
        )
    )

    def capabilities(self) -> PlatformCapabilities:
        return self.capabilities_declared

    def _reject(self, what: str) -> NoReturn:
        raise CapabilityNotImplemented(
            "NOT_IMPLEMENTED",
            f"{self.platform_id} 的「{what}」还没接。原因：{self.reason}。"
            f"规划中的接入方式：{self.planned_access_mode}"
            "（**只走平台官方授权通道**，不做签名逆向、不刷推荐流）。",
        )

    # ⚠️ 下面的参数**故意不用** —— 占位 adapter 的职责就是"明确拒绝"，
    #    而不是真去搜索。保留签名是为了与 :class:`PlatformAdapter` 一致。
    async def search(  # noqa: ARG002
        self,
        keyword: str,  # noqa: ARG002
        *,
        days: int | None = None,  # noqa: ARG002
        limit: int = 20,  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        self._reject("搜索")

    async def get_detail(self, native_id: str) -> dict[str, Any]:  # noqa: ARG002
        self._reject("取详情")

    async def list_subtitles(self, native_id: str) -> list[dict[str, Any]]:  # noqa: ARG002
        self._reject("字幕")

    async def get_comments(  # noqa: ARG002
        self,
        native_id: str,  # noqa: ARG002
        *,
        limit: int = 20,  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        self._reject("评论")


def _douyin_placeholder() -> _UnimplementedAdapter:
    return _UnimplementedAdapter(
        platform_id=PLATFORM_DOUYIN,
        reason=(
            "待 B 站基本完成后接入。抖音的 web 接口需要**三件套签名**"
            "（a_bogus + timestamp + x-secsdk-web-signature，"
            "其中 secsdk 是 177KB 混淆运行时），"
            "**逆向签名属于绕过平台技术保护措施**，本项目不做"
        ),
        planned_access_mode="official_api",
    )


def _xiaohongshu_placeholder() -> _UnimplementedAdapter:
    return _UnimplementedAdapter(
        platform_id=PLATFORM_XIAOHONGSHU,
        reason=(
            "待 B 站基本完成后接入。小红书没有公开的只读 API，"
            "且已有生效判决认定「绕开技术保护措施抓取」违法"
            "（常州天宁法院刑事案、上海知产法院判赔 110 万），"
            "**本项目只走官方授权通道**"
        ),
        planned_access_mode="official_api",
    )


def get_adapter(platform: str, *, reader: Any = None) -> Any:
    """按平台 id 取 adapter。

    * ``bilibili`` —— 需要传 ``reader``；
    * ``douyin`` / ``xiaohongshu`` —— 返回**占位 adapter**（调用即明确拒绝）；
    * 其他 —— 抛 :class:`UnsupportedPlatform`。

    ★ 不认识就**抛错**（不是返回 None）：让"平台名写错"立刻暴露，
    而不是走到后面才发现"怎么什么都没读到"。
    """
    key = platform.strip().lower()
    if key == PLATFORM_BILIBILI:
        if reader is None:
            raise PlatformError(
                "READER_REQUIRED",
                "bilibili adapter 需要 reader（否则会绕开统一请求队列）。",
            )
        return BilibiliAdapter(reader=reader)
    if key == PLATFORM_DOUYIN:
        return _douyin_placeholder()
    if key == PLATFORM_XIAOHONGSHU:
        return _xiaohongshu_placeholder()
    raise UnsupportedPlatform(
        "UNKNOWN_PLATFORM",
        f"未知平台 {platform!r}；已规划的有：{'、'.join(KNOWN_PLATFORMS)}",
    )


def platform_overview() -> list[dict[str, Any]]:
    """所有平台的现状一览（给 ``companion_status`` / CLI 用）。"""
    out: list[dict[str, Any]] = []
    for key in KNOWN_PLATFORMS:
        if key == PLATFORM_BILIBILI:
            out.append(
                {
                    "platform": key,
                    "implemented": True,
                    "access_mode": "web_readonly",
                    "capabilities": BilibiliAdapter(reader=None).capabilities().to_dict(),
                }
            )
            continue
        adapter = get_adapter(key)
        out.append(
            {
                "platform": key,
                "implemented": False,
                "access_mode": adapter.planned_access_mode,
                "capabilities": adapter.capabilities().to_dict(),
                "blocked_reason": adapter.reason,
            }
        )
    return out
