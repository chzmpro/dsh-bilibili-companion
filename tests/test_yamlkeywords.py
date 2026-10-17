"""``config.yaml`` 关键词写入测试（2026-10-17）。

对应"候选词要**人审**后才生效"的最后一环：采纳时把词写进
``discovery.keywords``。

核心不变量（都在这里钉死）：

* ★ **只动 `discovery.keywords`** —— 授权字段（`mode` / `dry_run` /
  `publish_enabled`）**一字不动**；
* **注释不能丢**（逐行替换，不是 YAML 解析再写回）；
* **去重**（大小写不敏感）；
* 写之前**先备份**；
* 找不到那段时给人话错误，不是 traceback。

**绝不碰真配置**：全部在 tmp 里造副本。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import yamlkeywords

SAMPLE = """\
# 顶部注释：解释为什么这么配
account:
  agent_mid: 1001
  owner_mid: 2002

discovery:
  keywords:
    - "甲"
    - "乙"
  published_within_days: 2 # curator 原值

interaction:
  mode: "observe" # observe | review | auto
  dry_run: true # 模拟与真实隔离
  publish_enabled: false
"""


@pytest.fixture
def fake_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把模块的路径指向 tmp 里的副本（**绝不碰真配置**）。"""
    target = tmp_path / "config.yaml"
    target.write_text(SAMPLE, encoding="utf-8")
    monkeypatch.setattr(yamlkeywords, "config_yaml_path", lambda: target)
    return target


def test_read_keywords(fake_config: Path) -> None:
    assert yamlkeywords.read_keywords() == ["甲", "乙"]


def test_add_keyword_appends(fake_config: Path) -> None:
    ok, message = yamlkeywords.add_keyword("丙")
    assert ok is True
    assert "丙" in message or "写进" in message
    assert yamlkeywords.read_keywords() == ["甲", "乙", "丙"]


def test_add_keyword_is_case_insensitive_dedup(fake_config: Path) -> None:
    """★ 去重（大小写不敏感）—— 重复采纳不该加两遍。"""
    ok, message = yamlkeywords.add_keyword("甲")
    assert ok is True
    assert "已经在" in message
    assert yamlkeywords.read_keywords() == ["甲", "乙"]

    ok2, _ = yamlkeywords.add_keyword("DSH插件")
    assert ok2 is True
    ok3, message3 = yamlkeywords.add_keyword("dsh插件")
    assert ok3 is True
    assert "已经在" in message3, "大小写不同也该判为重复"
    assert yamlkeywords.read_keywords().count("DSH插件") == 1


def test_add_keyword_empty_rejected(fake_config: Path) -> None:
    ok, message = yamlkeywords.add_keyword("   ")
    assert ok is False
    assert "不能为空" in message


def test_add_keyword_missing_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """找不到那段 → **人话错误**，不是 traceback。"""
    target = tmp_path / "config.yaml"
    target.write_text("account:\n  agent_mid: 1\n", encoding="utf-8")
    monkeypatch.setattr(yamlkeywords, "config_yaml_path", lambda: target)

    ok, message = yamlkeywords.add_keyword("甲")
    assert ok is False
    assert "找不到" in message


def test_add_keyword_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(yamlkeywords, "config_yaml_path", lambda: tmp_path / "nope.yaml")
    ok, message = yamlkeywords.add_keyword("甲")
    assert ok is False
    assert "找不到" in message


# ---------------------------------------------------------------------------
# ★★ 最重要的：授权字段与注释
# ---------------------------------------------------------------------------


def test_authorization_fields_untouched(fake_config: Path) -> None:
    """★★ 核心红线：授权字段**一字不动**。

    这是"人审采纳关键词"与"改授权"之间的边界 ——
    采纳一个关键词**绝不能**顺带把 `publish_enabled` 打开。
    """
    before = fake_config.read_text(encoding="utf-8")
    yamlkeywords.add_keyword("丙")
    after = fake_config.read_text(encoding="utf-8")

    def auth_lines(text: str) -> list[str]:
        return [
            line.strip()
            for line in text.split("\n")
            if line.strip().startswith(("mode:", "dry_run:", "publish_enabled:"))
        ]

    assert auth_lines(before) == auth_lines(after)
    assert 'mode: "observe"' in after
    assert "dry_run: true" in after
    assert "publish_enabled: false" in after


def test_comments_preserved(fake_config: Path) -> None:
    """★ 注释必须保留（逐行替换，不是 YAML 解析再写回）。"""
    before = fake_config.read_text(encoding="utf-8")
    yamlkeywords.add_keyword("丙")
    after = fake_config.read_text(encoding="utf-8")

    def comments(text: str) -> list[str]:
        return [line for line in text.split("\n") if line.strip().startswith("#")]

    assert comments(before) == comments(after)
    assert "顶部注释：解释为什么这么配" in after
    assert "curator 原值" in after, "行尾注释也要在"


def test_backup_created(fake_config: Path) -> None:
    """写之前先备份（出问题能手工对照）。"""
    yamlkeywords.add_keyword("丙")
    backup = fake_config.with_suffix(fake_config.suffix + ".bak-keywords")
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == SAMPLE


def test_other_sections_untouched(fake_config: Path) -> None:
    """除 keywords 之外，其它内容逐行不变。"""
    before = fake_config.read_text(encoding="utf-8").split("\n")
    yamlkeywords.add_keyword("丙")
    after = fake_config.read_text(encoding="utf-8").split("\n")

    # 只应多出一行
    assert len(after) == len(before) + 1
    # 去掉新增那行后，其余完全一致
    inserted = [line for line in after if line not in before]
    assert len(inserted) == 1
    assert "丙" in inserted[0]
    remaining = list(after)
    remaining.remove(inserted[0])
    assert remaining == before


def test_indent_matches_existing(fake_config: Path) -> None:
    """新加的行要**沿用已有缩进**（不能破坏 YAML 结构）。"""
    yamlkeywords.add_keyword("丙")
    text = fake_config.read_text(encoding="utf-8")
    assert '    - "丙"' in text, f"缩进应与既有一致：\n{text}"


def test_read_keywords_handles_no_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``keywords:`` 存在但没有列表项 → 返回空列表（不崩）。"""
    target = tmp_path / "config.yaml"
    target.write_text("discovery:\n  keywords:\n  other: 1\n", encoding="utf-8")
    monkeypatch.setattr(yamlkeywords, "config_yaml_path", lambda: target)
    assert yamlkeywords.read_keywords() == []
    ok, _ = yamlkeywords.add_keyword("甲")
    assert ok is True
    assert yamlkeywords.read_keywords() == ["甲"]
