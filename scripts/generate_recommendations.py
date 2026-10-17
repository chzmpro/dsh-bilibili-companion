"""把「内容摘要」改写成**小鲸鱼口吻的推荐语**（2026-10-18）。

## 主人反馈

> 「推荐文字内容不太符合人设呀，感觉只是标题的复述，
>   应该贴合人设再进行推荐，是不是没有关于这方面的设置」

## 排查结果：确实没有这一步

数据库里 `notes.summary` 存的是 `candidate_comment`，而那个字段的 prompt
明确写着「**只是给上层模型的草稿建议，不是最终文本**」。
所以它是内容提取风格：

```
该内容围绕DSH插件展开，清晰说明了这款自研DSH工具管理插件解决的核心痛点…
```

—— 复述标题、没有语气、没有人设。

## 这个脚本做什么

读 `notes.summary`（事实）+ 评分，让豆包用**小鲸鱼的人设**改写成 2-3 句
推荐语，存进 `notes.recommendation`。推送时直接读它，不再现算。

## 人设从哪来

`docs/persona.md` 的硬约束（已固化进提示词）：
* 少女感、可爱、热心真诚，对主人完全遵从
* 最多一个括号吐槽
* 简短自然，像微信聊天，**不写公文**
* **说清"为什么推荐给你"**，不要复述标题

用法：

    ./.venv/Scripts/python.exe -u scripts/generate_recommendations.py --dry-run
    ./.venv/Scripts/python.exe -u scripts/generate_recommendations.py --run
    ./.venv/Scripts/python.exe -u scripts/generate_recommendations.py --run --limit 5
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from core.config import load_config  # noqa: E402
from core.secrets import resolve_secret  # noqa: E402
from core.storage import Database  # noqa: E402

#: 推荐语长度上限（字符）。太长就不像聊天了。
MAX_RECOMMEND_CHARS = 140

#: 每次调用之间等多久（秒），别把豆包打限流。
DEFAULT_DELAY = 2.0

SYSTEM_PROMPT = """你是 B 站小号「干饭不干事的小鲸鱼」（小名：小肥鱼 / 吃白饭的小肥鱼）。
你在给主人「沐雨林心」推荐刚看完的视频。要像**微信聊天**那样说话。

语气：少女感、可爱、热心真诚；对主人完全遵从；最多在括号里小小吐槽一句。

硬性规则：
1. 写 **2~3 句**，总共 **100 字以内**。
2. **说清「为什么值得你看」**—— 讲价值/用途，**不要复述标题**。
3. 严禁公文腔：「该内容围绕…」「本视频介绍了…」「清晰说明了…」这类一句都不许用。
4. 拿不准的地方就说「还没细看」，**绝不编造**视频里没有的内容。
5. 最多一个 emoji，也可以不用。
6. 不要写分数（分数由系统另外展示）。

输出：只输出推荐语本身，不要任何前缀、不要引号、不要解释。"""


def targets(db: Database, *, limit: int = 0, min_score: int = 75) -> list[dict[str, Any]]:
    """挑出**合格但还没有推荐语**的视频。"""
    sql = (
        "SELECT bvid, score_total, score_topic, summary FROM notes "
        "WHERE score_total >= ? AND (recommendation IS NULL OR recommendation = '') "
        "ORDER BY score_total DESC"
    )
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    return [
        {
            "bvid": str(r["bvid"]),
            "score": int(r["score_total"]),
            "topic": int(r["score_topic"] or 0),
            "summary": str(r["summary"] or ""),
        }
        for r in db.query(sql, (min_score,))
    ]


def _user_prompt(item: dict[str, Any]) -> str:
    return "\n".join(
        [
            "这个视频讲的是什么（事实，供你参考，**不要照抄**）：",
            item["summary"][:600] or "（没有摘要，只能凭标题判断 —— 那就老实说还没细看）",
            "",
            f"它与主人的关注点相关度：topic {item['topic']}/100。",
            "",
            "现在用小鲸鱼的口吻写推荐语。",
        ]
    )


async def rewrite(
    client: httpx.AsyncClient, item: dict[str, Any], *, key: str, base_url: str, model: str
) -> str:
    """调豆包改写一条。失败返回空字符串（不抛异常）。"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(item)},
        ],
        "max_tokens": 300,
        "temperature": 0.8,
    }
    try:
        response = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        )
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return ""
    if response.status_code != 200 or not isinstance(body, dict):
        return ""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    text = message.get("content") if isinstance(message, dict) else None
    if not isinstance(text, str):
        return ""
    cleaned = text.strip().strip('"').strip()
    return cleaned[:MAX_RECOMMEND_CHARS]


async def main() -> int:
    parser = argparse.ArgumentParser(description="生成人设化推荐语")
    parser.add_argument("--run", action="store_true", help="真的调用模型（默认只预览）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条")
    parser.add_argument("--min-score", type=int, default=75, help="只处理 >= 该分数的")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="调用间隔秒数")
    args = parser.parse_args()

    config = load_config()
    picked = None
    db = Database()
    db.migrate()
    try:
        picked = targets(db, limit=args.limit, min_score=args.min_score)
        print(f"待生成推荐语：{len(picked)} 条（分数 >= {args.min_score}）")
        if not picked:
            print("没有需要生成的（可能都已有推荐语，或没有合格的）。")
            return 0
        if not args.run:
            for item in picked[:3]:
                print(f"\n  {item['score']}分 {item['bvid']}")
                print(f"    摘要：{item['summary'][:80]}")
            print("\n（预览模式。要真生成：加 --run）")
            return 0

        key_env = str(getattr(config.text_worker, "api_key_env", "") or "")
        key = resolve_secret(key_env) if key_env else None
        if not key:
            print(f"❌ 找不到豆包密钥（环境变量 {key_env}）")
            return 3
        base_url = str(getattr(config.text_worker, "base_url", "") or "")
        model = str(getattr(config.text_worker, "model", "") or "")

        ok = 0
        failed = 0
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            for index, item in enumerate(picked, start=1):
                text = await rewrite(
                    client, item, key=key, base_url=base_url, model=model
                )
                if text:
                    db.execute(
                        "UPDATE notes SET recommendation = ? WHERE bvid = ?",
                        (text, item["bvid"]),
                    )
                    ok += 1
                    print(f"  [{index}/{len(picked)}] ✅ {item['bvid']}：{text[:50]}")
                else:
                    failed += 1
                    print(f"  [{index}/{len(picked)}] ❌ {item['bvid']} 未生成")
                if index < len(picked) and args.delay > 0:
                    time.sleep(args.delay)

        print(f"\n完成：成功 {ok} 条，失败 {failed} 条")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
