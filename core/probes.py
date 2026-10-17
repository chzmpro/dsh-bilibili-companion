"""只读探测：在一个动作之前把"现在到底能不能干活"问清楚。

用途：``run_once`` 与诊断。**只做只读调用**（``nav``），
不读视频、不碰评论、不产生 AI 费用。

为什么需要它：任务书要求把"登录失效 / 验证码 / 限流"当作**一等异常分支**
明确处理，而不是让 DSH 忽略后继续跑。探测给出**可读的停机原因**，
避免"跑了半天才发现根本没登录"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.bilibili import BilibiliReader, BilibiliReadError
from core.credentials import cookie_diagnostics
from core.health import (
    BLOCKING_STATES,
    READY,
    VERIFICATION_REQUIRED,
    human_action_for,
)
from core.ratelimit import RateLimitExceeded, RequestsBlocked


@dataclass
class ProbeReport:
    """一次只读探测的结果。"""

    platform_health: str = READY
    blocking_reason: str = ""
    human_action: str = ""
    login_ok: bool = False
    logged_in_mid: int = 0
    logged_in_name: str = ""
    agent_mid_configured: int = 0
    mid_matches: bool | None = None
    credentials: dict[str, Any] = field(default_factory=dict)
    text_worker_usable: bool = False
    text_worker_code: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform_health": self.platform_health,
            "blocking_reason": self.blocking_reason,
            "human_action": self.human_action,
            "login_ok": self.login_ok,
            "logged_in_mid": self.logged_in_mid,
            "logged_in_name": self.logged_in_name,
            "agent_mid_configured": self.agent_mid_configured,
            "mid_matches": self.mid_matches,
            # ★ 只有名字与数量，绝无凭据值
            "credentials": self.credentials,
            "text_worker_usable": self.text_worker_usable,
            "text_worker_code": self.text_worker_code,
            "notes": self.notes,
        }


async def probe(
    *,
    reader: BilibiliReader,
    config: Any,
    worker: Any = None,
    check_login: bool = True,
) -> ProbeReport:
    """只读探测。**不抛异常**——把问题表达为报告字段。

    ``worker`` 目前只用于将来扩展（例如探测豆包连通性）；
    豆包可用性现在走 ``config.text_worker_status()``（纯本地判断，不发请求）。
    """
    _ = worker
    report = ProbeReport()
    report.agent_mid_configured = int(config.account.agent_mid)
    report.credentials = cookie_diagnostics()

    worker_ok, worker_code = config.text_worker_status()
    report.text_worker_usable = worker_ok
    report.text_worker_code = worker_code

    # ① 健康门禁（本地状态，先查——不发请求）
    state = reader.queue.db.health_state()
    report.platform_health = state
    if state in BLOCKING_STATES:
        report.blocking_reason = f"平台健康状态为 {state}"
        report.human_action = human_action_for(state)
        report.notes.append("未发起任何请求：阻断状态优先。")
        return report

    # ② 登录态（只读，nav 不需要签名）
    if not check_login:
        return report

    if not report.credentials.get("present"):
        report.notes.append(
            "本机没有凭据文件。公开搜索仍可用，但**字幕需要登录**，"
            "评论区读取也会受限 → 证据等级会降级为 description_only。"
        )

    try:
        nav = await reader.get_nav()
    except RequestsBlocked as exc:
        report.platform_health = exc.state
        report.blocking_reason = f"请求被阻断：{exc.state}"
        report.human_action = exc.human_action
        return report
    except RateLimitExceeded as exc:
        report.platform_health = "platform_error"
        report.blocking_reason = f"连续限流：{exc}"
        report.human_action = human_action_for("platform_error")
        return report
    except BilibiliReadError as exc:
        # 未登录时 nav 仍能返回（isLogin=false），所以这里多是网络/风控问题
        report.notes.append(f"nav 读取失败（{exc.code}）：{exc}")
        if exc.code in BLOCKING_STATES or exc.code.upper() in {
            s.upper() for s in BLOCKING_STATES
        }:
            report.blocking_reason = f"平台状态 {exc.code}"
            report.human_action = human_action_for(VERIFICATION_REQUIRED)
        return report
    except Exception as exc:  # noqa: BLE001
        # ★ 探测是诊断工具，**绝不**因为意外异常而让调用方崩掉。
        report.notes.append(f"nav 探测遇到意外异常（{type(exc).__name__}）：{exc}")
        return report

    report.login_ok = nav.is_login
    report.logged_in_mid = nav.mid
    report.logged_in_name = nav.uname

    if not nav.is_login:
        report.notes.append(
            "未登录（isLogin=false）。公开搜索可用；字幕不可用。"
            "需要字幕/评论时请由主人扫码登录小号。"
        )
    elif report.agent_mid_configured > 0:
        report.mid_matches = nav.mid == report.agent_mid_configured
        if not report.mid_matches:
            report.notes.append(
                f"⚠️ 实际登录 UID({nav.mid}) 与配置 agent_mid"
                f"({report.agent_mid_configured}) 不符 —— **发布会被门禁拒绝**。"
            )
    else:
        report.notes.append(
            f"已登录 UID={nav.mid}，但配置里 agent_mid=0（尚未填写）。"
            "只读功能不受影响；真实发布需要填写。"
        )

    # ③ 发布条件
    can_publish, reason = config.can_publish()
    report.notes.append(
        f"发布门禁：{'允许' if can_publish else '拒绝'}（{reason}）"
    )
    if not report.credentials.get("writable") and nav.is_login:
        report.notes.append(
            "凭据缺少发布所需项（需要 sessdata + bili_jct）→ 无法通过 csrf。"
        )

    return report
