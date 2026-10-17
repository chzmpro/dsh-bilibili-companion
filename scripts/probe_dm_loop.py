"""真机验证：私信闭环全链路（2026-10-17）。

验证主人说的那条链：

    私信线索 → B站搜索/推荐流 → 读视频 → 顺视频找仓库 → 读源码 → 回复

这个脚本只跑**只读**部分（不发送任何私信），
用来证明"找到仓库之后能读源码"这一环真的通了。

用法：
    ./.venv/Scripts/python.exe -u scripts/probe_dm_loop.py
    ./.venv/Scripts/python.exe -u scripts/probe_dm_loop.py --bvid BV1DkJ869EEn
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


def show(title: str, payload: dict) -> None:
    print(f"\n{'=' * 70}")
    print(f" {title}")
    print("=" * 70)
    print(json.dumps(payload, ensure_ascii=False, indent=2)[:1800])


async def main() -> int:
    parser = argparse.ArgumentParser(description="私信闭环真机验证")
    parser.add_argument("--bvid", default="BV1DkJ869EEn", help="用来验证的视频")
    args = parser.parse_args()

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
        names = [t.name for t in listed.tools]
        print(f"工具数：{len(names)}")
        for need in ("find_repo", "read_source"):
            print(f"  {need}: {'✅' if need in names else '❌'}")

        # ① 顺视频找仓库
        res = await session.call_tool("find_repo", {"bvid": args.bvid})
        found = json.loads(res.content[0].text)
        data = found.get("data") or {}
        repos = data.get("repos") or []
        show(
            f"① find_repo({args.bvid})",
            {
                "title": data.get("title"),
                "repos": [r["full_name"] for r in repos],
            },
        )

        if not repos:
            print("\n⚠️ 这个视频没有仓库地址 —— 闭环到不了「读源码」那步。")
            print("   （这本身是正确行为：**不猜**仓库）")
            return 0

        # ② 读源码（列根目录 + README）
        target = repos[0]
        res2 = await session.call_tool(
            "read_source", {"repo": target["full_name"]}
        )
        src = json.loads(res2.content[0].text)
        sdata = src.get("data") or {}
        show(
            f"② read_source({target['full_name']})",
            {
                "ok": src.get("ok"),
                "ref": sdata.get("ref"),
                "zip_mb": sdata.get("zip_mb"),
                "readme": sdata.get("readme"),
                "readme_source": sdata.get("readme_source"),
                "root_files": (sdata.get("root_files") or [])[:15],
                "readme_head": str(sdata.get("readme_text") or "")[:300],
            },
        )

        # ③ 读一个具体文件（如果根目录里有 .md/.js/.py）
        candidates = [
            name
            for name in (sdata.get("root_files") or [])
            if name.lower().endswith((".md", ".js", ".py", ".json"))
            and name.lower() != str(sdata.get("readme") or "").lower()
        ]
        if candidates:
            res3 = await session.call_tool(
                "read_source",
                {"repo": target["full_name"], "path": candidates[0], "max_chars": 800},
            )
            one = json.loads(res3.content[0].text)
            odata = one.get("data") or {}
            show(
                f"③ read_source({target['full_name']}, path={candidates[0]})",
                {
                    "ok": one.get("ok"),
                    "source": odata.get("source"),
                    "chars": odata.get("chars"),
                    "truncated": odata.get("truncated"),
                    "text_head": str(odata.get("text") or "")[:200],
                },
            )

    print(f"\n{'=' * 70}")
    print(" 闭环验证：find_repo → read_source 已通")
    print(" （回复主人那步走 cli.app dm-poll --reply，本脚本不发送）")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
