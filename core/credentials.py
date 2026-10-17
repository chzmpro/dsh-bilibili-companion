"""凭据（Cookie）装载与身份核对。

**红线**：Cookie 值**永不**进日志 / LLM 上下文 / MCP 返回 / git / 日报。
本模块只对外暴露：cookie 名字数量、UID、是否登录——**不暴露任何值**。

凭据文件格式（``%LOCALAPPDATA%\\dsh-bilibili-companion\\cookie.txt``）：一行
``k1=v1; k2=v2``，与浏览器 Cookie 头同形。文件必须 gitignored。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.paths import cookie_path as default_cookie_path

#: 已知的 B 站登录态 cookie 名（用于报告"有哪些"，**不报告值**）
KNOWN_COOKIE_NAMES: frozenset[str] = frozenset(
    {"sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid", "dedeuserid__ckmd5", "sid"}
)

#: 发布动作必需的 cookie（缺 bili_jct 无法通过 csrf）
REQUIRED_FOR_WRITE: frozenset[str] = frozenset({"sessdata", "bili_jct"})


class CredentialError(RuntimeError):
    """凭据缺失或不可用。**消息中不得含值。**"""


@dataclass(frozen=True)
class CookieBundle:
    """一组 cookie。``repr`` 被刻意屏蔽，避免误入日志。"""

    values: dict[str, str]

    def __repr__(self) -> str:
        return f"CookieBundle({len(self.values)} keys, values hidden)"

    __str__ = __repr__

    def to_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.values.items())

    def names(self) -> list[str]:
        """可安全报告的名字列表（小写）。"""
        return sorted(k.lower() for k in self.values)

    def has_write_credentials(self) -> bool:
        return REQUIRED_FOR_WRITE.issubset({k.lower() for k in self.values})

    def csrf_token(self) -> str | None:
        for key, value in self.values.items():
            if key.lower() == "bili_jct":
                return value
        return None


def parse_cookie_header(raw: str) -> CookieBundle:
    """解析 ``k=v; k2=v2`` 形式。忽略空项与无 ``=`` 的项。"""
    values: dict[str, str] = {}
    for chunk in raw.replace("\n", ";").split(";"):
        piece = chunk.strip()
        if not piece or "=" not in piece:
            continue
        key, _, value = piece.partition("=")
        key = key.strip()
        if key:
            values[key] = value.strip()
    return CookieBundle(values)


def load_cookie(path: Path | str | None = None) -> CookieBundle:
    """从文件装载凭据。

    Raises:
        CredentialError: 文件不存在或解析后无有效项。**不提值。**
    """
    target = Path(path) if path is not None else default_cookie_path()
    if not target.exists():
        raise CredentialError(
            f"未找到凭据文件（{target.name}）。请在官方 App/页面登录小号后，"
            "用人用入口写入本机凭据文件。"
        )
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise CredentialError(f"凭据文件不可读：{type(exc).__name__}") from exc

    bundle = parse_cookie_header(raw)
    if not bundle.values:
        raise CredentialError("凭据文件为空或格式不可识别。")
    return bundle


def save_cookie(bundle: CookieBundle, path: Path | str | None = None) -> Path:
    """写入凭据文件（**人用入口专用**）。返回写入路径。"""
    target = Path(path) if path is not None else default_cookie_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(bundle.to_header(), encoding="utf-8")
    return target


def cookie_diagnostics(path: Path | str | None = None) -> dict[str, object]:
    """可安全放进 ``companion_status`` 的诊断信息：**只有名字与数量**。"""
    target = Path(path) if path is not None else default_cookie_path()
    if not target.exists():
        return {"present": False, "names": [], "count": 0, "writable": False}
    try:
        bundle = load_cookie(target)
    except CredentialError as exc:
        return {"present": True, "names": [], "count": 0, "writable": False, "error": str(exc)}
    return {
        "present": True,
        "names": bundle.names(),
        "count": len(bundle.values),
        "known": sorted(set(bundle.names()) & KNOWN_COOKIE_NAMES),
        "writable": bundle.has_write_credentials(),
    }
