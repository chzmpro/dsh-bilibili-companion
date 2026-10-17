"""从 curator 的登录态读取真实小号 UID（**只打印 UID 与昵称，绝不打印凭据**）。

用法::

    ./.venv/Scripts/python.exe scripts/read_account_identity.py

用途：把主人的"真实小号 UID"从**实际登录态**读出来填进 config.yaml，
而不是让主人手抄。这同时是对 `core/bilibili.py` 读取层的**首次真实平台验证**。

红线：不打印 cookie、不写日志、不落盘任何凭据。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.bilibili import BilibiliReader, BilibiliReadError  # noqa: E402
from core.credentials import load_cookie  # noqa: E402
from core.runtime import build_runtime  # noqa: E402

#: curator 的登录态快照（它自己的 data/cookie.txt）
CURATOR_COOKIE = Path(r"D:\DeepSeek\projects\dsh-bili-curator\data\cookie.txt")


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


async def main() -> int:
    _configure_stdio()

    if not CURATOR_COOKIE.exists():
        print(f"未找到 curator 登录态：{CURATOR_COOKIE}", file=sys.stderr)
        return 2

    bundle = load_cookie(CURATOR_COOKIE)
    print(f"已载入登录态：{len(bundle.values)} 个 cookie 项")
    print(f"具备发布所需凭据：{'是' if bundle.has_write_credentials() else '否'}")
    print("（凭据值不会显示，也不会写进任何文件）")
    print()

    runtime = await build_runtime(with_worker=False)
    try:
        # 把 curator 的凭据注入本项目读取层（同一个 RequestQueue，自动限速+缓存）
        reader = BilibiliReader(queue=runtime.queue, cookies=bundle.values)
        try:
            nav = await reader.get_nav()
        except BilibiliReadError as exc:
            print(f"读取失败：{exc.code} {exc}", file=sys.stderr)
            print("提示：登录态可能已过期 → 需主人重新扫码。", file=sys.stderr)
            return 3

        print("═══ 实际登录态（来自 B 站 nav 接口）═══")
        print(f"  isLogin : {nav.is_login}")
        print(f"  mid     : {nav.mid}")
        print(f"  uname   : {nav.uname}")
        print(f"  WBI keys: {'已获取' if nav.has_wbi_keys else '缺失'}")
        print()
        print("═══ 可直接粘进 config.yaml ═══")
        print("account:")
        print(f"  agent_mid: {nav.mid}")
        print("  owner_mid: 0          # ← 主号 UID 仍需主人提供（B站不会告知）")
        print(f'  owner_name_hint: ""   # 已知小号昵称：{nav.uname}')
        print()
        print("⚠️ 提示：agent_mid 就是上面这个 mid，已从**实际登录态**读取。")
        print("   未核验到 rpid/作者 UID 时不得标记 sent_verified（门禁已实现）。")
        return 0
    finally:
        await runtime.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
