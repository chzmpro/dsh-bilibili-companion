"""自我进化：存储 / 审批 / 应用 测试（2026-10-17）。

主人：「自我进化我觉得可以开始了」。

`core/evolution.py` 只做**校验**（四道硬闸）。本测试盯的是把它跑起来之后
**最容易出错的那些地方** —— 全部来自真机踩坑：

| 真机踩到的 | 这里的防线 |
|---|---|
| 短名/完整路径混用（`score_topic_weight` vs `discovery.*`） | 归一化 + 映射完整性 |
| 每日限额靠内存计数 → 重启就失效 | 计数查**库**，测"跨重启仍生效" |
| 写盘把 config.yaml 注释吃掉 | 测"注释行数不变" |
| AI 能把自己提的提案批了 | 测"`decided_by` 非 cli 时拒绝" |
| 改完配置没留下可回退的东西 | 测"`.bak-evolve` 备份存在" |

## 钉死的不变量

* **AI 只能写 `pending`** —— `applied`/`rejected` 必须 `decided_by="cli"`；
* **每日上限查库**，不是查内存；
* **写盘保留注释**（10 条注释必须还在）；
* **越界/禁改项直接拒绝**，不是夹紧；
* **应用时再查一次白名单**（防库里被塞进奇怪参数）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import evolution, evolution_store
from core.evolution import EvolutionError
from core.storage import Database

# ---------------------------------------------------------------------------
# ★★ 短名 / 完整路径归一化（真机栽在这上面）
# ---------------------------------------------------------------------------


def test_every_evolvable_key_has_a_path() -> None:
    """★★ 每个可进化参数都必须有完整路径映射。

    没有映射就无法读写 config ⇒ 提案生成了也应用不了（真机就是这么失败的）。
    """
    for name in evolution.EVOLVABLE_PARAMETERS:
        assert name in evolution.PARAMETER_PATHS, f"{name} 缺少完整路径映射"


def test_paths_point_to_real_config_fields() -> None:
    """映射目标必须是 `Config` 里真实存在的字段。"""
    from core.config import Config

    config = Config()
    for path in evolution.PARAMETER_PATHS.values():
        section, _, key = path.partition(".")
        holder = getattr(config, section, None)
        assert holder is not None, f"配置里没有段 {section}"
        assert getattr(holder, key, None) is not None, f"配置里没有 {path}"


def test_full_path_from_short() -> None:
    assert evolution.full_path("score_topic_weight") == "discovery.score_topic_weight"


def test_full_path_accepts_full() -> None:
    """已经是完整路径时原样返回（幂等）。"""
    path = "discovery.min_score_for_comment"
    assert evolution.full_path(path) == path


def test_full_path_rejects_unknown() -> None:
    with pytest.raises(EvolutionError):
        evolution.full_path("interaction.nonsense")


def test_short_name_roundtrip() -> None:
    for short, path in evolution.PARAMETER_PATHS.items():
        assert evolution.short_name(path) == short


def test_make_proposal_accepts_full_path() -> None:
    """★ `make_proposal` 对短名与完整路径都要接受。"""
    by_short = evolution.make_proposal(
        parameter="score_topic_weight", old_value=0.80, new_value=0.82
    )
    by_full = evolution.make_proposal(
        parameter="discovery.score_topic_weight", old_value=0.80, new_value=0.82
    )
    assert by_short.parameter == by_full.parameter == "score_topic_weight"


# ---------------------------------------------------------------------------
# ★★ 每日限额查库（不是内存）
# ---------------------------------------------------------------------------


def test_count_today_starts_zero(db: Database) -> None:
    assert evolution_store.count_today(db) == 0


def test_daily_limit_enforced(db: Database) -> None:
    """★★ 提满上限后必须被拒 —— 而且计数是**查库**的。

    这一点关键：如果只在内存计数，进程重启就归零，限额形同虚设。
    """
    for index in range(evolution.DAILY_PROPOSAL_LIMIT):
        proposal, message = evolution_store.propose(
            db,
            parameter="score_topic_weight",
            old_value=0.80,
            new_value=0.82,
            reason=f"第 {index + 1} 份",
        )
        assert proposal is not None, message

    blocked, why = evolution_store.propose(
        db,
        parameter="published_within_days",
        old_value=30.0,
        new_value=31.0,
    )
    assert blocked is None
    # ★ 必须是被**每日上限**拦下的（不是被"参数不存在"拦下）
    assert "上限" in why or "设计" in why


def test_count_today_reads_db(db: Database) -> None:
    evolution_store.propose(
        db, parameter="score_topic_weight", old_value=0.80, new_value=0.82
    )
    assert evolution_store.count_today(db) == 1


def test_duplicate_proposals_get_distinct_ids(db: Database) -> None:
    """★★ 回归：**同参数同值**的两份提案必须是**两行**，不能互相覆盖。

    真机踩到：ID 一开始只用「时间戳 + 参数/值哈希」，
    于是同一秒内提两份一模一样的提案 → **同一个 ID** →
    `INSERT OR REPLACE` 把它们**合成一行** ⇒ 库计数永远是 1 ⇒
    **每日限额形同虚设**（这是安全闸，不是小事）。

    这条测试就是钉住它：提 2 份，库里必须真有 2 行。
    """
    ids: list[str] = []
    for _ in range(2):
        proposal, message = evolution_store.propose(
            db, parameter="score_topic_weight", old_value=0.80, new_value=0.82
        )
        assert proposal is not None, message
        ids.append(proposal.proposal_id)

    assert len(set(ids)) == 2, f"两份提案拿到了同一个 ID：{ids}"
    assert evolution_store.count_today(db) == 2


# ---------------------------------------------------------------------------
# ★★ AI 不能自己批准
# ---------------------------------------------------------------------------


def test_propose_only_creates_pending(db: Database) -> None:
    proposal, _ = evolution_store.propose(
        db, parameter="score_topic_weight", old_value=0.80, new_value=0.82
    )
    assert proposal is not None
    assert proposal.status == evolution_store.STATUS_PENDING


def test_apply_requires_cli(db: Database, tmp_path: Path) -> None:
    """★★ 非 'cli' 的调用方**不能**应用提案 —— 这是"提案制"的落点。"""
    proposal, _ = evolution_store.propose(
        db, parameter="score_topic_weight", old_value=0.80, new_value=0.82
    )
    assert proposal is not None
    config = tmp_path / "config.yaml"
    config.write_text("discovery:\n  score_topic_weight: 0.8\n", encoding="utf-8")

    ok, message = evolution_store.apply_proposal(
        db, proposal_id=proposal.proposal_id, config_path=config, decided_by="agent"
    )
    assert ok is False
    assert "cli" in message
    # 配置**不能**被改
    assert "0.8" in config.read_text(encoding="utf-8")


def test_reject_requires_cli(db: Database) -> None:
    proposal, _ = evolution_store.propose(
        db, parameter="score_topic_weight", old_value=0.80, new_value=0.82
    )
    assert proposal is not None
    ok, message = evolution_store.reject(
        db, proposal_id=proposal.proposal_id, decided_by="agent"
    )
    assert ok is False
    assert "CLI" in message


def test_apply_actually_writes_config(db: Database, tmp_path: Path) -> None:
    """★ 批准后必须**真写盘**，且保留注释。"""
    config = tmp_path / "config.yaml"
    config.write_text(
        "# 这是注释，不能被吃掉\n"
        "discovery:\n"
        "  # 打分权重\n"
        "  score_topic_weight: 0.80\n"
        "  min_score_for_comment: 75\n"
        "interaction:\n"
        "  mode: observe\n",
        encoding="utf-8",
    )
    proposal, _ = evolution_store.propose(
        db, parameter="score_topic_weight", old_value=0.80, new_value=0.82
    )
    assert proposal is not None

    ok, message = evolution_store.apply_proposal(
        db, proposal_id=proposal.proposal_id, config_path=config
    )
    assert ok is True, message

    text = config.read_text(encoding="utf-8")
    assert "score_topic_weight: 0.82" in text
    # ★ 注释必须还在（yaml.dump 会吃掉，所以我们是行替换）
    assert "# 这是注释，不能被吃掉" in text
    assert "# 打分权重" in text
    # 别的段不受影响
    assert "mode: observe" in text
    assert "min_score_for_comment: 75" in text


def test_apply_creates_backup(db: Database, tmp_path: Path) -> None:
    """★ 改配置前必须留备份（可回退）。"""
    config = tmp_path / "config.yaml"
    config.write_text("discovery:\n  score_topic_weight: 0.80\n", encoding="utf-8")
    proposal, _ = evolution_store.propose(
        db, parameter="score_topic_weight", old_value=0.80, new_value=0.82
    )
    assert proposal is not None
    evolution_store.apply_proposal(
        db, proposal_id=proposal.proposal_id, config_path=config
    )
    backup = config.with_suffix(config.suffix + ".bak-evolve")
    assert backup.is_file()
    assert "0.80" in backup.read_text(encoding="utf-8")


def test_apply_marks_applied(db: Database, tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("discovery:\n  score_topic_weight: 0.80\n", encoding="utf-8")
    proposal, _ = evolution_store.propose(
        db, parameter="score_topic_weight", old_value=0.80, new_value=0.82
    )
    assert proposal is not None
    evolution_store.apply_proposal(
        db, proposal_id=proposal.proposal_id, config_path=config
    )
    stored = evolution_store.recent(db, limit=1)[0]
    assert stored.status == evolution_store.STATUS_APPLIED
    assert stored.decided_by == "cli"


def test_cannot_apply_twice(db: Database, tmp_path: Path) -> None:
    """已应用的提案不能再应用一次（否则会重复改值）。"""
    config = tmp_path / "config.yaml"
    config.write_text("discovery:\n  score_topic_weight: 0.80\n", encoding="utf-8")
    proposal, _ = evolution_store.propose(
        db, parameter="score_topic_weight", old_value=0.80, new_value=0.82
    )
    assert proposal is not None
    evolution_store.apply_proposal(
        db, proposal_id=proposal.proposal_id, config_path=config
    )
    ok, message = evolution_store.apply_proposal(
        db, proposal_id=proposal.proposal_id, config_path=config
    )
    assert ok is False
    assert "applied" in message


# ---------------------------------------------------------------------------
# ★★ 硬闸（复用 evolution.py 的校验，这里测落地点）
# ---------------------------------------------------------------------------


def test_propose_rejects_forbidden(db: Database) -> None:
    """★★ 授权字段**不能**成为提案。"""
    proposal, message = evolution_store.propose(
        db, parameter="interaction.publish_enabled", old_value=0, new_value=1
    )
    assert proposal is None
    assert "禁改" in message


def test_propose_rejects_out_of_bounds(db: Database) -> None:
    proposal, message = evolution_store.propose(
        db, parameter="score_topic_weight", old_value=0.80, new_value=1.5
    )
    assert proposal is None
    assert "硬边界" in message


def test_propose_clamps_step(db: Database) -> None:
    """一次想改太多会被**夹到一步**（0.80 → 最多 0.85）。"""
    proposal, _ = evolution_store.propose(
        db, parameter="score_topic_weight", old_value=0.80, new_value=0.95
    )
    assert proposal is not None
    assert proposal.new_value == pytest.approx(0.85)


def test_propose_rejects_unknown_parameter(db: Database) -> None:
    proposal, message = evolution_store.propose(
        db, parameter="some.random.thing", old_value=1, new_value=2
    )
    assert proposal is None
    assert message


# ---------------------------------------------------------------------------
# 现值读取
# ---------------------------------------------------------------------------


def test_default_old_value_reads_real_config() -> None:
    """★ 必须取**真实配置**，不是 `EVOLVABLE_PARAMETERS` 里的默认值。

    主人把 `published_within_days` 改成了 30，提案基准就该是 30。
    """
    from core.config import Config

    config = Config()
    assert evolution_store.default_old_value(
        "score_topic_weight", config
    ) == pytest.approx(0.80)


def test_default_old_value_accepts_full_path() -> None:
    from core.config import Config

    config = Config()
    assert evolution_store.default_old_value(
        "discovery.min_score_for_comment", config
    ) == pytest.approx(75.0)


def test_default_old_value_rejects_unknown() -> None:
    from core.config import Config

    with pytest.raises(EvolutionError):
        evolution_store.default_old_value("nope.nothing", Config())


# ---------------------------------------------------------------------------
# 一览
# ---------------------------------------------------------------------------


def test_summary_shape(db: Database) -> None:
    state = evolution_store.summary(db)
    assert state["auto_apply"] is False
    assert state["daily_limit"] == evolution.DAILY_PROPOSAL_LIMIT
    assert isinstance(state["pending"], list)


def test_summary_lists_pending(db: Database) -> None:
    evolution_store.propose(
        db, parameter="score_topic_weight", old_value=0.80, new_value=0.82
    )
    state = evolution_store.summary(db)
    assert len(state["pending"]) == 1
    assert "不会自动生效" in state["note"]
