"""扫码登录（B 站 passport）—— 让人用入口带主人走完一次登录。

## 为什么要有它

任务书把"小号扫码登录"列为**主人待办**：现在只能让主人手工从浏览器导出 cookie
再 ``import-cookie``。这条通道把它变成"给个二维码，扫一下就完事"。

## 接口事实（**照竞品 `lib/login.mjs` 的实测实现**，含它标注的踩坑点）

======================  ==================================================
步骤                    接口
======================  ==================================================
① 申请二维码             ``GET /x/passport-login/web/qrcode/generate``
② 轮询扫码结果           ``GET /x/passport-login/web/qrcode/poll``
③ 补 buvid3（可选）      ``GET /x/frontend/finger/spi``
======================  ==================================================

★★ **关键事实**（竞品注释原文）：

* **凭证在轮询响应的 ``Set-Cookie`` 头里**，**不在** ``data.url`` 里 ——
  那个 url 是别的域的游戏票，**别在 url 里找 SESSDATA**；
* 所以请求必须带 ``redirect="manual"``，否则跟随跳转就把 Set-Cookie 丢了；
* ``data.code``：``86101`` 未扫 / ``86090`` 已扫待确认 / ``0`` 成功 / ``86038`` 过期。

## 安全边界

* 本模块**只写主人自己的凭据文件**（走 :func:`core.credentials.save_cookie`）；
* 返回值里**只有 cookie 名字与数量**，**永不回显值**；
* 轮询有**次数与间隔上限**（默认约 3 分钟），超时明确报错，不无限轮询。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from core.credentials import CookieBundle, save_cookie

PASSPORT = "https://passport.bilibili.com"
GENERATE_PATH = "/x/passport-login/web/qrcode/generate"
POLL_PATH = "/x/passport-login/web/qrcode/poll"
SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)

#: 轮询状态（**注意 "ok" 是成功**）
PollState = Literal["ok", "waiting", "scanned", "expired", "unknown"]

#: 轮询状态码 → 语义
CODE_TO_STATE: dict[int, PollState] = {
    0: "ok",
    86101: "waiting",  # 未扫码
    86090: "scanned",  # 已扫码，待手机确认
    86038: "expired",  # 二维码过期
}

#: 默认轮询节奏：每 2 秒一次、最多 90 次（≈3 分钟）
DEFAULT_POLL_INTERVAL_SEC = 2.0
DEFAULT_MAX_POLLS = 90

#: 登录态里我们要留下的字段（**四个**，与竞品一致）
_WANTED = {
    "SESSDATA": "sessdata",
    "bili_jct": "bili_jct",
    "DedeUserID": "dedeuserid",
    "buvid3": "buvid3",
}


class LoginError(RuntimeError):
    """扫码登录失败。``code`` 是稳定的语义码。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class QrSession:
    """一次扫码会话（**不含任何凭据**）。"""

    key: str
    url: str


@dataclass(frozen=True)
class PollResult:
    """一次轮询的结果。

    ``credential`` 只在 ``state == "ok"`` 时有值，且**永不打印**。
    """

    state: PollState
    code: int | None = None
    message: str = ""
    credential: CookieBundle | None = None

    def to_dict(self) -> dict[str, Any]:
        """**去掉 credential** 之后的安全形态（给人看/进日志用）。"""
        return {
            "state": self.state,
            "code": self.code,
            "message": self.message,
            "has_credential": self.credential is not None,
        }


@dataclass
class LoginOutcome:
    """``login_flow`` 的最终结果。"""

    ok: bool
    state: str
    message: str = ""
    names: list[str] = field(default_factory=list)
    count: int = 0
    saved_to: str = ""


def pick_cookies(set_cookie_headers: list[str]) -> dict[str, str]:
    """从 ``Set-Cookie`` 头里抠出我们要的四个字段。

    ★ 竞品的实测要点：**凭据在这里，不在 ``data.url`` 里**。
    只取每行第一个 ``;`` 之前的部分（那才是 ``name=value``）。
    """
    jar: dict[str, str] = {}
    for raw in set_cookie_headers:
        first = str(raw).split(";")[0]
        if "=" not in first:
            continue
        name, _, value = first.partition("=")
        name = name.strip()
        mapped = _WANTED.get(name)
        if mapped:
            jar[mapped] = value.strip()
    return jar


async def start_qr(
    *, client: httpx.AsyncClient | None = None
) -> QrSession:
    """申请一个登录二维码。"""
    owns = client is None
    http = client or httpx.AsyncClient(
        timeout=httpx.Timeout(20.0),
        headers={"User-Agent": DEFAULT_UA, "Referer": "https://www.bilibili.com/"},
        trust_env=False,
    )
    try:
        response = await http.get(
            f"{PASSPORT}{GENERATE_PATH}", params={"source": "main-fe-header"}
        )
        body = response.json()
    finally:
        if owns:
            await http.aclose()

    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or not data.get("qrcode_key") or not data.get("url"):
        message = body.get("message") if isinstance(body, dict) else ""
        raise LoginError(
            "QR_GENERATE_FAILED",
            f"生成二维码失败：{message or '返回里没有 qrcode_key'}",
        )
    return QrSession(key=str(data["qrcode_key"]), url=str(data["url"]))


async def ensure_buvid3(
    jar: dict[str, str], *, client: httpx.AsyncClient | None = None
) -> dict[str, str]:
    """缺 ``buvid3`` 时补一张"指纹"（**非致命**：拿不到就不加）。"""
    if jar.get("buvid3"):
        return jar
    owns = client is None
    http = client or httpx.AsyncClient(
        timeout=httpx.Timeout(10.0), headers={"User-Agent": DEFAULT_UA}, trust_env=False
    )
    try:
        response = await http.get(SPI_URL)
        payload = response.json()
        value = (payload.get("data") or {}).get("b_3") if isinstance(payload, dict) else None
        if value:
            jar["buvid3"] = str(value)
    except Exception:  # noqa: BLE001 —— 补指纹失败不该让登录失败
        pass
    finally:
        if owns:
            await http.aclose()
    return jar


async def poll_once(
    key: str, *, client: httpx.AsyncClient | None = None
) -> PollResult:
    """轮询一次扫码结果。

    ⚠️ ``follow_redirects=False``（等于竞品的 ``redirect: 'manual'``）——
    跟随跳转会把承载凭据的 ``Set-Cookie`` 丢掉。
    """
    owns = client is None
    http = client or httpx.AsyncClient(
        timeout=httpx.Timeout(20.0),
        headers={"User-Agent": DEFAULT_UA, "Referer": "https://www.bilibili.com/"},
        trust_env=False,
        follow_redirects=False,
    )
    try:
        response = await http.get(
            f"{PASSPORT}{POLL_PATH}",
            params={"qrcode_key": key, "source": "main-fe-header"},
        )
        set_cookies = response.headers.get_list("set-cookie")
        try:
            body: dict[str, Any] = response.json()
        except ValueError:
            body = {}
        code = ((body.get("data") or {}) if isinstance(body, dict) else {}).get("code")

        if code == 0:
            jar = await ensure_buvid3(pick_cookies(set_cookies), client=client)
            if not jar.get("sessdata"):
                # ★ 竞品同款处理：登录成功了但没拿到 SESSDATA → 不假称成功
                return PollResult(
                    state="unknown",
                    code=0,
                    message="登录成功了，但响应里没有 SESSDATA —— 记录这次结果再排查",
                )
            return PollResult(state="ok", code=0, credential=CookieBundle(jar))

        numeric = int(code) if isinstance(code, int) else -1
        state: PollState = CODE_TO_STATE.get(numeric, "unknown")
        message = str(body.get("message") or "") if isinstance(body, dict) else ""
        return PollResult(
            state=state,
            code=numeric if isinstance(code, int) else None,
            message=message,
        )
    finally:
        if owns:
            await http.aclose()


async def login_flow(
    *,
    poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
    max_polls: int = DEFAULT_MAX_POLLS,
    on_qr: Any = None,
    on_state: Any = None,
    client: httpx.AsyncClient | None = None,
    sleep: Any = None,
) -> LoginOutcome:
    """完整走一次扫码登录：申请二维码 → 轮询 → 存凭据。

    :param on_qr: ``callable(session)`` —— 拿到二维码时回调（人用入口用它显示）
    :param on_state: ``callable(state: str)`` —— 每次状态变化时回调（给进度用）
    :returns: :class:`LoginOutcome`（``ok=True`` 时凭据**已落盘**）
    """
    sleep_fn = sleep or asyncio.sleep

    try:
        session = await start_qr(client=client)
    except LoginError as exc:
        return LoginOutcome(ok=False, state="error", message=f"{exc.code}: {exc}")

    if callable(on_qr):
        on_qr(session)

    last_state = ""
    for _ in range(max_polls):
        result = await poll_once(session.key, client=client)

        if result.state != last_state and callable(on_state):
            on_state(result.state)
            last_state = result.state

        if result.state == "ok" and result.credential is not None:
            path = save_cookie(result.credential)
            return LoginOutcome(
                ok=True,
                state="ok",
                message="登录成功，凭据已写入本机数据目录",
                names=result.credential.names(),
                count=len(result.credential.values),
                saved_to=str(path),
            )
        if result.state == "expired":
            return LoginOutcome(ok=False, state="expired", message="二维码过期了，请重新开始")
        if result.state == "unknown" and result.code == 0:
            # 登录成功但拿不到 SESSDATA —— 如实报告，不假称成功
            return LoginOutcome(ok=False, state="unknown", message=result.message)

        await sleep_fn(poll_interval_sec)

    return LoginOutcome(
        ok=False,
        state="timeout",
        message=f"等了约 {int(poll_interval_sec * max_polls)} 秒还没确认，已停止（可重新开始）",
    )
