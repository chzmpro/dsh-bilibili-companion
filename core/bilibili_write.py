"""真实写入执行器（点赞 / 收藏 / 发布评论 / 回复）。

## 定位

这是 :mod:`core.publish` 的 :class:`~core.publish.ActionExecutor` 的**真实实现**。
只有 9 道门禁全过之后才会被调用；**它自己不判断该不该发**。

## 接口事实（来源：bilibili-API-collect 文档，非代码）

| 动作 | 端点 | 关键参数 |
|---|---|---|
| 点赞视频 | ``POST /x/web-interface/archive/like`` | ``bvid``, ``like=1/2``, ``csrf`` |
| 取消点赞 | 同上 | ``like=2`` |
| 收藏视频 | ``POST /medialist/gateway/coll/resource/deal`` |
  ``rid``(=aid), ``type=2``, ``add_media_ids``, ``del_media_ids``, ``csrf`` |
| 发顶层评论 | ``POST /x/v2/reply/add`` |
  ``type=1``, ``oid``(=aid), ``message``, ``plat=1``, ``csrf`` |
| 回复评论 | 同上 | 另加 ``root``, ``parent`` |
| 发动态 | ``POST /x/dynamic/feed/create/dyn`` | 见 :meth:`HttpWriteExecutor.send_dynamic` |

★ **没有** ``at_name_to_mid`` 之类的"指定 @ 谁"参数 —— 提及靠正文里的
``@昵称`` 文本，B站按昵称匹配，结果体现在读回时的 ``content.members[]``。

## 安全约束

* **只走本模块发写请求**，且**必须**经过 :class:`~core.publish.PublishGate`。
* ``csrf`` 从凭据里取（``bili_jct``），**永不外泄**。
* 写请求**绝不自动重试**：提交后结果不明 → 交给门禁置 ``uncertain``。
* 每次调用前检查有没有 ``csrf``；没有就直接拒绝（不发出半个请求）。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

import httpx

from core.credentials import CookieBundle

API_BASE = "https://api.bilibili.com"

LIKE_PATH = "/x/web-interface/archive/like"
FAV_PATH = "/medialist/gateway/coll/resource/deal"
FAV_FOLDER_ADD_PATH = "/x/v3/fav/folder/add"
FAV_FOLDER_LIST_PATH = "/x/v3/fav/folder/created/list-all"
HAS_LIKE_PATH = "/x/web-interface/archive/has/like"
REPLY_ADD_PATH = "/x/v2/reply/add"
DYNAMIC_CREATE_PATH = "/x/dynamic/feed/create/dyn"

#: 收藏夹内容类型：2 = 视频稿件
FAV_TYPE_VIDEO = 2

#: 评论区类型代码：1 = 视频稿件，17 = 动态（纯文字动态&分享）
COMMENT_TYPE_VIDEO = 1
COMMENT_TYPE_DYNAMIC = 17

#: 单次写请求超时（秒）。写操作宁可超时进 uncertain，也不要长时间挂着。
DEFAULT_WRITE_TIMEOUT = 20.0


class WriteError(RuntimeError):
    """写操作失败。``code`` 是稳定的语义码，不是异常字符串。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


class MissingCsrfError(WriteError):
    """凭据里没有 ``bili_jct`` → 无法构成写请求（**绝不发半个请求**）。"""

    def __init__(self) -> None:
        super().__init__(
            "CSRF_MISSING",
            "凭据缺少 bili_jct（csrf），无法构成写请求。请重新导入完整登录态。",
        )


@dataclass(frozen=True)
class WriteResult:
    """一次写操作的结果。"""

    ok: bool
    code: str
    remote_id: str | None = None
    remote_url: str | None = None
    raw_code: int | None = None
    message: str = ""

    def to_executor_dict(self) -> dict[str, Any]:
        """转成 :class:`core.publish.ActionExecutor` 期望的形状。"""
        return {"remote_rpid": self.remote_id, "remote_url": self.remote_url}


class HttpWriteExecutor:
    """真实 HTTP 写执行器。

    :param cookies: 登录态（含 ``sessdata`` 与 ``bili_jct``）。
    :param client: 可注入的 httpx 客户端（测试用 MockTransport）。

    ⚠️ **不自动重试**：写操作重试 = 可能重复发布。
    """

    def __init__(
        self,
        *,
        cookies: CookieBundle,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_WRITE_TIMEOUT,
    ) -> None:
        if not cookies.has_write_credentials():
            raise MissingCsrfError()
        self.cookies = cookies
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
            },
            trust_env=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---- 内部 --------------------------------------------------------------

    def _csrf(self) -> str:
        token = self.cookies.csrf_token()
        if not token:
            raise MissingCsrfError()
        return token

    async def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        """发一次 POST。**不重试**。"""
        payload = {**data, "csrf": self._csrf()}
        try:
            response = await self._client.post(
                f"{API_BASE}{path}", data=payload, cookies=self.cookies.values
            )
        except httpx.HTTPError as exc:
            raise WriteError(
                "WRITE_NETWORK", f"网络错误（{type(exc).__name__}）：结果未知，不得重发。"
            ) from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise WriteError("WRITE_BAD_JSON", "响应不是合法 JSON：结果未知。") from exc
        if not isinstance(body, dict):
            raise WriteError("WRITE_BAD_SHAPE", "响应结构异常。")
        return body

    @staticmethod
    def _check_code(body: dict[str, Any], *, ok_codes: tuple[int, ...] = (0,)) -> int:
        code = body.get("code")
        try:
            numeric = int(code) if code is not None else -1
        except (TypeError, ValueError):
            numeric = -1
        if numeric not in ok_codes:
            message = str(body.get("message") or body.get("msg") or "")
            raise WriteError(f"BILI_CODE_{numeric}", message or f"接口返回 {numeric}")
        return numeric

    # ---- 点赞 --------------------------------------------------------------

    async def like_video(self, *, bvid: str, undo: bool = False) -> WriteResult:
        """点赞 / 取消点赞。

        文档：``like`` 取 ``1`` 点赞、``2`` 取消赞。
        ``65006`` 表示重复点赞（视为幂等成功）。
        """
        body = await self._post(LIKE_PATH, {"bvid": bvid, "like": 2 if undo else 1})
        # 65006 = 重复点赞 → 已经是想要的终态，按成功处理（幂等）
        self._check_code(body, ok_codes=(0, 65006))
        return WriteResult(
            ok=True,
            code="OK" if not undo else "UNDONE",
            remote_id=bvid,
            remote_url=f"https://www.bilibili.com/video/{bvid}/",
            raw_code=int(body.get("code", 0) or 0),
            message=str(body.get("message") or ""),
        )

    # ---- 收藏 --------------------------------------------------------------

    async def favorite_video(
        self, *, aid: int, add_media_ids: list[int] | None = None,
        del_media_ids: list[int] | None = None,
    ) -> WriteResult:
        """收藏 / 取消收藏。

        ``add_media_ids`` 与 ``del_media_ids`` 至少给一个；用 ``,`` 连接。
        """
        add = ",".join(str(i) for i in (add_media_ids or []))
        remove = ",".join(str(i) for i in (del_media_ids or []))
        if not add and not remove:
            raise WriteError("FAV_NO_TARGET", "必须给出 add_media_ids 或 del_media_ids。")
        body = await self._post(
            FAV_PATH,
            {
                "rid": aid,
                "type": FAV_TYPE_VIDEO,
                "add_media_ids": add,
                "del_media_ids": remove,
                "platform": "web",
            },
        )
        self._check_code(body)
        return WriteResult(
            ok=True,
            code="OK",
            remote_id=str(aid),
            remote_url=f"https://www.bilibili.com/video/av{aid}/",
            raw_code=0,
            message=str(body.get("message") or ""),
        )

    async def list_favorite_folders(self, *, up_mid: int) -> list[dict[str, Any]]:
        """列出小号自己的收藏夹（只读，用于把名字映射成 media_id）。"""
        try:
            response = await self._client.get(
                f"{API_BASE}/x/v3/fav/folder/created/list-all",
                params={"up_mid": up_mid},
                cookies=self.cookies.values,
            )
        except httpx.HTTPError as exc:
            raise WriteError("FAV_LIST_NETWORK", type(exc).__name__) from exc
        body = response.json()
        if not isinstance(body, dict):
            raise WriteError("FAV_LIST_SHAPE", "收藏夹列表结构异常。")
        data = body.get("data") or {}
        raw = data.get("list") if isinstance(data, dict) else None
        return [item for item in (raw or []) if isinstance(item, dict)]

    async def create_favorite_folder(
        self, *, title: str, intro: str = "", private: bool = False
    ) -> dict[str, Any]:
        """新建一个收藏夹。返回 B站 给的元数据（含 ``id`` = media_id）。

        文档：``POST /x/v3/fav/folder/add``，参数 ``title``（必要）、
        ``intro``、``privacy``（0 公开 / 1 私密）、``csrf``。
        """
        name = title.strip()
        if not name:
            raise WriteError("FOLDER_TITLE_EMPTY", "收藏夹标题不能为空。")
        body = await self._post(
            FAV_FOLDER_ADD_PATH,
            {"title": name, "intro": intro, "privacy": 1 if private else 0},
        )
        self._check_code(body)
        data = body.get("data")
        if not isinstance(data, dict):
            raise WriteError("FOLDER_ADD_SHAPE", "建收藏夹返回结构异常。")
        return data

    async def ensure_favorite_folder(
        self, *, title: str, intro: str = "", private: bool = False
    ) -> dict[str, Any]:
        """确保存在名为 ``title`` 的收藏夹；已存在则直接返回它（**幂等**）。

        先列出现有收藏夹做精确匹配，避免重复创建同名夹子。
        """
        want = title.strip()
        existing = await self.list_favorite_folders(up_mid=self._self_mid())
        for folder in existing:
            if str(folder.get("title", "") or "").strip() == want:
                return folder
        return await self.create_favorite_folder(
            title=want, intro=intro, private=private
        )

    def _self_mid(self) -> int:
        """从凭据的 ``dedeuserid`` 取自己的 UID（建/列收藏夹需要 up_mid）。"""
        for key, value in self.cookies.values.items():
            if key.lower() == "dedeuserid":
                try:
                    return int(value)
                except (TypeError, ValueError):
                    break
        raise WriteError(
            "SELF_MID_UNKNOWN",
            "凭据里没有可解析的 dedeuserid，无法确定自己的 UID。",
        )

    async def has_liked(self, *, bvid: str) -> bool | None:
        """查这个视频**近期**是否已点赞。

        ⚠️ 文档明确：该接口只能判断"**近期**"，半年前点过的会返回未赞。
        因此它**只作为参考**（避免明显重复点赞），
        权威状态仍以本项目 ``video_states`` 表为准。
        返回 ``None`` 表示查询失败（不影响主流程）。
        """
        try:
            response = await self._client.get(
                f"{API_BASE}{HAS_LIKE_PATH}",
                params={"bvid": bvid},
                cookies=self.cookies.values,
            )
            body = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(body, dict):
            return None
        code = body.get("code")
        if code is None or int(str(code)) != 0:
            return None
        data = body.get("data")
        if data is None:
            return None
        # ★ 实测：``data`` 就是裸的 0/1（不是对象）。
        try:
            return int(str(data)) == 1
        except (TypeError, ValueError):
            return None

    # ---- 评论 --------------------------------------------------------------

    async def send_top_comment(self, *, aid: int, bvid: str, text: str) -> dict[str, Any]:
        """发顶层评论。返回 ``{remote_rpid, remote_url}``（门禁期望的形状）。"""
        result = await self._add_reply(aid=aid, message=text, bvid=bvid)
        return result.to_executor_dict()

    async def send_reply(
        self, *, aid: int, bvid: str, root: str, parent: str, text: str
    ) -> dict[str, Any]:
        """回复评论（二级及以上）。"""
        result = await self._add_reply(
            aid=aid, message=text, root=root, parent=parent, bvid=bvid
        )
        return result.to_executor_dict()

    async def _add_reply(
        self, *,
        aid: int,
        message: str,
        root: str = "",
        parent: str = "",
        bvid: str = "",
        comment_type: int = COMMENT_TYPE_VIDEO,
    ) -> WriteResult:
        data: dict[str, Any] = {
            "type": comment_type, "oid": aid, "message": message, "plat": 1
        }
        if root:
            data["root"] = root
        if parent:
            data["parent"] = parent

        body = await self._post(REPLY_ADD_PATH, data)
        self._check_code(body)

        inner = body.get("data") or {}
        rpid = ""
        if isinstance(inner, dict):
            rpid = str(inner.get("rpid_str") or inner.get("rpid") or "")
        if not rpid:
            # ★ 提交了但拿不到 rpid → 结果未知，**不得**当作成功
            raise WriteError(
                "NO_RPID_IN_RESPONSE",
                "接口返回成功但没有 rpid：结果未知，交由门禁置 uncertain，不得重发。",
            )
        return WriteResult(
            ok=True,
            code="OK",
            remote_id=rpid,
            remote_url=(
                f"https://www.bilibili.com/video/{bvid}/#reply{rpid}" if bvid else None
            ),
            raw_code=0,
            message=str(body.get("message") or ""),
        )

    async def send_dynamic_comment(
        self, *, dynamic_id: str, text: str, root: str = "", parent: str = ""
    ) -> WriteResult:
        """在一条**动态**下评论（可用于 @ 主人）。

        与视频评论的区别只有一个：``type=17``（动态）而不是 ``1``（视频），
        且 ``oid`` 是**动态 id**。

        提及同样靠正文里的 ``@昵称``；读回时看 ``content.members[]`` 核验。
        """
        try:
            oid = int(dynamic_id)
        except (TypeError, ValueError) as exc:
            raise WriteError("DYNAMIC_ID_INVALID", f"动态 id 必须是数字：{dynamic_id!r}") from exc
        return await self._add_reply(
            aid=oid, message=text, root=root, parent=parent,
            comment_type=COMMENT_TYPE_DYNAMIC,
        )

    async def read_dynamic_comments(
        self, *, dynamic_id: str, page_size: int = 20
    ) -> list[dict[str, Any]]:
        """读一条动态下的评论（核验 @ 用）。

        返回原始 ``replies`` 列表；每个元素的 ``content.members[]``
        就是"这条评论 @ 到了谁"的**服务端事实**。
        """
        try:
            oid = int(dynamic_id)
        except (TypeError, ValueError) as exc:
            raise WriteError("DYNAMIC_ID_INVALID", f"动态 id 必须是数字：{dynamic_id!r}") from exc
        try:
            response = await self._client.get(
                f"{API_BASE}/x/v2/reply",
                params={"type": COMMENT_TYPE_DYNAMIC, "oid": oid, "pn": 1, "ps": page_size,
                        "sort": 2},
                cookies=self.cookies.values,
            )
        except httpx.HTTPError as exc:
            raise WriteError("READBACK_NETWORK", type(exc).__name__) from exc
        body = response.json()
        if not isinstance(body, dict):
            raise WriteError("READBACK_SHAPE", "评论列表结构异常。")
        data = body.get("data") or {}
        replies = data.get("replies") if isinstance(data, dict) else None
        return [r for r in (replies or []) if isinstance(r, dict)]

    async def read_comment_at_members(
        self, *, dynamic_id: str, rpid: str
    ) -> list[int]:
        """读回**某条动态评论**实际 @ 到的 UID 列表（核验用）。

        ★ 以服务端 ``content.members[].mid`` 为准，不看正文字符。
        """
        target = str(rpid)
        for reply in await self.read_dynamic_comments(dynamic_id=dynamic_id):
            if str(reply.get("rpid_str") or reply.get("rpid")) != target:
                continue
            content = reply.get("content") or {}
            members = content.get("members") or []
            uids: list[int] = []
            for member in members:
                if not isinstance(member, dict):
                    continue
                # ★ 实测：B站 这里返回的 mid 是**字符串**（如 "660490601"），
                #   不是数字。直接比较会永远不相等 —— 必须转 int。
                try:
                    mid = int(str(member.get("mid", "") or "0"))
                except (TypeError, ValueError):
                    continue
                if mid > 0:
                    uids.append(mid)
            return uids
        raise WriteError("READBACK_NOT_FOUND", f"评论列表里没找到 {rpid}。")

    # ---- 动态（含 @ 提及）--------------------------------------------------

    async def send_dynamic(
        self, *, content: str, at_uids: list[int] | None = None
    ) -> WriteResult:
        """发一条纯文字动态，可在正文里 **@ 指定用户**。

        ## ★ @ 的正确构造方式（实测文档，非猜测）

        动态正文是一个**富文本节点数组**；``@某人`` 是其中一个**独立节点**：

        | 字段 | 值 |
        |---|---|
        | ``type`` | ``2`` = ``RICH_TEXT_NODE_TYPE_AT`` |
        | ``biz_id`` | **被 @ 的 UID** |
        | ``raw_text`` | 显示文本（如 ``@沐雨林心 ``） |

        普通文字节点则是 ``type=1``、``biz_id=""``。

        ⚠️ 我最初以为有个 ``at_uids`` 参数 —— **那是错的**，文档里没有这个字段。
        正确做法是把 @ 拆成一个 ``type=2`` 的节点放进 ``contents`` 数组。

        读回时对应 ``rich_text_nodes[].rid``（= UID），见 :meth:`read_dynamic_at_uids`。
        """
        contents: list[dict[str, Any]] = []
        if content:
            contents.append({"raw_text": content, "type": 1, "biz_id": ""})
        for uid in at_uids or []:
            if uid <= 0:
                continue
            # @ 节点：type=2，biz_id 放 UID。raw_text 由服务端渲染成 @昵称
            contents.append({"raw_text": f"@{uid} ", "type": 2, "biz_id": str(uid)})

        if not contents:
            raise WriteError("DYNAMIC_EMPTY", "动态内容不能为空。")

        payload: dict[str, Any] = {
            "dyn_req": {
                "content": {"contents": contents},
                "scene": 1,
                "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
            }
        }

        try:
            response = await self._client.post(
                f"{API_BASE}{DYNAMIC_CREATE_PATH}",
                params={"csrf": self._csrf(), "platform": "web"},
                json=payload,
                cookies=self.cookies.values,
            )
        except httpx.HTTPError as exc:
            raise WriteError(
                "DYNAMIC_NETWORK", f"网络错误（{type(exc).__name__}）：结果未知，不得重发。"
            ) from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise WriteError("DYNAMIC_BAD_JSON", "响应不是合法 JSON。") from exc
        if not isinstance(body, dict):
            raise WriteError("DYNAMIC_BAD_SHAPE", "响应结构异常。")
        self._check_code(body)

        dynamic_id = ""
        inner = body.get("data") or {}
        if isinstance(inner, dict):
            dynamic_id = str(
                inner.get("dyn_id_str")
                or inner.get("dynamic_id_str")
                or inner.get("dyn_id")
                or inner.get("dynamic_id")
                or ""
            )
        return WriteResult(
            ok=True,
            code="OK",
            remote_id=dynamic_id or None,
            remote_url=f"https://t.bilibili.com/{dynamic_id}" if dynamic_id else None,
            raw_code=0,
            message=str(body.get("message") or ""),
        )

    # ---- 读回核验 ----------------------------------------------------------

    async def read_dynamic_at_uids(self, *, dynamic_id: str, host_mid: int) -> list[int]:
        """读回一条动态**实际 @ 到**的 UID 列表（核验用）。

        与评论的 ``content.members`` 同理：以**服务端返回**为准，不看正文字符。
        """
        try:
            response = await self._client.get(
                f"{API_BASE}/x/polymer/web-dynamic/v1/feed/space",
                params={"host_mid": host_mid},
                cookies=self.cookies.values,
            )
        except httpx.HTTPError as exc:
            raise WriteError("READBACK_NETWORK", type(exc).__name__) from exc
        body = response.json()
        if not isinstance(body, dict):
            raise WriteError("READBACK_SHAPE", "动态列表结构异常。")
        items = ((body.get("data") or {}).get("items")) or []
        for item in items:
            if str(item.get("id_str")) != str(dynamic_id):
                continue
            modules = item.get("modules") or {}
            desc = ((modules.get("module_dynamic") or {}).get("desc")) or {}
            nodes = desc.get("rich_text_nodes") or []
            uids: list[int] = []
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                if node.get("type") != "RICH_TEXT_NODE_TYPE_AT":
                    continue
                try:
                    uids.append(int(node.get("rid", 0) or 0))
                except (TypeError, ValueError):
                    continue
            return [u for u in uids if u > 0]
        raise WriteError("READBACK_NOT_FOUND", f"动态列表里没找到 {dynamic_id}。")


def make_idempotency_fingerprint(*parts: Any) -> str:
    """给写操作算一个指纹（用于日志/诊断，**不替代**门禁的幂等键）。"""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def now_seconds() -> int:
    return int(time.time())
