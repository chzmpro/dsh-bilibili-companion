"""评论公开可见性复核（``core/verify.py``）测试。

★ 本模块固化的最重要一条纪律：**看不到 ≠ 没发出**。
「没查到」是允许的措辞，「没发出去」**不是** —— 观测不到不能反推不存在。

全部测试走 ``httpx.MockTransport``，**绝不联网**；并显式断言
``/x/v2/reply/detail`` 一次都没被调用（竞品实测它回 ``-400``，别换它）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from core import schema, verify
from core.bilibili import BilibiliReader
from core.ratelimit import RequestQueue
from core.storage import Database
from core.verify import (
    MISSING_WORD,
    TOP_HOW,
    VerifyResult,
    append_verify_to_report,
    recent_sent_records,
    verify_one,
    verify_recent,
)
from tests.conftest import FakeClock, build_client, make_config, nav_payload, ok_payload

BVID = "BV1test00001"
AID = 111
AGENT_MID = 1001
OUR_RPID = "1700000001"
ROOT_RPID = "1600000001"
#: 固定的复核时刻（``now_ts()`` 与 FakeClock 都用它，避免"7 天"边界抖动）
NOW = 1_700_000_000


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    """把竞品的 700ms / 300ms 礼貌间隔换成空操作（**只改测试时长，不改逻辑**）。"""

    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(verify, "_pause", instant)


def seed_action(
    db: Database,
    *,
    rpid: str = OUR_RPID,
    bvid: str = BVID,
    action_type: str = "top",
    root: str | None = None,
) -> str:
    """在 ``actions`` 表里造一条"我们已确认发出"的评论。

    顶层走真实 ``reserve_action`` + ``settle_action``；
    楼中楼额外挂一个带 ``target_root`` 的草稿（复核要用它当 ``root``）。
    """
    db.upsert_video(bvid=bvid, aid=AID, title="测试视频")

    draft_id: str | None = None
    if root is not None:
        draft_id = db.create_draft(
            draft_type="reply",
            bvid=bvid,
            text="回复内容",
            evidence_ids=[],
            target_rpid=ROOT_RPID,
            target_root=root,
        )

    action_id, outcome = db.reserve_action(
        agent_mid=AGENT_MID,
        action_type=action_type,
        bvid=bvid,
        target_rpid=ROOT_RPID if root is not None else None,
        draft_id=draft_id,
        is_simulated=False,
    )
    assert outcome == "reserved"
    db.settle_action(
        action_id=action_id,
        status="sent_verified",
        remote_rpid=rpid,
        remote_url=f"https://www.bilibili.com/video/{bvid}/#reply{rpid}",
    )
    return action_id


def build_reader(
    handler: Any, db: Database, clock: FakeClock
) -> tuple[BilibiliReader, list[httpx.Request]]:
    """建一个走 MockTransport 的读取器，并记录所有请求（供"没被调用"类断言）。"""
    captured: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    queue = RequestQueue(
        db=db,
        config=make_config(bilibili_requests={"min_interval_seconds": 1.0}),
        client=build_client(wrapped),
        clock=clock,
        sleep=clock.sleep,
    )
    return BilibiliReader(queue=queue, cookies={}), captured


def view_payload(aid: int = AID) -> dict[str, Any]:
    return ok_payload(
        {
            "bvid": BVID,
            "aid": aid,
            "cid": 1,
            "title": "测试视频",
            "desc": "简介",
            "owner": {"mid": 2002, "name": "某UP"},
            "pubdate": NOW,
            "duration": 60,
            "stat": {"reply": 3},
            "pages": [{"cid": 1}],
        }
    )


def comment_payload(
    *,
    rpid: str = OUR_RPID,
    mid: int = AGENT_MID,
    like: int = 0,
    invisible: bool = False,
) -> dict[str, Any]:
    return {
        "rpid": int(rpid),
        "rpid_str": rpid,
        "root": 0,
        "root_str": "0",
        "parent": 0,
        "parent_str": "0",
        "member": {"mid": mid, "uname": "干饭不干事的小鲸鱼"},
        "content": {"message": "一条测试评论", "members": []},
        "like": like,
        "ctime": NOW,
        "rcount": 0,
        "invisible": invisible,
    }


def reply_main_payload(replies: list[dict[str, Any]]) -> dict[str, Any]:
    return ok_payload({"replies": replies, "cursor": {"next": 0, "is_end": True, "all_count": 1}})


def reply_sub_payload(replies: list[dict[str, Any]]) -> dict[str, Any]:
    return ok_payload({"replies": replies, "cursor": {"next": 0, "is_end": True, "all_count": 1}})


def is_detail_path(request: httpx.Request) -> bool:
    return request.url.path == "/x/v2/reply/detail"


def make_transport(
    *,
    replies: list[dict[str, Any]] | None = None,
    subs: list[dict[str, Any]] | None = None,
    view_ok: bool = True,
    comment_code: int = 0,
    comment_message: str = "0",
    subs_code: int = 0,
    subs_message: str = "0",
    view_code: int = 0,
    sub_replies_called: list[bool] | None = None,
) -> Any:
    """一个只认识三件事的 Mock 处理器：``view`` / ``reply/main`` / ``reply/reply``。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/x/web-interface/nav":
            return httpx.Response(200, json=nav_payload(mid=AGENT_MID))
        if path == "/x/web-interface/view":
            if not view_ok:
                return httpx.Response(200, json={"code": -404, "message": "啥都木有"})
            if view_code != 0:
                return httpx.Response(200, json={"code": view_code, "message": "视频读取失败"})
            return httpx.Response(200, json=view_payload())
        if path == "/x/v2/reply/main":
            if comment_code != 0:
                return httpx.Response(
                    200, json={"code": comment_code, "message": comment_message}
                )
            return httpx.Response(200, json=reply_main_payload(replies or []))
        if path == "/x/v2/reply/reply":
            if sub_replies_called is not None:
                sub_replies_called.append(True)
            if subs_code != 0:
                return httpx.Response(200, json={"code": subs_code, "message": subs_message})
            return httpx.Response(200, json=reply_sub_payload(subs or []))
        return httpx.Response(404, json={"code": -404, "message": "no route for tests"})

    return handler


# ---------------------------------------------------------------------------
# 1. 顶层命中 → visible=True，且读到赞数
# ---------------------------------------------------------------------------


async def test_top_hit_is_visible_and_reads_likes(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db)
    reader, captured = build_reader(
        make_transport(replies=[comment_payload(like=7)]), db, clock
    )

    result = await verify_one(reader, recent_sent_records(db)[0], aid_cache={})

    assert result.visible is True
    assert result.likes == 7, "公开可见时要顺便把赞数读回来"
    assert result.how == TOP_HOW
    assert result.note == "", "没有 invisible 标记时不该有多余附注"
    assert result.rpid == OUR_RPID and result.bvid == BVID

    main = next(r for r in captured if r.url.path == "/x/v2/reply/main")
    # 竞品实测参数：type=1、oid=aid、mode=2（**按时间**）、next=0、ps=20
    assert main.url.params["type"] == "1"
    assert main.url.params["oid"] == str(AID)
    assert main.url.params["mode"] == "2", "必须按时间排序（mode=2）"
    assert main.url.params["next"] == "0"
    assert main.url.params["ps"] == "20"
    assert not any(is_detail_path(r) for r in captured), "别换 /x/v2/reply/detail（回 -400）"


# ---------------------------------------------------------------------------
# 2. 顶层没命中 → visible is None，措辞是「没查到」，**不含「没发出去」**
# ---------------------------------------------------------------------------


async def test_top_miss_is_none_with_weak_wording(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db)
    # 第一页里只有别人的评论
    reader, _captured = build_reader(
        make_transport(replies=[comment_payload(rpid="999999", mid=777)]), db, clock
    )

    result = await verify_one(reader, recent_sent_records(db)[0], aid_cache={})

    assert result.visible is None, "没查到 ≠ 不可见（不能是 False）"
    assert result.likes is None
    assert MISSING_WORD in result.note, "必须含「没查到」语义"
    assert "没发出去" not in result.note, "★ 观测不到不能反推不存在"
    assert "没发出去" not in result.how
    assert result.how == TOP_HOW


# ---------------------------------------------------------------------------
# 3. 楼中楼命中（有 root）→ visible=True
# ---------------------------------------------------------------------------


async def test_sub_reply_hit_is_visible(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db, action_type="reply", root=ROOT_RPID)
    reader, captured = build_reader(
        make_transport(subs=[comment_payload(like=2)]), db, clock
    )

    result = await verify_one(reader, recent_sent_records(db)[0], aid_cache={})

    assert result.visible is True
    assert result.likes == 2
    assert result.how == f"楼中楼·root={ROOT_RPID}"

    sub = next(r for r in captured if r.url.path == "/x/v2/reply/reply")
    # 竞品实测参数：type=1、oid=aid、root=<root>、ps=20、pn=1
    assert sub.url.params["root"] == ROOT_RPID
    assert sub.url.params["ps"] == "20"
    assert sub.url.params["pn"] == "1"
    # 走了楼中楼就**不该**再去翻顶层
    assert not any(r.url.path == "/x/v2/reply/main" for r in captured)


# ---------------------------------------------------------------------------
# 4. 楼中楼那串拉不到 → visible is None + 措辞正确
# ---------------------------------------------------------------------------


async def test_sub_reply_empty_thread_is_none_with_weak_wording(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db, action_type="reply", root=ROOT_RPID)
    reader, _captured = build_reader(make_transport(subs=[]), db, clock)

    result = await verify_one(reader, recent_sent_records(db)[0], aid_cache={})

    assert result.visible is None
    assert MISSING_WORD in result.note
    assert "没发出去" not in result.note
    assert "那一串" in result.note, "要如实说明「那一串拉不到」，而不是含糊过去"


async def test_sub_reply_thread_with_others_only_is_none(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """那一串有内容、但没有我们这条 —— 同样只能写「没查到」。"""
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db, action_type="reply", root=ROOT_RPID)
    reader, _captured = build_reader(
        make_transport(subs=[comment_payload(rpid="888888", mid=777)]), db, clock
    )

    result = await verify_one(reader, recent_sent_records(db)[0], aid_cache={})

    assert result.visible is None
    assert "1 条" in result.note, "要报出那一串读到了几条"
    assert MISSING_WORD in result.note
    assert "没发出去" not in result.note


# ---------------------------------------------------------------------------
# 5. 7 天内已确认可见的**不重复查**
# ---------------------------------------------------------------------------


async def test_recently_confirmed_visible_is_not_rechecked(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db)
    sub_replies_called: list[bool] = []
    reader, captured = build_reader(
        make_transport(
            replies=[comment_payload(like=3)],
            subs_code=0,
            sub_replies_called=sub_replies_called,
        ),
        db,
        clock,
    )

    first, _skipped, _text = await verify_recent(_Runtime(db, reader), max_items=6)
    assert len(first) == 1 and first[0].visible is True
    requests_first = len(captured)

    # 第二次：7 天 TTL 内，应该**一条都不查**
    second, skipped, text = await verify_recent(_Runtime(db, reader), max_items=6)
    assert second == []
    assert skipped == 1, "7 天内已确认可见的要计入跳过"
    assert "都复核过了" in text
    assert len(captured) == requests_first, "★ 不应产生任何新请求"


async def test_stale_visible_record_is_rechecked(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超过 7 天 → 撤销 TTL 保护，重新复核一次（竞品同款）。"""
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db)
    reader, captured = build_reader(make_transport(replies=[comment_payload()]), db, clock)

    await verify_recent(_Runtime(db, reader), max_items=6)
    # 把账本里的复核时刻推到 8 天前
    db.execute(
        "UPDATE comment_verifications SET checked_at = ? WHERE remote_rpid = ?",
        (NOW - 8 * 86400, OUR_RPID),
    )
    before = len(captured)

    checked, skipped, _text = await verify_recent(_Runtime(db, reader), max_items=6)

    assert skipped == 0
    assert len(checked) == 1
    assert len(captured) > before, "超过 7 天必须重新查"


async def test_missing_verdict_is_rechecked_within_ttl(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★★ 「没查到」**不享受** 7 天免查 —— 只有确认可见才免查。

    否则一条"还在审核"的评论会被永久搁置，再也没人复核它。
    """
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db)
    reader, captured = build_reader(
        make_transport(replies=[comment_payload(rpid="9999", mid=777)]), db, clock
    )

    first, _skipped, _text = await verify_recent(_Runtime(db, reader), max_items=6)
    assert first[0].visible is None
    before = len(captured)

    second, skipped, _text2 = await verify_recent(_Runtime(db, reader), max_items=6)

    assert second[0].visible is None
    assert skipped == 0, "没查到不算「已复核过」"
    assert len(captured) > before, "没查到的要允许下一轮再查"


# ---------------------------------------------------------------------------
# 6. invisible 标记要出现在 note 里
# ---------------------------------------------------------------------------


async def test_invisible_flag_is_surfaced_in_note(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db)
    reader, _captured = build_reader(
        make_transport(replies=[comment_payload(invisible=True)]), db, clock
    )

    result = await verify_one(reader, recent_sent_records(db)[0], aid_cache={})

    assert result.visible is True
    assert "invisible" in result.note, "★ 接口标了 invisible 必须如实附注"


# ---------------------------------------------------------------------------
# 7. 取视频信息失败 → 跳过并给出原因（不抛异常）
# ---------------------------------------------------------------------------


async def test_video_info_failure_skips_with_reason(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db)
    reader, captured = build_reader(make_transport(view_ok=False), db, clock)

    result = await verify_one(reader, recent_sent_records(db)[0], aid_cache={})

    assert result.visible is None
    assert result.how == verify.VIDEO_INFO_HOW
    assert "跳过" in result.note
    # 取不到 aid 就不该再瞎猜着去翻评论区
    assert not any(r.url.path == "/x/v2/reply/main" for r in captured)


async def test_video_info_error_code_skips_with_reason(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db)
    reader, _captured = build_reader(make_transport(view_code=-404), db, clock)

    result = await verify_one(reader, recent_sent_records(db)[0], aid_cache={})

    assert result.visible is None
    assert result.how == verify.VIDEO_INFO_HOW


# ---------------------------------------------------------------------------
# 8. API 报错（非 0 code）不崩，记为「没查到」
# ---------------------------------------------------------------------------


async def test_api_error_code_is_recorded_as_missing(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db)
    reader, _captured = build_reader(
        make_transport(comment_code=-404, comment_message="啥都木有"), db, clock
    )

    result = await verify_one(reader, recent_sent_records(db)[0], aid_cache={})

    assert result.visible is None, "接口报错不是「不可见」，而是「没查到」"
    assert MISSING_WORD in result.note
    assert "没发出去" not in result.note


async def test_sub_reply_api_error_is_recorded_as_missing(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db, action_type="reply", root=ROOT_RPID)
    reader, _captured = build_reader(
        make_transport(subs_code=-400, subs_message="请求错误"), db, clock
    )

    result = await verify_one(reader, recent_sent_records(db)[0], aid_cache={})

    assert result.visible is None
    assert MISSING_WORD in result.note


async def test_unexpected_exception_does_not_escape(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """底层抛任何异常（如超时）都不得把复核带崩 —— 收成"没查到"。"""
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db)
    reader, _captured = build_reader(make_transport(replies=[]), db, clock)

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise httpx.ConnectError("连接被掐了")

    monkeypatch.setattr(reader.queue, "get_json", boom)

    result = await verify_one(reader, recent_sent_records(db)[0], aid_cache={})

    assert result.visible is None
    assert "跳过" in result.note, "取视频信息失败 → 跳过并给出原因"


# ---------------------------------------------------------------------------
# 平台健康门禁：验证码 / 认证失效 / 平台故障必须**立即停**
# ---------------------------------------------------------------------------


async def test_health_block_stops_verification(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db)
    reader, _captured = build_reader(make_transport(replies=[comment_payload()]), db, clock)
    db.record_health(
        state="verification_required", raw_code="-352", category="captcha", detail="需人工验证"
    )

    result = await verify_one(reader, recent_sent_records(db)[0], aid_cache={})

    assert result.visible is None
    assert result.how == verify.HEALTH_BLOCKED_HOW
    assert "verification_required" in result.note


async def test_verify_recent_halts_on_health_block(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db, rpid="1700000001")
    seed_action(db, rpid="1700000002", action_type="reply", root=ROOT_RPID)
    reader, _captured = build_reader(make_transport(replies=[]), db, clock)
    db.record_health(state="platform_error", raw_code="-412", category="risk", detail="疑似风控")

    checked, _skipped, text = await verify_recent(_Runtime(db, reader), max_items=6)

    assert len(checked) == 1, "碰到需要人工处理的状态就该停，不要继续下一条"
    assert verify.HEALTH_BLOCKED_HOW in text
    assert "停止本轮复核" in text


# ---------------------------------------------------------------------------
# 账本落库 / 措辞纪律 / 日报追加
# ---------------------------------------------------------------------------


async def test_ledger_persists_three_state_visible(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db)
    reader, _captured = build_reader(
        make_transport(replies=[comment_payload(like=5)]), db, clock
    )

    await verify_recent(_Runtime(db, reader), max_items=6)

    row = db.query_one(
        "SELECT * FROM comment_verifications WHERE remote_rpid = ?", (OUR_RPID,)
    )
    assert row is not None, "复核结论必须落库（竞品的 verify-state.json 换成 SQLite 表）"
    assert int(row["visible"]) == 1
    assert int(row["likes"]) == 5
    assert row["bvid"] == BVID
    assert int(row["checked_at"]) == NOW


async def test_ledger_stores_null_for_missing_not_zero(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★★ 「没查到」在库里必须是 ``NULL``，**不能**存成 0（0 会被读成"不可见"）。"""
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db)
    reader, _captured = build_reader(make_transport(replies=[]), db, clock)

    await verify_recent(_Runtime(db, reader), max_items=6)

    row = db.query_one(
        "SELECT visible FROM comment_verifications WHERE remote_rpid = ?", (OUR_RPID,)
    )
    assert row is not None
    assert row["visible"] is None


async def test_report_text_never_says_not_sent(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 报告措辞：只写「没查到」，**绝不**写「没发出去」。"""
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db)
    reader, _captured = build_reader(make_transport(replies=[]), db, clock)

    _checked, _skipped, text = await verify_recent(_Runtime(db, reader), max_items=6)

    assert "## 评论复核（宿主自动跑的，不是那一轮自己说的）" in text
    assert MISSING_WORD in text
    assert "没发出去" not in text
    assert f"rpid **{OUR_RPID}**" in text


async def test_report_text_states_visible_with_likes(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db)
    reader, _captured = build_reader(
        make_transport(replies=[comment_payload(like=11)]), db, clock
    )

    _checked, _skipped, text = await verify_recent(_Runtime(db, reader), max_items=6)

    assert "公开可见（赞 11）" in text
    assert f"依据：{TOP_HOW}" in text


async def test_max_items_bounds_the_batch(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    for index in range(5):
        # ★ 一个视频只能有一条顶层评论（幂等键 = agent_mid + bvid + top），
        #   所以要用 5 个不同的视频。
        seed_action(db, rpid=f"17000000{index:02d}", bvid=f"BV1test{index:05d}")
    reader, _captured = build_reader(make_transport(replies=[]), db, clock)

    checked, _skipped, _text = await verify_recent(_Runtime(db, reader), max_items=2)

    assert len(checked) == 2


async def test_no_records_says_so(db: Database) -> None:
    reader, _captured = build_reader(make_transport(), db, FakeClock())
    checked, skipped, text = await verify_recent(_Runtime(db, reader), max_items=6)
    assert checked == [] and skipped == 0
    assert "还没有" in text


async def test_bare_db_says_no_reader(db: Database) -> None:
    """只给 ``Database``（没有读取器）时要如实说明，而不是假装复核过。"""
    checked, skipped, text = await verify_recent(db, max_items=6)
    assert checked == [] and skipped == 0
    assert "读取器" in text


def test_append_verify_to_report_appends_and_redacts(tmp_path: Path) -> None:
    """追加到已有日报末尾；日志尾里的凭据要过 redact。"""
    digest = tmp_path / "digest-20261016.md"
    digest.write_text("# 日报\n\n正文\n", encoding="utf-8")

    heading = "\n## 评论复核（宿主自动跑的，不是那一轮自己说的）\n- 一条"
    ok = append_verify_to_report(digest, heading)

    assert ok is True
    text = digest.read_text(encoding="utf-8")
    assert text.startswith("# 日报")
    assert "## 评论复核" in text

    # 凭据不得落进日报
    append_verify_to_report(digest, "SESSDATA=abcdef1234567890; 复核完毕")
    after = digest.read_text(encoding="utf-8")
    assert "abcdef1234567890" not in after
    assert "<REDACTED>" in after


def test_append_verify_to_report_missing_file_returns_false(tmp_path: Path) -> None:
    """★ 日报还不存在 → **别硬造一份**（竞品同款：不存在就返回失败）。"""
    missing = tmp_path / "nope.md"
    assert append_verify_to_report(missing, "复核结果") is False
    assert not missing.exists(), "不得凭空创建日报"


async def test_verify_one_accepts_mapping_record(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``verify_one`` 的 ``record`` 是宽容的（sqlite3.Row / mapping 都收）。"""
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    reader, _captured = build_reader(make_transport(replies=[comment_payload()]), db, clock)

    result = await verify_one(
        reader,
        {"remote_rpid": OUR_RPID, "bvid": BVID, "action_type": "top", "agent_mid": AGENT_MID},
        aid_cache={},
    )

    assert result.visible is True


async def test_degraded_mid_match_is_not_reported_as_visible(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 竞品的 ``mid`` 兜底**只用来定位**：rpid 对不上就不能报"公开可见"。

    否则别人的评论（或我们的另一条评论）会被当成目标评论核销掉。
    """
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db)
    reader, _captured = build_reader(
        make_transport(replies=[comment_payload(rpid="777777", mid=AGENT_MID)]), db, clock
    )

    result = await verify_one(reader, recent_sent_records(db)[0], aid_cache={})

    assert result.visible is None, "rpid 不等时，mid 命中不能算核销"
    assert MISSING_WORD in result.note


async def test_cache_is_bypassed_for_comment_pages(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复核要的是**当下**的可见性：不能吃旧的评论缓存（否则刚发的永远"没查到"）。"""
    monkeypatch.setattr(verify, "now_ts", lambda: NOW)
    seed_action(db)
    reader, captured = build_reader(make_transport(replies=[]), db, clock)

    await verify_one(reader, recent_sent_records(db)[0], aid_cache={})
    await verify_one(reader, recent_sent_records(db)[0], aid_cache={})

    main_requests = [r for r in captured if r.url.path == "/x/v2/reply/main"]
    assert len(main_requests) == 2, "两次复核就该有两次真实请求（use_cache=False）"


def test_no_sleep_in_tests_but_pause_constants_match_competitor() -> None:
    """竞品实测节奏要留在常量里（700ms / 300ms / 7 天），只许测试里替换掉。"""
    assert verify.ITEM_PAUSE_SECONDS == 0.7
    assert verify.VIDEO_INFO_PAUSE_SECONDS == 0.3
    assert verify.VISIBLE_TTL_SECONDS == 7 * 86400
    # 端点常量也必须逐字等于竞品实测值
    assert verify.TOP_REPLY_PATH == "/x/v2/reply/main"
    assert verify.SUB_REPLY_PATH == "/x/v2/reply/reply"


# ---------------------------------------------------------------------------
# 迁移 v5
# ---------------------------------------------------------------------------


def test_schema_version_is_11_and_migrations_are_append_only() -> None:
    assert schema.SCHEMA_VERSION == 11
    versions = [version for version, _ in schema.MIGRATIONS]
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11], "迁移必须连续且只追加"


def test_migration_v5_creates_verification_ledger_on_existing_v4_db(tmp_path: Path) -> None:
    """★ 回归：新表必须是**新版本**，否则在既有库上根本不会执行。"""
    db = Database(tmp_path / "v4.db")
    cursor = db._conn.cursor()
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
    )
    for version, script in schema.MIGRATIONS:
        if version > 4:
            break
        cursor.executescript(script)
        cursor.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, 0)", (version,)
        )
    assert db.query_one(
        "SELECT name FROM sqlite_master WHERE name='comment_verifications'"
    ) is None
    db.upsert_video(bvid=BVID, aid=AID, title="保留我")
    db.close()

    upgraded = Database(tmp_path / "v4.db")
    assert upgraded.migrate() == schema.SCHEMA_VERSION == 11
    assert upgraded.query_one(
        "SELECT name FROM sqlite_master WHERE name='comment_verifications'"
    ) is not None, "v5 必须建出复核账本表"
    assert upgraded.get_video(BVID) is not None, "迁移把数据弄丢了"
    upgraded.close()


def test_migration_v7_creates_trends_rounds_on_existing_v6_db(tmp_path: Path) -> None:
    """★ 回归（2026-10-17）：推荐流轮次账本必须是**新版本**。

    与 v5 那条同一个道理：把新表塞进**已应用过**的版本号里，
    既有库不会重跑那段 SQL ⇒ 表永远不存在 ⇒ 限额检查直接报错。
    """
    db = Database(tmp_path / "v6.db")
    cursor = db._conn.cursor()
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
    )
    for version, script in schema.MIGRATIONS:
        if version > 6:
            break
        cursor.executescript(script)
        cursor.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, 0)", (version,)
        )
    assert (
        db.query_one("SELECT name FROM sqlite_master WHERE name='trends_rounds'") is None
    ), "v6 时还不该有轮次表"
    db.upsert_video(bvid=BVID, aid=AID, title="保留我")
    db.close()

    upgraded = Database(tmp_path / "v6.db")
    assert upgraded.migrate() == schema.SCHEMA_VERSION == 11
    assert (
        upgraded.query_one("SELECT name FROM sqlite_master WHERE name='trends_rounds'")
        is not None
    ), "v7 必须建出轮次账本表"
    # ★ 限额检查在升级后的既有库上要能直接用（不抛错）
    from core.trends import quota_check

    allowed, _ = quota_check(upgraded)
    assert allowed is True
    assert upgraded.get_video(BVID) is not None, "迁移把数据弄丢了"
    upgraded.close()


async def test_verify_recent_max_items_must_be_positive(db: Database) -> None:
    checked, skipped, text = await verify_recent(db, max_items=0)
    assert (checked, skipped) == ([], 0)
    assert "≥ 1" in text


def test_verify_result_is_frozen_dataclass() -> None:
    result = VerifyResult(bvid=BVID, rpid=OUR_RPID, visible=None, likes=None, how="", note="")
    assert result.visible is None
    with pytest.raises(Exception):  # noqa: B017 - frozen dataclass 必须拒绝赋值
        result.visible = True  # type: ignore[misc]


class _Runtime:
    """最小运行时替身（``core.runtime.Runtime`` 里本模块只用这两项）。"""

    def __init__(self, db: Database, reader: BilibiliReader) -> None:
        self.db = db
        self.reader = reader
