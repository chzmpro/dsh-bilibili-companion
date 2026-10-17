"""真机验证：推荐流**严格限额**与 status 新字段（走真实 MCP stdio）。

用法：./.venv/Scripts/python.exe scripts/probe_trends_quota.py
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


async def call(session: ClientSession, name: str, args: dict | None = None) -> dict:
    res = await session.call_tool(name, args or {})
    return json.loads(res.content[0].text)


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

        print("=== ① companion_status 的新字段 ===")
        status = await call(session, "companion_status")
        data = status.get("data") or {}
        print("  trends:", json.dumps(data.get("trends"), ensure_ascii=False)[:200])
        plats = data.get("platforms") or []
        for p in plats:
            print(f"  platform {p.get('platform'):<13} implemented={p.get('implemented')}")

        print()
        print("=== ② 连跑 trends_suggest 直到被限额拒绝 ===")
        for i in range(1, 5):
            res = await call(session, "trends_suggest", {"limit": 6, "min_hits": 2})
            ok = res.get("ok")
            d = res.get("data") or {}
            if ok:
                print(f"  第{i}轮：✅ fetched={d.get('fetched')} new={d.get('new_in_pool')}"
                      f" 剩余={ (d.get('quota') or {}).get('remaining_today') }")
            else:
                print(f"  第{i}轮：⛔ 被拒绝 code={res.get('code')}")
                print(f"        理由：{str(res.get('message'))[:70]}")
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
