"""豆包主动回复测试（2026-10-17）。

主人选了方案 A：「豆包主动回（推荐先跑通）」。

## 为什么是豆包（实测约束，不是偏好）

| | 豆包 | DSH 的 DeepSeek |
|---|---|---|
| key 能拿到 | ✅ 注册表里有 | ❌ 在 DSH 进程内存 |
| 常驻进程能自己调 | ✅ 能 | ❌ 不能 |
| 能**主动**回复 | ✅ 能 | ❌ 只能等对话唤醒 |

## 钉死的不变量

* **不知道就说不知道** —— 提示词强制，测试检查提示词里真有这条；
* **长度上限** —— 长回复最容易说错话；
* **失败必须降级**（不抛异常），否则监听会挂；
* **形状不可信** —— 响应缺字段/非 dict 都要能扛；
* **带历史** —— 只给最后一句会答得莫名其妙。
"""

from __future__ import annotations

from typing import Any

import pytest

from core import dm_reply_gen
from core.dm_reply_gen import (
    MAX_REPLY_CHARS,
    SYSTEM_PROMPT,
    ReplyGenResult,
    build_messages,
    generate_reply,
)


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeClient:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def aclose(self) -> None:
        return None


class _Config:
    class text_worker:  # noqa: N801
        api_key_env = "TEST_DOUBAO_KEY"
        base_url = "https://example.test/api/v3"
        model = "test-model"


# ---------------------------------------------------------------------------
# ★★ 提示词必须含"不知道就说不知道"
# ---------------------------------------------------------------------------


def test_prompt_forbids_fabrication() -> None:
    """★★ 这是自动回复最重要的约束：宁可说不知道，绝不编造。

    主人抱怨过"低级模型"——低级模型最典型的问题就是**自信地编**。
    """
    assert "不知道就说不知道" in SYSTEM_PROMPT or "不知道" in SYSTEM_PROMPT
    assert "编造" in SYSTEM_PROMPT


def test_prompt_forbids_false_promises() -> None:
    """不能承诺做不到的事（例如"我已经看完了"但其实没看）。"""
    assert "不能承诺" in SYSTEM_PROMPT or "承诺" in SYSTEM_PROMPT


def test_prompt_has_persona() -> None:
    """人设要在提示词里（否则回复像客服）。"""
    assert "小鲸鱼" in SYSTEM_PROMPT
    assert "主人" in SYSTEM_PROMPT


def test_prompt_limits_length() -> None:
    assert "200 字" in SYSTEM_PROMPT or "字以内" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------


def test_build_messages_includes_system() -> None:
    messages = build_messages(incoming="你好")
    assert messages[0]["role"] == "system"
    assert messages[-1] == {"role": "user", "content": "你好"}


def test_build_messages_includes_history() -> None:
    """★ 带历史：主人说过「查看我前面的发言并回复我」。"""
    messages = build_messages(
        incoming="最新一句",
        history=[
            {"text": "第一句", "from_owner": True},
            {"text": "我之前的回复", "from_owner": False},
        ],
    )
    contents = [m["content"] for m in messages]
    assert "第一句" in contents
    assert "我之前的回复" in contents


def test_build_messages_role_mapping() -> None:
    messages = build_messages(
        incoming="x",
        history=[
            {"text": "主人说的", "from_owner": True},
            {"text": "我说的", "from_owner": False},
        ],
    )
    assert messages[1]["role"] == "user"
    assert messages[2]["role"] == "assistant"


def test_build_messages_mentions_shared_video() -> None:
    """分享卡片要标出来，否则模型不知道那是视频。"""
    messages = build_messages(
        incoming="x", history=[{"text": "看这个", "title": "某视频", "from_owner": True}]
    )
    assert "分享了一个视频" in messages[1]["content"]


def test_build_messages_caps_history() -> None:
    """历史最多 6 条（省 token、防跑偏）。"""
    history = [{"text": f"第{i}句", "from_owner": True} for i in range(20)]
    messages = build_messages(incoming="x", history=history)
    # system + 最多 6 条历史 + 最新一句
    assert len(messages) <= 8


def test_build_messages_skips_empty() -> None:
    messages = build_messages(incoming="x", history=[{"text": "", "from_owner": True}])
    assert len(messages) == 2


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


async def test_generate_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dm_reply_gen, "resolve_secret", lambda name: None)
    result = await generate_reply(incoming="你好", config=_Config())
    assert result.ok is False
    assert result.error_code == "KEY_MISSING"


async def test_generate_empty_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dm_reply_gen, "resolve_secret", lambda name: "k")
    result = await generate_reply(incoming="   ", config=_Config())
    assert result.ok is False
    assert result.error_code == "EMPTY_INPUT"


async def test_generate_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dm_reply_gen, "resolve_secret", lambda name: "k")
    client = _FakeClient(
        _FakeResponse(
            200,
            {
                "choices": [
                    {"message": {"content": "好哒主人，我这就去看～"}}
                ]
            },
        )
    )
    result = await generate_reply(incoming="看一下这个", config=_Config(), client=client)
    assert result.ok is True
    assert "好哒" in result.text
    # URL 要拼对
    assert client.calls[0]["url"].endswith("/chat/completions")


async def test_generate_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dm_reply_gen, "resolve_secret", lambda name: "k")
    client = _FakeClient(
        _FakeResponse(200, {"choices": [{"message": {"content": "字" * 2000}}]})
    )
    result = await generate_reply(incoming="x", config=_Config(), client=client)
    assert result.ok is True
    assert len(result.text) <= MAX_REPLY_CHARS


async def test_generate_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dm_reply_gen, "resolve_secret", lambda name: "k")
    client = _FakeClient(_FakeResponse(401, {"error": "bad key"}))
    result = await generate_reply(incoming="x", config=_Config(), client=client)
    assert result.ok is False
    assert result.error_code == "HTTP_401"


async def test_generate_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    monkeypatch.setattr(dm_reply_gen, "resolve_secret", lambda name: "k")
    client = _FakeClient(httpx.ConnectError("boom"))
    result = await generate_reply(incoming="x", config=_Config(), client=client)
    assert result.ok is False
    assert result.error_code == "NETWORK"


async def test_generate_empty_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dm_reply_gen, "resolve_secret", lambda name: "k")
    client = _FakeClient(_FakeResponse(200, {"choices": [{"message": {"content": ""}}]}))
    result = await generate_reply(incoming="x", config=_Config(), client=client)
    assert result.ok is False
    assert result.error_code == "EMPTY_REPLY"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"choices": []},
        {"choices": "不是列表"},
        {"choices": [None]},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
    ],
)
async def test_generate_survives_bad_shape(
    monkeypatch: pytest.MonkeyPatch, payload: Any
) -> None:
    """★ 响应形状不可信 —— 任何畸形都不能让监听崩。"""
    monkeypatch.setattr(dm_reply_gen, "resolve_secret", lambda name: "k")
    client = _FakeClient(_FakeResponse(200, payload))
    result = await generate_reply(incoming="x", config=_Config(), client=client)
    assert result.ok is False
    assert result.error_code in {"EMPTY_REPLY", "BAD_JSON"}


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------


def test_reads_key_env_from_config() -> None:
    assert dm_reply_gen._api_key_env(_Config()) == "TEST_DOUBAO_KEY"


def test_reads_base_url_from_config() -> None:
    assert dm_reply_gen._base_url(_Config()) == "https://example.test/api/v3"


def test_reads_model_from_config() -> None:
    assert dm_reply_gen._model(_Config()) == "test-model"


def test_handles_missing_config() -> None:
    """config 为 None 时要有合理默认，不能崩。"""
    assert dm_reply_gen._api_key_env(None) == "ARK_API_KEY"
    assert "volces" in dm_reply_gen._base_url(None)


def test_result_to_dict() -> None:
    payload = ReplyGenResult(ok=True, text="hi").to_dict()
    assert payload["ok"] is True
    assert payload["text"] == "hi"
