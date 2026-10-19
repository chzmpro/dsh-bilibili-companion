"""C 项：推荐流抽词自动回填的护栏与顺序（2026-10-19）。

主人选择打开 ``discovery.auto_accept_suggestions``（算法抽出的词直接写进
``config.yaml``）。这个开关一旦打开，必须有护栏与审计，否则会失控：

* 一轮最多写 3 个词；
* 关键词**总数**不超过 ``discovery.max_keywords``（默认 12）—— 每多一个词
  就多一路搜索请求，请求量与风控暴露面线性上涨；
* 每个写/跳过都落 ``keyword_adoptions`` 账本（可审计、可撤销）；
* 推荐流有自己的每日轮次上限（``trends_rounds``），与 ``runs`` 分开。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.storage import Database
from core.trends import active_adoptions, adoption_summary, record_adoption, revert_adoption
from core.trends_round import MAX_BACKFILL_PER_ROUND, run_trends_round
from tests.conftest import make_config


class FakeReader:
    """只回推荐流 payload 的假读取器。"""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls = 0

    async def _get_signed(
        self, url: str, params: dict[str, Any], *, kind: str = "", use_cache: bool = True
    ) -> Any:
        self.calls += 1
        return self.payload


def _feed(keywords: list[str]) -> dict[str, Any]:
    """构造一个推荐流 payload。

    ⚠️ `parse_feed` 吃的是 ``_get_signed`` **解包后的 data** —— 顶层直接是
    ``item`` 列表，**没有** ``code`` / ``data`` 外壳（实测结论）。
    """
    items = [
        {
            "bvid": f"BV1feed{i:05d}",
            "title": f"{kw} 相关视频 {i}",
            "owner": {"name": "某UP"},
            "stat": {"view": 100},
            "rcmd_reason": {"content": kw},
        }
        for kw in keywords
        for i in range(3)
    ]
    return {"item": items}


class FakeRuntime:
    def __init__(self, db: Database, config: Any, payload: Any) -> None:
        self.db = db
        self.config = config
        self.reader = FakeReader(payload)


class _KeywordHome:
    """把 ``core.yamlkeywords`` 指到一个临时 config.yaml 上。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any, keywords: list[str]) -> None:
        from core import paths, yamlkeywords

        project = tmp_path / "proj"
        project.mkdir(parents=True, exist_ok=True)
        body = "discovery:\n  keywords:\n" + "".join(f'    - "{k}"\n' for k in keywords)
        (project / "config.yaml").write_text(body, encoding="utf-8")
        monkeypatch.setattr(paths, "project_root", lambda: project)
        monkeypatch.setattr(yamlkeywords, "config_yaml_path", lambda: project / "config.yaml")


def test_g6_adoption_ledger_roundtrip(db: Database) -> None:
    record_adoption(db, keyword="DSH 插件", run_id="run_1", state="active")
    record_adoption(db, keyword="无用词", run_id="run_1", state="skipped", note="到上限")
    assert active_adoptions(db) == ["DSH 插件"]
    assert adoption_summary(db) == {"active": 1, "skipped": 1, "reverted": 0}

    assert revert_adoption(db, "DSH 插件") is True
    assert active_adoptions(db) == []
    assert adoption_summary(db)["reverted"] == 1


def test_g6_illegal_adoption_state_rejected(db: Database) -> None:
    with pytest.raises(ValueError):
        record_adoption(db, keyword="x", state="bogus")


async def test_backfill_writes_keywords_and_ledger(
    db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _KeywordHome(monkeypatch, tmp_path, ["DSH插件"])
    config = make_config(
        discovery={"keywords": ["DSH插件"], "auto_accept_suggestions": True, "max_keywords": 12}
    )
    runtime = FakeRuntime(db, config, _feed(["Harness插件", "Agent框架", "MCP工具"]))
    result = await run_trends_round(runtime, run_id="run_x")

    assert result.ok and result.code == "OK"
    assert len(result.backfilled) == MAX_BACKFILL_PER_ROUND
    assert result.backfill_skipped == []
    from core.yamlkeywords import read_keywords

    written = read_keywords()
    assert "DSH插件" in written
    assert len(written) == 1 + MAX_BACKFILL_PER_ROUND
    assert adoption_summary(db)["active"] == MAX_BACKFILL_PER_ROUND


async def test_backfill_respects_total_keyword_cap(
    db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """★ 总数上限必须硬性生效：满了就**停止回填**并如实报告跳过了几个。"""
    _KeywordHome(monkeypatch, tmp_path, ["a", "b"])
    config = make_config(
        discovery={"keywords": ["a", "b"], "auto_accept_suggestions": True, "max_keywords": 3}
    )
    runtime = FakeRuntime(db, config, _feed(["c", "d", "e"]))
    result = await run_trends_round(runtime, run_id="run_y")

    assert len(result.backfilled) == 1, "只剩 1 个名额"
    assert len(result.backfill_skipped) == 2, "超出的要如实报告，不静默丢弃"
    from core.yamlkeywords import read_keywords

    assert len(read_keywords()) == 3
    assert adoption_summary(db)["skipped"] == 2


async def test_backfill_off_keeps_pool_only(
    db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """开关关着时：只进候选池，**一个词都不写**（默认安全姿态）。"""
    _KeywordHome(monkeypatch, tmp_path, ["DSH插件"])
    config = make_config(
        discovery={"keywords": ["DSH插件"], "auto_accept_suggestions": False}
    )
    runtime = FakeRuntime(db, config, _feed(["Harness插件"]))
    result = await run_trends_round(runtime)

    assert result.backfilled == []
    from core.yamlkeywords import read_keywords

    assert read_keywords() == ["DSH插件"], "关着时不能改关键词"
    assert result.new_in_pool >= 1, "但仍要进候选池"


async def test_trends_round_uses_own_round_ledger(
    db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """推荐流记 ``trends_rounds``，**不写** ``runs``（否则会骗过 schedule）。"""
    _KeywordHome(monkeypatch, tmp_path, ["DSH插件"])
    config = make_config(discovery={"keywords": ["DSH插件"]})
    runtime = FakeRuntime(db, config, _feed(["Harness插件"]))
    await run_trends_round(runtime)

    rounds = db.query_one("SELECT COUNT(*) AS n FROM trends_rounds")
    runs = db.query_one("SELECT COUNT(*) AS n FROM runs")
    assert rounds is not None and int(rounds["n"]) == 1
    assert runs is not None and int(runs["n"]) == 0, "推荐流不能写 runs"


def test_feed_payload_parses() -> None:
    from core.trends import parse_feed

    items = parse_feed(_feed(["x"]))
    assert items, "假 payload 至少要能解析出一条"
    assert json.dumps(items[0].to_dict(), ensure_ascii=False)
