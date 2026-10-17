"""点赞 / 收藏测试（主人 2026-10-15 要求实现）。

任务书 §4 的 14 个工具里**没有**点赞与收藏，但人设（curator 13 号 §4.1/4.2/4.3）
把它们当核心行为。主人明确要求补做，因此：

* 新增 ``like_video`` / ``favorite_video`` 两个 MCP 工具；
* **与发布评论受同一套门禁**（模式/dry_run/总开关/暂停/平台健康/身份/配额/幂等）；
* 收藏夹名 → ``media_id`` 解析并缓存；
* 点赞/收藏状态落 ``video_states``，已赞/已藏直接拒绝。

核心不变量：**点赞和收藏都是公开写入**，绝不能因为"看起来无害"就绕过门禁。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.bilibili_write import WriteResult
from core.publish import (
    REJECT_ALREADY_FAVORITED,
    REJECT_ALREADY_LIKED,
    REJECT_FAVORITING_DISABLED,
    REJECT_IDENTITY_MISMATCH,
    REJECT_LIKING_DISABLED,
    REJECT_MODE_OBSERVE,
    REJECT_NO_FAVORITE_FOLDER,
    REJECT_PAUSED,
    REJECT_PLATFORM_BLOCKED,
    REJECT_PUBLISH_DISABLED,
    REJECT_QUOTA_FAVORITE,
    REJECT_QUOTA_LIKE,
    PublishGate,
)
from core.storage import FAVORITE_ACTION, LIKE_ACTION, Database
from tests.conftest import make_config

AGENT_MID = 1001
OWNER_MID = 2002
BVID = "BV1test00001"


class FakeReader:
    def __init__(self, mid: int = AGENT_MID, is_login: bool = True) -> None:
        self.mid = mid
        self.is_login = is_login

    async def get_nav(self) -> Any:
        from core.bilibili import NavInfo

        return NavInfo(is_login=self.is_login, uname="测试号", mid=self.mid)


class RecordingExecutor:
    """记录点赞/收藏调用；真实写测试里**必须**能看到调用次数。"""

    def __init__(self, *, ok: bool = True, boom: bool = False) -> None:
        self.likes: list[str] = []
        self.favorites: list[tuple[int, list[int]]] = []
        self.ok = ok
        self.boom = boom

    async def like_video(self, *, bvid: str, undo: bool = False) -> Any:
        self.likes.append(bvid)
        if self.boom:
            raise TimeoutError("点击后超时")
        return WriteResult(ok=self.ok, code="OK" if self.ok else "LIKE_FAILED", remote_id=bvid)

    async def favorite_video(
        self, *, aid: int, add_media_ids: list[int] | None = None,
        del_media_ids: list[int] | None = None,
    ) -> Any:
        self.favorites.append((aid, list(add_media_ids or [])))
        if self.boom:
            raise TimeoutError("点击后超时")
        return WriteResult(ok=self.ok, code="OK" if self.ok else "FAV_FAILED", remote_id=str(aid))

    async def list_favorite_folders(self, *, up_mid: int) -> list[dict[str, Any]]:
        return [
            {"id": 555, "fid": 55, "title": "工具与技术"},
            {"id": 666, "fid": 66, "title": "方法论"},
        ]


def ready_config(**interaction: Any) -> Any:
    return make_config(
        account={"agent_mid": AGENT_MID, "owner_mid": OWNER_MID},
        interaction={
            "mode": "auto", "dry_run": False, "publish_enabled": True, **interaction,
        },
    )


def make_gate(db: Database, config: Any, executor: Any, reader: Any = None) -> PublishGate:
    return PublishGate(
        db=db, config=config,
        reader=reader if reader is not None else FakeReader(),
        executor=executor,
    )


def seed(db: Database, bvid: str = BVID) -> str:
    db.upsert_video(bvid=bvid, aid=111, title="t")
    return bvid


# ---------------------------------------------------------------------------
# 点赞
# ---------------------------------------------------------------------------


async def test_like_succeeds_and_records_state(db: Database) -> None:
    seed(db)
    executor = RecordingExecutor()
    gate = make_gate(db, ready_config(), executor)

    result = await gate.publish_like(bvid=BVID)
    assert result.ok is True
    assert executor.likes == [BVID], "必须真的调用执行器"
    assert db.get_video_state(BVID)["like_state"] == 1


async def test_like_twice_is_rejected(db: Database) -> None:
    """★ 同一视频只能赞一次。"""
    seed(db)
    executor = RecordingExecutor()
    gate = make_gate(db, ready_config(), executor)

    assert (await gate.publish_like(bvid=BVID)).ok is True
    second = await gate.publish_like(bvid=BVID)
    assert second.ok is False
    assert second.code == REJECT_ALREADY_LIKED
    assert len(executor.likes) == 1, "不得重复提交"


async def test_like_requires_publish_enabled(db: Database) -> None:
    """★ 点赞是公开写入：总开关关着就不许发。"""
    seed(db)
    executor = RecordingExecutor()
    gate = make_gate(db, ready_config(publish_enabled=False), executor)
    result = await gate.publish_like(bvid=BVID)
    assert result.ok is False
    assert result.code == REJECT_PUBLISH_DISABLED
    assert executor.likes == []


async def test_like_blocked_in_observe_mode(db: Database) -> None:
    seed(db)
    executor = RecordingExecutor()
    gate = make_gate(db, ready_config(mode="observe"), executor)
    result = await gate.publish_like(bvid=BVID)
    assert result.code == REJECT_MODE_OBSERVE
    assert executor.likes == []


async def test_like_blocked_when_paused(db: Database) -> None:
    seed(db)
    executor = RecordingExecutor()
    gate = make_gate(db, ready_config(pause=True), executor)
    assert (await gate.publish_like(bvid=BVID)).code == REJECT_PAUSED
    assert executor.likes == []


@pytest.mark.parametrize(
    "state", ["verification_required", "auth_expired", "platform_error"]
)
async def test_like_blocked_by_health(db: Database, state: str) -> None:
    """★ 验证码等状态下，点赞也必须停。"""
    seed(db)
    db.record_health(state=state, category="test")
    executor = RecordingExecutor()
    gate = make_gate(db, ready_config(), executor)
    result = await gate.publish_like(bvid=BVID)
    assert result.code == REJECT_PLATFORM_BLOCKED
    assert executor.likes == []


async def test_like_blocked_on_identity_mismatch(db: Database) -> None:
    seed(db)
    executor = RecordingExecutor()
    gate = make_gate(db, ready_config(), executor, reader=FakeReader(mid=9999))
    assert (await gate.publish_like(bvid=BVID)).code == REJECT_IDENTITY_MISMATCH
    assert executor.likes == []


async def test_like_respects_daily_quota(db: Database) -> None:
    """★ 点赞配额（档位给的值）必须生效。"""
    seed(db, "BV1a0000001")
    seed(db, "BV1b0000002")
    executor = RecordingExecutor()
    gate = make_gate(db, ready_config(max_likes_per_day=1), executor)

    assert (await gate.publish_like(bvid="BV1a0000001")).ok is True
    second = await gate.publish_like(bvid="BV1b0000002")
    assert second.code == REJECT_QUOTA_LIKE
    assert len(executor.likes) == 1


async def test_like_dry_run_never_touches_platform(db: Database) -> None:
    """★ dry_run 下只模拟，执行器一次都不许被调。"""
    seed(db)
    executor = RecordingExecutor()
    gate = make_gate(db, ready_config(dry_run=True), executor)
    result = await gate.publish_like(bvid=BVID)
    assert result.ok is True
    assert result.simulated is True
    assert executor.likes == []
    assert db.count_actions_today(action_type=LIKE_ACTION, is_simulated=True) == 1
    assert db.count_actions_today(action_type=LIKE_ACTION, is_simulated=False) == 0


async def test_like_disabled_by_config(db: Database) -> None:
    seed(db)
    executor = RecordingExecutor()
    config = make_config(
        account={"agent_mid": AGENT_MID, "owner_mid": OWNER_MID},
        interaction={"mode": "auto", "dry_run": False, "publish_enabled": True},
        persona={"enable_liking": False},
    )
    gate = make_gate(db, config, executor)
    assert (await gate.publish_like(bvid=BVID)).code == REJECT_LIKING_DISABLED
    assert executor.likes == []


async def test_like_timeout_becomes_uncertain_not_success(db: Database) -> None:
    """★ 提交后超时 → uncertain，不得标成功、不得自动重发。"""
    seed(db)
    gate = make_gate(db, ready_config(), RecordingExecutor(boom=True))
    result = await gate.publish_like(bvid=BVID)
    assert result.ok is False
    assert result.code == "PUBLISH_UNCERTAIN"
    assert db.get_video_state(BVID)["like_state"] == 0, "不确定时不得标记已赞"


async def test_like_without_executor_is_uncertain(db: Database) -> None:
    seed(db)
    gate = make_gate(db, ready_config(), None)
    result = await gate.publish_like(bvid=BVID)
    assert result.code == "EXECUTOR_UNAVAILABLE"
    assert db.get_video_state(BVID)["like_state"] == 0


async def test_like_unknown_video_rejected(db: Database) -> None:
    gate = make_gate(db, ready_config(), RecordingExecutor())
    assert (await gate.publish_like(bvid="BV1nope")).ok is False


# ---------------------------------------------------------------------------
# 收藏
# ---------------------------------------------------------------------------


async def test_favorite_succeeds_and_records_folder(db: Database) -> None:
    seed(db)
    executor = RecordingExecutor()
    gate = make_gate(db, ready_config(), executor)

    result = await gate.publish_favorite(bvid=BVID, folder_name="工具与技术", media_id=555)
    assert result.ok is True
    assert executor.favorites == [(111, [555])]
    state = db.get_video_state(BVID)
    assert state["fav_state"] == 1
    assert state["fav_media_ids"] == [555]


async def test_favorite_twice_is_rejected(db: Database) -> None:
    seed(db)
    executor = RecordingExecutor()
    gate = make_gate(db, ready_config(), executor)
    assert (
        await gate.publish_favorite(bvid=BVID, folder_name="工具与技术", media_id=555)
    ).ok is True
    second = await gate.publish_favorite(
        bvid=BVID, folder_name="方法论", media_id=666
    )
    assert second.code == REJECT_ALREADY_FAVORITED
    assert len(executor.favorites) == 1


async def test_favorite_requires_valid_media_id(db: Database) -> None:
    """★ 没解析出 media_id 就不能发（否则收藏到错误位置）。"""
    seed(db)
    executor = RecordingExecutor()
    gate = make_gate(db, ready_config(), executor)
    result = await gate.publish_favorite(bvid=BVID, folder_name="不存在", media_id=0)
    assert result.code == REJECT_NO_FAVORITE_FOLDER
    assert executor.favorites == []


async def test_favorite_respects_quota(db: Database) -> None:
    seed(db, "BV1a0000001")
    seed(db, "BV1b0000002")
    executor = RecordingExecutor()
    gate = make_gate(db, ready_config(max_favorites_per_day=1), executor)
    assert (
        await gate.publish_favorite(bvid="BV1a0000001", folder_name="工具与技术", media_id=555)
    ).ok is True
    second = await gate.publish_favorite(
        bvid="BV1b0000002", folder_name="工具与技术", media_id=555
    )
    assert second.code == REJECT_QUOTA_FAVORITE


async def test_favorite_dry_run_never_touches_platform(db: Database) -> None:
    seed(db)
    executor = RecordingExecutor()
    gate = make_gate(db, ready_config(dry_run=True), executor)
    result = await gate.publish_favorite(bvid=BVID, folder_name="工具与技术", media_id=555)
    assert result.simulated is True
    assert executor.favorites == []
    assert db.count_actions_today(action_type=FAVORITE_ACTION, is_simulated=True) == 1


async def test_favorite_disabled_by_config(db: Database) -> None:
    seed(db)
    executor = RecordingExecutor()
    config = make_config(
        account={"agent_mid": AGENT_MID, "owner_mid": OWNER_MID},
        interaction={"mode": "auto", "dry_run": False, "publish_enabled": True},
        persona={"enable_favoriting": False},
    )
    gate = make_gate(db, config, executor)
    result = await gate.publish_favorite(bvid=BVID, folder_name="工具与技术", media_id=555)
    assert result.code == REJECT_FAVORITING_DISABLED


async def test_favorite_timeout_is_uncertain(db: Database) -> None:
    seed(db)
    gate = make_gate(db, ready_config(), RecordingExecutor(boom=True))
    result = await gate.publish_favorite(bvid=BVID, folder_name="工具与技术", media_id=555)
    assert result.code == "PUBLISH_UNCERTAIN"
    assert db.get_video_state(BVID)["fav_state"] == 0


# ---------------------------------------------------------------------------
# 收藏夹名 → media_id 解析
# ---------------------------------------------------------------------------


def test_resolve_favorite_folder_exact_match(db: Database) -> None:
    seed(db)
    gate = make_gate(db, ready_config(), RecordingExecutor())
    folders = [{"id": 555, "fid": 55, "title": "工具与技术"}]
    assert gate.resolve_favorite_folder(folder_name="工具与技术", folders=folders) == 555
    assert db.resolve_favorite_media_id("工具与技术") == 555


def test_resolve_favorite_folder_loose_match(db: Database) -> None:
    """B站返回的标题可能与配置名略有差异 → 宽松匹配。"""
    seed(db)
    gate = make_gate(db, ready_config(), RecordingExecutor())
    folders = [{"id": 777, "fid": 77, "title": "【工具与技术】"}]
    assert gate.resolve_favorite_folder(folder_name="工具与技术", folders=folders) == 777


def test_resolve_favorite_folder_not_found(db: Database) -> None:
    seed(db)
    gate = make_gate(db, ready_config(), RecordingExecutor())
    folders = [{"id": 555, "fid": 55, "title": "完全不相干"}]
    assert gate.resolve_favorite_folder(folder_name="工具与技术", folders=folders) == 0


def test_resolve_favorite_folder_ignores_bad_ids(db: Database) -> None:
    seed(db)
    gate = make_gate(db, ready_config(), RecordingExecutor())
    assert gate.resolve_favorite_folder(
        folder_name="X", folders=[{"id": "abc", "title": "X"}]
    ) == 0


def test_favorite_folder_cache_roundtrip(db: Database) -> None:
    db.upsert_favorite_folder(name="方法论", media_id=666, folder_id=66, title="方法论")
    assert db.resolve_favorite_media_id("方法论") == 666
    assert db.resolve_favorite_media_id("不存在") == 0
    rows = db.list_favorite_folders()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# 配额计入与不确定态
# ---------------------------------------------------------------------------


async def test_uncertain_like_still_consumes_quota(db: Database) -> None:
    """★ uncertain 占额度（不靠时间释放），避免崩溃后超额。"""
    seed(db)
    gate = make_gate(db, ready_config(), RecordingExecutor(boom=True))
    await gate.publish_like(bvid=BVID)
    assert db.count_actions_today(action_type=LIKE_ACTION, is_simulated=False) == 1


async def test_like_and_favorite_have_separate_quotas(db: Database) -> None:
    """点赞与收藏的额度**互相独立**。"""
    seed(db)
    executor = RecordingExecutor()
    gate = make_gate(db, ready_config(max_likes_per_day=1, max_favorites_per_day=1), executor)
    assert (await gate.publish_like(bvid=BVID)).ok is True
    assert (
        await gate.publish_favorite(bvid=BVID, folder_name="工具与技术", media_id=555)
    ).ok is True
    assert db.count_actions_today(action_type=LIKE_ACTION, is_simulated=False) == 1
    assert db.count_actions_today(action_type=FAVORITE_ACTION, is_simulated=False) == 1


# ---------------------------------------------------------------------------
# 频率档位（主人要求"多档可选"）
# ---------------------------------------------------------------------------


def test_frequency_tiers_expand() -> None:
    from core.config import FREQUENCY_TIERS, Config

    for tier, preset in FREQUENCY_TIERS.items():
        config = Config.model_validate({"interaction": {"frequency_tier": tier}})
        for key, value in preset.items():
            assert getattr(config.interaction, key) == value, (tier, key)


def test_frequency_tier_explicit_value_wins() -> None:
    """★ 选了档位仍可逐项覆盖（显式值优先）。"""
    from core.config import Config

    config = Config.model_validate(
        {"interaction": {"frequency_tier": "normal", "max_likes_per_day": 99}}
    )
    assert config.interaction.max_likes_per_day == 99
    assert config.interaction.max_top_comments_per_day == 3, "其它项仍取档位"


def test_unknown_frequency_tier_rejected() -> None:
    from pydantic import ValidationError

    from core.config import Config

    with pytest.raises(ValidationError, match="frequency_tier"):
        Config.model_validate({"interaction": {"frequency_tier": "turbo"}})


def test_tiers_are_monotonic() -> None:
    """★ 档位必须单调：conservative ≤ normal ≤ active（不能有倒挂）。"""
    from core.config import FREQUENCY_TIERS

    order = ["conservative", "normal", "active"]
    for key in ("max_top_comments_per_day", "max_likes_per_day", "max_favorites_per_day"):
        values = [FREQUENCY_TIERS[t][key] for t in order]
        assert values == sorted(values), f"{key} 档位倒挂：{values}"
    # 间隔应当反向单调（越积极间隔越短）
    gaps = [FREQUENCY_TIERS[t]["min_action_interval_min"] for t in order]
    assert gaps == sorted(gaps, reverse=True), f"间隔倒挂：{gaps}"


def test_default_tier_is_conservative() -> None:
    """默认取最保守档（宁少勿多）。"""
    from core.config import Config

    assert Config().interaction.frequency_tier == "conservative"


# ---------------------------------------------------------------------------
# ★ 模拟与真实的幂等键必须**分开**（实测踩到的 bug）
# ---------------------------------------------------------------------------


def test_simulated_and_real_keys_differ(db: Database) -> None:
    """★ 回归：dry_run 演练不该挡住之后的真实发布。

    真实踩到的 bug：先 dry_run 演练一次（写了一条 is_simulated=1 的
    ``sent_verified``），之后真发时被自己的模拟记录挡住，
    报 ``ALREADY_LIKED``，而平台侧一次都没发过。
    任务书 §5 明确要求"模拟与真实分开存储/计数"。
    """
    for action in ("top", "reply", "like", "favorite", "dynamic"):
        sim = db.make_idempotency_key(
            agent_mid=1001, action_type=action, bvid="BV1a",
            target_rpid="9", is_simulated=True,
        )
        real = db.make_idempotency_key(
            agent_mid=1001, action_type=action, bvid="BV1a",
            target_rpid="9", is_simulated=False,
        )
        assert sim != real, f"{action}: 模拟与真实用了同一个幂等键"


async def test_dry_run_does_not_block_later_real_like(db: Database) -> None:
    """★ 端到端回归：先模拟、后真实，真实必须能发出去。"""
    seed(db)
    executor = RecordingExecutor()

    # ① dry_run 演练
    sim_gate = make_gate(db, ready_config(dry_run=True), executor)
    sim = await sim_gate.publish_like(bvid=BVID)
    assert sim.simulated is True
    assert executor.likes == [], "模拟不得触达平台"

    # ② 再开真实发布 —— 不能被刚才的模拟挡住
    real_gate = make_gate(db, ready_config(), executor)
    real = await real_gate.publish_like(bvid=BVID)
    assert real.ok is True, f"被模拟记录挡住了：{real.code}"
    assert executor.likes == [BVID], "真实点赞必须真的发出去"
    assert db.get_video_state(BVID)["like_state"] == 1


async def test_dry_run_does_not_block_later_real_favorite(db: Database) -> None:
    seed(db)
    executor = RecordingExecutor()
    sim_gate = make_gate(db, ready_config(dry_run=True), executor)
    await sim_gate.publish_favorite(bvid=BVID, folder_name="工具与技术", media_id=555)
    assert executor.favorites == []

    real_gate = make_gate(db, ready_config(), executor)
    real = await real_gate.publish_favorite(
        bvid=BVID, folder_name="工具与技术", media_id=555
    )
    assert real.ok is True, f"被模拟记录挡住了：{real.code}"
    assert executor.favorites == [(111, [555])]


def test_simulated_and_real_counted_separately(db: Database) -> None:
    """模拟与真实的配额也分开计（任务书 §5）。"""
    seed(db)
    import asyncio

    executor = RecordingExecutor()
    asyncio.run(make_gate(db, ready_config(dry_run=True), executor).publish_like(bvid=BVID))
    assert db.count_actions_today(action_type=LIKE_ACTION, is_simulated=True) == 1
    assert db.count_actions_today(action_type=LIKE_ACTION, is_simulated=False) == 0

    asyncio.run(make_gate(db, ready_config(), executor).publish_like(bvid=BVID))
    assert db.count_actions_today(action_type=LIKE_ACTION, is_simulated=False) == 1
