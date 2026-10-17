"""实测 B 站推荐流接口（一次性探针，只读）。

目的：确认 `/x/web-interface/wbi/index/top/feed/rcmd` 在**本机登录态**下
能不能拿到数据、返回什么形状、**需不需要 WBI 签名**。

只发 GET，不改任何状态。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config  # noqa: E402
from core.credentials import CredentialError, load_cookie  # noqa: E402
from core.ratelimit import RequestQueue  # noqa: E402
from core.storage import Database  # noqa: E402

RCMD_PATH = "/x/web-interface/wbi/index/top/feed/rcmd"
BASE = "https://api.bilibili.com"
BASE_PARAMS = {"fresh_type": 3, "ps": 6, "fresh_idx": 1, "fresh_idx_1h": 1}


def describe(outcome) -> None:
    print("status:", outcome.status, "code:", outcome.code, "msg:", str(outcome.message)[:80])
    data = outcome.data if isinstance(outcome.data, dict) else {}
    items = (data.get("data") or {}).get("item")
    ok = isinstance(items, list)
    print("有 item 列表:", ok, "长度:", len(items) if ok else 0)
    if ok and items:
        first = items[0]
        print("第一条关键字段：")
        print("  bvid:", first.get("bvid"))
        print("  title:", str(first.get("title"))[:40])
        owner = first.get("owner") or {}
        print("  owner:", {"mid": owner.get("mid"), "name": str(owner.get("name"))[:20]})
        stat = first.get("stat") or {}
        print("  stat:", {"view": stat.get("view"), "like": stat.get("like")})
        print("  rcmd_reason:", str(first.get("rcmd_reason"))[:40])


async def main() -> int:
    config = load_config()
    db = Database()
    db.migrate()
    try:
        cookies = load_cookie().values
    except CredentialError as exc:
        print(f"没有凭据：{exc}")
        db.close()
        return 2

    queue = RequestQueue(db=db, config=config)
    try:
        print("=== ① 不带签名 ===")
        unsigned = await queue.get_json(
            BASE + RCMD_PATH,
            params=dict(BASE_PARAMS),
            kind="metadata",
            use_cache=False,
            cookies=cookies,
        )
        describe(unsigned)

        print("\n=== ② 带 WBI 签名（走 reader 现成的签名链路） ===")
        from core.bilibili import BilibiliReader

        reader = BilibiliReader(queue=queue, cookies=cookies)
        # ★ 复用 reader 的 _get_signed：它已经处理"取 key → 签名 → -403 刷新重试"
        signed = await reader._get_signed(  # noqa: SLF001 —— 探针脚本，直连内部链路
            BASE + RCMD_PATH,
            dict(BASE_PARAMS),
            kind="metadata",
            use_cache=False,
        )
        print("status: 200 code:", signed.get("code") if isinstance(signed, dict) else "?")
        items = (signed or {}).get("data", {}).get("item") if isinstance(signed, dict) else None
        ok = isinstance(items, list)
        print("有 item 列表:", ok, "长度:", len(items) if ok else 0)
        if ok and items:
            first = items[0]
            print("  bvid:", first.get("bvid"))
            print("  title:", str(first.get("title"))[:40])
            owner = first.get("owner") or {}
            print("  owner:", {"mid": owner.get("mid"), "name": str(owner.get("name"))[:20]})
        return 0
    finally:
        await queue.aclose()
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
