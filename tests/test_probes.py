"""只读探测测试（``core.probes``）。

探测的价值在于：把"现在到底能不能干活"变成**可读的原因**，
而不是让调用方在跑到一半时撞上 ``-101`` 才知道没登录。
"""

from __future__ import annotations

from typing import Any

import httpx

from core.bilibili import BilibiliReader
from core.probes import ProbeReport, probe
from core.ratelimit import RequestQueue
from core.storage import Database
from tests.conftest import FakeClock, build_client, make_config, nav_payload, ok_payload


def make_reader(db: Database, handler: Any, clock: FakeClock) -> BilibiliReader:
    config = make_config(bilibili_requests={"min_interval_seconds": 1.0})
    queue = RequestQueue(
        db=db, config=config, client=build_client(handler), clock=clock, sleep=clock.sleep
    )
    return BilibiliReader(queue=queue, cookies={})


def nav_handler(mid: int, is_login: bool, extra: list[str] | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if "nav" in str(request.url):
            return httpx.Response(200, json=nav_payload(mid=mid, is_login=is_login))
        if extra:
            for needle in extra:
                if needle in str(request.url):
                    return httpx.Response(200, json=ok_payload({}))
        return httpx.Response(404, json={"code": -404, "message": "no route"})

    return handler


async def test_probe_reports_not_logged_in(db: Database, clock: FakeClock) -> None:
    reader = make_reader(db, nav_handler(0, False), clock)
    report = await probe(reader=reader, config=make_config())

    assert isinstance(report, ProbeReport)
    assert report.login_ok is False
    assert "未登录" in " ".join(report.notes)
    assert report.blocking_reason == "", "未登录不是停机理由——公开搜索仍可用"


async def test_probe_reports_logged_in_identity(db: Database, clock: FakeClock) -> None:
    reader = make_reader(db, nav_handler(1001, True), clock)
    report = await probe(reader=reader, config=make_config())

    assert report.login_ok is True
    assert report.logged_in_mid == 1001
    assert report.logged_in_name


async def test_probe_flags_mid_mismatch(db: Database, clock: FakeClock) -> None:
    """★ 实际登录 UID 与配置不符 → 明确提示（发布会被拒）。"""
    reader = make_reader(db, nav_handler(9999, True), clock)
    config = make_config(account={"agent_mid": 1001, "owner_mid": 2002})
    report = await probe(reader=reader, config=config)

    assert report.mid_matches is False
    assert any("不符" in n for n in report.notes)


async def test_probe_confirms_matching_identity(db: Database, clock: FakeClock) -> None:
    reader = make_reader(db, nav_handler(1001, True), clock)
    config = make_config(account={"agent_mid": 1001, "owner_mid": 2002})
    report = await probe(reader=reader, config=config)
    assert report.mid_matches is True


async def test_probe_notes_unconfigured_agent_mid(db: Database, clock: FakeClock) -> None:
    reader = make_reader(db, nav_handler(1001, True), clock)
    report = await probe(reader=reader, config=make_config())
    assert report.mid_matches is None
    assert any("agent_mid=0" in n for n in report.notes)


async def test_probe_stops_immediately_when_blocked(db: Database, clock: FakeClock) -> None:
    """★ 阻断状态下**不发任何请求**，直接给出人工动作。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=nav_payload())

    db.record_health(state="verification_required", category="captcha")
    reader = make_reader(db, handler, clock)
    report = await probe(reader=reader, config=make_config())

    assert report.platform_health == "verification_required"
    assert report.blocking_reason
    assert report.human_action, "必须给出人工动作"
    assert calls == [], "阻断状态下不得发起任何请求"


async def test_probe_reports_health_for_auth_expired(db: Database, clock: FakeClock) -> None:
    db.record_health(state="auth_expired", category="login")
    reader = make_reader(db, nav_handler(0, False), clock)
    report = await probe(reader=reader, config=make_config())
    assert report.blocking_reason
    assert "扫码" in report.human_action


async def test_probe_notes_missing_credentials(db: Database, clock: FakeClock) -> None:
    reader = make_reader(db, nav_handler(0, False), clock)
    report = await probe(reader=reader, config=make_config())
    assert report.credentials["present"] is False
    assert any("凭据" in n for n in report.notes)


async def test_probe_never_reports_credential_values(
    db: Database, clock: FakeClock, isolated_data_dir: Any
) -> None:
    """★ 探测报告里绝不能出现凭据值。"""
    secret = "ProbeSecretSessdata1234567890"
    (isolated_data_dir / "cookie.txt").write_text(
        f"SESSDATA={secret}; bili_jct=abc123abc123abc123abc123abc12345", encoding="utf-8"
    )
    reader = make_reader(db, nav_handler(1001, True), clock)
    report = await probe(reader=reader, config=make_config())

    import json

    serialized = json.dumps(report.to_dict(), ensure_ascii=False)
    assert secret not in serialized
    assert report.credentials["present"] is True
    assert "sessdata" in report.credentials["names"]


async def test_probe_reports_text_worker_state(db: Database, clock: FakeClock) -> None:
    reader = make_reader(db, nav_handler(1001, True), clock)
    report = await probe(reader=reader, config=make_config())
    assert report.text_worker_usable is False
    assert report.text_worker_code == "TEXT_WORKER_NOT_CONFIGURED"


async def test_probe_notes_publish_gate_reason(db: Database, clock: FakeClock) -> None:
    reader = make_reader(db, nav_handler(1001, True), clock)
    config = make_config(account={"agent_mid": 1001, "owner_mid": 2002})
    report = await probe(reader=reader, config=config)
    joined = " ".join(report.notes)
    assert "发布门禁" in joined
    assert "DRY_RUN" in joined or "MODE_OBSERVE" in joined


async def test_probe_survives_network_error(db: Database, clock: FakeClock) -> None:
    """★ 探测**不能抛异常**——网络问题也要变成报告字段。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    reader = make_reader(db, handler, clock)
    report = await probe(reader=reader, config=make_config())
    assert isinstance(report, ProbeReport)
    assert report.login_ok is False


async def test_probe_survives_unexpected_error(db: Database, clock: FakeClock) -> None:
    class ExplodingReader:
        class queue:  # noqa: N801
            class db:  # noqa: N801
                @staticmethod
                def health_state() -> str:
                    return "ready"

        async def get_nav(self) -> Any:
            raise RuntimeError("意料之外的失败")

    report = await probe(reader=ExplodingReader(), config=make_config())
    assert isinstance(report, ProbeReport)


async def test_probe_report_is_json_serializable(db: Database, clock: FakeClock) -> None:
    import json

    reader = make_reader(db, nav_handler(1001, True), clock)
    report = await probe(reader=reader, config=make_config())
    json.dumps(report.to_dict(), ensure_ascii=False)


async def test_probe_can_skip_login_check(db: Database, clock: FakeClock) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=nav_payload())

    reader = make_reader(db, handler, clock)
    await probe(reader=reader, config=make_config(), check_login=False)
    assert calls == []
