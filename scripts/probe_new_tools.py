"""真机验证两个新工具（走真实 MCP stdio，与 DSH 同一条路）。

用法：./.venv/Scripts/python.exe scripts/probe_new_tools.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "bridge.server"],
        env={**os.environ, "PYTHONPATH": ".", "PYTHONIOENCODING": "utf-8"},
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()

        listed = await session.list_tools()
        print(f"工具数：{len(listed.tools)}")
        names = [t.name for t in listed.tools]
        has_expand = "expand_intent" in names
        has_trends = "trends_suggest" in names
        print(f"新工具在吗：expand_intent={has_expand} trends_suggest={has_trends}")
        print()

        print("=== ① expand_intent（模糊意图 → 关键词）===")
        res = await session.call_tool(
            "expand_intent",
            {"intent": "想看看有没有人做 DSH 的记忆插件", "limit": 5},
        )
        payload = json.loads(res.content[0].text)
        data = payload.get("data") or {}
        print("  ok:", payload.get("ok"), "| code:", payload.get("code"))
        print("  keywords:", data.get("keywords"))
        used = data.get("used_worker")
        print("  used_worker:", used, "| error_code:", data.get("error_code"))
        print()

        print("=== ② trends_suggest（推荐流 → 候选池）===")
        res2 = await session.call_tool("trends_suggest", {"limit": 8, "min_hits": 2})
        payload2 = json.loads(res2.content[0].text)
        data2 = payload2.get("data") or {}
        print("  ok:", payload2.get("ok"), "| code:", payload2.get("code"))
        fetched = data2.get("fetched")
        print("  fetched:", fetched, "| new_in_pool:", data2.get("new_in_pool"))
        for s in (data2.get("suggestions") or [])[:6]:
            print(f"    {s['keyword']:<16} hits={s['hits']}")
        print("  note:", str(data2.get("note"))[:80])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
