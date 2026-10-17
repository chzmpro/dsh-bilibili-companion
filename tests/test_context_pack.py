"""项目上下文包测试（2026-10-17）。

## 主人要求

> 「没法给豆包模型塞上下文让他看懂最起码我们浅层的内容吗」

## 设计原则：给"认识"，不给"全部历史"

| 给 | 不给 |
|---|---|
| 我是谁、主人在做什么项目 | 完整对话记录（几万 token） |
| 项目现在什么状态、有什么能力 | 内部实现细节 |
| 最近跑了几轮、看了什么 | 每次运行的原始数据 |
| 主人的偏好与红线 | 全部配置项 |

**为什么**：喂太多会烧 token、抓不住重点、说错概率反而上升。
实测 655 字就够（见文档里的对比）。

## 钉死的不变量

* **总长有上限**（防 token 爆炸）；
* **任何一块读不到都要降级**，不能整个上下文崩；
* **只读** —— 绝不因为拼上下文而写库；
* **真实数据** —— 状态里的数字必须来自数据库，不能编。
"""

from __future__ import annotations

import pytest

from core import context_pack
from core.context_pack import (
    IDENTITY_BLOCK,
    MAX_CONTEXT_CHARS,
    PROJECT_BLOCK,
    ContextBundle,
    build_context,
    build_preferences,
    build_status,
)
from core.storage import Database

# ---------------------------------------------------------------------------
# 固定块
# ---------------------------------------------------------------------------


def test_identity_has_persona() -> None:
    assert "小鲸鱼" in IDENTITY_BLOCK
    assert "沐雨林心" in IDENTITY_BLOCK


def test_project_block_lists_capabilities() -> None:
    """项目块要让模型知道"我们会什么"。"""
    for keyword in ("B 站", "GitHub", "联网搜"):
        assert keyword in PROJECT_BLOCK


def test_project_block_states_read_only_default() -> None:
    """★ 必须说清"默认只读不写" —— 否则模型可能以为自己能随便发评论。"""
    assert "只读不写" in PROJECT_BLOCK


# ---------------------------------------------------------------------------
# 上下文组装
# ---------------------------------------------------------------------------


def test_bundle_to_prompt_shape() -> None:
    bundle = ContextBundle(status="- 测试状态", preferences=["偏好一"])
    text = bundle.to_prompt()
    assert "你是谁" in text
    assert "主人在做什么" in text
    assert "现在的状态" in text
    assert "主人的要求" in text
    assert "- 偏好一" in text


def test_bundle_respects_limit() -> None:
    """★ 超长必须截断（防 token 爆炸）。"""
    bundle = ContextBundle(status="字" * (MAX_CONTEXT_CHARS * 2))
    assert len(bundle.to_prompt()) <= MAX_CONTEXT_CHARS


def test_bundle_skips_empty_blocks() -> None:
    bundle = ContextBundle(status="", preferences=[])
    text = bundle.to_prompt()
    assert "现在的状态" not in text
    assert "主人的要求" not in text


def test_bundle_to_dict_reports_chars() -> None:
    payload = ContextBundle().to_dict()
    assert payload["chars"] > 0
    assert payload["chars"] <= MAX_CONTEXT_CHARS


# ---------------------------------------------------------------------------
# 状态（真实数据）
# ---------------------------------------------------------------------------


def test_status_reads_runs(db: Database) -> None:
    db.execute(
        "INSERT INTO runs (run_id, trigger_source, started_at, candidates_n, read_n, "
        "drafts_n, sent_n, sim_sent_n, errors_n, conclusion, detail_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("run_test_1", "manual", 1700000000, 5, 50, 0, 0, 0, 0, "ok", "{}"),
    )
    text = build_status(db)
    assert "最近几轮运行" in text
    assert "ok" in text


def test_status_reads_inbox_count(db: Database) -> None:
    db.execute(
        "INSERT INTO dm_inbox (msg_seqno, text, received_at, status, created_at) "
        "VALUES (?,?,?,?,?)",
        (12345, "测试", 1700000000, "pending", 1700000000),
    )
    text = build_status(db)
    assert "待回复的私信：1 条" in text


def test_status_reports_empty_inbox(db: Database) -> None:
    text = build_status(db)
    assert "队列是空的" in text or "0 条" in text


def test_status_reads_video_count(db: Database) -> None:
    db.execute(
        "INSERT INTO videos (bvid, aid, title, up_name, source_url, first_seen_at, "
        "last_seen_at, status, discovery_keyword) VALUES (?,?,?,?,?,?,?,?,?)",
        ("BV1test", 1, "测试视频", "UP", "https://x", 1700000000, 1700000000,
         "discovered", "kw"),
    )
    text = build_status(db)
    assert "已记录的视频" in text


def test_status_survives_broken_db() -> None:
    """★ 数据库对象坏掉时返回空字符串，**不能抛异常**（否则整个回复流程崩）。"""

    class _Broken:
        def query(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("库坏了")

        def query_one(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("库坏了")

    text = build_status(_Broken())
    assert isinstance(text, str)


def test_status_reads_digest_titles(db: Database, tmp_path: object) -> None:
    from pathlib import Path

    digest_dir = Path(str(tmp_path)) / "digests"
    digest_dir.mkdir()
    (digest_dir / "digest-20261017.md").write_text(
        "# 日报\n\n### 某视频标题（BV1abc）\n\n- 总分：80\n", encoding="utf-8"
    )
    text = build_status(db, digest_dir=digest_dir)
    assert "某视频标题" in text
    # BV 号要从标题里去掉（模型不需要）
    assert "BV1abc" not in text


# ---------------------------------------------------------------------------
# 偏好
# ---------------------------------------------------------------------------


def test_preferences_reads_table(db: Database) -> None:
    db.execute(
        "INSERT INTO owner_preferences (pref_id, kind, subject, content, scope, active, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("pref_1", "rule", "", "优先讲清解决了什么问题", "content", 1, 1700000000, 1700000000),
    )
    prefs = build_preferences(db)
    assert "优先讲清解决了什么问题" in prefs


def test_preferences_survives_broken_db() -> None:
    class _Broken:
        def query(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("坏了")

    assert build_preferences(_Broken()) == []


def test_preferences_respects_limit(db: Database) -> None:
    for index in range(10):
        db.execute(
            "INSERT INTO owner_preferences (pref_id, kind, subject, content, scope, "
            "active, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"pref_{index}", "rule", "", f"规则{index}", "content", 1,
             1700000000 + index, 1700000000 + index),
        )
    prefs = build_preferences(db, limit=3)
    assert len(prefs) == 3


# ---------------------------------------------------------------------------
# 整体
# ---------------------------------------------------------------------------


def test_build_context_end_to_end(db: Database) -> None:
    bundle = build_context(db)
    text = bundle.to_prompt()
    assert "小鲸鱼" in text
    assert len(text) <= MAX_CONTEXT_CHARS


def test_describe_shape() -> None:
    info = context_pack.describe()
    assert info["max_chars"] == MAX_CONTEXT_CHARS
    assert "身份" in info["blocks"]


@pytest.mark.parametrize("limit", [200, 500, 1400])
def test_limit_is_respected(limit: int) -> None:
    bundle = ContextBundle(status="字" * 3000)
    assert len(bundle.to_prompt(limit=limit)) <= limit
