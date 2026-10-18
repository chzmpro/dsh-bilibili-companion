"""从评论区学习表达风格（R6，2026-10-18）。

## 主人要求

> 「也能根据这些评论学习更好的表达。」

## 做法（受控）

1. 只取**高分视频**下的评论（本地 evidence，不再抓网络）；
2. 让豆包提炼 3–5 条**可复用的表达技巧**（不是复述评论内容）；
3. 写进 ``owner_preferences`` 的 ``note``（subject=「表达风格（自动学习）」），
   由 ``context_pack`` 注入回复上下文。

## 边界

* **不改 `docs/persona.md`**（人设是权威，自动学习只能加"表达技巧"注记）；
* 不学攻击性/刷屏/无信息量评论（长度与字符过滤）；
* 每次学习会**停用上一条**自动风格注记，保留可追溯记录。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from core.secrets import resolve_secret
from core.storage import Database

#: 自动学习写入的偏好主题（用于停用上一版）。
STYLE_SUBJECT = "表达风格（自动学习）"

#: 分析用的评论条数上限。
MAX_COMMENTS = 20

#: 提示词（输出 JSON 数组，便于解析）。
STYLE_PROMPT = """你是中文社区表达风格分析器。
下面是 B 站技术类视频评论区里的评论（可能有噪声）。请提炼 3-5 条
**可复用的表达技巧**，帮助一个"少女感、可爱、热心"的助手写推荐语和回复。

要求：
- 每条 ≤40 字，是**技巧**（如"开头先给结论"），不是复述评论内容；
- 关注：怎么切入、怎么给具体信息、怎么避免公文腔、怎么表达不确定；
- 只输出 JSON 数组，不要 markdown、不要解释。

示例输出：
["开头先给结论，再补一句为什么", "用具体数字/命令代替形容词", "拿不准就说还没细看"]
"""


@dataclass
class StyleResult:
    ok: bool
    reason: str = ""
    points: list[str] | None = None
    pref_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "points": list(self.points or []),
            "pref_id": self.pref_id,
        }


def collect_comments(db: Database, *, limit: int = MAX_COMMENTS) -> list[str]:
    """取高分视频下的评论（本地 evidence，过滤噪声）。"""
    rows = db.query(
        """
        SELECT e.text FROM evidence e
          JOIN notes n ON n.bvid = e.bvid
         WHERE e.source_type = 'comment' AND e.availability = 'available'
           AND n.score_total >= 75
         ORDER BY e.fetched_at DESC
         LIMIT ?
        """,
        (max(1, min(int(limit) * 5, 200)),),
    )
    comments: list[str] = []
    for row in rows:
        text = " ".join(str(row["text"] or "").split())
        if 8 <= len(text) <= 300:
            comments.append(text)
        if len(comments) >= limit:
            break
    return comments


def _parse_points(content: str) -> list[str]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    points = [str(item).strip() for item in data if str(item).strip()]
    return points[:5]


async def learn_style(
    db: Database,
    config: Any,
    *,
    limit: int = MAX_COMMENTS,
    client: httpx.AsyncClient | None = None,
) -> StyleResult:
    """从评论里学表达技巧，写进偏好。失败如实返回，不抛。"""
    comments = collect_comments(db, limit=limit)
    if len(comments) < 3:
        return StyleResult(False, "COMMENTS_NOT_ENOUGH")

    key_env = str(getattr(getattr(config, "text_worker", None), "api_key_env", "") or "")
    key = resolve_secret(key_env) if key_env else None
    if not key:
        return StyleResult(False, "TEXT_WORKER_KEY_MISSING")
    base_url = str(getattr(getattr(config, "text_worker", None), "base_url", "") or "")
    model = str(getattr(getattr(config, "text_worker", None), "model", "") or "")
    if not model:
        return StyleResult(False, "TEXT_WORKER_NOT_CONFIGURED")

    owns = client is None
    http = client or httpx.AsyncClient(timeout=90, trust_env=False)
    try:
        response = await http.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": STYLE_PROMPT},
                    {
                        "role": "user",
                        "content": "\n".join(f"- {item}" for item in comments),
                    },
                ],
                "max_tokens": 800,
                "temperature": 0.2,
            },
        )
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return StyleResult(False, "TEXT_WORKER_FAILED")
    finally:
        if owns:
            await http.aclose()

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return StyleResult(False, "TEXT_WORKER_EMPTY_CONTENT")
    points = _parse_points(str(content))
    if not points:
        return StyleResult(False, "STYLE_PARSE_FAILED")

    # 停用上一版自动风格注记（保留历史，不删）
    for row in db.list_owner_preferences(active_only=True):
        if str(row["subject"] or "") == STYLE_SUBJECT:
            db.deactivate_owner_preference(str(row["pref_id"]))
    pref_id = db.add_owner_preference(
        kind="note",
        subject=STYLE_SUBJECT,
        content="；".join(points),
        scope="content",
    )
    return StyleResult(True, "OK", points, pref_id)
