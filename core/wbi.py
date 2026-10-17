"""WBI 签名。

**来源与许可**：算法与实现思路移植自同工作区的 ``projects/dsh-bili-curator``
（该项目为 MIT，自建 httpx 客户端，**零 GPL**）。
此处按本项目结构重写，不 import curator 包（两套 venv 隔离）。

实测要点（见 ``docs/compatibility.md`` 引用的交接包 04 §1.2）：

* ``wts`` **必须参与签名**，不能先签后加。
* ``!()*`` **只从值里剔除**，不要动 key。
* URL 编码**必须在 md5 之前**完成。
* key 缓存 TTL **300 秒**；超时会遇到 ``v_voucher``。
* ``nav`` 接口本身**不需要签名**（否则取 key 会死循环）。
"""

from __future__ import annotations

import hashlib
import time
from typing import Final
from urllib.parse import quote

MIXIN_KEY_ENC_TAB: Final[list[int]] = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]

MIXIN_KEY_LEN: Final[int] = 32
_FILTER_CHARS: Final[str] = "!'()*"
KEY_TTL_SEC: Final[float] = 300.0


class WbiError(Exception):
    """WBI 相关错误（如 nav 未返回 wbi keys）。"""


def extract_key_from_url(url: str) -> str:
    """从 ``https://i0.hdslb.com/bfs/wbi/<key>.png`` 提取 ``<key>``。"""
    name = url.rsplit("/", 1)[-1]
    return name.split(".", 1)[0]


def mixin_key(img_key: str, sub_key: str) -> str:
    orig = img_key + sub_key
    mixed = "".join(orig[i] for i in MIXIN_KEY_ENC_TAB if i < len(orig))
    return mixed[:MIXIN_KEY_LEN]


def _sanitize(value: object) -> str:
    return str(value).translate({ord(c): None for c in _FILTER_CHARS})


def _canonical_query(params: dict[str, object], mixin: str, wts: int) -> str:
    query = "&".join(
        f"{quote(str(k), safe='')}={quote(_sanitize(v), safe='')}"
        for k, v in sorted({**params, "wts": wts}.items(), key=lambda kv: str(kv[0]))
    )
    return query + mixin


def sign_params(params: dict[str, object], mixin: str, *, wts: int | None = None) -> str:
    """签名并返回完整 query 串（含 ``w_rid``）。"""
    ts = int(time.time()) if wts is None else int(wts)
    canonical = _canonical_query(params, mixin, ts)
    w_rid = hashlib.md5(canonical.encode("utf-8")).hexdigest()
    plain = canonical[: -len(mixin)]
    return f"{plain}&w_rid={w_rid}"


class WbiSigner:
    """带 key 缓存的签名器。"""

    def __init__(self) -> None:
        self._mixin: str | None = None
        self._fetched_at: float = 0.0

    @property
    def cached_mixin(self) -> str | None:
        return self._mixin

    def is_expired(self, *, now: float | None = None) -> bool:
        if self._mixin is None:
            return True
        t = time.monotonic() if now is None else now
        return (t - self._fetched_at) > KEY_TTL_SEC

    def invalidate(self) -> None:
        self._mixin = None
        self._fetched_at = 0.0

    def set_keys(self, img_key: str, sub_key: str, *, now: float | None = None) -> str:
        if not img_key or not sub_key:
            raise WbiError("WBI key 为空：nav 响应缺少 wbi_img.img_url 或 sub_url")
        self._mixin = mixin_key(img_key, sub_key)
        self._fetched_at = time.monotonic() if now is None else now
        return self._mixin

    def sign(self, params: dict[str, object], *, wts: int | None = None) -> str:
        if self._mixin is None:
            raise WbiError("WBI mixin key 尚未获取")
        return sign_params(params, self._mixin, wts=wts)

    def sign_dict(self, params: dict[str, object], *, wts: int | None = None) -> dict[str, object]:
        """签名并返回**字典**参数（含 ``wts``/``w_rid``），交给 httpx 编码。"""
        if self._mixin is None:
            raise WbiError("WBI mixin key 尚未获取")
        ts = int(time.time()) if wts is None else int(wts)
        canonical = _canonical_query(params, self._mixin, ts)
        w_rid = hashlib.md5(canonical.encode("utf-8")).hexdigest()
        return {**params, "wts": ts, "w_rid": w_rid}
