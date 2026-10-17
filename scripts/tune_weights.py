"""权重敏感性分析（2026-10-17）。

## 为什么需要它

调打分时最容易犯的错是**凭感觉改权重** —— 改完看一两条觉得"好像合理了"
就收工。但权重是**全局参数**，它同时影响所有视频，
一两条的观感不能说明问题。

这个脚本用**同一批真实分项分数**（跑一次模型、缓存下来），
然后扫不同权重组合，看哪个的**区分度**最好：

    区分度 = 相关组平均分 − 无关组平均分

★ 关键：**只调权重、不重跑模型** —— 这样才能公平比较
（重跑模型会引入随机性，把"权重的作用"和"模型的抖动"混在一起）。

用法：
    # ① 先采样（会调模型，约 5 分钟，结果存成 json）
    ./.venv/Scripts/python.exe -u scripts/tune_weights.py --sample
    # ② 再扫权重（纯计算，秒出）
    ./.venv/Scripts/python.exe -u scripts/tune_weights.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
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
)

#: 采样结果存这里（纯数据，方便反复调权重）
SAMPLE_PATH = Path("D:/DeepSeek/workspace-temp/scoring-samples.json")

#: 人工标注：哪些**应该**高分。
#:
#: ⚠️ 只按"标题里有没有 dsh / deepseek harness"判断，**不要**把
#: "插件" 当关键词 —— 实测踩到：`让 QQ 机器人开口说话…语音插件` 与
#: `WorkBuddy vs Codex…Harness` 都会被"插件/harness"误判成相关，
#: 于是把**标注错误**混进了"区分度"这个指标里（看起来像打分不准，其实是标错）。
RELEVANT_HINTS: tuple[str, ...] = ("dsh", "deepseek harness")


def human_expectation(title: str) -> str:
    lowered = title.casefold()
    return "relevant" if any(hint in lowered for hint in RELEVANT_HINTS) else "irrelevant"


async def sample(limit: int, evidence: int) -> int:
    """跑一次模型，把分项分数存下来（**只读，不写库**）。"""
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
        (limit,),
    )

    samples: list[dict[str, object]] = []
    try:
        for row in rows:
            bvid = str(row["bvid"])
            title = str(row["title"] or "")
            blocks_rows = db.query(
                "SELECT evidence_id, source_type, text FROM evidence WHERE bvid = ? LIMIT ?",
                (bvid, evidence),
            )
            blocks = [
                {
                    "evidence_id": r["evidence_id"],
                    "source_type": r["source_type"],
                    "text": r["text"],
                }
                for r in blocks_rows
            ]
            parsed, error, _ = await worker._call_model(  # noqa: SLF001
                title=title, evidence_blocks=blocks, focus=focus
            )
            if parsed is None:
                print(f"  跳过 {bvid}：{error}")
                continue

            raw_scores = parsed.get("scores")
            dims: dict[str, int] = {}
            if isinstance(raw_scores, dict):
                for name in SCORE_WEIGHTS:
                    value = _clamp_score(raw_scores.get(name))
                    if value is not None:
                        dims[name] = value

            evidence_text = "\n".join(str(b["text"]) for b in blocks)
            if dims:
                dims = _apply_keyword_floor(
                    dims, title=title, text=evidence_text, focus=focus
                )

            samples.append(
                {
                    "bvid": bvid,
                    "title": title,
                    "expect": human_expectation(title),
                    "dims": dims,
                    "raw": _clamp_score(parsed.get("relevance_score")) or 0,
                }
            )
            print(f"  {samples[-1]['expect']:<10} {title[:40]}  {dims}")
    finally:
        await worker.aclose()
        db.close()

    SAMPLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SAMPLE_PATH.write_text(json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已存 {len(samples)} 条样本到 {SAMPLE_PATH}")
    return 0


def separation(samples: list[dict[str, object]], weights: dict[str, float]) -> tuple[float, float]:
    """算区分度：返回 ``(相关均值, 无关均值)``。"""
    rel: list[int] = []
    irr: list[int] = []
    for item in samples:
        dims = item.get("dims") or {}
        if not isinstance(dims, dict) or not dims:
            continue
        total = 0.0
        for name, weight in weights.items():
            total += int(dims.get(name, 0)) * weight
        score = max(0, min(100, int(round(total))))
        if item.get("expect") == "relevant":
            rel.append(score)
        else:
            irr.append(score)
    rel_avg = sum(rel) / len(rel) if rel else 0.0
    irr_avg = sum(irr) / len(irr) if irr else 0.0
    return rel_avg, irr_avg


def tune() -> int:
    if not SAMPLE_PATH.exists():
        print(f"没有样本：{SAMPLE_PATH}\n先跑 --sample")
        return 1
    samples = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    print(f"样本 {len(samples)} 条\n")

    current = dict(SCORE_WEIGHTS)
    rel, irr = separation(samples, current)
    print(f"当前权重 {current}")
    print(f"  相关均值 {rel:.1f} | 无关均值 {irr:.1f} | 区分度 {rel - irr:+.1f}\n")

    # 扫：topic 占比从 0.4 到 0.8，其余按原比例分配
    results: list[tuple[float, dict[str, float], float, float]] = []
    for topic_w in (0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80):
        rest = 1.0 - topic_w
        others = {k: v for k, v in current.items() if k != "topic"}
        other_sum = sum(others.values()) or 1.0
        weights = {"topic": topic_w}
        weights.update({k: rest * v / other_sum for k, v in others.items()})
        rel, irr = separation(samples, weights)
        results.append((rel - irr, weights, rel, irr))

    print("topic 占比 → 区分度")
    for gap, weights, rel, irr in sorted(results, key=lambda x: -x[0]):
        mark = "  ← 当前" if abs(weights["topic"] - current["topic"]) < 1e-9 else ""
        print(
            f"  topic={weights['topic']:.2f}  相关{rel:5.1f} 无关{irr:5.1f} "
            f"区分度{gap:+6.1f}{mark}"
        )

    best = max(results, key=lambda x: x[0])
    print(f"\n★ 最佳 topic 占比：{best[1]['topic']:.2f}（区分度 {best[0]:+.1f}）")

    # 再用最佳权重看"该留的留、该挡的挡"（阈值 75）
    print("\n阈值 75 下，各条判定：")
    weights = best[1]
    for item in samples:
        dims = item.get("dims") or {}
        if not isinstance(dims, dict) or not dims:
            continue
        total = sum(int(dims.get(k, 0)) * w for k, w in weights.items())
        score = max(0, min(100, int(round(total))))
        keep = "留" if score >= 75 else "挡"
        matched = (keep == "留") == (item.get("expect") == "relevant")
        flag = "" if matched else "  ⚠️ 不符"
        expect = str(item.get("expect"))
        title = str(item.get("title"))[:36]
        print(f"  {keep} {score:>3}  {expect:<10} {title}{flag}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="权重敏感性分析")
    parser.add_argument("--sample", action="store_true", help="先采样（会调模型）")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--evidence", type=int, default=30)
    args = parser.parse_args()
    if args.sample:
        return asyncio.run(sample(args.limit, args.evidence))
    return tune()


if __name__ == "__main__":
    raise SystemExit(main())
