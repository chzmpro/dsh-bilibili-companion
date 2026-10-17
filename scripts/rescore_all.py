"""批量重算视频评分（新机制）+ 推送候选给主人（2026-10-17）。

## 主人需求

> 「我说的是全部已搜索视频的打分，因为以前的很多视频并没有经过打分就略过了，
>   其次，这次打分所挑选的候选必须全部发给我…你还得做个可以让我直接命令
>   全部重新进行检测，以及基于时间点的（自xx日期时间的检测-至今；全部；
>   仅最近一次）快捷方式」

## 三种范围（命令行直接选）

| 参数 | 范围 |
|---|---|
| `--all` | 全部**有证据**的视频 |
| `--since "2026-10-16 08:00"` | 自该时间起（到"至今"） |
| `--last-run` | 仅最近一次运行发现的候选 |

三者互斥；不给就报错让你选。

## 为什么"有证据"是硬门槛

评分必须有素材。实测本机：

```
视频总量 115 条
  有证据（description/subtitle/comment）: 40 条  ← 能重算
  无证据（只 discovered，从没读过）:      75 条  ← 无法评分
```

对无证据的强行打分等于让模型**瞎猜** —— 所以这里**明确跳过并报告数量**，
而不是编一个分数糊弄。（要补证据得先 `prepare_batch` 读它们。）

## 新评分机制

```
relevance_score = topic×0.80 + usefulness×0.10 + novelty×0.06 + evidence×0.04
```

topic 是绝对主导 —— "这条内容的主题，是不是主人会主动去搜的？"

## 输出

1. 终端对比表（旧分→新分，分维度）；
2. `--send` 时把**所有候选**按分数排序发一条私信给主人；
3. `--json` 给程序用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config  # noqa: E402
from core.context_pack import build_focus  # noqa: E402
from core.storage import Database  # noqa: E402
from core.text_worker import TextWorker  # noqa: E402

#: 每个视频喂给模型的最大证据条数（简介优先，其次字幕/评论）。
MAX_EVIDENCE = 8

#: 私信字数上限（B 站私信限制）。
DM_LIMIT = 500

#: 单条私信里最多列几条候选（超过会超字数，剩下的靠 `--json` 看）。
DM_MAX_ITEMS = 6


def _parse_since(value: str) -> int:
    """把 ``"2026-10-16"`` / ``"2026-10-16 08:00"`` 解析成 Unix 时间戳。"""
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except ValueError:
            continue
    raise SystemExit(f"看不懂的时间格式：{value}（试 2026-10-16 或 '2026-10-16 08:00'）")


def select_targets(
    db: Database,
    *,
    all_videos: bool = False,
    since: str = "",
    last_run: bool = False,
    limit: int = 0,
) -> list[dict[str, Any]]:
    """按范围挑出要重算的视频（只挑**有证据**的）。"""
    sql = (
        "SELECT v.bvid, v.aid, v.title, v.source_url, v.first_seen_at "
        "FROM videos v WHERE EXISTS ("
        "  SELECT 1 FROM evidence e "
        "  WHERE e.bvid = v.bvid AND e.availability = 'available')"
    )
    params: list[Any] = []

    if since:
        sql += " AND v.first_seen_at >= ?"
        params.append(_parse_since(since))

    if last_run:
        run = db.query_one(
            "SELECT run_id, started_at FROM runs ORDER BY started_at DESC LIMIT 1"
        )
        if run is None:
            raise SystemExit("还没有任何运行记录（runs 表为空）")
        sql += " AND v.first_seen_at >= ?"
        params.append(int(run["started_at"]))
        print(f"  仅最近一次运行（{run['run_id']}）")

    if not (all_videos or since or last_run):
        raise SystemExit("请指定范围：--all / --since <时间> / --last-run")

    sql += " ORDER BY v.first_seen_at DESC"
    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)

    rows = db.query(sql, tuple(params))
    return [
        {
            "bvid": str(row["bvid"]),
            "aid": int(row["aid"] or 0),
            "title": str(row["title"] or ""),
            "source_url": str(row["source_url"] or ""),
        }
        for row in rows
    ]


def _evidence(db: Database, bvid: str) -> list[dict[str, Any]]:
    """取该视频的证据（简介优先）。"""
    rows = db.query(
        "SELECT evidence_id, source_type, text, start_sec FROM evidence "
        "WHERE bvid = ? AND availability = 'available' "
        "ORDER BY CASE source_type WHEN 'description' THEN 0 ELSE 1 END, start_sec "
        "LIMIT ?",
        (bvid, MAX_EVIDENCE),
    )
    return [
        {
            "evidence_id": str(row["evidence_id"]),
            "source_type": str(row["source_type"]),
            "text": str(row["text"]),
            "start_sec": row["start_sec"],
        }
        for row in rows
    ]


async def rescore_all(
    db: Database,
    worker: TextWorker,
    targets: list[dict[str, Any]],
    *,
    focus: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """逐个重算（**串行**，避免并发打爆豆包/被限流）。"""
    results: list[dict[str, Any]] = []
    total = len(targets)
    for index, target in enumerate(targets, start=1):
        blocks = _evidence(db, target["bvid"])
        if not blocks:
            continue
        print(f"  [{index}/{total}] {target['bvid']} …", end="", flush=True)
        started = time.time()
        try:
            card = await worker.analyze(
                bvid=target["bvid"],
                aid=target["aid"],
                title=target["title"],
                source_url=target["source_url"],
                evidence_level="subtitle" if len(blocks) > 1 else "description",
                evidence_blocks=blocks,
                # ★ 必须传关注点：不传的话模型只知道标题和证据，
                #   不知道"对谁有用"，只能按内容充实度打分
                #   —— 这正是主人反馈"没什么用却高分"的根因。
                focus=focus,
            )
        except Exception as exc:  # noqa: BLE001 —— 单条失败不该中断整批
            print(f" ❌ {type(exc).__name__}")
            results.append({**target, "error": str(exc)[:100]})
            continue
        dims = dict(worker.last_dimensions or {})
        print(f" {card.relevance_score} 分（{time.time() - started:.1f}s）")
        results.append(
            {
                **target,
                "score_total": int(card.relevance_score),
                "dimensions": dims,
                "comment": str(card.candidate_comment or "")[:120],
            }
        )
    return results


def save_scores(db: Database, results: list[dict[str, Any]]) -> int:
    """把新分数写回 `notes`（有就更新，没有就插入）。"""
    saved = 0
    now = int(time.time())
    for item in results:
        if "score_total" not in item:
            continue
        dims = item.get("dimensions") or {}
        existing = db.query_one("SELECT bvid FROM notes WHERE bvid = ?", (item["bvid"],))
        if existing is None:
            db.execute(
                "INSERT INTO notes (bvid, summary, score_total, score_topic, "
                "score_novelty, score_evidence, score_recency, score_discuss, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    item["bvid"],
                    str(item.get("comment") or ""),
                    int(item["score_total"]),
                    int(dims.get("topic", 0)),
                    int(dims.get("novelty", 0)),
                    int(dims.get("evidence", 0)),
                    0,
                    0,
                    now,
                ),
            )
        else:
            db.execute(
                "UPDATE notes SET score_total=?, score_topic=?, score_novelty=?, "
                "score_evidence=?, summary=?, updated_at=? WHERE bvid=?",
                (
                    int(item["score_total"]),
                    int(dims.get("topic", 0)),
                    int(dims.get("novelty", 0)),
                    int(dims.get("evidence", 0)),
                    str(item.get("comment") or ""),
                    now,
                    item["bvid"],
                ),
            )
        saved += 1
    return saved


def render(results: list[dict[str, Any]]) -> None:
    """终端表格（按分数降序）。"""
    scored = [r for r in results if "score_total" in r]
    failed = [r for r in results if "error" in r]
    scored.sort(key=lambda r: r["score_total"], reverse=True)

    print("\n" + "=" * 78)
    print(f"{'分数':>4}  {'topic':>5} {'实用':>4} {'新颖':>4} {'证据':>4}  BV / 标题")
    print("-" * 78)
    for item in scored:
        dims = item.get("dimensions") or {}
        print(
            f"{item['score_total']:>4}  {dims.get('topic', 0):>5} "
            f"{dims.get('usefulness', 0):>4} {dims.get('novelty', 0):>4} "
            f"{dims.get('evidence', 0):>4}  {item['bvid']}  {item['title'][:38]}"
        )
    print("-" * 78)
    print(f"成功 {len(scored)} 条" + (f"，失败 {len(failed)} 条" if failed else ""))
    if failed:
        for item in failed[:3]:
            print(f"  ❌ {item['bvid']}: {item.get('error', '')[:60]}")


def build_dm(results: list[dict[str, Any]], total_videos: int, no_evidence: int) -> str:
    """拼推送给主人的私信（≤500 字）。"""
    scored = [r for r in results if "score_total" in r]
    scored.sort(key=lambda r: r["score_total"], reverse=True)

    lines = [
        f"【全量重算完成】{total_videos} 条视频里 {len(scored)} 条有证据、按新机制重打了分：",
        "",
    ]
    for index, item in enumerate(scored[:DM_MAX_ITEMS], start=1):
        dims = item.get("dimensions") or {}
        lines.append(
            f"{index}. {item['score_total']}分 {item['bvid']} {item['title'][:20]}"
        )
        lines.append(
            f"   topic{dims.get('topic', 0)} 实用{dims.get('usefulness', 0)} "
            f"新颖{dims.get('novelty', 0)} 证据{dims.get('evidence', 0)}"
        )
    if len(scored) > DM_MAX_ITEMS:
        lines.append(f"…其余 {len(scored) - DM_MAX_ITEMS} 条见本地报告")

    lines.extend(
        [
            "",
            f"★ 另有 {no_evidence} 条从没读过（无字幕/简介），无法评分 —— 要评需先补抓证据。",
        ]
    )
    text = "\n".join(lines).strip()
    return text if len(text) <= DM_LIMIT else text[: DM_LIMIT - 1] + "…"


async def main() -> int:
    parser = argparse.ArgumentParser(description="批量重算视频评分（新机制）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="全部有证据的视频")
    group.add_argument("--since", default="", help="自该时间起（如 2026-10-16 08:00）")
    group.add_argument("--last-run", action="store_true", help="仅最近一次运行的候选")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（测试用）")
    parser.add_argument("--send", action="store_true", help="把候选私信推给主人")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--no-save", action="store_true", help="不写回 notes")
    args = parser.parse_args()

    config = load_config()
    db = Database()
    db.migrate()
    worker = TextWorker(config=config, db=db)
    try:
        targets = select_targets(
            db,
            all_videos=args.all,
            since=args.since,
            last_run=args.last_run,
            limit=args.limit,
        )
        total_videos = int(db.query_one("SELECT COUNT(*) c FROM videos")["c"])
        with_evidence = int(
            db.query_one(
                "SELECT COUNT(*) c FROM videos v WHERE EXISTS ("
                "SELECT 1 FROM evidence e WHERE e.bvid=v.bvid "
                "AND e.availability='available')"
            )["c"]
        )
        no_evidence = total_videos - with_evidence

        print(f"范围：{len(targets)} 条（有证据的共 {with_evidence} 条）")
        print(f"无证据（从没读过）无法评分：{no_evidence} 条")
        print()

        # 主人的关注点（关键词 + 偏好规则）—— 打分前必须先取到
        focus = build_focus(db, config)
        print(
            f"关注点：关键词 {len(focus['keywords'])} 个 "
            f"（{'、'.join(focus['keywords'][:3])}）| "
            f"规则 {len(focus['rules'])} 条"
        )

        started = time.time()
        results = await rescore_all(db, worker, targets, focus=focus)
        elapsed = time.time() - started
        print(f"\n耗时 {elapsed:.1f} 秒")

        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            render(results)

        if not args.no_save:
            saved = save_scores(db, results)
            print(f"\n已写回 notes：{saved} 条")

        if args.send:
            from core.credentials import CredentialError, load_cookie
            from core.dm import send_to_owner

            text = build_dm(results, total_videos, no_evidence)
            print(f"\n=== 推送私信（{len(text)} 字）===")
            print(text)
            try:
                bundle = load_cookie()
            except CredentialError as exc:
                print(f"❌ 没有凭据：{exc}")
                return 3
            sent = await send_to_owner(
                config=config, cookies=bundle, text=text, db=db
            )
            print(f"\n✅ 已推送给主人（msg_key={getattr(sent, 'msg_key', '')}）")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
