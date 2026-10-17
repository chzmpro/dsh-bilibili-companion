"""B 站读取适配器（**唯一**的读取实现，零 GPL）。

对齐任务书 §4 的读取类工具：

===================  =========================================
工具                 本模块对应函数
===================  =========================================
``discover_videos``  :func:`search_videos`
（读取层）            :func:`get_video_detail`
``get_discussion``   :func:`get_comments`
（字幕）              :func:`get_subtitles` / :func:`fetch_subtitle_body`
（身份核对）          :func:`get_nav`
===================  =========================================

**所有请求都经 :class:`core.ratelimit.RequestQueue`**，不自己发 HTTP。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from core import health
from core.ratelimit import RateLimitExceeded, RequestQueue, RequestsBlocked
from core.wbi import WbiError, WbiSigner, extract_key_from_url

API_BASE = "https://api.bilibili.com"

NAV_PATH = "/x/web-interface/nav"  # 不需要签名
SEARCH_PATH = "/x/web-interface/wbi/search/type"  # 需要签名
VIEW_PATH = "/x/web-interface/view"  # 免签（实测）
PLAYER_PATH = "/x/player/wbi/v2"  # 需要签名 + 需登录
REPLY_MAIN_PATH = "/x/v2/reply/wbi/main"  # 需要签名
REPLY_REPLY_PATH = "/x/v2/reply/reply"  # 楼中楼

_EM_TAG = re.compile(r"</?em[^>]*>")


class BilibiliReadError(RuntimeError):
    """读取失败。``code`` 为语义分类，不是异常字符串。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class NavInfo:
    """``/x/web-interface/nav`` 的关键字段。**不含凭据。**"""

    is_login: bool
    uname: str = ""
    mid: int = 0
    wbi_img_key: str = ""
    wbi_sub_key: str = ""

    @property
    def has_wbi_keys(self) -> bool:
        return bool(self.wbi_img_key and self.wbi_sub_key)


@dataclass(frozen=True)
class VideoMeta:
    """搜索结果里的候选视频（标准化后）。"""

    bvid: str
    aid: int
    title: str
    up_mid: int
    up_name: str
    published_at: int
    duration_sec: int
    source_url: str
    description: str = ""
    cover_url: str = ""


@dataclass(frozen=True)
class VideoDetail:
    bvid: str
    aid: int
    cid: int
    title: str
    description: str
    up_mid: int
    up_name: str
    published_at: int
    duration_sec: int
    reply_count: int
    pages: int = 1
    source_url: str = ""


@dataclass(frozen=True)
class SubtitleMeta:
    lan: str
    lan_doc: str
    url: str


@dataclass(frozen=True)
class SubtitleSegment:
    start_sec: float
    end_sec: float
    text: str


@dataclass(frozen=True)
class SubtitleBody:
    language: str
    label: str
    segments: list[SubtitleSegment] = field(default_factory=list)

    def full_text(self) -> str:
        return "".join(seg.text for seg in self.segments)


@dataclass(frozen=True)
class AtMember:
    """一条评论里**实际 @ 到**的用户（B站服务端给出的权威结果）。

    来源：评论对象的 ``content.members[]``（见 API 文档
    ``comment/readme.md`` 的「content 对象」表：``members | array | at 到的用户信息``）。

    ★ 这是我方 ``@`` 核验的关键：**不需要浏览器**，
    只要读回评论就能知道"那个 @ 到底指向谁"。
    """

    mid: int
    uname: str


@dataclass(frozen=True)
class CommentItem:
    rpid: str
    root: str
    parent: str
    author_mid: int
    author_name: str
    message: str
    like_count: int
    created_at: int
    reply_count: int = 0
    #: 本评论 @ 到的用户（可能为空）
    at_members: tuple[AtMember, ...] = ()

    def mentions(self, mid: int) -> bool:
        """本条评论是否确实 @ 到了指定 UID。"""
        return any(m.mid == mid for m in self.at_members)


@dataclass(frozen=True)
class CommentPage:
    replies: list[CommentItem] = field(default_factory=list)
    next_cursor: int = 0
    is_end: bool = True
    total: int = 0


class BilibiliReader:
    """只读适配器。

    身份与签名 key 都通过同一 :class:`RequestQueue` 获取，
    因此**自动**享受缓存/限速/健康门禁。
    """

    def __init__(self, *, queue: RequestQueue, cookies: dict[str, str] | None = None) -> None:
        self.queue = queue
        self.cookies = cookies or {}
        self._signer = WbiSigner()

    # ---- 身份与签名 --------------------------------------------------------

    async def get_nav(self) -> NavInfo:
        """校验登录态并顺带取 WBI keys。

        ``nav`` **不需要签名**（否则取签名 key 会死循环）。
        **不把凭据写进返回。**
        """
        outcome = await self.queue.get_json(
            f"{API_BASE}{NAV_PATH}",
            kind="metadata",
            use_cache=False,
            cookies=self.cookies or None,
        )
        if outcome.data is None:
            code = (
                outcome.health_state.upper()
                if outcome.health_state != health.READY
                else "NAV_FAILED"
            )
            raise BilibiliReadError(code, outcome.message)
        data = outcome.data if isinstance(outcome.data, dict) else {}
        wbi = data.get("wbi_img") or {}
        img_url = str(wbi.get("img_url", "") or "")
        sub_url = str(wbi.get("sub_url", "") or "")
        info = NavInfo(
            is_login=bool(data.get("isLogin")),
            uname=str(data.get("uname", "") or ""),
            mid=int(data.get("mid", 0) or 0),
            wbi_img_key=extract_key_from_url(img_url) if img_url else "",
            wbi_sub_key=extract_key_from_url(sub_url) if sub_url else "",
        )
        if info.has_wbi_keys:
            self._signer.set_keys(info.wbi_img_key, info.wbi_sub_key)
        return info

    async def _signed_kwargs(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._signer.cached_mixin is None or self._signer.is_expired():
            nav = await self.get_nav()
            if not nav.has_wbi_keys:
                raise WbiError("nav 未返回 WBI keys，无法签名")
        return dict(self._signer.sign_dict(params))

    async def _get_signed(
        self,
        url: str,
        params: dict[str, Any],
        *,
        kind: str = "metadata",
        headers: dict[str, str] | None = None,
        use_cache: bool = True,
    ) -> Any:
        """签名 GET。``-403``（签名 key 过期）时刷新 key **重试一次**。"""
        signed = await self._signed_kwargs(params)
        outcome = await self.queue.get_json(
            url, params=signed, kind=kind, cookies=self.cookies or None,
            headers=headers, use_cache=use_cache,
        )
        if outcome.code == -403 and self._signer.cached_mixin is not None:
            self._signer.invalidate()
            signed = await self._signed_kwargs(params)
            outcome = await self.queue.get_json(
                url, params=signed, kind=kind, cookies=self.cookies or None,
                headers=headers, use_cache=use_cache,
            )
        if outcome.data is None:
            raise BilibiliReadError(
                _code_for(outcome.code, outcome.health_state), outcome.message
            )
        return outcome.data

    # ---- 发现（只读）------------------------------------------------------

    async def search_videos(
        self,
        keyword: str,
        *,
        order: str = "pubdate",
        page: int = 1,
        page_size: int = 20,
        pubtime_begin: int | None = None,
        pubtime_end: int | None = None,
    ) -> list[VideoMeta]:
        """按关键词搜索（默认按发布时间）。"""
        params: dict[str, Any] = {
            "search_type": "video",
            "keyword": keyword,
            "order": order,
            "page": page,
            "page_size": page_size,
        }
        if pubtime_begin is not None:
            params["pubtime_begin_s"] = pubtime_begin
        if pubtime_end is not None:
            params["pubtime_end_s"] = pubtime_end

        # ★ HTTP 头必须是 ASCII：中文关键词必须先 URL 编码。
        #   （实测：直接拼中文 keyword 会让 httpx 抛
        #    UnicodeEncodeError: 'ascii' codec can't encode ...）
        data = await self._get_signed(
            f"{API_BASE}{SEARCH_PATH}",
            params,
            kind="search",
            headers={
                "Referer": _search_referer(keyword),
                "Origin": "https://search.bilibili.com",
            },
        )
        results = data.get("result") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return []
        return [meta for item in results if (meta := _parse_search_item(item)) is not None]

    async def get_video_detail(self, bvid: str) -> VideoDetail:
        """取视频详情（``cid`` + ``stat.reply``）。

        ``view`` 实测**免 WBI 签名**；``duration`` 单位是**秒**。
        """
        outcome = await self.queue.get_json(
            f"{API_BASE}{VIEW_PATH}",
            params={"bvid": bvid},
            kind="metadata",
            cookies=self.cookies or None,
        )
        if outcome.data is None or not isinstance(outcome.data, dict):
            raise BilibiliReadError(_code_for(outcome.code, outcome.health_state), outcome.message)
        return _parse_video_detail(outcome.data, bvid)

    # ---- 字幕（只读，需登录）-----------------------------------------------

    async def get_subtitles(self, bvid: str, cid: int) -> list[SubtitleMeta]:
        """取字幕列表（``/x/player/wbi/v2``，**需登录 + 签名**）。

        实测：匿名拿不到字幕列表。
        """
        data = await self._get_signed(
            f"{API_BASE}{PLAYER_PATH}", {"bvid": bvid, "cid": cid}, kind="metadata"
        )
        subtitle = data.get("subtitle") if isinstance(data, dict) else None
        if not isinstance(subtitle, dict):
            return []
        items = subtitle.get("subtitles")
        if not isinstance(items, list):
            return []
        result: list[SubtitleMeta] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("subtitle_url", "") or "")
            if url.startswith("//"):
                url = f"https:{url}"
            if not url:
                continue
            result.append(
                SubtitleMeta(
                    lan=str(item.get("lan", "")),
                    lan_doc=str(item.get("lan_doc", "")),
                    url=url,
                )
            )
        return result

    async def fetch_subtitle_body(self, url: str) -> SubtitleBody:
        """下载字幕正文（JSON 数组，带 ``from``/``to``/``content``）。"""
        outcome = await self.queue.get_json(
            url, kind="subtitle", cookies=self.cookies or None
        )
        payload = outcome.data
        if payload is None:
            raise BilibiliReadError("SUBTITLE_UNAVAILABLE", outcome.message)
        body = payload.get("body") if isinstance(payload, dict) else payload
        if not isinstance(body, list):
            raise BilibiliReadError("SUBTITLE_MALFORMED", "字幕结构不是列表")
        segments: list[SubtitleSegment] = []
        for entry in body:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("content", "") or "")
            if not text:
                continue
            segments.append(
                SubtitleSegment(
                    start_sec=float(entry.get("from", 0.0) or 0.0),
                    end_sec=float(entry.get("to", 0.0) or 0.0),
                    text=text,
                )
            )
        return SubtitleBody(language="", label="", segments=segments)

    # ---- 评论（只读）------------------------------------------------------

    async def get_comments(
        self,
        aid: int,
        *,
        mode: int = 2,
        next_cursor: int = 0,
        page_size: int = 20,
    ) -> CommentPage:
        """读一页顶层评论。注意入参是 **aid 不是 bvid**。"""
        params: dict[str, Any] = {
            "type": 1,
            "oid": aid,
            "mode": mode,
            "next": next_cursor,
            "ps": page_size,
            "web_location": "1315875",
        }
        data = await self._get_signed(f"{API_BASE}{REPLY_MAIN_PATH}", params, kind="comment")
        return _parse_comment_page(data)

    async def get_comment_replies(
        self, aid: int, root: str, *, page: int = 1, page_size: int = 20
    ) -> CommentPage:
        """读楼中楼（``/x/v2/reply/reply``）。"""
        params: dict[str, Any] = {
            "type": 1,
            "oid": aid,
            "root": root,
            "pn": page,
            "ps": page_size,
        }
        data = await self._get_signed(f"{API_BASE}{REPLY_REPLY_PATH}", params, kind="comment")
        return _parse_comment_page(data)


# ---- 解析辅助（纯函数，便于单测）-------------------------------------------


def _search_referer(keyword: str) -> str:
    """构造搜索页 Referer。

    **必须**对关键词做 URL 编码：HTTP 头只允许 ASCII，
    中文关键词直接拼进去会让 httpx 在编码头值时抛 UnicodeEncodeError。
    """
    return f"https://search.bilibili.com/all?keyword={quote(keyword, safe='')}"


def _code_for(code: str | int | None, health_state: str) -> str:
    if health_state != health.READY:
        return health_state.upper()
    if code is None:
        return "READ_FAILED"
    return f"BILI_CODE_{code}"


def _parse_search_item(item: Any) -> VideoMeta | None:
    if not isinstance(item, dict):
        return None
    bvid = str(item.get("bvid", "") or "")
    if not bvid:
        return None
    title = _EM_TAG.sub("", str(item.get("title", "") or "")).strip()
    return VideoMeta(
        bvid=bvid,
        aid=int(item.get("aid", 0) or 0),
        title=title,
        up_mid=int(item.get("mid", 0) or 0),
        up_name=str(item.get("author", "") or ""),
        published_at=int(item.get("pubdate", 0) or 0),
        duration_sec=_parse_duration(item.get("duration")),
        source_url=f"https://www.bilibili.com/video/{bvid}/",
        description=_EM_TAG.sub("", str(item.get("description", "") or "")).strip(),
        cover_url=_normalize_url(str(item.get("pic", "") or "")),
    )


def _parse_duration(raw: Any) -> int:
    """搜索结果里 ``duration`` 可能是 ``"12:34"`` 字符串，也可能是秒数。"""
    if isinstance(raw, int):
        return raw
    text = str(raw or "").strip()
    if not text:
        return 0
    if ":" in text:
        parts = [int(p) for p in text.split(":") if p.isdigit()]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        return 0
    return int(text) if text.isdigit() else 0


def _normalize_url(url: str) -> str:
    return f"https:{url}" if url.startswith("//") else url


def _parse_video_detail(data: dict[str, Any], bvid: str) -> VideoDetail:
    stat = data.get("stat") or {}
    pages_raw = data.get("pages") or []
    pages = len(pages_raw) if isinstance(pages_raw, list) and pages_raw else 1
    owner = data.get("owner") or {}
    return VideoDetail(
        bvid=str(data.get("bvid", bvid) or bvid),
        aid=int(data.get("aid", 0) or 0),
        cid=int(data.get("cid", 0) or 0),
        title=str(data.get("title", "") or ""),
        description=str(data.get("desc", "") or ""),
        up_mid=int(owner.get("mid", 0) or 0),
        up_name=str(owner.get("name", "") or ""),
        published_at=int(data.get("pubdate", 0) or 0),
        duration_sec=int(data.get("duration", 0) or 0),
        reply_count=int(stat.get("reply", 0) or 0),
        pages=pages,
        source_url=f"https://www.bilibili.com/video/{bvid}/",
    )


def _parse_at_members(content: Any) -> tuple[AtMember, ...]:
    """解析 ``content.members[]`` —— 该评论**实际 @ 到**的用户。

    B站把"这条评论 @ 了谁"作为**服务端事实**放在这里，
    与正文里的 ``@昵称`` 文本是两回事（文本可能只是普通文字）。
    因此核验提及必须看这个字段，而不是看正文。
    """
    if not isinstance(content, dict):
        return ()
    raw = content.get("members")
    if not isinstance(raw, list):
        return ()
    out: list[AtMember] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        # ★ 实测：B站 的 members[].mid 是**字符串**（"660490601"）。
        #   用 int(...) 包一层 str() 才稳（数字形态也兼容）。
        try:
            mid = int(str(entry.get("mid", "") or "0"))
        except (TypeError, ValueError):
            continue
        if mid <= 0:
            continue
        out.append(AtMember(mid=mid, uname=str(entry.get("uname", "") or "")))
    return tuple(out)


def _parse_comment_page(data: Any) -> CommentPage:
    if not isinstance(data, dict):
        return CommentPage()
    cursor = data.get("cursor") or {}
    replies_raw = data.get("replies") or []
    replies: list[CommentItem] = []
    if isinstance(replies_raw, list):
        for item in replies_raw:
            parsed = _parse_comment_item(item)
            if parsed is not None:
                replies.append(parsed)
    return CommentPage(
        replies=replies,
        next_cursor=int(cursor.get("next", 0) or 0),
        is_end=bool(cursor.get("is_end", True)),
        total=int(cursor.get("all_count", 0) or 0),
    )


def _parse_comment_item(item: Any) -> CommentItem | None:
    if not isinstance(item, dict):
        return None
    rpid = str(item.get("rpid_str") or item.get("rpid") or "")
    if not rpid:
        return None
    member = item.get("member") or {}
    content = item.get("content") or {}
    return CommentItem(
        at_members=_parse_at_members(content),
        rpid=rpid,
        root=str(item.get("root_str") or item.get("root") or "0"),
        parent=str(item.get("parent_str") or item.get("parent") or "0"),
        author_mid=int(member.get("mid", 0) or 0),
        author_name=str(member.get("uname", "") or ""),
        message=str(content.get("message", "") or ""),
        like_count=int(item.get("like", 0) or 0),
        created_at=int(item.get("ctime", 0) or 0),
        reply_count=int(item.get("rcount", 0) or 0),
    )


__all__ = [
    "API_BASE",
    "AtMember",
    "BilibiliReadError",
    "BilibiliReader",
    "CommentItem",
    "CommentPage",
    "NavInfo",
    "RateLimitExceeded",
    "RequestsBlocked",
    "SubtitleBody",
    "SubtitleMeta",
    "SubtitleSegment",
    "VideoDetail",
    "VideoMeta",
]
