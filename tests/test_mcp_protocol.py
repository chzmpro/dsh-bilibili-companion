"""真实 stdio 协议下的 MCP 集成测试（任务书 §7 任务 2 验收）。

与其它测试不同：这里**真的启动子进程**，用 MCP 协议通信，
验证"在新 DSH 会话里能看见并调用 ``mcp__bili_companion__companion_status``"这条验收。

功能用例走**官方 MCP 客户端库**（``mcp.ClientSession`` + ``stdio_client``）——
那正是 DSH 自己的 ``@deepseek-ai/dsh-mcp-client`` 所使用的协议栈，
比手写 JSON 帧更忠实。

覆盖任务书要求：

* MCP 启动、工具枚举
* 空配置只读启动
* **异常不污染 stdout**（stdout 只准放协议帧）
* ``companion_status`` 可调用且不含凭据
* MCP 进程启动本身**不访问 B 站**
* 人用入口**不在**模型可见工具列表里
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():  # pragma: no cover - 非 Windows
    PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"

#: ★ 26 个工具 = 任务书 §4 的 14 个 + 主人 2026-10-15 要求新增的 3 个
#: + 2026-10-17 新增的搜索/私信/源码/联网工具
#: + 2026-10-18 的 ``search_notes``（R5：检索以前学过的笔记）。
EXPECTED_TOOLS = {
    # —— 任务书 §4 的 14 个 ——
    "companion_status",
    "discover_videos",
    "prepare_batch",
    "read_video",
    "read_evidence_excerpt",
    "get_discussion",
    "save_analysis",
    "list_drafts",
    "publish_draft",
    "check_interactions",
    "prepare_reply",
    "publish_reply",
    "feedback",
    "pause",
    # —— 主人追加的 3 个 ——
    "like_video",
    "favorite_video",
    "owner_preferences",
    # —— 搜索方式 ①②（2026-10-17）——
    #    expand_intent：模糊意图 → 关键词（只读，不搜视频）
    #    trends_suggest：推荐流 → 候选词池（只读、限额，**不自动改关键词**）
    "expand_intent",
    "trends_suggest",
    # —— 2026-10-17：私信闭环（视频 → 仓库 → 读源码）——
    "find_repo",
    "read_source",
    # —— 2026-10-17：免费联网搜索（主人要的"免费互联网搜索"）——
    "web_search",
    # —— 2026-10-17：DSH 思考后再回复（主人要求别用低级模型）——
    "dm_inbox",
    "dm_reply",
    "dm_push",
    # —— 2026-10-18：检索以前学过的知识笔记（R5）——
    "search_notes",
}

#: 人用入口**绝不能**出现在模型可见的工具列表里（任务书 §3）
FORBIDDEN_IN_MCP = {"approve", "resume", "reject", "import-cookie", "recover-health"}


def server_params(data_dir: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=str(PYTHON),
        args=["-m", "bridge.server"],
        cwd=str(PROJECT_ROOT),
        env={
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(PROJECT_ROOT),
            "COMPANION_DATA_DIR": str(data_dir),
            # 子进程也要读到"零配置"，否则会捡起项目根的 config.yaml
            "COMPANION_CONFIG_PATH": str(data_dir / "no-such-config.yaml"),
        },
    )


@asynccontextmanager
async def mcp_session(data_dir: Path) -> AsyncIterator[ClientSession]:
    """开一个真实的 stdio MCP 会话。"""
    async with (
        stdio_client(server_params(data_dir)) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


def tool_payload(result: Any) -> dict[str, Any]:
    """从 ``tools/call`` 结果里取出 bridge 的 JSON 返回。"""
    assert result.content, "工具没有返回任何内容"
    first = result.content[0]
    assert first.type == "text"
    return json.loads(first.text)


@pytest.fixture
def data_dir(isolated_data_dir: Path) -> Path:
    return isolated_data_dir


# ---------------------------------------------------------------------------
# 启动与枚举
# ---------------------------------------------------------------------------


async def test_server_starts_and_lists_all_26_tools(data_dir: Path) -> None:
    """★ 任务书 §7 任务 2 验收：工具能在协议层被枚举出来。"""
    async with mcp_session(data_dir) as session:
        listed = await session.list_tools()
        names = {tool.name for tool in listed.tools}
    assert names == EXPECTED_TOOLS, f"工具集合不符，差异：{names ^ EXPECTED_TOOLS}"
    assert len(names) == 26


async def test_tools_have_descriptions(data_dir: Path) -> None:
    """每个工具都要有说明——否则模型不知道何时用。"""
    async with mcp_session(data_dir) as session:
        listed = await session.list_tools()
    for tool in listed.tools:
        assert tool.description, f"{tool.name} 缺少 description"
        assert tool.input_schema is not None, f"{tool.name} 缺少 input schema"


async def test_human_entrypoints_absent_from_mcp(data_dir: Path) -> None:
    """★ 人用入口不得暴露给模型（防"评论里自称主人要求批准"越权）。"""
    async with mcp_session(data_dir) as session:
        listed = await session.list_tools()
        names = {tool.name for tool in listed.tools}
    leaked = FORBIDDEN_IN_MCP & names
    assert not leaked, f"人用入口泄漏进 MCP：{leaked}"


# ---------------------------------------------------------------------------
# companion_status
# ---------------------------------------------------------------------------


async def test_companion_status_works_with_zero_config(data_dir: Path) -> None:
    """★ 空配置（没有任何 config.yaml）也要能只读启动并返回结构化状态。"""
    async with mcp_session(data_dir) as session:
        payload = tool_payload(await session.call_tool("companion_status", {}))

    assert payload["ok"] is True
    assert payload["code"] == "OK"
    data = payload["data"]
    assert data["server"]["name"] == "bili_companion"
    assert data["config"]["mode"] == "observe"
    assert data["config"]["dry_run"] is True
    assert data["config"]["publish_enabled"] is False
    assert data["config"]["agent_mid"] == 0
    assert data["config"]["publish_allowed"] is False
    assert data["platform_health"]["state"] == "ready"
    assert "quota" in data
    assert "credentials" in data


async def test_companion_status_repeatable(data_dir: Path) -> None:
    """同一会话内反复调用必须稳定（无隐式副作用）。"""
    async with mcp_session(data_dir) as session:
        first = tool_payload(await session.call_tool("companion_status", {}))
        second = tool_payload(await session.call_tool("companion_status", {}))
    assert first["data"]["quota"] == second["data"]["quota"]
    assert first["data"]["config"]["mode"] == second["data"]["config"]["mode"]


async def test_status_never_leaks_credentials(data_dir: Path) -> None:
    """★ 凭据值绝不能出现在 MCP 返回里。"""
    secret = "SuperSecretSessdataValue1234567890"
    csrf = "abcdef1234567890abcdef1234567890"
    (data_dir / "cookie.txt").write_text(
        f"SESSDATA={secret}; bili_jct={csrf}; buvid3=abc", encoding="utf-8"
    )
    async with mcp_session(data_dir) as session:
        result = await session.call_tool("companion_status", {})
    raw = json.dumps([block.model_dump() for block in result.content], ensure_ascii=False)
    assert secret not in raw, "SESSDATA 泄漏进 MCP 返回"
    assert csrf not in raw, "bili_jct 泄漏进 MCP 返回"

    payload = tool_payload(result)
    # 但应报告"有哪些名字"，以便人诊断
    assert payload["data"]["credentials"]["present"] is True
    assert payload["data"]["credentials"]["writable"] is True
    assert "sessdata" in payload["data"]["credentials"]["names"]


async def test_status_reports_worker_not_configured(data_dir: Path) -> None:
    """未配豆包模型时，状态要**明确**报出来（不能静默）。"""
    async with mcp_session(data_dir) as session:
        payload = tool_payload(await session.call_tool("companion_status", {}))
    config = payload["data"]["config"]
    assert config["text_worker_usable"] is False
    assert config["text_worker_code"] == "TEXT_WORKER_NOT_CONFIGURED"


# ---------------------------------------------------------------------------
# 错误码
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        ("read_video", {"bvid": "not-a-bvid"}, "INVALID_ARGUMENT"),
        ("read_evidence_excerpt", {"evidence_id": "ev_missing"}, "EVIDENCE_NOT_FOUND"),
        ("read_evidence_excerpt", {"evidence_id": ""}, "INVALID_ARGUMENT"),
        ("publish_draft", {"draft_id": "draft_nope"}, "DRAFT_NOT_FOUND"),
        ("publish_reply", {"draft_id": "draft_nope"}, "DRAFT_NOT_FOUND"),
        ("list_drafts", {"status": "bogus"}, "INVALID_ARGUMENT"),
        ("feedback", {"bvid": "BV1x", "verdict": "maybe"}, "INVALID_ARGUMENT"),
        ("get_discussion", {"bvid": "BV1x", "page": 999}, "INVALID_ARGUMENT"),
    ],
)
async def test_errors_have_explicit_codes(
    data_dir: Path, tool: str, args: dict[str, Any], expected: str
) -> None:
    """★ 任务书 §4：错误必须有明确 code，不能吞成空列表。"""
    async with mcp_session(data_dir) as session:
        payload = tool_payload(await session.call_tool(tool, args))
    assert payload["ok"] is False, f"{tool} 应当失败"
    assert payload["code"] == expected, f"{tool}: 期望 {expected}，实得 {payload['code']}"


async def test_prepare_batch_refuses_without_text_worker(data_dir: Path) -> None:
    """★ 任务书 §3：不能偷偷退化为"DSH 读全文"。"""
    async with mcp_session(data_dir) as session:
        payload = tool_payload(await session.call_tool("prepare_batch", {}))
    assert payload["ok"] is False
    assert payload["code"] == "TEXT_WORKER_NOT_CONFIGURED"


async def test_save_analysis_requires_real_evidence(data_dir: Path) -> None:
    """★ 证据必须真实可反查：编造的 evidence_id 一律拒绝。"""
    async with mcp_session(data_dir) as session:
        payload = tool_payload(
            await session.call_tool(
                "save_analysis",
                {
                    "bvid": "BV1test00001",
                    "evidence_ids": ["ev_fabricated0000"],
                    "score_total": 90,
                    "summary": "看起来很棒的总结",
                },
            )
        )
    assert payload["ok"] is False
    assert payload["code"] == "EVIDENCE_NOT_FOUND"


async def test_save_analysis_rejects_evidence_from_other_bvid(data_dir: Path) -> None:
    """跨 BV 引用证据也要拒绝。"""
    async with mcp_session(data_dir) as session:
        # 先写入一个属于 BV1aaa 的证据
        payload = tool_payload(
            await session.call_tool(
                "save_analysis",
                {
                    "bvid": "BV1aaa",
                    "evidence_ids": ["ev_nope"],
                    "score_total": 50,
                    "summary": "x",
                },
            )
        )
    assert payload["ok"] is False
    assert payload["code"] in ("EVIDENCE_NOT_FOUND", "EVIDENCE_REQUIRED")


# ---------------------------------------------------------------------------
# stdout 纯净性（任务书 §4）
# ---------------------------------------------------------------------------


def test_stdout_contains_only_protocol_frames(data_dir: Path) -> None:
    """★ stdout 只输出协议帧，诊断写 stderr。

    用**原始子进程**验证（官方客户端看不到原始字节）：
    正确顺序是先收 ``initialize`` 响应，再发 ``notifications/initialized``，
    然后才能发请求——顺序错了会让会话卡住。
    """
    import threading

    def rpc(id_: int, method: str, params: dict[str, Any] | None = None) -> str:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": id_, "method": method}
        if params is not None:
            payload["params"] = params
        return json.dumps(payload) + "\n"

    proc = subprocess.Popen(
        [str(PYTHON), "-m", "bridge.server"],
        cwd=str(PROJECT_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env={
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(PROJECT_ROOT),
            "COMPANION_DATA_DIR": str(data_dir),
            "COMPANION_CONFIG_PATH": str(data_dir / "no-such-config.yaml"),
        },
    )
    assert proc.stdin and proc.stdout and proc.stderr

    responses: dict[int, dict[str, Any]] = {}
    stdout_lines: list[str] = []
    done = threading.Event()

    def read_stdout() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            stripped = line.strip()
            if not stripped:
                continue
            stdout_lines.append(stripped)
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed.get("id"), int):
                responses[parsed["id"]] = parsed
                if parsed["id"] == 3:
                    done.set()

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()

    # 正确握手顺序：先 initialize，**等响应**，再 initialized
    proc.stdin.write(
        rpc(
            1,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest-raw", "version": "1.0"},
            },
        )
    )
    proc.stdin.flush()
    for _ in range(200):
        if 1 in responses:
            break
        done.wait(0.05)
    assert 1 in responses, "未收到 initialize 响应"

    proc.stdin.write(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
    )
    proc.stdin.flush()

    # 触发一个错误路径：异常必须走 stderr，stdout 依然是合法 JSON-RPC
    proc.stdin.write(
        rpc(3, "tools/call", {"name": "read_evidence_excerpt", "arguments": {"evidence_id": ""}})
    )
    proc.stdin.flush()
    done.wait(20)

    proc.stdin.close()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:  # pragma: no cover
        proc.kill()

    # ★ 每一行 stdout 都必须是合法 JSON-RPC
    for line in stdout_lines:
        parsed = json.loads(line)
        assert "jsonrpc" in parsed

    assert 3 in responses, f"未收到 tools/call 响应。stdout={stdout_lines}"
    payload = json.loads(responses[3]["result"]["content"][0]["text"])
    assert payload["ok"] is False
    assert payload["code"] == "INVALID_ARGUMENT"


# ---------------------------------------------------------------------------
# 启动无副作用
# ---------------------------------------------------------------------------


async def test_startup_does_not_touch_network_or_create_actions(data_dir: Path) -> None:
    """★ 任务书 §7 任务 3：MCP 进程**启动本身不访问 B 站**。

    断言：只启动 + 调 ``companion_status`` 不产生任何平台读取、
    不产生健康事件、不产生动作。
    """
    async with mcp_session(data_dir) as session:
        await session.call_tool("companion_status", {})

    db_file = data_dir / "companion.db"
    assert db_file.exists()
    conn = sqlite3.connect(str(db_file))
    try:
        cache_rows = conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0]
        health_rows = conn.execute(
            "SELECT COUNT(*) FROM platform_health WHERE recovered_at IS NULL"
        ).fetchone()[0]
        actions = conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
        runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    finally:
        conn.close()

    assert cache_rows == 0, "启动阶段不应有任何平台读取"
    assert health_rows == 0, "启动阶段不应有健康事件"
    assert actions == 0, "启动阶段不应有任何动作"
    assert runs == 0, "启动阶段不应产生运行记录"


async def test_publish_refuses_in_default_observe_mode(data_dir: Path) -> None:
    """★ 默认姿态下，任何发布尝试都被代码层拒绝。"""
    async with mcp_session(data_dir) as session:
        payload = tool_payload(
            await session.call_tool("publish_draft", {"draft_id": "draft_whatever"})
        )
    assert payload["ok"] is False
    # 默认没配置时，草稿不存在会先报 DRAFT_NOT_FOUND；关键是**没有**发布成功
    assert payload["code"] in ("DRAFT_NOT_FOUND", "MODE_OBSERVE", "DRY_RUN")


async def test_pause_tool_does_not_grant_publish(data_dir: Path) -> None:
    """模型调用 ``pause`` 只能收紧，不能放宽。"""
    async with mcp_session(data_dir) as session:
        payload = tool_payload(await session.call_tool("pause", {"reason": "测试"}))
        assert payload["ok"] is True
        assert payload["data"]["pause"] is True
        status = tool_payload(await session.call_tool("companion_status", {}))
    assert status["data"]["config"]["publish_allowed"] is False
