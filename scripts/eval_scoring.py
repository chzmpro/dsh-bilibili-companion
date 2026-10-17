"""打分机制评测（2026-10-17）。

## 为什么需要它

主人反馈：*"这个打分机制我觉得有问题，为什么我感觉没什么用的能给这么高分"*。

查出来的根因是**三条叠加**：

1. 提示词里 `relevance_score` **只有一句 "0-100"，没有任何标准**；
2. 系统提示第一句还写着"**不做价值判断**"；
3. `analyze()` **连 keywords 都没传给模型** —— 它压根不知道主人想看什么。

⇒ 模型只能按"内容充实度"打分，于是"评论区扎堆的大肥鱼"（讲 B 站上一堆
AI 号发一样评论，与 DSH 插件无关）拿了 **100 分**。

## 这个脚本做什么

拿**真实库里的 20 条视频**（天然分成相关/无关两组）重新打分，输出对照表，
用来判断"新打分是否真的把该高的高、该低的低"。

**不写入数据库**（只读 + 只调模型），可以反复跑。

用法：
    ./.venv/Scripts/python.exe -u scripts/eval_scoring.py
    ./.venv/Scripts/python.exe -u scripts/eval_scoring.py --limit 8
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from core.storage import Database  # noqa: E402
from core.text_worker import (  # noqa: E402
    SCORE_WEIGHTS,
    TextWorker,
    _apply_keyword_floor,
    _clamp_score,
    _score_from_dimensions,
)

#: 人工标注：哪些**应该**高分（与"DSH 插件 / DeepSeek Harness"相关）
RELEVANT_HINTS: tuple[str, ...] = (
    "dsh",
    "deepseek harness",
    "插件",
    "agent",
)


def human_expectation(title: str) -> str:
    """粗判"这条该不该高"（**只用于对照，不参与打分**）。"""
    lowered = title.casefold()
    return "相关" if any(hint in lowered for hint in RELEVANT_HINTS) else "无关"


async def score_one(
    worker: TextWorker,
    db: Database,
    focus: dict[str, list[str]],
    *,
    bvid: str,
    title: str,
    limit: int,
) -> tuple[dict[str, int], int, int]:
    """返回 ``(分项, 加权总分, 模型原始 relevance_score)``。"""
    rows = db.query(
        "SELECT evidence_id, source_type, text FROM evidence WHERE bvid = ? LIMIT ?",
        (bvid, limit),
    )
    blocks = [
        {"evidence_id": row["evidence_id"], "source_type": row["source_type"], "text": row["text"]}
        for row in rows
    ]
    parsed, error, _ = await worker._call_model(  # noqa: SLF001 —— 评测脚本直连
        title=title, evidence_blocks=blocks, focus=focus
    )
    if parsed is None:
        raise RuntimeError(f"模型调用失败：{error}")

    raw_scores = parsed.get("scores")
    dimensions: dict[str, int] = {}
    if isinstance(raw_scores, dict):
        for name in SCORE_WEIGHTS:
            value = _clamp_score(raw_scores.get(name))
            if value is not None:
                dimensions[name] = value

    evidence_text = "\n".join(str(b["text"]) for b in blocks)
    if dimensions:
        dimensions = _apply_keyword_floor(
            dimensions, title=title, text=evidence_text, focus=focus
        )

    total = _score_from_dimensions(
        dimensions if dimensions else None, fallback=parsed.get("relevance_score")
    )
    raw = _clamp_score(parsed.get("relevance_score")) or 0
    return dimensions, total, raw


async def main() -> int:
    parser = argparse.ArgumentParser(description="打分机制评测")
    parser.add_argument("--limit", type=int, default=12, help="最多评几条")
    parser.add_argument("--evidence", type=int, default=6, help="每条用几段证据")
    args = parser.parse_args()

    cfg = load_config()
    db = Database()
    pipeline = Pipeline(db=db, config=cfg, reader=None, queue=None, worker=None)
    focus = pipeline.build_focus()
    worker = TextWorker(db=db, config=cfg)

    rows = db.query(
        """SELECT e.bvid, COUNT(*) AS ev_n, v.title
             FROM evidence e LEFT JOIN videos v ON v.bvid = e.bvid
             GROUP BY e.bvid HAVING ev_n >= 5
             ORDER BY ev_n DESC LIMIT ?""",
        (args.limit,),
    )

    print(f"权重：{SCORE_WEIGHTS}")
    print(f"关注点：{focus['keywords']}")
    print(f"样本：{len(rows)} 条，每条 {args.evidence} 段证据\n")
    print(f"{'期望':<5}{'新分':>5}{'模型原分':>9}  分项(topic/useful/novel/evid)  标题")
    print("-" * 108)

    results: list[tuple[str, int, int, dict[str, int], str]] = []
    try:
        for row in rows:
            bvid = str(row["bvid"])
            title = str(row["title"] or "")
            try:
                dimensions, total, raw = await score_one(
                    worker, db, focus, bvid=bvid, title=title, limit=args.evidence
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  跳过 {bvid}：{type(exc).__name__}: {exc}")
                continue
            expect = human_expectation(title)
            dims = (
                f"t{dimensions.get('topic', 0):>3} "
                f"u{dimensions.get('usefulness', 0):>3} "
                f"n{dimensions.get('novelty', 0):>3} "
                f"e{dimensions.get('evidence', 0):>3}"
            )
            print(f"{expect:<5}{total:>5}{raw:>9}  {dims}  {title[:40]}")
            results.append((expect, total, raw, dimensions, title))
    finally:
        await worker.aclose()
        db.close()

    # 汇总：相关组 vs 无关组的平均分（这是"打分有没有区分度"的核心指标）
    print("-" * 108)
    for label in ("相关", "无关"):
        group = [item[1] for item in results if item[0] == label]
        if group:
            avg = sum(group) / len(group)
            print(f"  {label}组：{len(group)} 条，平均 {avg:.1f} 分")
        else:
            print(f"  {label}组：0 条")

    relevant = [item[1] for item in results if item[0] == "相关"]
    irrelevant = [item[1] for item in results if item[0] == "无关"]
    if relevant and irrelevant:
        gap = sum(relevant) / len(relevant) - sum(irrelevant) / len(irrelevant)
        print(f"  ★ 区分度（相关均值 − 无关均值）：{gap:+.1f}")
        if gap <= 0:
            print("  ⚠️ 区分度不为正 —— 打分没起到筛选作用")
        elif gap < 15:
            print("  ⚠️ 区分度偏小 —— 建议继续调提示词")
        else:
            print("  ✅ 区分度足够")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
