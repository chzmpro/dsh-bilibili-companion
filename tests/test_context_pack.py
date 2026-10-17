"""完整上下文测试（2026-10-17 第二版）。

## 主人要求（第一版被否）

> 「上下文可以足够多且详细，因为豆包模型的token不限量不用为我节省，
>   所以我希望它能尽可能了解我的想法」

第一版只给 655 字。**那个原则对主人不适用** —— 他要"尽可能了解我的想法"。

## 实测：豆包窗口够大

```
8,000 字   → prompt_tokens = 4,842
30,000 字  → prompt_tokens = 18,042
80,000 字  → prompt_tokens = 48,042
```

## 钉死的不变量

* **人设全文要进来**（8,940 字）—— 这是"怎么说话"的权威来源；
* **主人的原话要进来**（不是我的转述）—— 最直接反映他的想法；
* **AGENTS.md 自动发现全局那份**（项目内那份不含网络通道/铁律）；
* **超限时按块丢弃**，不砍半句；
* **任何一块读不到都降级**，不抛异常。
"""

from __future__ import annotations

import os
from pathlib import Path

from core import context_pack
from core.context_pack import (
    BLOCK_LIMITS,
    IDENTITY_BLOCK,
    MAX_CONTEXT_CHARS,
    PROJECT_BLOCK,
    ContextBundle,
    build_context,
    build_owner_voice,
    build_preferences,
    build_status,
    find_agents_md,
)
from core.storage import Database

# ---------------------------------------------------------------------------
# 上限（不是为省 token，是防卡死）
# ---------------------------------------------------------------------------


def test_limit_is_large_but_bounded() -> None:
    """★ 上限要**大**（主人说不限量），但仍要有界（防超时/抓不住重点）。"""
    assert MAX_CONTEXT_CHARS >= 50_000, "太小了，主人说要详细"
    assert MAX_CONTEXT_CHARS <= 100_000, "太大有超时风险（实测 8 万字能过）"


def test_block_limits_cover_all_blocks() -> None:
    for name in ("persona", "owner_voice", "preferences", "conventions", "status", "watched"):
        assert name in BLOCK_LIMITS, f"{name} 缺少块上限"


# ---------------------------------------------------------------------------
# 固定块
# ---------------------------------------------------------------------------


def test_identity_has_persona() -> None:
    assert "小鲸鱼" in IDENTITY_BLOCK
    assert "沐雨林心" in IDENTITY_BLOCK


def test_project_block_lists_capabilities() -> None:
    for keyword in ("B 站", "GitHub", "联网搜"):
        assert keyword in PROJECT_BLOCK


def test_project_block_states_read_only_default() -> None:
    assert "只读不写" in PROJECT_BLOCK


# ---------------------------------------------------------------------------
# ★ 人设全文
# ---------------------------------------------------------------------------


def test_persona_full_text_is_included(tmp_path: Path) -> None:
    """★★ 人设全文要进上下文 —— 这是"怎么说话"的权威来源。

    真机验证：带上人设后，豆包能准确说出
    「少女感 + 括号吐槽 + 不堆表情 + 不写公文」和「不瞎编/不乱承诺/不泄密」。
    """
    persona = tmp_path / "persona.md"
    persona.write_text("# 人设\n\n## 说话方式\n少女感、括号吐槽\n", encoding="utf-8")
    bundle = build_context(Database(":memory:"), persona_path=persona)
    assert "少女感、括号吐槽" in bundle.to_prompt()


def test_persona_missing_degrades() -> None:
    """读不到人设文件时降级，不崩。"""
    bundle = build_context(Database(":memory:"), persona_path=Path("/no/such/file.md"))
    assert bundle.persona == ""


# ---------------------------------------------------------------------------
# ★ 主人的原话
# ---------------------------------------------------------------------------


def test_owner_voice_uses_raw_quotes(db: Database) -> None:
    """★★ 用**他的原话**，不用我的转述 —— 转述会丢语气和重点。"""
    db.execute(
        "INSERT INTO dm_inbox (msg_seqno, text, received_at, status, created_at) "
        "VALUES (?,?,?,?,?)",
        (111, "我想让豆包也懂上下文，不要省token", 1700000000, "pending", 1700000000),
    )
    text = build_owner_voice(db)
    assert "我想让豆包也懂上下文，不要省token" in text


def test_owner_voice_includes_replied(db: Database) -> None:
    """已回过的要标出来，避免重复答。"""
    db.execute(
        "INSERT INTO dm_replies (msg_seqno, reply_text, replied_at) VALUES (?,?,?)",
        (222, "这是上次的回复", 1700000000),
    )
    text = build_owner_voice(db)
    assert "不要重复答" in text
    assert "这是上次的回复" in text


def test_owner_voice_includes_shared_video(db: Database) -> None:
    db.execute(
        "INSERT INTO dm_inbox (msg_seqno, text, title, received_at, status, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (333, "看这个", "某视频标题", 1700000000, "pending", 1700000000),
    )
    text = build_owner_voice(db)
    assert "分享视频" in text
    assert "某视频标题" in text


def test_owner_voice_survives_broken_db() -> None:
    class _Broken:
        def query(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("坏了")

    assert build_owner_voice(_Broken()) == ""


# ---------------------------------------------------------------------------
# ★ AGENTS.md 自动发现
# ---------------------------------------------------------------------------


def test_find_agents_md_finds_something() -> None:
    """★ 全局那份才全（项目内不含网络通道/铁律）。"""
    found = find_agents_md()
    assert found is not None, "本机应该能找到 AGENTS.md"
    assert found.is_file()
    assert found.stat().st_size > 10_000, "应该是全局那份（更大更全）"


def test_find_agents_md_falls_back_to_project() -> None:
    """★ 没有全局那份时，**兜底**找项目内的。

    这条一开始我断言成"返回 None"，是错的：
    `find_agents_md` 有三级顺序（DSH_HOME → APPDATA → 项目内），
    清掉前两个后它**应该**找到项目内那份，而不是返回 None。
    """
    old_home = os.environ.pop("DSH_HOME", None)
    old_appdata = os.environ.pop("APPDATA", None)
    try:
        found = find_agents_md()
        # 本机项目内有 AGENTS.md ⇒ 应该找到它（而不是 None）
        assert found is not None
        assert "dsh-bilibili-companion" in str(found).replace("\\", "/")
    finally:
        if old_home is not None:
            os.environ["DSH_HOME"] = old_home
        if old_appdata is not None:
            os.environ["APPDATA"] = old_appdata


def test_conventions_extracts_relevant_sections(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        "# 主标题\n\n## 无关章节\n这里不该出现\n\n"
        "## 🌐 网络通道\n这是网络内容\n\n## 铁律\n这是铁律\n",
        encoding="utf-8",
    )
    text = context_pack.build_conventions(agents)
    assert "网络内容" in text
    assert "这是铁律" in text
    assert "这里不该出现" not in text


# ---------------------------------------------------------------------------
# 状态与偏好
# ---------------------------------------------------------------------------


def test_status_reads_real_data(db: Database) -> None:
    db.execute(
        "INSERT INTO videos (bvid, aid, title, up_name, source_url, first_seen_at, "
        "last_seen_at, status, discovery_keyword) VALUES (?,?,?,?,?,?,?,?,?)",
        ("BV1x", 1, "视频标题", "UP", "https://x", 1700000000, 1700000000,
         "discovered", "kw"),
    )
    text = build_status(db)
    assert "已记录的视频" in text


def test_preferences_not_truncated_to_few(db: Database) -> None:
    """★ 主人说不限量 ⇒ 偏好不该像第一版那样只取 6 条。"""
    for index in range(15):
        db.execute(
            "INSERT INTO owner_preferences (pref_id, kind, subject, content, scope, "
            "active, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"p{index}", "rule", "", f"规则{index}", "content", 1,
             1700000000 + index, 1700000000 + index),
        )
    prefs = build_preferences(db)
    assert len(prefs) == 15, f"只取了 {len(prefs)} 条，应该全部"


# ---------------------------------------------------------------------------
# 超限行为
# ---------------------------------------------------------------------------


def test_overflow_drops_blocks_not_characters() -> None:
    """★★ 超限时**整块丢弃**，不砍半句。

    砍末尾会留下"半句话"，模型看到残缺内容反而更容易胡说。
    """
    bundle = ContextBundle(
        persona="人设" * 5000,
        owner_voice="原话" * 5000,
        status="状态",
        watched=["看过1", "看过2"],
    )
    text = bundle.to_prompt(limit=2000)
    assert len(text) <= 2000
    # 低优先级的"看过"应该先被丢掉
    assert "看过1" not in text
    # 高优先级的应该还在
    assert "状态" in text


def test_full_context_length_is_substantial() -> None:
    """★ 完整上下文应该**明显大于**第一版的 655 字。"""
    bundle = ContextBundle(persona="人" * 8000, owner_voice="话" * 1000, status="状态")
    assert len(bundle.to_prompt()) > 8000


def test_describe_mentions_blocks() -> None:
    info = context_pack.describe()
    assert "人设全文" in info["blocks"]
    assert "主人原话" in info["blocks"]
