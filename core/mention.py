"""主号汇报（``@`` 提及）—— **纯 API 实现**（不需要浏览器）。

任务书 §6.6 原文：

    "``owner_mid`` 是身份校验值，**不能把 ``@数字UID`` 或纯文本 ``@昵称`` 视为一定成功**；
    应通过 B 站页面候选选择/渲染后的 UID 检查，并把通知状态记为
    ``verified/uncertain/unsupported``。"

## ★ 关键发现（2026-10-15 实测）

任务书假设"必须用页面交互才能核验提及"，因此写了"若上游 API 无法可靠表达提及，
用隔离浏览器处理该步" —— 但**这条假设不成立**。

实测（读 B站 API 文档 + 真机验证）：**评论对象自带 `content.members[]` 字段**，
B站把"这条评论实际 @ 到了谁"作为**服务端事实**返回：

```
content: {
  message: "@MsGsrs @HJQC",     ← 正文（只是文本，不算证据）
  members: [                     ← ★ 服务端确认的 @ 结果
    { mid: 282831595, uname: "MsGsrs" },
    { mid: 351873378, uname: "HJQC" }
  ]
}
```

真机验证（BV1nFp46qECm 评论区）：

```
★ @到 ['MsGsrs','HJQC'] mids=['282831595','351873378']
   内容: @MsGsrs @HJQC
★★ 楼中楼 @到 ['大好人六六六'] mids=['626246532']
   内容: 回复 @大好人六六六 :...
```

**→ 结论**：`@` 的**核验**（"到底 @ 到谁了"）可以**纯 API 完成，
完全不需要浏览器**，因此与任务书"定时 preset 不给通用浏览器"的约束**不冲突**。

## 因此本模块的架构

1. **发评论时**在正文里写 ``@主号昵称``（B站按昵称文本匹配）；
2. **发完读回**该评论（或该视频的最新评论），检查 ``content.members[]``
   是否含 ``owner_mid``；
3. 按结果落 **``verified`` / ``uncertain`` / ``failed``** 三态（外加 ``unsupported``）。

``unsupported`` 现在只在一个情况下出现：**昵称解析不出来**（无法确定该 @ 哪个昵称）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from core.bilibili import AtMember, BilibiliReader, BilibiliReadError, CommentItem
from core.storage import Database

NotifyStatus = Literal["planned", "verified", "uncertain", "unsupported", "failed"]
NotifyChannel = Literal["local", "browser", "api"]

#: 单条评论最多 @ 几个人（B站限制未知，自设保守值）
MAX_MENTIONS_PER_COMMENT = 3

#: "大众爆款"阈值（主人 B13 甲：点赞 ≥10000 → 公开 @ 主号）
HIGH_VALUE_LIKES = 10000

#: "小众宝藏"阈值（主人 B13 甲：点赞 ≤50 且内容好 → 私信/分享）
NICHE_LIKES = 50

_MENTION_RE = re.compile(r"@([A-Za-z0-9_\-\u4e00-\u9fff]{1,32})")


class MentionCapability(Protocol):
    """读取"某条评论实际 @ 到了谁"的能力。

    由 :class:`ApiMentionVerifier` 实现（纯 API）。
    保留 Protocol 是为了测试可替换，**不是**要求浏览器。
    """

    async def read_at_members(self, *, bvid: str, rpid: str) -> tuple[AtMember, ...]:
        """读回该评论的 ``content.members[]``（服务端事实）。"""
        ...


@dataclass(frozen=True)
class MentionPlan:
    """一次 ``@`` 汇报的判定结果。"""

    should_mention: bool
    reason: str
    tier: Literal["high_value", "niche", "normal", "blocked"]


@dataclass
class MentionOutcome:
    """一次提及尝试的结果。"""

    notification_id: str
    status: NotifyStatus
    observed_mid: int | None
    detail: str
    owner_name_used: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "notification_id": self.notification_id,
            "status": self.status,
            "observed_mid": self.observed_mid,
            "detail": self.detail,
            "owner_name_used": self.owner_name_used,
            "local_digest_is_authoritative": True,
            "note": (
                "本地日报始终可靠生成，**不依赖** @ 是否成功。"
                "status 不是 verified 时，不得声称已通知主人。"
            ),
        }


def classify_recommendation(
    *, like_count: int, score_total: int, min_score: int = 75
) -> MentionPlan:
    """按主人定的"双峰"规则判定是否值得向主号汇报。

    * **大众爆款**：点赞 ≥ :data:`HIGH_VALUE_LIKES` → 公开 ``@`` 主号
    * **小众宝藏**：点赞 ≤ :data:`NICHE_LIKES` 且内容评分达标 → 建议私下分享
    * 其余：不主动汇报
    """
    if like_count >= HIGH_VALUE_LIKES:
        return MentionPlan(
            True, f"点赞 {like_count} ≥ {HIGH_VALUE_LIKES}（大众爆款）", "high_value"
        )
    if like_count <= NICHE_LIKES and score_total >= min_score:
        return MentionPlan(
            True,
            f"点赞 {like_count} ≤ {NICHE_LIKES} 且评分 {score_total} ≥ {min_score}（小众宝藏）",
            "niche",
        )
    if score_total >= min_score:
        return MentionPlan(False, f"评分 {score_total} 达标但热度不在双峰区间", "normal")
    return MentionPlan(False, f"评分 {score_total} 未达阈值 {min_score}", "normal")


def extract_mention_candidates(text: str) -> list[str]:
    """抽取草稿文本里的 ``@`` 候选项（**仅用于诊断，不能当作成功依据**）。"""
    return _MENTION_RE.findall(text or "")


def looks_like_numeric_uid(candidate: str) -> bool:
    """``@123456`` 形态。

    ⚠️ 任务书明确：**不能**因为文本像 UID 就认为提及成功 ——
    B站的 ``@昵称`` 是按**昵称**匹配的，写数字通常只是普通文字。
    """
    return candidate.isdigit()


def build_mention_text(*, owner_name: str, body: str) -> str:
    """把正文构造成"@主号 + 正文"。

    ★ B站按**昵称**文本匹配提及，所以这里必须用**昵称**而不是数字 UID。
    """
    name = (owner_name or "").strip().lstrip("@")
    if not name:
        raise ValueError("owner_name 不能为空 —— 无法用数字 UID 构造提及")
    return f"@{name} {body}".strip()


class ApiMentionVerifier:
    """纯 API 的提及核验器：读回评论的 ``content.members[]``。

    **不需要浏览器**。这是本项目对任务书"可用隔离浏览器处理该步"的替代方案，
    且与"定时 preset 不给通用浏览器"的硬约束相容。
    """

    def __init__(self, *, reader: BilibiliReader) -> None:
        self.reader = reader

    async def read_at_members(self, *, bvid: str, rpid: str) -> tuple[AtMember, ...]:
        """读回指定评论实际 @ 到的用户。

        B站没有"按 rpid 取单条评论"的稳定公开接口，所以这里**扫该视频的评论**
        找到目标 rpid。扫描量有上限（见 ``max_pages``），避免拉全站。
        """
        detail = await self.reader.get_video_detail(bvid)
        target = str(rpid)
        page_size = 20
        max_pages = 3

        for page in range(max_pages):
            page_result = await self.reader.get_comments(
                detail.aid, mode=2, next_cursor=page, page_size=page_size
            )
            for item in page_result.replies:
                if item.rpid != target:
                    continue
                if item.at_members:
                    return item.at_members
                # 找到了但 members 为空 → 可能 @ 在楼中楼里
                sub = await self._read_sub_replies(detail.aid, item.root or item.rpid, target)
                if sub is not None:
                    return sub
                return ()
            if page_result.is_end:
                break
        return ()

    async def _read_sub_replies(
        self, aid: int, root: str, target_rpid: str
    ) -> tuple[AtMember, ...] | None:
        try:
            page = await self.reader.get_comment_replies(aid, root, page_size=20)
        except BilibiliReadError:
            return None
        for item in page.replies:
            if item.rpid == target_rpid:
                return item.at_members
        for item in page.replies:
            if item.parent == target_rpid or item.root == target_rpid:
                return item.at_members
        return None


async def try_mention(
    *,
    db: Database,
    bvid: str,
    owner_mid: int,
    owner_name: str,
    text: str,
    rpid: str | None = None,
    verifier: MentionCapability | None = None,
    channel: NotifyChannel = "api",
) -> MentionOutcome:
    """核验一次 ``@`` 汇报，并**如实**落状态。

    :param owner_name: 主号**昵称**（B站按昵称匹配提及；数字 UID 不算）。
    :param rpid: 已发出的评论 ID。``None`` 表示"还没发"（只做计划记录）。
    :param verifier: 核验器。``None`` 时无法核验 → ``uncertain``。
    """
    notification_id = db.plan_owner_notification(
        bvid=bvid, owner_mid=owner_mid, channel=channel
    )

    if owner_mid <= 0:
        return _settle(
            db, notification_id, "unsupported", None,
            "OWNER_MID_NOT_CONFIGURED",
            "未配置 owner_mid → 无法校验 @ 目标身份，仅保留本地日报。",
        )

    if not owner_name.strip():
        return _settle(
            db, notification_id, "unsupported", None,
            "OWNER_NAME_REQUIRED",
            "B站按**昵称**匹配提及，数字 UID 不会产生提醒。"
            "未提供 owner_name_hint → 无法构造提及，仅保留本地日报。",
        )

    candidate = build_mention_text(owner_name=owner_name, body=text)
    if candidate.count("@") > MAX_MENTIONS_PER_COMMENT:
        return _settle(
            db, notification_id, "unsupported", None,
            "TOO_MANY_MENTIONS",
            f"单条评论最多 @ {MAX_MENTIONS_PER_COMMENT} 人。",
        )

    if rpid is None:
        return _settle(
            db, notification_id, "uncertain", None,
            "NOT_YET_POSTED",
            "评论尚未发出 → 无法核验。发出后必须读回 content.members 再定状态。",
        )

    if verifier is None:
        return _settle(
            db, notification_id, "uncertain", None,
            "NO_VERIFIER",
            "没有核验器 → 无法确认 @ 是否生效，不声称成功。",
        )

    try:
        members = await verifier.read_at_members(bvid=bvid, rpid=rpid)
    except Exception as exc:  # noqa: BLE001 - 异常一律 uncertain，不记成功
        return _settle(
            db, notification_id, "uncertain", None,
            type(exc).__name__,
            f"读回评论时异常（{type(exc).__name__}）：无法确认 @ 是否生效，需人工核对。",
        )

    # ★ 服务端事实：members 里有没有 owner_mid
    matched = next((m for m in members if m.mid == owner_mid), None)
    if matched is not None:
        return _settle(
            db, notification_id, "verified", owner_mid,
            None,
            f"已核验：评论的 content.members 含 owner_mid({owner_mid})，"
            f"昵称 {matched.uname!r}。B站确认提醒已建立。",
        )

    observed = members[0].mid if members else None
    if members:
        return _settle(
            db, notification_id, "failed", observed,
            "MENTION_TARGET_MISMATCH",
            f"该评论 @ 到了 {[m.uname for m in members]}"
            f"（mids={[m.mid for m in members]}），**不含** owner_mid({owner_mid})"
            " → 提及了错误对象，不得声称已通知主人。",
        )

    return _settle(
        db, notification_id, "uncertain", None,
        "NO_AT_MEMBERS",
        "读回的评论 content.members 为空 → 正文里的 @ 只是普通文字，"
        "**未产生提醒**。不确认成功。",
    )


def _settle(
    db: Database,
    notification_id: str,
    status: NotifyStatus,
    observed_mid: int | None,
    error_code: str | None,
    detail: str,
) -> MentionOutcome:
    db.settle_owner_notification(
        notification_id=notification_id,
        status=status,
        observed_mid=observed_mid if status == "verified" else None,
        error_code=error_code,
        detail=detail,
    )
    return MentionOutcome(
        notification_id=notification_id,
        status=status,
        observed_mid=observed_mid,
        detail=detail,
    )


def verify_from_comments(
    *, comments: list[CommentItem], rpid: str, owner_mid: int
) -> tuple[NotifyStatus, int | None, str]:
    """从一个已取回的评论列表里核验（便于测试与离线核对）。

    返回 ``(status, observed_mid, detail)``。
    """
    target = next((c for c in comments if c.rpid == str(rpid)), None)
    if target is None:
        return "uncertain", None, "评论列表里没找到该 rpid → 无法核验。"
    if target.mentions(owner_mid):
        return "verified", owner_mid, "content.members 含 owner_mid。"
    if target.at_members:
        return (
            "failed",
            target.at_members[0].mid,
            f"@ 到了 {[m.uname for m in target.at_members]}，不含 owner_mid。",
        )
    return "uncertain", None, "content.members 为空 → 未产生提醒。"
