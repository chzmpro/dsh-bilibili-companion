"""意图展开 + 推荐流候选词测试（2026-10-17）。

对应两个新能力：

* **搜索方式 ①**：模糊意图 → 一组关键词（:mod:`core.intent`）；
* **搜索方式 ②**：推荐流 → **候选词池**（:mod:`core.trends`），
  **不自动生效**，等人审。

核心不变量（都在这里钉死）：

* 意图展开**失败必须降级**（不抛异常、不让整轮停摆）；
* 模型给的形状**不可信**（非字符串 / 超长 / 重复 一律清洗掉）；
* 推荐流候选**不自动写进 config** —— 它只是"建议"；
* 中文必须切 **n-gram**，不能整段当词（否则会得到"让百万人尖叫的鬼屋"）；
* **绝不联网**（全部用假 worker / 假数据）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.intent import (
    MAX_KEYWORD_CHARS,
    MAX_KEYWORDS,
    IntentExpansion,
    expand_intent,
    fallback_keywords,
    sanitize_keywords,
)
from core.storage import Database
from core.trends import (
    RecommendationItem,
    drop_suggestion,
    extract_candidates,
    list_suggestions,
    parse_feed,
    record_suggestions,
    suggestion_summary,
)

# ---------------------------------------------------------------------------
# ① 意图 → 关键词
# ---------------------------------------------------------------------------


def test_sanitize_keywords_rejects_bad_shapes() -> None:
    """★ 不信任模型给的形状：非字符串 / 空 / 超长 / 重复 一律清掉。"""
    raw: list[Any] = [
        "  DSH  ",  # 首尾空白
        "dsh",  # 与上一条重复（大小写不敏感）
        "DSH 插件",  # 正常
        123,  # 非字符串
        None,
        "",  # 空
        "   ",  # 只有空白
        "x" * (MAX_KEYWORD_CHARS + 1),  # 超长
        "记忆\n插件",  # 含换行
        {"nested": "object"},  # 非字符串
        ["list"],  # 非字符串
    ]
    out = sanitize_keywords(raw)

    assert out[0] == "DSH"
    assert "dsh" not in out, "大小写不敏感去重"
    assert "DSH 插件" in out
    assert not any("\n" in item for item in out), "换行必须被清掉"
    assert all(isinstance(item, str) for item in out)
    assert all(len(item) <= MAX_KEYWORD_CHARS for item in out)


def test_sanitize_keywords_respects_limit() -> None:
    out = sanitize_keywords([f"词{i}" for i in range(50)], limit=3)
    assert len(out) == 3


def test_sanitize_keywords_non_list_is_empty() -> None:
    assert sanitize_keywords("不是列表") == []
    assert sanitize_keywords(None) == []


def test_fallback_keywords_extracts_useful_fragments() -> None:
    """降级时**必须**还能搜 —— 至少拆出有信息量的片段。"""
    out = fallback_keywords("想看看有没有人做 DSH 的记忆插件")
    assert out, "降级不该产出空列表"
    joined = " ".join(out)
    assert "DSH" in joined
    assert out[0] == "想看看有没有人做 DSH 的记忆插件", "整句排第一（往往就能搜）"


def test_fallback_keywords_empty_intent() -> None:
    assert fallback_keywords("   ") == []


class _FakeWorker:
    """可控的假 worker（**绝不联网**）。"""

    def __init__(
        self,
        *,
        usable: bool = True,
        code: str = "OK",
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self._usable = usable
        self._code = code
        self._result = result
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def is_usable(self) -> tuple[bool, str]:
        return self._usable, self._code

    async def complete_json(
        self, **kwargs: Any
    ) -> tuple[dict[str, Any] | None, str | None, dict[str, int]]:
        self.calls.append(kwargs)
        if self._error is not None:
            return None, self._error, {"prompt_tokens": 10, "completion_tokens": 5}
        return self._result, None, {"prompt_tokens": 20, "completion_tokens": 8}


async def test_expand_intent_happy_path() -> None:
    worker = _FakeWorker(
        result={
            "keywords": ["DSH 记忆插件", "Agent 记忆", "长期记忆"],
            "reason": "理解为找记忆类插件",
        }
    )
    out = await expand_intent("想看看有没有人做 DSH 的记忆插件", worker=worker)

    assert out.used_worker is True
    assert out.error_code is None
    assert out.keywords == ["DSH 记忆插件", "Agent 记忆", "长期记忆"]
    assert "记忆" in out.reason
    assert out.usage["prompt_tokens"] == 20
    assert len(worker.calls) == 1


async def test_expand_intent_degrades_without_worker() -> None:
    """★ 没有 worker → **降级**，不报错。"""
    out = await expand_intent("DSH 插件", worker=None)

    assert out.used_worker is False
    assert out.error_code == "TEXT_WORKER_UNAVAILABLE"
    assert out.keywords, "降级后仍要有可搜的词"


async def test_expand_intent_degrades_when_worker_unusable() -> None:
    worker = _FakeWorker(usable=False, code="TEXT_WORKER_NOT_CONFIGURED")
    out = await expand_intent("DSH 插件", worker=worker)

    assert out.used_worker is False
    assert out.error_code == "TEXT_WORKER_NOT_CONFIGURED"
    assert out.keywords


async def test_expand_intent_degrades_on_model_error() -> None:
    worker = _FakeWorker(error="TEXT_WORKER_TIMEOUT")
    out = await expand_intent("DSH 插件", worker=worker)

    assert out.used_worker is False
    assert out.error_code == "TEXT_WORKER_TIMEOUT"
    assert out.keywords, "超时也要给出降级关键词"


async def test_expand_intent_degrades_on_empty_expansion() -> None:
    """模型返回了合法 JSON 但**关键词是空的** → 也要降级。"""
    worker = _FakeWorker(result={"keywords": [], "reason": "没想法"})
    out = await expand_intent("DSH 插件", worker=worker)

    assert out.used_worker is False
    assert out.error_code == "EMPTY_EXPANSION"
    assert out.keywords


async def test_expand_intent_sanitizes_model_output() -> None:
    """★ 模型给了脏数据也必须能用（形状不可信）。"""
    worker = _FakeWorker(result={"keywords": ["DSH", "dsh", 123, "", "x" * 99, "插件"]})
    out = await expand_intent("DSH", worker=worker)

    assert out.keywords == ["DSH", "插件"]
    assert out.used_worker is True


async def test_expand_intent_empty_intent_returns_empty() -> None:
    out = await expand_intent("   ", worker=_FakeWorker(result={"keywords": ["x"]}))
    assert out.keywords == []
    assert out.used_worker is False


async def test_expand_intent_respects_limit() -> None:
    worker = _FakeWorker(result={"keywords": [f"词{i}" for i in range(30)]})
    out = await expand_intent("x", worker=worker, limit=3)
    assert len(out.keywords) == 3


def test_intent_expansion_to_dict_is_serializable() -> None:
    import json

    item = IntentExpansion(keywords=["a"], reason="r", used_worker=True)
    assert json.loads(json.dumps(item.to_dict(), ensure_ascii=False))["keywords"] == ["a"]


def test_max_keywords_ceiling_is_sane() -> None:
    assert 1 <= MAX_KEYWORDS <= 20


# ---------------------------------------------------------------------------
# ② 推荐流 → 候选词
# ---------------------------------------------------------------------------


def _real_items() -> list[RecommendationItem]:
    """2026-10-17 **实测**拿到的真实推荐标题（用于验证降噪效果）。

    ⚠️ 这份数据里 ``deepseek`` 出现在 2 条标题、``AI`` 也出现在 2 条
    （"14 个 AI 各写一个坦克程序" + "越贵的AI越菜"）——
    所以 ``min_hits=2`` 时两个都该留下。
    """
    return [
        RecommendationItem(
            "BV1y4Hj6LEfA",
            "什么？deepseek v4.1，kimi k3免费了？只需安装一个插件，无需登录注",
            "Nefert-zx",
            154989,
        ),
        RecommendationItem(
            "BV11DJt66Egx", "14 个 AI 各写一个坦克程序，打了 14400 局", "喵了个猫", 40532
        ),
        RecommendationItem(
            "BV11fJX6GEcL", "越贵的AI越菜？我对各大AI工具进行了多维度测试", "Zhan丨", 97738
        ),
        RecommendationItem(
            "BV1KiJk67Ezw", "让百万人尖叫的鬼屋？我们发现了这里的秘密……", "影视飓风", 1458484
        ),
        RecommendationItem("BV1kgaL6YEod", "Deepseekの千层套路", "嘚德尼翁", 310679),
        RecommendationItem("BV1fPJQ6FE6U", "DSH插件下载榜Top10-1016", "是人民啊", 3265),
    ]


def test_extract_candidates_finds_topics_not_whole_sentences() -> None:
    """★★ 核心回归：中文必须切 **n-gram**，不能把整句当词。

    实测踩到：把连续汉字整段当一个词，会得到
    "让百万人尖叫的鬼屋" —— **根本没法拿去搜索**。
    """
    out = extract_candidates(_real_items(), known_keywords=["DSH插件"], limit=20, min_hits=1)
    keywords = [s.keyword for s in out]

    assert keywords, "应该提取出候选"
    assert not any(len(k) > 20 for k in keywords), "不该出现整句长词"
    assert "让百万人尖叫的鬼屋" not in keywords, "整段中文不该作为候选"
    # 真话题应该出现
    assert "deepseek" in [k.lower() for k in keywords]
    assert "AI" in keywords


def test_extract_candidates_min_hits_filters_noise() -> None:
    """★ ``min_hits=2`` 是降噪旋钮：只留**在 ≥2 条视频里出现**的词。

    实测：n-gram 会产出"需安装一"这类跨词边界碎片，
    但它们几乎不会在两条不同视频里同时出现。
    """
    loose = extract_candidates(_real_items(), limit=30, min_hits=1)
    strict = extract_candidates(_real_items(), limit=30, min_hits=2)

    assert len(strict) < len(loose), "min_hits=2 应该更少"
    assert all(s.hits >= 2 for s in strict), "严格的都应 hits>=2"
    # 真话题要留下（注意比较用小写：去重是大小写不敏感的）
    assert {"deepseek", "ai"} <= {s.keyword.lower() for s in strict}


def test_extract_candidates_excludes_known_keywords() -> None:
    """已经在 config 里的词不算"新发现"。"""
    out = extract_candidates(_real_items(), known_keywords=["deepseek", "AI"], limit=30)
    lowered = {s.keyword.lower() for s in out}
    assert "deepseek" not in lowered
    assert "ai" not in lowered


def test_extract_candidates_carries_sources_for_audit() -> None:
    """★ 可审计：每个候选都要能追到"从哪条推荐来"。"""
    out = extract_candidates(_real_items(), limit=30)
    for suggestion in out:
        assert suggestion.sources, f"{suggestion.keyword} 缺来源"
        first = suggestion.sources[0]
        assert first["bvid"].startswith("BV")
        assert "title" in first


def test_extract_candidates_dedupes_within_one_title() -> None:
    """同一条标题里重复出现只算一次（否则单条视频就能把 hits 刷上去）。"""
    items = [RecommendationItem("BV1x", "插件插件插件", "up", 1)]
    out = extract_candidates(items, limit=10, min_hits=1)
    plugin = [s for s in out if s.keyword == "插件"]
    assert plugin and plugin[0].hits == 1


def test_extract_candidates_empty_input() -> None:
    assert extract_candidates([], limit=10) == []


def test_parse_feed_handles_unwrapped_payload() -> None:
    """★ 实测形状：``_get_signed`` 返回的**已是解包后的 data**（顶层就是 item）。"""
    payload = {
        "item": [
            {
                "bvid": "BV1test",
                "title": "标题",
                "owner": {"mid": 1, "name": "UP"},
                "stat": {"view": 123},
                "rcmd_reason": {"content": "6万点赞"},
            }
        ]
    }
    items = parse_feed(payload)
    assert len(items) == 1
    assert items[0].bvid == "BV1test"
    assert items[0].up_name == "UP"
    assert items[0].view == 123
    assert items[0].reason == "6万点赞"


def test_parse_feed_tolerates_garbage() -> None:
    assert parse_feed(None) == []
    assert parse_feed({}) == []
    assert parse_feed({"item": "不是列表"}) == []
    assert parse_feed({"item": [{"no_bvid": 1}, "字符串", None]}) == []


# ---------------------------------------------------------------------------
# 候选池：攒着等人审，**不自动生效**
# ---------------------------------------------------------------------------


def test_suggestions_are_pooled_not_applied(db: Database) -> None:
    """★★ 核心边界：候选词进池子，**绝不自动改 config**。"""
    suggestions = extract_candidates(_real_items(), limit=10, min_hits=1)
    added = record_suggestions(db, suggestions)

    assert added > 0
    pooled = list_suggestions(db)
    assert len(pooled) == added
    # 池子里有来源，能审计
    assert all(item["sources"] for item in pooled)


def test_record_suggestions_accumulates_hits(db: Database) -> None:
    """同一个词再次出现 → 累加 hits，而不是重复插入。"""
    from core.trends import Suggestion

    record_suggestions(db, [Suggestion(keyword="插件", hits=1)])
    record_suggestions(db, [Suggestion(keyword="插件", hits=2)])

    pooled = list_suggestions(db)
    assert len(pooled) == 1
    assert pooled[0]["hits"] == 3


def test_drop_suggestion(db: Database) -> None:
    from core.trends import Suggestion

    record_suggestions(db, [Suggestion(keyword="不想要的词")])
    assert drop_suggestion(db, "不想要的词") is True
    assert drop_suggestion(db, "不想要的词") is False
    assert list_suggestions(db) == []


def test_suggestion_summary(db: Database) -> None:
    from core.trends import Suggestion

    assert suggestion_summary(db)["total"] == 0
    record_suggestions(db, [Suggestion(keyword="a"), Suggestion(keyword="b")])
    assert suggestion_summary(db)["total"] == 2


def test_list_suggestions_orders_by_hits(db: Database) -> None:
    from core.trends import Suggestion

    record_suggestions(
        db, [Suggestion(keyword="少", hits=1), Suggestion(keyword="多", hits=9)]
    )
    pooled = list_suggestions(db)
    assert pooled[0]["keyword"] == "多"


def test_record_suggestions_empty_is_noop(db: Database) -> None:
    assert record_suggestions(db, []) == 0


@pytest.mark.parametrize("limit", [1, 5, 10])
def test_extract_candidates_respects_limit(limit: int) -> None:
    out = extract_candidates(_real_items(), limit=limit, min_hits=1)
    assert len(out) <= limit


# ---------------------------------------------------------------------------
# ★★ 严格限额：轮次账本（2026-10-17 补 —— 原先只写在文档里，代码从不检查）
# ---------------------------------------------------------------------------


def test_quota_allows_then_blocks(db: Database) -> None:
    """★★ 核心：跑满上限后**必须拒绝**。

    第一版只把 ``DEFAULT_MAX_ROUNDS_PER_DAY`` 写在文档里，代码从不检查 ——
    也就是说"限额"是假的。推荐流是算法喂的，跑得越多画像越被带跑，
    所以这道闸必须是**真的**。
    """
    from core.trends import DEFAULT_MAX_ROUNDS_PER_DAY, quota_check, record_round

    for i in range(DEFAULT_MAX_ROUNDS_PER_DAY):
        allowed, _ = quota_check(db)
        assert allowed is True, f"第 {i + 1} 轮应该允许"
        record_round(db, fetched_n=8, added_n=2)

    allowed, reason = quota_check(db)
    assert allowed is False, "跑满后必须拒绝"
    assert "上限" in reason
    assert "画像" in reason, "拒绝理由要说清为什么有这个限制"


def test_quota_reason_is_human_readable(db: Database) -> None:
    """拒绝信息要**人话**（agent 会把这句话转述给主人）。"""
    from core.trends import quota_check

    allowed, reason = quota_check(db)
    assert allowed is True
    assert "还可跑" in reason


def test_record_round_writes_to_own_ledger_not_runs(db: Database) -> None:
    """★★ 关键隔离：轮次必须写进 ``trends_rounds``，**不是 ``runs``**。

    若写进 ``runs``，``schedule.is_due()`` 会误以为**主流程今天已经干过活了**
    ⇒ 当天不再抓取/评论。刷一次推荐流就把整天的主流程废掉，是绝不能有的耦合。
    """
    from core.trends import record_round

    runs_before = db.query("SELECT COUNT(*) AS n FROM runs")[0]["n"]
    record_round(db, fetched_n=5, added_n=1)
    runs_after = db.query("SELECT COUNT(*) AS n FROM runs")[0]["n"]

    assert runs_after == runs_before, "轮次绝不能写进 runs 表"
    rounds = db.query("SELECT COUNT(*) AS n FROM trends_rounds")[0]["n"]
    assert rounds == 1


def test_rounds_today_counts_only_today(db: Database) -> None:
    """昨天的轮次不算今天的额度。"""
    from core.trends import rounds_today

    db.execute(
        "INSERT INTO trends_rounds (round_id, started_at, fetched_n, added_n, conclusion) "
        "VALUES (?,?,?,?,?)",
        ("old", 1_600_000_000, 8, 1, "ok"),  # 2020 年，肯定不是今天
    )
    assert rounds_today(db) == 0


def test_rounds_remaining_never_negative(db: Database) -> None:
    """超额（比如上限被调小）时 remaining **不能是负数**。"""
    from core.trends import record_round, rounds_remaining

    for _ in range(5):
        record_round(db, conclusion="ok")
    assert rounds_remaining(db, max_per_day=2) == 0


def test_rounds_summary_shape(db: Database) -> None:
    from core.trends import record_round, rounds_summary

    summary = rounds_summary(db)
    assert summary["used_today"] == 0
    assert summary["max_per_day"] >= 1
    assert summary["remaining_today"] == summary["max_per_day"]

    record_round(db, fetched_n=8, added_n=3)
    after = rounds_summary(db)
    assert after["used_today"] == 1
    assert after["remaining_today"] == summary["max_per_day"] - 1
    assert after["last_at"] > 0


def test_rounds_ledger_records_outcome(db: Database) -> None:
    """**失败也要记轮次** —— 否则"每次都被拦"会变成无限重试。"""
    from core.trends import record_round, rounds_today

    for conclusion in ("blocked", "rate_limited", "error", "no_content"):
        record_round(db, conclusion=conclusion)
    assert rounds_today(db) == 4


def test_default_round_cap_is_conservative() -> None:
    """默认上限必须**保守**（这是"不让算法带跑画像"的第一道闸）。"""
    from core.trends import DEFAULT_MAX_ITEMS, DEFAULT_MAX_ROUNDS_PER_DAY

    assert 1 <= DEFAULT_MAX_ROUNDS_PER_DAY <= 4
    assert 1 <= DEFAULT_MAX_ITEMS <= 20


def test_start_of_today_is_shared_with_storage() -> None:
    """★ "今天从几点算起"**只有一份实现**（复用它，不自己再写）。

    两份实现早晚会一份本地时区、一份 UTC ⇒ 配额在半夜附近对不上，
    而且白天测不出来。
    """
    from core.storage import _start_of_today as storage_impl
    from core.trends import _start_of_today as trends_impl

    assert trends_impl() == storage_impl()


# ---------------------------------------------------------------------------
# 自动回填开关（**默认关**）
# ---------------------------------------------------------------------------


def test_auto_accept_suggestions_defaults_off() -> None:
    """★★ 默认**必须**是关 —— 自动回填等于让算法间接决定小号关注什么。"""
    from core.config import DiscoveryConfig

    cfg = DiscoveryConfig(keywords=["a"])
    assert cfg.auto_accept_suggestions is False


def test_auto_accept_suggestions_can_be_enabled() -> None:
    """显式打开要能生效（给"我接受这个代价"的主人留口子）。"""
    from core.config import DiscoveryConfig

    cfg = DiscoveryConfig(keywords=["a"], auto_accept_suggestions=True)
    assert cfg.auto_accept_suggestions is True


def test_backfill_writes_into_config_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """开着时，候选词要真的写进 ``discovery.keywords``（模拟回填动作）。"""
    from core import yamlkeywords

    sample = (
        'discovery:\n  keywords:\n    - "甲"\n'
        'interaction:\n  mode: "observe"\n  dry_run: true\n  publish_enabled: false\n'
    )
    target = tmp_path / "config.yaml"
    target.write_text(sample, encoding="utf-8")
    monkeypatch.setattr(yamlkeywords, "config_yaml_path", lambda: target)

    # 这就是 bridge 里回填分支调用的东西
    ok, _ = yamlkeywords.add_keyword("新词")
    assert ok is True
    assert "新词" in yamlkeywords.read_keywords()
    # ★ 授权字段仍不能被动
    after = target.read_text(encoding="utf-8")
    assert 'mode: "observe"' in after
    assert "dry_run: true" in after
    assert "publish_enabled: false" in after


def test_quota_applies_regardless_of_auto_backfill(db: Database) -> None:
    """★★ 无论自动回填开关如何，**每日轮次上限始终生效**。

    这是关键：开关只决定"词怎么进 keywords"，**不决定"能跑几轮"**。
    否则打开自动回填就等于同时解除了防画像漂移的闸门。
    """
    from core.trends import DEFAULT_MAX_ROUNDS_PER_DAY, quota_check, record_round

    for _ in range(DEFAULT_MAX_ROUNDS_PER_DAY):
        assert quota_check(db)[0] is True
        record_round(db, conclusion="ok")

    allowed, _ = quota_check(db)
    assert allowed is False, "开关不影响轮次上限"
