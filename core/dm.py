"""小号 ↔ 主人的 B 站站内私信（**只通向主人一个人**）。

## 为什么单独一个模块

任务书 §1 把"私信机器人"列为非目标，本项目**不做**给陌生人的私信。
但主人明确要求"回复我发给小号的私信"，于是这里实现一条**结构上只能通向主人**的通道：

* 接收者不是参数，而是从 ``account.owner_mid`` 取的 —— 调用方**无法**指定别人。
* 它有自己的开关 ``interaction.dm_reply_enabled``，与 ``publish_enabled`` **相互独立**：
  打开回复私信**不会**顺带打开视频评论 / 点赞 / 收藏。
* 写请求**绝不自动重试**（重试 = 可能重复发送）。

## 接口事实（来源：bilibili-API-collect ``docs/message/private_msg.md``，非代码）

====================  ==========================================================
动作                  端点
====================  ==========================================================
未读数                ``GET /session_svr/v1/session_svr/single_unread``
消息记录              ``GET /svr_sync/v1/svr_sync/fetch_session_msgs``
发送                  ``POST /web_im/v1/web_im/send_msg``
====================  ==========================================================

私信的 ``content`` 是**嵌套 JSON 字符串**（``{"content": "..."}`` 或分享卡片的
``{"title": ..., "bvid": ...}``），这里统一解开。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from core import health
from core.credentials import CookieBundle
from core.ratelimit import RequestQueue

DM_HOST = "https://api.vc.bilibili.com"
UNREAD_PATH = "/session_svr/v1/session_svr/single_unread"
MSGS_PATH = "/svr_sync/v1/svr_sync/fetch_session_msgs"
SEND_PATH = "/web_im/v1/web_im/send_msg"

#: 私信正文上限（字符）。刻意保守：太长既不像人说话，也更容易被判定为异常。
MAX_DM_CHARS = 500

#: 消息类型：1 文字；7 分享卡片（视频/专栏等）。
MSG_TYPE_TEXT = 1
MSG_TYPE_SHARE = 7

DEFAULT_WRITE_TIMEOUT = 20.0


class DmError(RuntimeError):
    """私信读写失败。``code`` 是稳定的语义码。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class DmMessage:
    """一条私信（**已解开内容**）。"""

    msg_seqno: int
    sender_uid: int
    receiver_id: int
    msg_type: int
    text: str
    timestamp: int
    bvid: str = ""
    title: str = ""
    author: str = ""

    def from_owner(self, owner_mid: int) -> bool:
        return self.sender_uid == owner_mid

    def from_agent(self, agent_mid: int) -> bool:
        return self.sender_uid == agent_mid

    def to_dict(self) -> dict[str, Any]:
        return {
            "msg_seqno": self.msg_seqno,
            "sender_uid": self.sender_uid,
            "msg_type": self.msg_type,
            "text": self.text,
            "timestamp": self.timestamp,
            "bvid": self.bvid,
            "title": self.title,
            "author": self.author,
        }


@dataclass(frozen=True)
class DmSendResult:
    ok: bool
    code: str
    msg_key: str | None = None
    message: str = ""
    #: 是不是"想发卡片但平台不支持，已自动降级成文本"（2026-10-17）。
    #: B站 **web 端** `send_msg` 只吃 `msg_type=1`，卡片一律 `-400`。
    card_dropped: bool = False


def parse_content(raw: Any) -> tuple[str, dict[str, Any]]:
    """解开私信 ``content``。返回 ``(可读文字, 原始对象)``。"""
    obj: Any = raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return raw, {}
    if isinstance(obj, dict):
        for key in ("content", "title", "text"):
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value, obj
        return json.dumps(obj, ensure_ascii=False), obj
    return str(obj), {}


def dm_reply_allowed(config: Any) -> tuple[bool, str]:
    """回复主人私信是否被允许。返回 ``(allowed, reason_code)``。

    **刻意不查** ``mode``/``dry_run``/``publish_enabled``：那些管的是**公开**
    写入（评论/点赞/收藏）。回复主人私信不是公开动作，用它自己的开关。
    """
    if config.account.agent_mid <= 0:
        return False, "AGENT_MID_NOT_CONFIGURED"
    if config.account.owner_mid <= 0:
        return False, "OWNER_MID_NOT_CONFIGURED"
    if config.account.agent_mid == config.account.owner_mid:
        return False, "AGENT_OWNER_SAME_MID"
    if config.interaction.pause:
        return False, "PAUSED"
    if not config.interaction.dm_reply_enabled:
        return False, "DM_REPLY_DISABLED"
    return True, "OK"


def extract_share_bvids(messages: list[DmMessage]) -> list[str]:
    """从私信里抽出**分享卡片的 BV 号**：按时间**从新到旧**、去重。

    主人转发视频给小号时，卡片就是 ``msg_type=7`` 的消息；
    这里只取 BV，具体的读料交给 :meth:`core.pipeline.Pipeline.read_video`
    ——**和 ``prepare_batch`` 走同一条双模型管道**，原文不进 DSH。
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for item in sorted(messages, key=lambda m: m.msg_seqno, reverse=True):
        bvid = item.bvid.strip()
        if not bvid or bvid in seen:
            continue
        seen.add(bvid)
        ordered.append(bvid)
    return ordered


async def fetch_unread(*, queue: RequestQueue, cookies: dict[str, str]) -> dict[str, Any]:
    """未读私信数（**只读**）。"""
    outcome = await queue.get_json(
        DM_HOST + UNREAD_PATH, use_cache=False, cookies=cookies
    )
    return outcome.data if isinstance(outcome.data, dict) else {}


async def fetch_messages(
    *,
    queue: RequestQueue,
    cookies: dict[str, str],
    talker_id: int,
    size: int = 20,
) -> list[DmMessage]:
    """拉取与 ``talker_id`` 的消息记录（**只读**，按 seqno 升序）。"""
    outcome = await queue.get_json(
        DM_HOST + MSGS_PATH,
        params={
            "sender_device_id": 1,
            "talker_id": talker_id,
            "session_type": 1,
            "size": size,
            "build": 0,
            "mobi_app": "web",
        },
        use_cache=False,
        cookies=cookies,
    )
    payload = outcome.data if isinstance(outcome.data, dict) else {}
    raw_items = payload.get("messages")
    messages: list[DmMessage] = []
    if not isinstance(raw_items, list):
        return messages
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        text, meta = parse_content(item.get("content"))
        messages.append(
            DmMessage(
                msg_seqno=int(item.get("msg_seqno", 0)),
                sender_uid=int(item.get("sender_uid", 0)),
                receiver_id=int(item.get("receiver_id", 0)),
                msg_type=int(item.get("msg_type", 0)),
                text=text,
                timestamp=int(item.get("timestamp", 0)),
                bvid=str(meta.get("bvid") or ""),
                title=str(meta.get("title") or ""),
                author=str(meta.get("author") or ""),
            )
        )
    messages.sort(key=lambda m: m.msg_seqno)
    return messages


async def send_to_owner(
    *,
    config: Any,
    cookies: CookieBundle,
    text: str,
    db: Any = None,
    client: httpx.AsyncClient | None = None,
    share_bvid: str = "",
    share_title: str = "",
    share_author: str = "",
    share_cover: str = "",
    share_aid: int = 0,
) -> DmSendResult:
    """给**主人**发一条私信。

    接收者是 ``config.account.owner_mid``，**调用方无法指定其他人**。
    写请求**不重试**：网络错误一律报"结果未知，不得重发"。

    ★ 2026-10-17 新增**分享卡片**（主人要求）：

    > 「在B站给我发私信时，有视频参考的要发视频，
    >   而且视频是要分享的那种卡片形式而不是链接形式」

    * 给了 ``share_bvid`` 就走 ``msg_type=7``（B站分享卡片）；
    * 否则仍是纯文本（``msg_type=1``）；
    * 卡片字段**照抄真实样本**（从主人发来的私信里抓的）：
      ``{author, headline, id, source, thumb, title, bvid}``。
    """
    allowed, reason = dm_reply_allowed(config)
    if not allowed:
        raise DmError(reason, f"私信发送被门禁拒绝：{reason}")

    if db is not None:
        state = db.health_state()
        if state in health.BLOCKING_STATES:
            raise DmError(
                "PLATFORM_BLOCKED",
                f"平台状态 {state}：已停止一切自动请求，私信不发送。",
            )

    body_text = text.strip()
    # ★ 分享卡片允许**空正文**（卡片本身就是内容）；
    #   纯文本私信仍然不许空。
    if not body_text and not share_bvid.strip():
        raise DmError("EMPTY_TEXT", "私信正文为空。")
    if len(body_text) > MAX_DM_CHARS:
        raise DmError("TEXT_TOO_LONG", f"私信正文超过 {MAX_DM_CHARS} 字符。")
    if not cookies.has_write_credentials():
        raise DmError("CSRF_MISSING", "凭据缺少 bili_jct，无法构成写请求。")

    owner_mid = int(config.account.owner_mid)
    agent_mid = int(config.account.agent_mid)
    csrf = cookies.csrf_token() or ""

    # ★ 2026-10-17 实测结论：B站 **web 端** `send_msg` 只接受 `msg_type=1`。
    #
    #   证据链（三层）：
    #   1. **参数层**：卡片（`msg_type=6`/`7`）在此接口上一律 `-400`，
    #      换过 `source` 0/1/2/5、`id` 字符串/int、带 content 字段等全部形态；
    #   2. **签名层**（决定性）：在浏览器里抓包拿到网页版真实的 WBI 签名方式
    #      （签的是 URL 上 `w_sender_uid`/`w_receiver_id`/`w_dev_id`/`statistics`
    #      这 4 个参数，**不是**整个 body），用同一份 query + 同一份 body：
    #        · `msg_type=1` → `code=0 OK`
    #        · `msg_type=7` → `-400`
    #      **唯一差别就是 msg_type** ⇒ 不是参数错、不是缺签名；
    #   3. **UI 层**：PC 网页版视频分享面板只有「动态/微信/QQ/QQ空间/微博/贴吧/
    #      嵌入代码」，**没有「私信」** ⇒ 卡片走的是 **App 接口**
    #      （appkey + access_key 签名，另一套凭据）。
    #
    #   ⇒ 所以 `share_bvid` 的行为是：**先按卡片尝试一次**（将来若放开就直接生效），
    #   遇到 `-400` 再**自动降级**为排版好的文本 —— 而不是让你收到一个报错。
    #   详见 docs/dm-share-card.md。
    bvid = share_bvid.strip()
    use_card = bool(bvid)
    if use_card:
        content_obj: dict[str, Any] = {
            "author": share_author or "",
            "headline": "",
            "id": int(share_aid) if share_aid else 0,
            "source": 5,  # 5 = 视频（从真实样本观测）
            "thumb": share_cover or "",
            "title": share_title or "",
            "bvid": bvid,
        }
        msg_type = MSG_TYPE_SHARE
    else:
        content_obj = {"content": body_text}
        msg_type = MSG_TYPE_TEXT

    owns_client = client is None
    http = client or httpx.AsyncClient(
        timeout=httpx.Timeout(DEFAULT_WRITE_TIMEOUT),
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

    async def _post(mtype: int, content: dict[str, Any]) -> dict[str, Any]:
        """发一次；返回响应体。网络错误**一律**抛 `WRITE_NETWORK`（不重试）。"""
        payload: dict[str, str] = {
            "msg[sender_uid]": str(agent_mid),
            "msg[receiver_id]": str(owner_mid),
            "msg[receiver_type]": "1",
            "msg[msg_type]": str(mtype),
            "msg[msg_status]": "0",
            "msg[dev_id]": uuid.uuid4().hex.upper(),
            "msg[timestamp]": str(int(time.time())),
            "msg[content]": json.dumps(content, ensure_ascii=False),
            "csrf": csrf,
            "csrf_token": csrf,
            "build": "0",
            "mobi_app": "web",
        }
        try:
            response = await http.post(
                DM_HOST + SEND_PATH, data=payload, cookies=cookies.values
            )
            parsed: Any = response.json()
            return parsed if isinstance(parsed, dict) else {}
        except httpx.HTTPError as exc:
            raise DmError(
                "WRITE_NETWORK",
                f"网络错误（{type(exc).__name__}）：结果未知，**不得重发**。",
            ) from exc
        except ValueError as exc:
            raise DmError("WRITE_BAD_JSON", "响应不是合法 JSON：结果未知。") from exc

    def _code_of(body: Any) -> int:
        if not isinstance(body, dict):
            raise DmError("WRITE_BAD_SHAPE", "响应结构异常。")
        code = body.get("code")
        return int(code) if isinstance(code, int) else -1

    dropped_share = False
    try:
        body = await _post(msg_type, content_obj)
        numeric = _code_of(body)
        if use_card and numeric == -400:
            # ★ B站 web 接口不支持卡片（实测，见上方注释）→ 降级为排版好的文本。
            #   只在**明确 -400** 时降级；网络错误/其他错误**不重试**（写请求铁律）。
            dropped_share = True
            body = await _post(MSG_TYPE_TEXT, {"content": _share_text_fallback(
                body_text=body_text,
                title=share_title,
                author=share_author,
                bvid=bvid,
            )})
            numeric = _code_of(body)
    finally:
        if owns_client:
            await http.aclose()

    if numeric != 0:
        raise DmError(
            f"BILI_CODE_{numeric}",
            str(body.get("message") or body.get("msg") or "") or f"接口返回 {numeric}",
        )

    data = body.get("data")
    msg_key = str(data.get("msg_key")) if isinstance(data, dict) and data.get("msg_key") else None
    return DmSendResult(
        ok=True,
        code="OK",
        msg_key=msg_key,
        message=str(body.get("message") or ""),
        card_dropped=dropped_share,
    )


def _share_text_fallback(
    *, body_text: str, title: str, author: str, bvid: str
) -> str:
    """卡片发不出去时，把视频信息排成**人看着像卡片**的文本。

    ★ 为什么还要排版：既然 B站不给发卡片（见 :func:`send_to_owner` 注释），
    那就至少在**文本层面**把标题、UP、BV、链接给全，别让主人收到一个光秃秃的链接。
    """
    parts: list[str] = []
    if body_text:
        parts.append(body_text.strip())
    lines: list[str] = []
    if title:
        lines.append(f"🎬 {title}")
    if author:
        lines.append(f"👤 UP：{author}")
    if bvid:
        lines.append(f"🔗 https://www.bilibili.com/video/{bvid}/")
    if lines:
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
