"""手动跑一轮只读闭环（任务 7 的 ``run_once``；也是 README 演示用的入口）。

**人用工具**，不在 MCP 工具列表里。

用法::

    export PYTHONIOENCODING=utf-8
    cd D:/DeepSeek/projects/dsh-bilibili-companion
    ./.venv/Scripts/python.exe scripts/run_once.py --keywords "DeepSeek Harness" --dry-run

它做的事：发现 → 去重 → 限速读取 → 登记证据 → 豆包预处理 → 短候选包 → 日报。
**不做任何公开写入。**
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import paths  # noqa: E402
from core.config import ConfigError, load_config  # noqa: E402
from core.health import human_action_for  # noqa: E402
from core.lock import LockError, TaskLock  # noqa: E402
from core.probes import probe  # noqa: E402
from core.ratelimit import RateLimitExceeded, RequestsBlocked  # noqa: E402
from core.report import DigestInput, DigestWriter  # noqa: E402
from core.runtime import build_runtime  # noqa: E402


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


async def _append_verification(runtime: Any, digest_path: Any) -> None:
    """复核"发出去的评论公开可见没有"，把结果**追加**到今天的日报末尾。

    ★ 三条纪律（都来自竞品实测 + 本项目红线）：

    1. **只读**：只发 GET，不改任何平台状态；
    2. **查不到 ≠ 没发出去**：措辞一律「没查到」（翻页/热度排序都会漏）；
    3. **复核失败绝不能影响这一轮**：它是"附加信息"，不是主流程 ——
       所以整体包 try/except，失败只打印一行。

    没有已确认发出的评论时静默跳过（`observe` 模式下这就是常态）。
    """
    try:
        from core.verify import append_verify_to_report, verify_recent

        results, skipped, text = await verify_recent(runtime, max_items=6)
        if not results:
            # 没有可复核的记录 —— 静默（observe 模式下这是常态，不必刷屏）
            if skipped:
                print(f"（评论复核：{skipped} 条最近已确认过，没重复查）")
            return
        visible = sum(1 for item in results if item.visible is True)
        print(f"评论复核：{len(results)} 条 → 公开可见 {visible} 条，其余「没查到」")
        if append_verify_to_report(digest_path, text):
            print(f"（已追加到日报：{digest_path}）")
    except Exception as exc:  # noqa: BLE001 —— 附加信息，失败不拖累主流程
        print(f"（评论复核跳过：{type(exc).__name__}: {exc}）", file=sys.stderr)


async def run(args: argparse.Namespace) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    keywords = args.keywords.split(",") if args.keywords else None
    runtime = await build_runtime()
    db = runtime.db
    log_file = paths.data_dir() / "run.log"

    def ensure_stub(reason: str, exit_code: int | None = None) -> None:
        """**保底日报**：这一轮没写出正式日报时，至少留下"发生了什么"。

        为什么（竞品精读 §2.4 + 任务书"不能悄悄什么都没写"）：
        ``out/`` 空着 = 使用者完全看不到这一轮发生了什么，也不知道白烧了钱。
        真日报优先：已有当天日报时**不覆盖**（``write_stub`` 内部判断）。
        """
        try:
            DigestWriter(db=db, out_dir=paths.data_dir() / "digests").write_stub(
                reason=reason,
                exit_code=exit_code,
                log_tail=DigestWriter.log_tail(log_file=log_file),
            )
        except Exception as exc:  # noqa: BLE001 —— 保底动作自己绝不能把主流程带崩
            print(f"（写占位日报也失败了：{exc}）", file=sys.stderr)

    lock = TaskLock(purpose="run_once")
    try:
        with lock:
            run_id = db.start_run("manual")
            print(f"run_id = {run_id}")
            print(f"数据目录 = {paths.data_dir()}（来源 {paths.data_dir_source()}）")

            # 先做只读探测，给出可读的失败原因（不烧 AI token）
            report = await probe(
                reader=runtime.reader,
                config=runtime.config,
                worker=runtime.worker,
            )
            print("\n=== 只读探测 ===")
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
            if report.blocking_reason:
                print(f"\n⛔ 已停机：{report.blocking_reason}", file=sys.stderr)
                db.finish_run(run_id, errors_n=1, conclusion=f"BLOCKED:{report.blocking_reason}")
                ensure_stub(f"只读探测发现停机：{report.blocking_reason}", 3)
                return 3

            if not config.text_worker.is_configured():
                print(
                    "\n⚠️ 未配置 text_worker.model → 跳过批处理。"
                    "这是**刻意**的：任务书禁止在豆包不可用时把原文灌给 DSH。",
                    file=sys.stderr,
                )
                db.finish_run(run_id, conclusion="TEXT_WORKER_NOT_CONFIGURED")
                ensure_stub("未配置豆包 text_worker.model → 拒绝批处理（刻意设计）", 4)
                return 4

            try:
                batch = await runtime.pipeline.prepare_batch(keywords=keywords)
            except RequestsBlocked as exc:
                print(f"\n⛔ 平台状态阻断：{exc}\n人工动作：{exc.human_action}", file=sys.stderr)
                db.finish_run(run_id, errors_n=1, conclusion=f"BLOCKED:{exc.state}")
                ensure_stub(f"平台状态阻断：{exc.state}", 3)
                return 3
            except RateLimitExceeded as exc:
                print(f"\n⛔ 连续限流停机：{exc}", file=sys.stderr)
                db.finish_run(run_id, errors_n=1, conclusion="RATE_LIMIT_EXCEEDED")
                ensure_stub(f"连续限流停机：{exc}", 3)
                return 3


            payload = batch.to_payload(max_batch_chars=config.text_worker.max_batch_chars)
            print("\n=== 短候选包（原文留在 bridge 内）===")
            print(json.dumps(payload, ensure_ascii=False, indent=2))

            recommended = []
            for card in batch.candidates:
                evidence = db.evidence_for_bvid(card.bvid)
                recommended.append(
                    {
                        "bvid": card.bvid,
                        "title": card.title,
                        "source_url": card.source_url,
                        "evidence_level": card.evidence_level,
                        "score": card.relevance_score,
                        "evidence_ids": [str(row["evidence_id"]) for row in evidence],
                        "key_points": [
                            {"claim": p.claim, "evidence_id": p.evidence_id}
                            for p in card.key_points
                        ],
                    }
                )

            writer = DigestWriter(db=db, out_dir=paths.data_dir() / "digests")
            digest_path = writer.write(
                DigestInput(
                    run_id=run_id,
                    trigger_source="manual",
                    keywords=keywords or config.discovery.keywords,
                    candidates_n=len(batch.candidates),
                    # ★ 读的是**视频条数**，不是证据条数 —— 两者混用会误导主人
                    read_n=len(batch.read_bvids),
                    recommended=recommended,
                    errors=[f"{f['keyword_or_bvid']}:{f['reason']}" for f in batch.failed],
                    pending=[f"{s['bvid']}:{s['reason']}" for s in batch.skipped],
                    cache_stats=batch.cache_stats,
                    health_state=db.health_state(),
                    health_note=(
                        human_action_for(db.health_state())
                        if db.health_state() != "ready"
                        else ""
                    ),
                    simulated=True,
                    next_steps=[
                        "由人审阅日报与草稿",
                        "如需真实发布：填 UID → 切 review/auto → 关 dry_run "
                        "→ publish_enabled=true（并承担平台风险）",
                    ],
                )
            )
            db.finish_run(
                run_id,
                candidates_n=len(batch.candidates),
                read_n=len(batch.evidence_ids),
                errors_n=len(batch.failed),
                conclusion="ok" if batch.candidates else "no_candidates",
            )
            print(f"\n✅ 日报已写入：{digest_path}")

            # ★ 评论可见性复核（P1，2026-10-16）—— **只读**，跑在日报之后。
            #   动因：接口回"发送成功" ≠ 公开可见（新号/风控会让评论进审核，
            #   那时连 @ 通知都不推）⇒ 必须**回头核一次**，否则"以为发了、其实没人看到"。
            #   ⚠️ 查不到只写「没查到」，**绝不写「没发出去」**（观测不到 ≠ 不存在）。
            await _append_verification(runtime, digest_path)

            print("⚠️ 本轮**未**做任何公开写入（observe + dry_run）。")
            return 0
    except LockError as exc:
        print(f"⛔ 已有任务在运行：{exc}", file=sys.stderr)
        return 5
    except Exception as exc:  # noqa: BLE001 —— 兜底：**任何**未预期异常都要留痕
        # ★ 这是"保底产物"的最后一道：崩了也要让主人知道发生了什么。
        #   不 re-raise 成 traceback 之外的东西 —— 退出码仍是非 0，调用方能看出失败。
        print(f"\n⛔ 本轮异常终止：{type(exc).__name__}: {exc}", file=sys.stderr)
        ensure_stub(f"未预期异常：{type(exc).__name__}: {exc}", 1)
        return 1
    finally:
        await runtime.aclose()


def main() -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="跑一轮只读闭环（不发布）")
    parser.add_argument("--keywords", default="", help="逗号分隔；省略则用配置里的关键词")
    parser.add_argument("--dry-run", action="store_true", help="显式声明只读（默认即只读）")
    args = parser.parse_args()
    _ = args.dry_run
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
