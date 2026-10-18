"""受控发布与幂等（任务书 §4/§5/§7 任务 5）。

## 9 道门禁（全部通过才允许真实提交）

1. 草稿存在且状态合法（``approved`` 或 ``auto`` 模式下的 ``pending``）
2. ``dry_run == false``
3. ``publish_enabled == true``
4. ``mode != observe``
5. ``pause == false``
6. 平台健康状态**不在**阻断集合（验证码/认证失效/平台错误 → 直接拒绝）
7. **身份每次从实际登录态读取**并比对 ``agent_mid``（不信配置、不信缓存）
8. 目标校验（回复目标必须是自己的评论串，或已开启的第三方讨论且相关）
9. 配额（顶层/回复/单串回复）+ **幂等键**：
   顶层 ``agent_mid+bvid+top``；回复 ``agent_mid+target_rpid+reply``

## 状态机（任务书 §5）

``draft → reserved → sent_verified``；不确定 → ``uncertain``。

* ``reserved`` **在点击前**持久化（事务内）。
* 点击后超时/断连/进程重启 → ``uncertain``，**冻结同目标后续动作，不自动重发**。
* ``uncertain`` **占用额度**，不靠时间自动释放。
* 模拟与真实**分开存储/计数**；模拟 ID **不得**当远端 ``rpid``。

## 明令禁止

**不做**：轮换 IP/账号、伪造客户端、改指纹、绕过验证码。
`publish_draft`/`publish_reply` **不能接收任意文本或任意 URL**——
只能引用已保存、带证据、目标受限的草稿。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from dataclasses import field as _field
from typing import Any, Protocol

from core import health
from core.bilibili import BilibiliReader
from core.health import BLOCKING_STATES
from core.storage import (
    FAVORITE_ACTION,
    LIKE_ACTION,
    REPLY_ACTION,
    TOP_ACTION,
    Database,
)

#: 拒绝码（稳定，便于测试与 DSH 判断）
REJECT_DRAFT_NOT_FOUND = "DRAFT_NOT_FOUND"
REJECT_DRAFT_NOT_APPROVED = "DRAFT_NOT_APPROVED"
REJECT_DRY_RUN = "DRY_RUN"
REJECT_PUBLISH_DISABLED = "PUBLISH_DISABLED"
REJECT_MODE_OBSERVE = "MODE_OBSERVE"
REJECT_PAUSED = "PAUSED"
REJECT_PLATFORM_BLOCKED = "PLATFORM_BLOCKED"
REJECT_AGENT_MID_NOT_CONFIGURED = "AGENT_MID_NOT_CONFIGURED"
REJECT_IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
REJECT_IDENTITY_UNVERIFIED = "IDENTITY_UNVERIFIED"
REJECT_TARGET_INVALID = "TARGET_INVALID"
REJECT_THIRD_PARTY_DISABLED = "THIRD_PARTY_THREADS_DISABLED"
REJECT_QUOTA_TOP = "QUOTA_TOP_EXCEEDED"
REJECT_QUOTA_REPLY = "QUOTA_REPLY_EXCEEDED"
REJECT_QUOTA_THREAD = "QUOTA_THREAD_EXCEEDED"
REJECT_DUPLICATE = "DUPLICATE_ACTION"
REJECT_TARGET_FROZEN = "TARGET_FROZEN_UNCERTAIN"
REJECT_EVIDENCE_MISSING = "EVIDENCE_MISSING"
REJECT_LIKING_DISABLED = "LIKING_DISABLED"
REJECT_FAVORITING_DISABLED = "FAVORITING_DISABLED"
REJECT_QUOTA_LIKE = "QUOTA_LIKE_EXCEEDED"
REJECT_QUOTA_FAVORITE = "QUOTA_FAVORITE_EXCEEDED"
REJECT_ALREADY_LIKED = "ALREADY_LIKED"
REJECT_ALREADY_FAVORITED = "ALREADY_FAVORITED"
REJECT_SCORE_TOO_LOW = "SCORE_TOO_LOW"
REJECT_NO_FAVORITE_FOLDER = "NO_FAVORITE_FOLDER"


class ActionExecutor(Protocol):
    """真实写动作的执行器（由 :mod:`core.bilibili_write` 实现）。

    只有门禁全过才会被调用；**测试中一律用假执行器**。
    """

    async def send_top_comment(self, *, aid: int, bvid: str, text: str) -> dict[str, Any]:
        """返回 ``{"remote_rpid": ..., "remote_url": ...}``；失败时抛异常。"""
        ...

    async def send_reply(
        self, *, aid: int, bvid: str, root: str, parent: str, text: str
    ) -> dict[str, Any]: ...

    async def like_video(self, *, bvid: str, undo: bool = False) -> Any:
        """点赞 / 取消点赞。返回带 ``ok`` 的结果对象。"""
        ...

    async def favorite_video(
        self,
        *,
        aid: int,
        add_media_ids: list[int] | None = None,
        del_media_ids: list[int] | None = None,
    ) -> Any:
        """收藏 / 取消收藏。返回带 ``ok`` 的结果对象。"""
        ...

    async def list_favorite_folders(self, *, up_mid: int) -> list[dict[str, Any]]:
        """列出小号自己的收藏夹（用于把名字解析成 media_id）。"""
        ...


@dataclass
class GateResult:
    allowed: bool
    reject_code: str = "OK"
    detail: str = ""
    checks: dict[str, Any] = field(default_factory=dict)


@dataclass
class PublishResult:
    ok: bool
    code: str
    action_id: str | None = None
    remote_rpid: str | None = None
    remote_url: str | None = None
    simulated: bool = False
    detail: str = ""
    gates: dict[str, Any] = _field(default_factory=dict)


class PublishGate:
    """发布门禁 + 幂等状态机。

    :param reader: 用于**每次**从实际登录态读取身份
    :param executor: 真实写执行器；``None`` 时只允许模拟
    """

    def __init__(
        self,
        *,
        db: Database,
        config: Any,
        reader: BilibiliReader | None = None,
        executor: ActionExecutor | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.reader = reader
        self.executor = executor

    # ---- 写侧健康传播 ------------------------------------------------------

    def _record_write_failure(self, exc: BaseException) -> None:
        """把写请求的稳定错误码交给**同一套**全局健康状态机（#5）。

        ★ 为什么不能只把当前动作置 ``uncertain``：
        写请求走的是独立 httpx 客户端，不经过读队列；如果 ``-412`` 只冻结
        当前草稿，其它 BV、其它互动类型的后续动作仍会按 ``ready`` 放行。

        ★ 不把一般业务错误升级成验证码：
        只有 ``BILI_CODE_*`` / ``HTTP_*`` 这类**平台响应码**才进分类器；
        网络错误 / 响应不是 JSON 属于"结果未知"，不改变平台健康状态。
        """
        raw_code = getattr(exc, "code", None)
        http_status = getattr(exc, "http_status", None)
        biz_code: str | None = None
        if isinstance(raw_code, str) and raw_code.startswith("BILI_CODE_"):
            biz_code = raw_code[len("BILI_CODE_") :]
        elif isinstance(raw_code, str) and raw_code.startswith("HTTP_"):
            suffix = raw_code[len("HTTP_") :]
            if suffix.isdigit():
                http_status = int(suffix)

        if biz_code is None and http_status is None:
            return

        message = str(exc)
        queue = getattr(self.reader, "queue", None)
        if queue is not None:
            try:
                queue.record_response(
                    code=biz_code, http_status=http_status, message=message
                )
                return
            except Exception:  # noqa: BLE001 - 连续限流会在落库后抛，不重复升级
                if self.db.health_state() in BLOCKING_STATES:
                    return

        verdict = health.classify(code=biz_code, http_status=http_status, message=message)
        if verdict.state == health.READY:
            return
        self.db.record_health(
            state=verdict.state,
            raw_code=biz_code if biz_code is not None else str(http_status),
            category=verdict.category,
            detail=verdict.human_action,
        )

    @staticmethod
    def _stable_error_code(exc: BaseException) -> str:
        """保留稳定的平台错误码；没有就用异常类名（不编造）。"""
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code:
            return code
        return type(exc).__name__

    # ---- 门禁 --------------------------------------------------------------

    def check(
        self,
        *,
        draft_id: str,
        identity_mid: int | None,
        identity_verified: bool,
        target_checked: bool = True,
    ) -> GateResult:
        """执行除"身份读取"之外的全部门禁检查（身份由调用方传入）。

        ``target_checked``：回复目标是否已通过 :meth:`validate_reply_target`。
        """
        checks: dict[str, Any] = {}
        interaction = self.config.interaction

        draft = self.db.get_draft(draft_id)
        if draft is None:
            return GateResult(False, REJECT_DRAFT_NOT_FOUND, f"草稿不存在：{draft_id}")

        status = str(draft["status"])
        checks["draft_status"] = status
        allowed_statuses = {"approved"}
        if interaction.mode == "auto":
            allowed_statuses.add("pending")
        if status in ("reserved", "sent_verified"):
            return GateResult(False, REJECT_DUPLICATE, f"草稿已处理：{status}", checks)
        if status == "uncertain":
            return GateResult(False, REJECT_TARGET_FROZEN, "该草稿处于不确定态，已冻结", checks)
        if status not in allowed_statuses:
            return GateResult(
                False,
                REJECT_DRAFT_NOT_APPROVED if status == "pending" else REJECT_DRAFT_NOT_APPROVED,
                f"草稿未被批准（status={status}；review 模式需人用入口 approve）",
                checks,
            )

        checks["dry_run"] = interaction.dry_run
        checks["publish_enabled"] = interaction.publish_enabled
        checks["mode"] = interaction.mode
        checks["pause"] = interaction.pause
        checks["platform_health"] = self.db.health_state()

        # ★ 幂等检查必须**排在配额之前**：
        #   同一个动作再来一次并不是"又要占用一份新额度"，而是"这件事已经存在"。
        #   先报配额超额会把"重复提交"误诊为"配额用完"，掩盖真正的安全问题。
        draft_type = str(draft["draft_type"])
        target_rpid = str(draft["target_rpid"]) if draft["target_rpid"] else None
        existing = self.db.find_action_by_key(
            self.db.make_idempotency_key(
                agent_mid=int(self.config.account.agent_mid) or 0,
                action_type=draft_type,
                bvid=str(draft["bvid"]),
                target_rpid=target_rpid,
                is_simulated=False,  # ★ 只与**真实**动作比对
            )
        )
        if existing is not None:
            existing_status = str(existing["status"])
            checks["existing_action"] = existing_status
            # ★ ``reserved`` 残留与 ``uncertain`` 一样属于"结果未知"：
            #   预留已落库但没有任何结论（点击后超时 / 断连 / 进程重启）。
            #   按任务书 §5 必须冻结、不自动重发，而不是当成"已经发过了"。
            if existing_status in ("uncertain", "reserved"):
                return GateResult(
                    False,
                    REJECT_TARGET_FROZEN,
                    f"同目标存在结果未知的动作（status={existing_status}）："
                    "冻结，不自动重发，需人工核对",
                    checks,
                )
            if existing_status in ("sent_verified", "failed"):
                return GateResult(
                    False,
                    REJECT_DUPLICATE,
                    f"同幂等键已有动作（status={existing_status}），不重复提交",
                    checks,
                )

        # ★ 模拟模式：门禁只跑到"真实发布三条件"，返回 DRY_RUN（由调用方执行模拟）
        if interaction.dry_run:
            return GateResult(False, REJECT_DRY_RUN, "dry_run=true：只做模拟，不触达平台", checks)
        if not interaction.publish_enabled:
            return GateResult(False, REJECT_PUBLISH_DISABLED, "publish_enabled=false", checks)
        if interaction.mode == "observe":
            return GateResult(False, REJECT_MODE_OBSERVE, "observe 模式禁止任何公开写入", checks)
        if interaction.pause:
            return GateResult(False, REJECT_PAUSED, "已暂停新公开动作", checks)

        health_state = str(checks["platform_health"])
        if health_state in BLOCKING_STATES:
            return GateResult(
                False,
                REJECT_PLATFORM_BLOCKED,
                f"平台健康状态 {health_state}：必须人工恢复，程序不自动恢复",
                checks,
            )

        configured_mid = int(self.config.account.agent_mid)
        if configured_mid <= 0:
            return GateResult(False, REJECT_AGENT_MID_NOT_CONFIGURED, "未配置 agent_mid", checks)
        if not identity_verified:
            return GateResult(
                False, REJECT_IDENTITY_UNVERIFIED, "未能从实际登录态核验身份", checks
            )
        checks["identity_mid"] = identity_mid
        checks["configured_agent_mid"] = configured_mid
        if identity_mid != configured_mid:
            return GateResult(
                False,
                REJECT_IDENTITY_MISMATCH,
                f"实际登录 UID({identity_mid}) 与配置 agent_mid({configured_mid}) 不符",
                checks,
            )

        if not target_checked:
            return GateResult(False, REJECT_TARGET_INVALID, "回复目标未通过校验", checks)

        # 证据必须存在且属于同一 BV（任务书 §4）
        try:
            evidence_ids = json.loads(str(draft["evidence_ids"]))
        except json.JSONDecodeError:
            evidence_ids = []
        evidence_ok, reason = self.db.verify_evidence_ids(str(draft["bvid"]), evidence_ids)
        checks["evidence"] = reason
        if not evidence_ok:
            return GateResult(False, REJECT_EVIDENCE_MISSING, reason, checks)

        # 配额
        quota = self.quota_check(
            draft_type=str(draft["draft_type"]),
            bvid=str(draft["bvid"]),
            target_root=str(draft["target_root"] or ""),
            is_simulated=False,
        )
        checks["quota"] = quota
        if quota["reject_code"]:
            return GateResult(False, quota["reject_code"], quota["detail"], checks)

        return GateResult(True, "OK", "", checks)

    def quota_check(
        self, *, draft_type: str, bvid: str, target_root: str, is_simulated: bool
    ) -> dict[str, Any]:
        """配额检查。``uncertain``/``reserved`` 都计入（不靠时间释放）。"""
        _ = bvid
        interaction = self.config.interaction

        if draft_type == LIKE_ACTION:
            used = self.db.count_actions_today(action_type=LIKE_ACTION, is_simulated=is_simulated)
            limit = interaction.max_likes_per_day
            if used >= limit:
                return {
                    "reject_code": REJECT_QUOTA_LIKE,
                    "detail": f"今日点赞已用 {used}/{limit}",
                    "used": used,
                    "limit": limit,
                }
            return {"reject_code": "", "used": used, "limit": limit}

        if draft_type == FAVORITE_ACTION:
            used = self.db.count_actions_today(
                action_type=FAVORITE_ACTION, is_simulated=is_simulated
            )
            limit = interaction.max_favorites_per_day
            if used >= limit:
                return {
                    "reject_code": REJECT_QUOTA_FAVORITE,
                    "detail": f"今日收藏已用 {used}/{limit}",
                    "used": used,
                    "limit": limit,
                }
            return {"reject_code": "", "used": used, "limit": limit}

        if draft_type == TOP_ACTION:
            used = self.db.count_actions_today(action_type=TOP_ACTION, is_simulated=is_simulated)
            limit = interaction.max_top_comments_per_day
            if used >= limit:
                return {
                    "reject_code": REJECT_QUOTA_TOP,
                    "detail": f"今日顶层评论已用 {used}/{limit}",
                    "used": used,
                    "limit": limit,
                }
            return {"reject_code": "", "used": used, "limit": limit}

        used = self.db.count_actions_today(action_type=REPLY_ACTION, is_simulated=is_simulated)
        limit = interaction.max_replies_per_day
        if used >= limit:
            return {
                "reject_code": REJECT_QUOTA_REPLY,
                "detail": f"今日回复已用 {used}/{limit}",
                "used": used,
                "limit": limit,
            }
        if target_root:
            thread_used = self.db.count_replies_to_thread_today(
                target_root=target_root, is_simulated=is_simulated
            )
            thread_limit = interaction.max_replies_per_thread_per_day
            if thread_used >= thread_limit:
                return {
                    "reject_code": REJECT_QUOTA_THREAD,
                    "detail": f"该评论串今日回复已用 {thread_used}/{thread_limit}",
                    "used": thread_used,
                    "limit": thread_limit,
                }
        return {"reject_code": "", "used": used, "limit": limit}

    async def validate_reply_target(
        self, *, bvid: str, target_rpid: str, own_comment_rpids: set[str] | None = None
    ) -> tuple[bool, str]:
        """校验回复目标（任务书 §4）。

        允许的两种情况：

        1. **小号本人评论串**——只要目标是我们自己发出过的评论/回复，
           不需要第三方开关。
        2. **已学习视频上的相关第三方讨论**——**必须** ``allow_third_party_threads=true``。

        其余（未知 ``rpid``、跨 BV）一律拒绝。

        "自己的评论串"判定依据（任一即可）：

        * 调用方显式传入的 ``own_comment_rpids``；
        * 该 ``rpid`` 是本地 ``actions`` 表里**我们已发送过**的远端 ID；
        * 该 ``rpid`` 的作者就是小号本人（``author_mid == agent_mid``）；
        * 该 ``rpid`` 的 ``seen_replies.parent_rpid`` 指向我们发过的评论（楼中楼回复我们）。
        """
        row = self.db.query_one(
            "SELECT * FROM seen_replies WHERE remote_rpid = ?", (target_rpid,)
        )
        if row is not None and str(row["bvid"]) != bvid:
            return False, "TARGET_BVID_MISMATCH"

        if own_comment_rpids and target_rpid in own_comment_rpids:
            return True, "OK"

        if self._is_our_own_comment_thread(target_rpid, row=row):
            return True, "OK"

        if row is None:
            # 完全未知的目标：即便开关开着也不许凭空回复
            return False, "TARGET_UNKNOWN_RPID"

        if not self.config.interaction.allow_third_party_threads:
            return False, REJECT_THIRD_PARTY_DISABLED

        return True, "OK"

    def _is_our_own_comment_thread(self, target_rpid: str, *, row: Any = None) -> bool:
        """该目标是否属于小号本人的评论串。"""
        own = self.db.query_one(
            "SELECT 1 FROM actions WHERE remote_rpid = ? AND status = 'sent_verified' LIMIT 1",
            (target_rpid,),
        )
        if own is not None:
            return True

        if row is None:
            row = self.db.query_one(
                "SELECT * FROM seen_replies WHERE remote_rpid = ?", (target_rpid,)
            )
        if row is None:
            return False

        # 作者就是小号本人 → 天然属于我们自己的评论串
        agent_mid = int(self.config.account.agent_mid)
        if agent_mid > 0 and int(row["author_mid"] or 0) == agent_mid:
            return True

        # 楼中楼：父评论/根评论是我们发过的
        for candidate in (row["parent_rpid"], row["root_rpid"]):
            if not candidate:
                continue
            parent_own = self.db.query_one(
                "SELECT 1 FROM actions WHERE remote_rpid = ? AND status = 'sent_verified' "
                "LIMIT 1",
                (str(candidate),),
            )
            if parent_own is not None:
                return True
        return False

    async def read_identity(self) -> tuple[int | None, bool]:
        """从**实际登录态**读取 UID。绝不只信配置或缓存。

        返回 ``(mid, verified)``。

        * ``verified=True`` 只在 ``isLogin`` 为真且 ``mid > 0`` 时给出——
          这是唯一能进入发布门禁的状态。
        * 未登录时即使响应里带了 ``mid`` 也返回 ``verified=False``。
        """
        if self.reader is None:
            return None, False
        try:
            nav = await self.reader.get_nav()
        except Exception:  # noqa: BLE001 - 任何读取失败都视为"身份未核实"
            return None, False
        if not nav.is_login or nav.mid <= 0:
            return None, False
        return nav.mid, True

    # ---- 发布 --------------------------------------------------------------

    async def publish(
        self, *, draft_id: str, own_comment_rpids: set[str] | None = None
    ) -> PublishResult:
        """发布一个已存草稿。

        流程：读身份 → 校验目标 → 门禁 → **先持久化 reserved** → 提交/模拟 → 落终态。
        """
        draft = self.db.get_draft(draft_id)
        if draft is None:
            return PublishResult(False, REJECT_DRAFT_NOT_FOUND)

        identity_mid, verified = await self.read_identity()

        target_checked = True
        if str(draft["draft_type"]) == REPLY_ACTION:
            target_rpid = str(draft["target_rpid"] or "")
            if not target_rpid:
                target_checked = False
            else:
                target_checked, _reason = await self.validate_reply_target(
                    bvid=str(draft["bvid"]),
                    target_rpid=target_rpid,
                    own_comment_rpids=own_comment_rpids,
                )
                if not target_checked and self.db.freeze_target(
                    bvid=str(draft["bvid"]), target_rpid=target_rpid
                ):
                    return PublishResult(False, REJECT_TARGET_FROZEN, gates={"frozen": True})

        gate = self.check(
            draft_id=draft_id,
            identity_mid=identity_mid,
            identity_verified=verified,
            target_checked=target_checked,
        )

        # ★ 模拟分支：dry_run 时返回模拟成功，**绝不触达平台**，且模拟 ID 不当 rpid
        if gate.reject_code == REJECT_DRY_RUN:
            return await self._simulate(draft=draft, gate=gate)

        if not gate.allowed:
            return PublishResult(False, gate.reject_code, detail=gate.detail, gates=gate.checks)

        # ---- 真实提交 ------------------------------------------------------
        bvid = str(draft["bvid"])
        aid = self._aid_for(bvid)
        action_type = str(draft["draft_type"])
        send_target: str | None = (
            str(draft["target_rpid"]) if draft["target_rpid"] else None
        )

        action_id, outcome = self.db.reserve_action(
            agent_mid=identity_mid or 0,
            action_type=action_type,
            bvid=bvid,
            target_rpid=send_target,
            draft_id=draft_id,
            is_simulated=False,
        )
        if outcome == "duplicate":
            return PublishResult(False, REJECT_DUPLICATE, action_id=action_id, gates=gate.checks)
        if outcome == "uncertain_pending":
            return PublishResult(
                False, REJECT_TARGET_FROZEN, action_id=action_id, gates=gate.checks
            )

        self.db.set_draft_status(draft_id, "reserved")

        if self.executor is None:
            # 没有真实执行器 → 保持 reserved 并进 uncertain（绝不假装成功）
            self.db.settle_action(
                action_id=action_id, status="uncertain", error_code="EXECUTOR_UNAVAILABLE"
            )
            self.db.set_draft_status(draft_id, "uncertain")
            return PublishResult(
                False,
                "EXECUTOR_UNAVAILABLE",
                action_id=action_id,
                detail="未配置真实执行器；已置为 uncertain 并冻结，不自动重发",
                gates=gate.checks,
            )

        try:
            if action_type == TOP_ACTION:
                result = await self.executor.send_top_comment(
                    aid=aid, bvid=bvid, text=str(draft["text"])
                )
            else:
                result = await self.executor.send_reply(
                    aid=aid,
                    bvid=bvid,
                    root=str(draft["target_root"] or send_target or ""),
                    parent=send_target or "",
                    text=str(draft["text"]),
                )
        except Exception as exc:  # noqa: BLE001 - 任何异常都不得当作成功
            # 点击后异常 → uncertain，冻结，不自动重发；同时传播写侧风控
            self._record_write_failure(exc)
            self.db.settle_action(
                action_id=action_id, status="uncertain",
                error_code=self._stable_error_code(exc),
            )
            self.db.set_draft_status(draft_id, "uncertain")
            return PublishResult(
                False,
                "PUBLISH_UNCERTAIN",
                action_id=action_id,
                detail=f"提交后未获得明确结果（{type(exc).__name__}）：冻结并需人工核对",
                gates=gate.checks,
            )

        remote_rpid = str(result.get("remote_rpid", "") or "")
        remote_url = str(result.get("remote_url", "") or "")
        if not remote_rpid:
            # 没有远端 ID → 不得标记 sent_verified
            self.db.settle_action(
                action_id=action_id, status="uncertain", error_code="NO_REMOTE_RPID"
            )
            self.db.set_draft_status(draft_id, "uncertain")
            return PublishResult(
                False,
                "PUBLISH_UNVERIFIED",
                action_id=action_id,
                detail="未取得远端 rpid，不得标记 sent_verified",
                gates=gate.checks,
            )

        self.db.settle_action(
            action_id=action_id,
            status="sent_verified",
            remote_rpid=remote_rpid,
            remote_url=remote_url,
        )
        self.db.set_draft_status(draft_id, "sent_verified")
        return PublishResult(
            True,
            "OK",
            action_id=action_id,
            remote_rpid=remote_rpid,
            remote_url=remote_url,
            gates=gate.checks,
        )

    async def publish_like(self, *, bvid: str) -> PublishResult:
        """点赞一个视频（**与发布受同一套门禁约束**）。

        ★ 点赞也是**公开写入**：模式 / dry_run / 总开关 / 暂停 / 平台健康 /
        身份核对 / 配额 / 幂等，一个都不能少（主人 2026-10-15 要求实现）。

        ``like_state`` 已是 1 → 直接 ``ALREADY_LIKED``，不重复提交。
        """
        config = self.config
        checks: dict[str, Any] = {}

        if not config.persona.enable_liking:
            return PublishResult(False, REJECT_LIKING_DISABLED, detail="配置关闭了点赞")
        if self.db.get_video(bvid) is None:
            return PublishResult(False, REJECT_DRAFT_NOT_FOUND, detail=f"库里没有 {bvid}")

        state = self.db.get_video_state(bvid)
        checks["like_state"] = state["like_state"]
        if int(state["like_state"]) == 1:
            return PublishResult(
                False, REJECT_ALREADY_LIKED, detail="该视频已点赞过", gates=checks
            )

        identity_mid, verified = await self.read_identity()
        gate = self._write_gate(
            identity_mid=identity_mid, identity_verified=verified, checks=checks
        )
        if gate.reject_code == REJECT_DRY_RUN:
            return await self._simulate_action(
                action_type=LIKE_ACTION, bvid=bvid, target_rpid=None, gate=gate
            )
        if not gate.allowed:
            return PublishResult(
                False, gate.reject_code, detail=gate.detail, gates=gate.checks
            )

        quota = self.quota_check(
            draft_type=LIKE_ACTION, bvid=bvid, target_root="", is_simulated=False
        )
        if quota["reject_code"]:
            return PublishResult(False, quota["reject_code"], detail=quota["detail"])

        action_id, outcome = self.db.reserve_action(
            agent_mid=identity_mid or 0, action_type=LIKE_ACTION, bvid=bvid,
            target_rpid=None, draft_id=None, is_simulated=False,
        )
        if outcome == "duplicate":
            return PublishResult(False, REJECT_ALREADY_LIKED, action_id=action_id)
        if outcome == "uncertain_pending":
            return PublishResult(False, REJECT_TARGET_FROZEN, action_id=action_id)

        if self.executor is None:
            self.db.settle_action(
                action_id=action_id, status="uncertain", error_code="EXECUTOR_UNAVAILABLE"
            )
            return PublishResult(
                False, "EXECUTOR_UNAVAILABLE", action_id=action_id,
                detail="未配置真实执行器；已置 uncertain",
            )
        try:
            result = await self.executor.like_video(bvid=bvid)
        except Exception as exc:  # noqa: BLE001 - 任何异常都不得当作成功
            self._record_write_failure(exc)
            self.db.settle_action(
                action_id=action_id, status="uncertain",
                error_code=self._stable_error_code(exc),
            )
            return PublishResult(
                False, "PUBLISH_UNCERTAIN", action_id=action_id,
                detail=f"提交后结果未知（{type(exc).__name__}）",
            )
        if not getattr(result, "ok", False):
            self.db.settle_action(
                action_id=action_id, status="failed",
                error_code=str(getattr(result, "code", "LIKE_FAILED")),
            )
            return PublishResult(
                False, "LIKE_FAILED", action_id=action_id,
                detail=str(getattr(result, "message", "")),
            )

        self.db.settle_action(
            action_id=action_id, status="sent_verified",
            remote_rpid=getattr(result, "remote_id", None),
            remote_url=getattr(result, "remote_url", None),
        )
        self.db.set_video_like_state(bvid, 1)
        return PublishResult(
            True, "OK", action_id=action_id,
            remote_url=getattr(result, "remote_url", None), gates=gate.checks,
        )

    async def publish_favorite(
        self, *, bvid: str, folder_name: str, media_id: int
    ) -> PublishResult:
        """收藏一个视频到指定收藏夹。

        ``folder_name`` 用于日志与状态；``media_id`` 是提交给 B站 的 mlid
        （由 :meth:`resolve_favorite_folder` 从名字解析）。
        """
        config = self.config
        checks: dict[str, Any] = {"folder": folder_name, "media_id": media_id}

        if not config.persona.enable_favoriting:
            return PublishResult(False, REJECT_FAVORITING_DISABLED, detail="配置关闭了收藏")
        if self.db.get_video(bvid) is None:
            return PublishResult(False, REJECT_DRAFT_NOT_FOUND, detail=f"库里没有 {bvid}")
        if media_id <= 0:
            return PublishResult(
                False, REJECT_NO_FAVORITE_FOLDER, detail="未解析出有效的收藏夹 media_id"
            )

        state = self.db.get_video_state(bvid)
        if int(state["fav_state"]) == 1:
            return PublishResult(
                False, REJECT_ALREADY_FAVORITED, detail="该视频已收藏过", gates=checks
            )

        identity_mid, verified = await self.read_identity()
        gate = self._write_gate(
            identity_mid=identity_mid, identity_verified=verified, checks=checks
        )
        if gate.reject_code == REJECT_DRY_RUN:
            return await self._simulate_action(
                action_type=FAVORITE_ACTION, bvid=bvid, target_rpid=None, gate=gate
            )
        if not gate.allowed:
            return PublishResult(
                False, gate.reject_code, detail=gate.detail, gates=gate.checks
            )

        quota = self.quota_check(
            draft_type=FAVORITE_ACTION, bvid=bvid, target_root="", is_simulated=False
        )
        if quota["reject_code"]:
            return PublishResult(False, quota["reject_code"], detail=quota["detail"])

        action_id, outcome = self.db.reserve_action(
            agent_mid=identity_mid or 0, action_type=FAVORITE_ACTION, bvid=bvid,
            target_rpid=None, draft_id=None, is_simulated=False,
        )
        if outcome == "duplicate":
            return PublishResult(False, REJECT_ALREADY_FAVORITED, action_id=action_id)
        if outcome == "uncertain_pending":
            return PublishResult(False, REJECT_TARGET_FROZEN, action_id=action_id)

        if self.executor is None:
            self.db.settle_action(
                action_id=action_id, status="uncertain", error_code="EXECUTOR_UNAVAILABLE"
            )
            return PublishResult(
                False, "EXECUTOR_UNAVAILABLE", action_id=action_id,
                detail="未配置真实执行器；已置 uncertain",
            )
        aid = self._aid_for(bvid)
        try:
            result = await self.executor.favorite_video(aid=aid, add_media_ids=[media_id])
        except Exception as exc:  # noqa: BLE001
            self._record_write_failure(exc)
            self.db.settle_action(
                action_id=action_id, status="uncertain",
                error_code=self._stable_error_code(exc),
            )
            return PublishResult(
                False, "PUBLISH_UNCERTAIN", action_id=action_id,
                detail=f"提交后结果未知（{type(exc).__name__}）",
            )
        if not getattr(result, "ok", False):
            self.db.settle_action(
                action_id=action_id, status="failed",
                error_code=str(getattr(result, "code", "FAVORITE_FAILED")),
            )
            return PublishResult(
                False, "FAVORITE_FAILED", action_id=action_id,
                detail=str(getattr(result, "message", "")),
            )

        self.db.settle_action(
            action_id=action_id, status="sent_verified",
            remote_rpid=getattr(result, "remote_id", None),
            remote_url=getattr(result, "remote_url", None),
        )
        self.db.set_video_fav_state(bvid, 1, media_id)
        return PublishResult(
            True, "OK", action_id=action_id,
            remote_url=getattr(result, "remote_url", None), gates=gate.checks,
        )

    def _write_gate(
        self,
        *,
        identity_mid: int | None,
        identity_verified: bool,
        checks: dict[str, Any],
    ) -> GateResult:
        """点赞/收藏共用门禁（与评论同一套条件，只是不涉及草稿/目标/证据）。"""
        interaction = self.config.interaction
        checks.update(
            {
                "dry_run": interaction.dry_run,
                "publish_enabled": interaction.publish_enabled,
                "mode": interaction.mode,
                "pause": interaction.pause,
                "platform_health": self.db.health_state(),
            }
        )
        if interaction.dry_run:
            return GateResult(False, REJECT_DRY_RUN, "dry_run=true：只做模拟", checks)
        if not interaction.publish_enabled:
            return GateResult(False, REJECT_PUBLISH_DISABLED, "publish_enabled=false", checks)
        if interaction.mode == "observe":
            return GateResult(False, REJECT_MODE_OBSERVE, "observe 模式禁止公开写入", checks)
        if interaction.pause:
            return GateResult(False, REJECT_PAUSED, "已暂停", checks)
        health_state = str(checks["platform_health"])
        if health_state in BLOCKING_STATES:
            return GateResult(
                False, REJECT_PLATFORM_BLOCKED,
                f"平台健康状态 {health_state}：必须人工恢复", checks,
            )
        configured_mid = int(self.config.account.agent_mid)
        if configured_mid <= 0:
            return GateResult(
                False, REJECT_AGENT_MID_NOT_CONFIGURED, "未配置 agent_mid", checks
            )
        if not identity_verified:
            return GateResult(False, REJECT_IDENTITY_UNVERIFIED, "未能核验身份", checks)
        if identity_mid != configured_mid:
            return GateResult(
                False, REJECT_IDENTITY_MISMATCH,
                f"实际 UID({identity_mid}) != agent_mid({configured_mid})", checks,
            )
        return GateResult(True, "OK", "", checks)

    async def _simulate_action(
        self, *, action_type: str, bvid: str, target_rpid: str | None, gate: GateResult
    ) -> PublishResult:
        """点赞/收藏的模拟分支：**绝不触达平台**。"""
        quota = self.quota_check(
            draft_type=action_type, bvid=bvid, target_root="", is_simulated=True
        )
        if quota["reject_code"]:
            return PublishResult(False, quota["reject_code"], detail=quota["detail"])
        action_id, outcome = self.db.reserve_action(
            agent_mid=int(self.config.account.agent_mid) or 0,
            action_type=action_type, bvid=bvid, target_rpid=target_rpid,
            draft_id=None, is_simulated=True,
        )
        if outcome in ("duplicate", "uncertain_pending"):
            return PublishResult(False, REJECT_DUPLICATE, action_id=action_id)
        self.db.settle_action(action_id=action_id, status="sent_verified", remote_rpid=None)
        return PublishResult(
            True, "SIMULATED", action_id=action_id, simulated=True,
            detail="dry_run=true：仅模拟，未触发任何真实写入", gates=gate.checks,
        )

    def resolve_favorite_folder(
        self, *, folder_name: str, folders: list[dict[str, Any]]
    ) -> int:
        """把收藏夹**名字**解析成 ``media_id`` 并缓存到库。

        主人给的 5 个名字是「工具与技术/方法论/案例与实操/观点与洞察/待深挖」；
        B站返回的 ``title`` 可能略有差异，因此做**宽松匹配**（精确 → 包含）。
        """
        want = folder_name.strip()
        chosen: dict[str, Any] | None = None
        for folder in folders:
            if str(folder.get("title", "") or "").strip() == want:
                chosen = folder
                break
        if chosen is None:
            for folder in folders:
                title = str(folder.get("title", "") or "").strip()
                if want and (want in title or title in want):
                    chosen = folder
                    break
        if chosen is None:
            return 0
        try:
            media_id = int(chosen.get("id", 0) or 0)
            folder_id = int(chosen.get("fid", 0) or 0)
        except (TypeError, ValueError):
            return 0
        if media_id <= 0:
            return 0
        self.db.upsert_favorite_folder(
            name=want,
            media_id=media_id,
            folder_id=folder_id,
            title=str(chosen.get("title", "") or ""),
        )
        return media_id

    async def _simulate(self, *, draft: Any, gate: GateResult) -> PublishResult:
        """模拟动作：真实与模拟**分开计数**，模拟 ID 不当远端 ``rpid``。"""
        bvid = str(draft["bvid"])
        action_type = str(draft["draft_type"])
        target_rpid = str(draft["target_rpid"]) if draft["target_rpid"] else None

        quota = self.quota_check(
            draft_type=action_type,
            bvid=bvid,
            target_root=str(draft["target_root"] or ""),
            is_simulated=True,
        )
        if quota["reject_code"]:
            return PublishResult(
                False, quota["reject_code"], detail=quota["detail"], gates=gate.checks
            )

        action_id, outcome = self.db.reserve_action(
            agent_mid=int(self.config.account.agent_mid) or 0,
            action_type=action_type,
            bvid=bvid,
            target_rpid=target_rpid,
            draft_id=str(draft["draft_id"]),
            is_simulated=True,
        )
        if outcome in ("duplicate", "uncertain_pending"):
            return PublishResult(False, REJECT_DUPLICATE, action_id=action_id, gates=gate.checks)

        # 模拟动作**不写远端 rpid**，只落一个明确的 simulated 终态
        self.db.settle_action(action_id=action_id, status="sent_verified", remote_rpid=None)
        self.db.set_draft_status(str(draft["draft_id"]), "sent_verified")
        return PublishResult(
            True,
            "SIMULATED",
            action_id=action_id,
            remote_rpid=None,
            simulated=True,
            detail="dry_run=true：仅模拟，未触发任何真实平台写入",
            gates=gate.checks,
        )

    def _aid_for(self, bvid: str) -> int:
        row = self.db.get_video(bvid)
        if row is None or row["aid"] is None:
            return 0
        return int(row["aid"])
