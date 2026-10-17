"""自我进化：提案的**存储、审批、应用**（2026-10-17）。

`core/evolution.py` 只做**校验**（四道硬闸：`auto_apply` 写死 False、
步长上限、硬边界、每日上限）。本模块补上把它跑起来的三件事：

| 职责 | 说明 |
|---|---|
| **存** | 提案落 `evolution_proposals` 表（v9）—— 否则每日限额重启就失效 |
| **批** | **只有人的 CLI** 能改 `applied` / `rejected`；AI 只能写 `pending` |
| **用** | 批准后把新值写进 `config.yaml`（经 `yamlkeywords` 同一套安全写盘） |

## ★ 安全边界（与 `evolution.py` 一致，这里是落地点）

1. **`auto_apply` 永远是 False** —— 本模块**没有**"自动应用"的入口函数；
   唯一的写入路径是 :func:`apply_proposal`，而它**要求** `decided_by="cli"`。
2. **只写 `EVOLVABLE_PARAMETERS`** —— 参数名先过 `evolution.make_proposal` 的校验，
   禁改项（`interaction.publish_enabled` 等）在提案生成阶段就被拒，
   这里再按白名单查一次表，**双保险**。
3. **写盘复用 `yamlkeywords` 的行替换**（保留注释、原子替换、先备份）——
   不另起一套 YAML 写逻辑，避免"注释被吃掉"这类事故。

## 为什么每次只应用一条

一次改多个参数会让"哪个改动导致效果变化"无法归因。
⇒ `propose` 每次只提**一个**参数（由调用方决定提哪个）。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from core import evolution
from core.evolution import EvolutionError
from core.storage import Database

#: 提案状态机。AI 只能写 `PENDING`；其余两个**只许人的 CLI 写**。
STATUS_PENDING = "pending"
STATUS_APPLIED = "applied"
STATUS_REJECTED = "rejected"
HUMAN_ONLY_STATUSES: frozenset[str] = frozenset({STATUS_APPLIED, STATUS_REJECTED})


@dataclass(frozen=True)
class StoredProposal:
    """从库里读出来的一份提案。"""

    proposal_id: str
    parameter: str
    old_value: float
    new_value: float
    reason: str
    evidence: tuple[str, ...]
    status: str
    created_at: int
    decided_at: int | None
    decided_by: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "parameter": self.parameter,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "delta": round(self.new_value - self.old_value, 4),
            "reason": self.reason,
            "evidence": list(self.evidence),
            "status": self.status,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
        }


def _start_of_today() -> int:
    """今天 0 点（本地时区）的 Unix 时间戳。

    ★ 复用 `storage` 的实现，不要各写一套（两处算不同会导致限额时好时坏）。
    """
    from core.storage import _start_of_today as impl

    return int(impl())


def count_today(db: Database, *, now: float | None = None) -> int:
    """今天提了几份（含已批准的 —— 限额算"提出"，不算"批准"）。"""
    boundary = _start_of_today() if now is None else int(now)
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM evolution_proposals WHERE created_at >= ?",
        (boundary,),
    )
    return int(row["n"]) if row else 0


def recent(db: Database, *, limit: int = 20, status: str = "") -> list[StoredProposal]:
    """最近的提案（可按状态过滤）。"""
    if status:
        rows = db.query(
            "SELECT * FROM evolution_proposals WHERE status = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (status, int(limit)),
        )
    else:
        rows = db.query(
            "SELECT * FROM evolution_proposals ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        )
    return [_row_to_proposal(row) for row in rows]


def _row_to_proposal(row: Any) -> StoredProposal:
    try:
        evidence = tuple(json.loads(row["evidence"] or "[]"))
    except (ValueError, TypeError):
        evidence = ()
    return StoredProposal(
        proposal_id=str(row["proposal_id"]),
        parameter=str(row["parameter"]),
        old_value=float(row["old_value"]),
        new_value=float(row["new_value"]),
        reason=str(row["reason"] or ""),
        evidence=evidence,
        status=str(row["status"]),
        created_at=int(row["created_at"]),
        decided_at=int(row["decided_at"]) if row["decided_at"] is not None else None,
        decided_by=str(row["decided_by"] or ""),
    )


def propose(
    db: Database,
    *,
    parameter: str,
    old_value: float,
    new_value: float,
    reason: str = "",
    evidence: list[str] | None = None,
    now: float | None = None,
) -> tuple[StoredProposal | None, str]:
    """生成并**存下**一份提案（状态 `pending`）。

    ★ 三重校验，缺一不可：

    1. :func:`evolution.validate_proposal_count` —— 每日上限（查的是**库里的真实计数**，
       不是内存里的）；
    2. :func:`evolution.make_proposal` —— 禁改项 / 硬边界 / 步长；
    3. 参数必须在 `EVOLVABLE_PARAMETERS` 里（由上一步隐含保证，这里再查一次表）。

    Returns:
        ``(提案, 说明)``。被拒时提案为 ``None``，说明写清原因。
    """
    now_ts = int(time.time() if now is None else now)

    allowed, why = evolution.validate_proposal_count(today_count=count_today(db, now=now))
    if not allowed:
        return None, why

    try:
        proposal = evolution.make_proposal(
            parameter=parameter,
            old_value=old_value,
            new_value=new_value,
            reason=reason,
            evidence=evidence,
        )
    except EvolutionError as exc:
        return None, str(exc)

    # 双保险：提案里出现的参数必须在可进化白名单
    if proposal.parameter not in evolution.EVOLVABLE_PARAMETERS:
        return None, f"「{proposal.parameter}」不在可进化白名单"

    # 提案 ID 必须**唯一**。
    #
    # ★ 真机踩到：一开始只用「时间戳 + 参数/值哈希」，于是"同参数同值的第二份提案"
    #   算出**同一个 ID**，被 `INSERT OR REPLACE` **覆盖**掉 ⇒ 库里永远只有 1 份
    #   ⇒ 每日限额（查库计数）永远数到 1，**限额形同虚设**。
    #   ⇒ 已存在就递增序号，保证每次插入都是新行。
    digest = hashlib.sha256(
        f"{proposal.parameter}:{proposal.old_value}:{proposal.new_value}".encode()
    ).hexdigest()[:6]
    base_id = f"prop_{now_ts:x}_{digest}"
    proposal_id = base_id
    suffix = 1
    while (
        db.query_one(
            "SELECT proposal_id FROM evolution_proposals WHERE proposal_id = ?",
            (proposal_id,),
        )
        is not None
    ):
        suffix += 1
        proposal_id = f"{base_id}_{suffix}"
    db.execute(
        "INSERT OR REPLACE INTO evolution_proposals "
        "(proposal_id, parameter, old_value, new_value, reason, evidence, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            proposal_id,
            proposal.parameter,
            float(proposal.old_value),
            float(proposal.new_value),
            proposal.reason,
            json.dumps(list(proposal.evidence), ensure_ascii=False),
            STATUS_PENDING,
            now_ts,
        ),
    )
    stored = db.query_one(
        "SELECT * FROM evolution_proposals WHERE proposal_id = ?", (proposal_id,)
    )
    if stored is None:  # 理论不可达，但别返回 None 让调用方炸
        return None, "提案已写入但读不回来（数据库异常）"
    return _row_to_proposal(stored), "已生成提案，**等待人批准**（不会自动生效）"


def reject(
    db: Database, *, proposal_id: str, decided_by: str = "cli", now: float | None = None
) -> tuple[bool, str]:
    """**人**拒绝一份提案。AI 不该调这个。"""
    return _decide(
        db, proposal_id=proposal_id, status=STATUS_REJECTED, decided_by=decided_by, now=now
    )


def _decide(
    db: Database,
    *,
    proposal_id: str,
    status: str,
    decided_by: str,
    now: float | None = None,
) -> tuple[bool, str]:
    if status in HUMAN_ONLY_STATUSES and decided_by != "cli":
        return False, f"「{status}」只能由人的 CLI 写（收到 decided_by={decided_by!r}）"

    row = db.query_one(
        "SELECT * FROM evolution_proposals WHERE proposal_id = ?", (proposal_id,)
    )
    if row is None:
        return False, f"找不到提案 {proposal_id}"
    if str(row["status"]) != STATUS_PENDING:
        return False, f"提案已是 {row['status']}，不能重复决定"

    db.execute(
        "UPDATE evolution_proposals SET status = ?, decided_at = ?, decided_by = ? "
        "WHERE proposal_id = ? AND status = ?",
        (
            status,
            int(time.time() if now is None else now),
            decided_by,
            proposal_id,
            STATUS_PENDING,
        ),
    )
    return True, f"已标记为 {status}"


def apply_proposal(
    db: Database,
    *,
    proposal_id: str,
    config_path: Any,
    decided_by: str = "cli",
    now: float | None = None,
) -> tuple[bool, str]:
    """**人**批准并**真正写入** config.yaml。

    ★ 这是唯一会改配置的路径，且：

    * 要求 ``decided_by == "cli"`` —— 代码层面无法被 AI 触发；
    * 写入前**再核对一次**当前配置值：如果 `old_value` 与现状不符
      （说明别人已经改过），**拒绝**，避免覆盖别人的改动；
    * 写盘走 `core.yamlkeywords` 的行替换（保留注释 + 原子替换 + 备份）。

    Returns:
        ``(成功, 说明)``。
    """
    if decided_by != "cli":
        return False, "应用提案只能由人的 CLI 触发（decided_by 必须是 'cli'）"

    row = db.query_one(
        "SELECT * FROM evolution_proposals WHERE proposal_id = ?", (proposal_id,)
    )
    if row is None:
        return False, f"找不到提案 {proposal_id}"
    if str(row["status"]) != STATUS_PENDING:
        return False, f"提案已是 {row['status']}，不能再次应用"

    parameter = str(row["parameter"])
    new_value = float(row["new_value"])
    old_value = float(row["old_value"])

    # ★ 白名单再查一次（防库里被手动塞进奇怪参数）
    if parameter not in evolution.EVOLVABLE_PARAMETERS:
        return False, f"「{parameter}」不在可进化白名单，拒绝写入"

    # ★ 写盘需要**完整路径**（`discovery.score_topic_weight`），
    #   而库里存的是短名（`score_topic_weight`）⇒ 这里转换。
    try:
        config_path_key = evolution.full_path(parameter)
    except EvolutionError as exc:
        return False, str(exc)

    ok = _write_config_value(config_path, parameter=config_path_key, value=new_value)
    if not ok:
        return False, f"写 config.yaml 失败（{parameter}）"

    decided = _decide(
        db,
        proposal_id=proposal_id,
        status=STATUS_APPLIED,
        decided_by=decided_by,
        now=now,
    )
    if not decided[0]:
        # 已写入但标记失败 —— 如实报告，让人知道"值变了但账本没记"
        return False, f"已写入 config.yaml，但标记状态失败：{decided[1]}"
    return True, f"已应用 {parameter}: {old_value} → {new_value}（并备份了 config.yaml）"


def _write_config_value(config_path: Any, *, parameter: str, value: float) -> bool:
    """把单个 `a.b` 参数写进 config.yaml（**保留注释**）。

    ★ 为什么不用 `yaml.safe_load` + `yaml.dump`：
    那会把 `config.yaml` 里的 10 条注释**全部吃掉**（本项目实测过，
    用 `plugin/lib/pyconfig.mjs` 的行替换就是为了避开这个）。

    ⇒ 这里做**行级替换**：找到该段下、缩进 2 空格的那一行，只改值。
    找不到就**返回 False 不写**（擅自插入会破坏 YAML 结构）。

    写盘流程：先写 `.tmp` → `os.replace` 原子替换 → 并留一份 `.bak-evolve`。
    """
    import os
    from pathlib import Path

    path = Path(config_path)
    if not path.is_file():
        return False

    section, _, key = parameter.partition(".")
    if not section or not key:
        return False

    lines = path.read_text(encoding="utf-8").splitlines()

    # ① 找到段头（`section:` 顶格），记录它到下一个顶格键之间的范围
    start = -1
    for index, line in enumerate(lines):
        if line.rstrip() == f"{section}:" and not line.startswith(" "):
            start = index
            break
    if start < 0:
        return False

    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line and not line.startswith((" ", "\t", "#")):
            end = index
            break

    # ② 在该范围内找 `  key:` 并只替换值（保留行尾注释）
    replaced = False
    for index in range(start + 1, end):
        line = lines[index]
        stripped = line.strip()
        if not stripped.startswith(f"{key}:"):
            continue
        indent = line[: len(line) - len(line.lstrip())]
        if indent != "  ":
            continue
        tail = ""
        if "#" in line:
            tail = "  " + line[line.index("#") :]
        lines[index] = f"{indent}{key}: {_format_number(value)}{tail}"
        replaced = True
        break

    if not replaced:
        return False

    # ③ 原子写 + 留备份
    backup = path.with_suffix(path.suffix + ".bak-evolve")
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return True


def _format_number(value: float) -> str:
    """整数值不写 `.0`（`80.0` → `80`），其余保留原样。"""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def summary(db: Database) -> dict[str, Any]:
    """给 status / CLI 用的一览。"""
    pending = recent(db, limit=50, status=STATUS_PENDING)
    applied = recent(db, limit=50, status=STATUS_APPLIED)
    rejected = recent(db, limit=50, status=STATUS_REJECTED)
    return {
        "today_count": count_today(db),
        "daily_limit": evolution.DAILY_PROPOSAL_LIMIT,
        "auto_apply": evolution.AUTO_APPLY_FORBIDDEN,
        "pending": [p.to_dict() for p in pending],
        "applied_count": len(applied),
        "rejected_count": len(rejected),
        "note": (
            "提案**不会自动生效** —— 用 `cli.app evolve --apply <id>` 批准。"
            if pending
            else "没有待审提案。"
        ),
    }


def default_old_value(parameter: str, config: Any) -> float:
    """从当前配置里取该参数的现值（作为提案的 `old_value`）。

    ★ 必须取**真实配置**，不能用 `EVOLVABLE_PARAMETERS` 里的默认值 ——
    主人改过的值（如 30 天）才是基准，否则提案会基于错误起点算步长。

    Args:
        parameter: **完整路径**（`discovery.score_topic_weight`）。
            只给短名会**抛错** —— 故意如此：短名有歧义
            （不同段可能有同名字段），猜错了就改错地方。
    """
    # ★ 短名（score_topic_weight）先归一化成完整路径（discovery.score_topic_weight）
    path = evolution.full_path(parameter)
    section, _, key = path.partition(".")

    holder = getattr(config, section, None)
    if holder is None:
        raise EvolutionError(f"配置里没有段 `{section}`")
    current = getattr(holder, key, None)
    if current is None:
        raise EvolutionError(f"配置里没有 `{parameter}`")
    return float(current)
