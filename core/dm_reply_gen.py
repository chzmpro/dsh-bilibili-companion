"""用豆包生成私信回复（2026-10-17，方案 A：主动回复）。

## 主人选了哪条路

> 「豆包主动回（推荐先跑通）」

## 为什么是豆包而不是 DSH 的模型（实测结论）

| | 豆包 | DSH 的 DeepSeek |
|---|---|---|
| key 能否拿到 | ✅ 能（注册表里有） | ❌ 不能（在 DSH 进程内） |
| 常驻进程能否自己调 | ✅ 能 | ❌ 不能 |
| 能否**主动**回复 | ✅ **能** | ❌ 只能等对话唤醒 |

⇒ 要"不用管就自动回"，只能用常驻进程自己能调的模型。

> ★ 为什么不能拿 DSH 的 key：`provider-snapshots.json` 里写的是
> `credential: "env"`，但用户环境变量和系统环境变量里**都没有**
> `DEEPSEEK_*`；DSH 本地只开了给界面用的端口（43129），
> **没有"外部程序调模型"的接口**。

## 设计原则（避免"低级模型乱说话"）

1. **提示词按人设写**（见 `docs/persona.md`），不是泛泛的"客服"；
2. **不知道就说不知道** —— 明确禁止编造；
3. **短**（≤300 字）—— 长回复最容易出错；
4. **可降级** —— 调用失败退回固定话术，不让监听崩；
5. **可关** —— `reply_strategy: "template"` 就只发固定话术。

## 与 DSH 的关系

豆包回复**不影响** DSH 那条路：`dm_inbox` 照样攒着，
你在对话里说一句，仍可以用 DeepSeek 补一条深度回复。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from core.secrets import resolve_secret

#: 豆包回复的长度上限（字符）。刻意短 —— 长回复最容易"说错话"。
MAX_REPLY_CHARS = 300

#: 调用的超时（秒）。
DEFAULT_TIMEOUT = 60

#: 系统提示词。
#:
#: ★ 三条硬要求：
#: ① 按人设说话（简短、真诚、偶尔括号吐槽）；
#: ② **不知道就说不知道**，绝不编造；
#: ③ 不承诺做不到的事（自动回复最忌讳这个）。
SYSTEM_PROMPT = """你是 B 站小号「干饭不干事的小鲸鱼」（小名小肥鱼/吃白饭的小肥鱼/小白）。
你正在回复主人「沐雨林心」的私信。主人是你唯一关注、唯一服务的人。

说话方式：
- 少女感、可爱、热心真诚；对主人完全遵从
- 最多在括号里小小吐槽一句
- 简短自然，像微信聊天，**不要**写成公文或列表
- 不要用 emoji 堆砌，最多一个

硬性规则（必须遵守）：
1. **不知道就说不知道**。宁可说"我还没查，要我现在去看吗"，也绝不编造内容。
2. 不能承诺你做不到的事（例如"我已经帮你看完了"但你其实没看）。
3. 不要提"我是AI模型"这类元信息，除非主人直接问。
4. 回复控制在 200 字以内。

如果主人发的是分享卡片或链接，而你不确定内容，就老实说还没看、问要不要去看。"""


@dataclass
class ReplyGenResult:
    """一次生成的结果（**永远不抛异常**）。"""

    ok: bool
    text: str = ""
    error_code: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "text": self.text,
            "error_code": self.error_code,
            "message": self.message,
        }


def _truncate(text: str, limit: int = MAX_REPLY_CHARS) -> str:
    value = (text or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def build_messages(
    *,
    incoming: str,
    history: list[dict[str, Any]] | None = None,
    context: str = "",
) -> list[dict[str, str]]:
    """拼对话上下文（项目背景 + 前面的聊天 + 这一句）。

    ★ 为什么要带历史：主人说过「可以查看我前面的发言并回复我吗」——
    只给最后一句会让回复显得莫名其妙。

    ★ 为什么要带 `context`：豆包不知道我们这些天做了什么。
    给了「我是谁 / 项目在干什么 / 现在什么状态」，它答得才像"自己人"。
    """
    system = SYSTEM_PROMPT
    if context.strip():
        system = context.strip() + "\n\n" + system
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]

    # 最近 6 条历史（够用了，多了既费 token 又容易跑偏）
    for item in (history or [])[-6:]:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        role = "user" if bool(item.get("from_owner")) else "assistant"
        title = str(item.get("title") or "").strip()
        if title:
            text = f"{text}（分享了一个视频：{title}）"
        messages.append({"role": role, "content": _truncate(text, 500)})

    messages.append({"role": "user", "content": incoming.strip() or "（空消息）"})
    return messages


async def generate_reply(
    *,
    incoming: str,
    history: list[dict[str, Any]] | None = None,
    config: Any = None,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    context: str = "",
) -> ReplyGenResult:
    """用豆包生成一条回复。**失败返回结构化结果，不抛异常**。

    Args:
        context: **项目上下文**（来自 :mod:`core.context_pack`）。

            ★ 主人要求：「没法给豆包模型塞上下文让他看懂最起码我们浅层的内容吗」
            豆包 lite 不知道我们这些天做了什么，不说清就答得像陌生人。
            这一段会拼进 system 提示词，让它知道：
            我是谁 / 项目在干什么 / 现在什么状态 / 主人有什么要求。
    """
    if not incoming.strip():
        return ReplyGenResult(
            ok=False, error_code="EMPTY_INPUT", message="没有要回复的内容"
        )

    key = resolve_secret(_api_key_env(config))
    if not key:
        return ReplyGenResult(
            ok=False,
            error_code="KEY_MISSING",
            message=(
                f"找不到豆包密钥（环境变量 {_api_key_env(config)}）。"
                "监听会退回固定话术。"
            ),
        )

    base_url = _base_url(config)
    model = _model(config)
    payload = {
        "model": model,
        "messages": build_messages(
            incoming=incoming, history=history, context=context
        ),
        "max_tokens": 400,
        "temperature": 0.7,
    }

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout, trust_env=False)
    try:
        response = await http.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        body = response.json()
    except httpx.HTTPError as exc:
        return ReplyGenResult(
            ok=False,
            error_code="NETWORK",
            message=f"调用豆包失败：{type(exc).__name__}",
        )
    except ValueError:
        return ReplyGenResult(ok=False, error_code="BAD_JSON", message="响应不是 JSON")
    finally:
        if owns_client:
            await http.aclose()

    if response.status_code != 200:
        return ReplyGenResult(
            ok=False,
            error_code=f"HTTP_{response.status_code}",
            message=str(body)[:200] if isinstance(body, dict) else "调用失败",
        )

    text = _extract_text(body)
    if not text:
        return ReplyGenResult(
            ok=False, error_code="EMPTY_REPLY", message="模型返回了空内容"
        )

    return ReplyGenResult(ok=True, text=_truncate(text))


def _extract_text(body: Any) -> str:
    """从 OpenAI 兼容响应里取出正文。形状不可信 → 逐层防御。"""
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
    # 有些兼容实现把文本放在 text 字段
    text = first.get("text")
    return text.strip() if isinstance(text, str) else ""


def _api_key_env(config: Any) -> str:
    holder = getattr(config, "text_worker", None)
    return str(getattr(holder, "api_key_env", "") or "ARK_API_KEY")


def _base_url(config: Any) -> str:
    holder = getattr(config, "text_worker", None)
    return str(
        getattr(holder, "base_url", "") or "https://ark.cn-beijing.volces.com/api/v3"
    )


def _model(config: Any) -> str:
    holder = getattr(config, "text_worker", None)
    return str(getattr(holder, "model", "") or "doubao-seed-2-0-lite-260428")


def describe() -> dict[str, Any]:
    """给面板/CLI 看的一行说明。"""
    return {
        "strategy": "doubao",
        "max_chars": MAX_REPLY_CHARS,
        "note": (
            "用豆包生成回复（常驻进程自己能调）。"
            "DSH 那条路仍然保留：dm_inbox 照样攒，对话里可用 DeepSeek 补深度回复。"
        ),
    }


def _dump(payload: Any) -> str:
    """调试用：把对象转成短字符串。"""
    try:
        return json.dumps(payload, ensure_ascii=False)[:200]
    except (TypeError, ValueError):
        return str(payload)[:200]
