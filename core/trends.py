"""推荐流 → **候选关键词**（搜索方式 ②，2026-10-17）。

## 定位：它只是"词库扩展器"，**不接管内容判断**

主人原话：*"第二种是根据 B 站搜索推荐机制学习人类一样达成刷视频的功能"*。
但推荐流是**个性化算法** —— 实测一屏 8 条里，与我们兴趣相关的只有 2~3 条，
其余是鬼屋、原神、影视飓风这类**高互动内容**。

⇒ 如果让它直接决定"看什么、评什么"，**小号的兴趣画像就被算法带跑了**，
与项目"按主人给的关键词、只看最近 48 小时"的**可控姿态相反**。

因此本模块的设计是：

```
推荐流（只读、限额）→ 提取**候选关键词** → 进候选池（不自动生效）
                                          ↓
                              主人用 CLI 过一眼 → 接受后才进 config
                                          ↓
                                    回到关键词搜索的可控轨道
```

**关键取舍：候选词不自动写进 `config.yaml`。**
理由是"谁能改我的关注点"必须由人决定 —— 与项目既有的
「授权类改动只能由人做」是同一条原则。

## 三条硬约束（都在代码里）

1. **只读**：只发 GET，不点赞/不评论/不关注；
2. **限额**：一次最多拉 ``max_items`` 条，一天最多 ``max_rounds_per_day`` 轮；
3. **可审计**：每个候选词都记下**它从哪条推荐来**（bvid + 标题），可回溯。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from core.storage import Database

#: 推荐流接口（**需要 WBI 签名**；实测返回顶层就是 ``item`` 列表）
RCMD_PATH = "/x/web-interface/wbi/index/top/feed/rcmd"

#: 一次最多看几条推荐（**刻意小** —— 它只用来"发现新词"）
DEFAULT_MAX_ITEMS = 8
#: 一天最多跑几轮（避免"刷视频"式高频访问）
DEFAULT_MAX_ROUNDS_PER_DAY = 2

#: 候选关键词的长度范围
MIN_KEYWORD_CHARS = 2
MAX_KEYWORD_CHARS = 20

#: 停用词：这些词出现在标题里也**不是**有信息量的搜索词
STOPWORDS: frozenset[str] = frozenset(
    {
        # 通用高频词
        "视频", "一个", "什么", "怎么", "如何", "为什么", "我们", "他们", "自己",
        "可以", "这个", "那个", "就是", "已经", "真的", "不是", "没有", "这么",
        "第一", "第二", "最后", "今天", "明天", "现在", "以后", "之前",
        # 平台/内容形态
        "哔哩", "bilibili", "官方", "合集", "全集", "更新", "预告", "直播", "切片",
        "教程", "解说", "实况", "记录", "分享", "推荐", "测评", "评测", "盘点",
        # 口语/情绪
        "哈哈", "笑死", "绝了", "牛逼", "厉害", "离谱", "震惊", "泪目",
        # 我们自己的词（已在 config 里，不算"新发现"）
        "dsh插件", "deepseek harness", "dsh 插件",
    }
)

#: 纯数字/纯符号不算词
_PURE_NON_WORD = re.compile(r"^[\W\d_]+$", re.UNICODE)
#: 英文/数字片段（这类天然有分隔，直接切）
_LATIN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]{1,}")
#: 连续中文片段（**不能整段当词** —— 中文没有空格，整段会是"我们发现了这里的秘密"）
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")

#: 中文 n-gram 的长度范围（实测：2~4 字最像"能搜的词"）
CJK_MIN_GRAM = 2
CJK_MAX_GRAM = 4
#: 中文片段超过这个长度就只取 n-gram，不整段保留
CJK_WHOLE_MAX = 6


def _cjk_candidates(run: str) -> list[str]:
    """从一个连续中文片段里切出候选词（**n-gram，不是整段**）。

    ★ 为什么必须这样（2026-10-17 实测踩到）：
    中文没有空格，若把连续的汉字整段当一个"词"，
    "让百万人尖叫的鬼屋" 会变成一整个候选 —— 那**根本没法拿去搜索**。

    做法：短片段（≤ :data:`CJK_WHOLE_MAX` 字）整段保留 + 所有 2~4 字 n-gram；
    长片段只出 n-gram。
    """
    out: list[str] = []
    length = len(run)
    if length <= CJK_WHOLE_MAX:
        out.append(run)
    for size in range(CJK_MIN_GRAM, min(CJK_MAX_GRAM, length) + 1):
        for start in range(0, length - size + 1):
            out.append(run[start : start + size])
    return out


def _tokens_in(title: str) -> list[str]:
    """从标题里切出**候选词**（英文片段 + 中文 n-gram）。"""
    out: list[str] = []
    out.extend(_LATIN_TOKEN.findall(title))
    for run in _CJK_RUN.findall(title):
        out.extend(_cjk_candidates(run))
    return out


@dataclass(frozen=True)
class RecommendationItem:
    """一条推荐（**只留我们需要的字段**）。"""

    bvid: str
    title: str
    up_name: str
    view: int
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "bvid": self.bvid,
            "title": self.title,
            "up_name": self.up_name,
            "view": self.view,
            "reason": self.reason,
        }


@dataclass
class Suggestion:
    """一个候选关键词（**带来源，可审计**）。"""

    keyword: str
    #: 在几条推荐里出现过（≥2 说明是个"话题"而不是偶然词）
    hits: int = 1
    #: 来源推荐（最多记 3 条，避免无限膨胀）
    sources: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"keyword": self.keyword, "hits": self.hits, "sources": self.sources}


def _normalize(text: str) -> str:
    return " ".join(str(text).split())


def extract_candidates(
    items: list[RecommendationItem],
    *,
    known_keywords: list[str] | None = None,
    limit: int = 10,
    min_hits: int = 1,
) -> list[Suggestion]:
    """从推荐里提取**候选关键词**（**确定性算法，不调模型**）。

    做法（简单、可测、不花钱）：

    1. 标题切成候选词：**英文片段直接切**，**中文按 2~4 字 n-gram 切**
       （中文没空格，整段当词会得到"让百万人尖叫的鬼屋"这种没法搜的东西）；
    2. 丢掉停用词、纯数字符号、以及**已经在我们关键词里**的（那不是"新发现"）；
    3. **按出现次数排序** —— 在 ≥2 条标题里重复出现的词更可能是"话题"；
    4. 最多返回 ``limit`` 个。

    ★ ``min_hits`` 是**降噪的关键旋钮**（2026-10-17 实测）：

    n-gram 天然会产出跨词边界的碎片（"需安装一"、"无需登录注"）。
    这些碎片几乎**不会在两条不同视频里同时出现**，而真话题（`deepseek`、`AI`、
    `插件`）会。所以：

    * ``min_hits=1`` —— 全都要（信息全，但噪音多）；
    * **``min_hits=2`` —— 只留"重复出现"的**（推荐值，噪音几乎为零）。

    Args:
        items: 推荐条目。
        known_keywords: 我们已有的关键词（会被排除，大小写不敏感）。
        limit: 最多几个候选。
        min_hits: 至少在几条推荐里出现过才保留。

    Returns:
        按 ``hits`` 降序、同分按长度降序排列的候选列表。
    """
    known = {_normalize(k).lower() for k in (known_keywords or []) if k.strip()}
    counts: dict[str, int] = {}
    #: 保留原始大小写（展示用）
    display: dict[str, str] = {}
    sources: dict[str, list[dict[str, Any]]] = {}

    for item in items:
        seen_in_this_item: set[str] = set()
        for token in _tokens_in(_normalize(item.title)):
            candidate = token.strip()
            if not candidate:
                continue
            if len(candidate) < MIN_KEYWORD_CHARS or len(candidate) > MAX_KEYWORD_CHARS:
                continue
            if _PURE_NON_WORD.match(candidate):
                continue
            lowered = candidate.lower()
            if lowered in STOPWORDS or lowered in known:
                continue
            if lowered in seen_in_this_item:
                continue  # 同一条标题里重复出现只算一次
            seen_in_this_item.add(lowered)

            counts[lowered] = counts.get(lowered, 0) + 1
            display.setdefault(lowered, candidate)
            bucket = sources.setdefault(lowered, [])
            if len(bucket) < 3:
                bucket.append(
                    {"bvid": item.bvid, "title": item.title[:60], "up_name": item.up_name}
                )

    # 排序：出现次数降序 → 长度降序（长词更有信息量）→ 原顺序
    order = [k for k, n in counts.items() if n >= max(1, min_hits)]
    order.sort(key=lambda k: (-counts[k], -len(k)))

    out: list[Suggestion] = []
    for lowered in order[:limit]:
        out.append(
            Suggestion(
                keyword=display[lowered],
                hits=counts[lowered],
                sources=sources.get(lowered, []),
            )
        )
    return out


def parse_feed(payload: Any) -> list[RecommendationItem]:
    """把推荐流响应解析成 :class:`RecommendationItem` 列表。

    ★ 实测（2026-10-17）：``BilibiliReader._get_signed`` 返回的**已是解包后的 data**
    —— 顶层直接就是 ``item`` 列表，**没有** ``code`` / ``data`` 外壳。
    """
    if not isinstance(payload, dict):
        return []
    raw_items = payload.get("item")
    if not isinstance(raw_items, list):
        return []

    out: list[RecommendationItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        bvid = str(raw.get("bvid") or "")
        if not bvid:
            continue
        owner_raw = raw.get("owner")
        owner: dict[str, Any] = owner_raw if isinstance(owner_raw, dict) else {}
        stat_raw = raw.get("stat")
        stat: dict[str, Any] = stat_raw if isinstance(stat_raw, dict) else {}
        reason_raw = raw.get("rcmd_reason")
        reason = ""
        if isinstance(reason_raw, dict):
            reason = str(reason_raw.get("content") or "")
        out.append(
            RecommendationItem(
                bvid=bvid,
                title=_normalize(str(raw.get("title") or "")),
                up_name=_normalize(str(owner.get("name") or "")),
                view=int(stat.get("view") or 0),
                reason=reason,
            )
        )
    return out


# ---------------------------------------------------------------------------
# 候选池（落库，等人审）
# ---------------------------------------------------------------------------


def record_suggestions(
    db: Database,
    suggestions: list[Suggestion],
    *,
    source: str = "rcmd",
) -> int:
    """把候选词写进候选池，返回**新增**条数（已存在的只累加 hits）。

    候选池**不影响**任何行为 —— 只有主人用 ``cli.app keywords --accept`` 接受后，
    才会写进 ``config.yaml``。
    """
    added = 0
    for item in suggestions:
        existing = db.query_one(
            "SELECT keyword, hits FROM keyword_suggestions WHERE keyword = ?",
            (item.keyword,),
        )
        if existing is None:
            db.execute(
                "INSERT INTO keyword_suggestions "
                "(keyword, source, hits, sources_json, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    item.keyword,
                    source,
                    item.hits,
                    _json_dumps(item.sources),
                    _now(),
                    _now(),
                ),
            )
            added += 1
        else:
            db.execute(
                "UPDATE keyword_suggestions SET hits = hits + ?, updated_at = ? "
                "WHERE keyword = ?",
                (item.hits, _now(), item.keyword),
            )
    return added


def list_suggestions(db: Database, *, limit: int = 30) -> list[dict[str, Any]]:
    """列出候选池（按 hits 降序）。"""
    rows = db.query(
        "SELECT keyword, source, hits, sources_json, created_at, updated_at "
        "FROM keyword_suggestions ORDER BY hits DESC, updated_at DESC LIMIT ?",
        (limit,),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "keyword": str(row["keyword"]),
                "source": str(row["source"]),
                "hits": int(row["hits"]),
                "sources": _json_loads(str(row["sources_json"] or "[]")),
                "created_at": int(row["created_at"]),
                "updated_at": int(row["updated_at"]),
            }
        )
    return out


def drop_suggestion(db: Database, keyword: str) -> bool:
    """从候选池删掉一个词（主人明确不要它）。"""
    before = db.query_one(
        "SELECT keyword FROM keyword_suggestions WHERE keyword = ?", (keyword,)
    )
    if before is None:
        return False
    db.execute("DELETE FROM keyword_suggestions WHERE keyword = ?", (keyword,))
    return True


def suggestion_summary(db: Database) -> dict[str, int]:
    """候选池概况（给 ``companion_status`` 用）。"""
    row = db.query_one("SELECT COUNT(*) AS n FROM keyword_suggestions")
    total = int(row["n"]) if row is not None else 0
    return {"total": total}


# ---------------------------------------------------------------------------
# 关键词采纳账本（2026-10-19，C 项自动回填的护栏与审计）
# ---------------------------------------------------------------------------


def record_adoption(
    db: Database,
    *,
    keyword: str,
    run_id: str = "",
    source: str = "rcmd",
    state: str = "active",
    note: str = "",
) -> None:
    """记一条"这个词被采纳 / 被跳过"（**不删历史**，同词覆盖状态）。"""
    text = (keyword or "").strip()
    if not text:
        return
    if state not in ("active", "skipped", "reverted"):
        raise ValueError(f"非法采纳状态：{state}")
    db.execute(
        """
        INSERT INTO keyword_adoptions (keyword, adopted_at, source, run_id, state, note)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(keyword) DO UPDATE SET
            adopted_at = excluded.adopted_at,
            source     = excluded.source,
            run_id     = excluded.run_id,
            state      = excluded.state,
            note       = excluded.note
        """,
        (text, _now(), source, run_id, state, note[:200]),
    )


def active_adoptions(db: Database) -> list[str]:
    """当前处于 ``active`` 的采纳词（供撤销与配额判断）。"""
    rows = db.query(
        "SELECT keyword FROM keyword_adoptions WHERE state = 'active' "
        "ORDER BY adopted_at DESC"
    )
    return [str(row["keyword"]) for row in rows]


def revert_adoption(db: Database, keyword: str) -> bool:
    """把一条采纳标记为 ``reverted``（只改状态，**不删**，便于对照）。"""
    text = (keyword or "").strip()
    if not text:
        return False
    row = db.query_one("SELECT state FROM keyword_adoptions WHERE keyword = ?", (text,))
    if row is None:
        return False
    db.execute(
        "UPDATE keyword_adoptions SET state = 'reverted' WHERE keyword = ?", (text,)
    )
    return True


def adoption_summary(db: Database) -> dict[str, int]:
    """采纳账本概况（给 ``companion_status`` 用）。"""
    out = {"active": 0, "skipped": 0, "reverted": 0}
    for row in db.query(
        "SELECT state, COUNT(*) AS n FROM keyword_adoptions GROUP BY state"
    ):
        state = str(row["state"])
        if state in out:
            out[state] = int(row["n"])
    return out


# ---------------------------------------------------------------------------
# 轮次账本（**严格限额**的执行层）
# ---------------------------------------------------------------------------


def rounds_today(db: Database) -> int:
    """今天已经跑过几轮推荐流（本地时区当天）。"""
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM trends_rounds WHERE started_at >= ?",
        (_start_of_today(),),
    )
    return int(row["n"]) if row is not None else 0


def rounds_remaining(db: Database, *, max_per_day: int = DEFAULT_MAX_ROUNDS_PER_DAY) -> int:
    """今天还能跑几轮（不会小于 0）。"""
    return max(0, max_per_day - rounds_today(db))


def quota_check(
    db: Database, *, max_per_day: int = DEFAULT_MAX_ROUNDS_PER_DAY
) -> tuple[bool, str]:
    """**跑之前**检查今天的推荐流轮次够不够。

    ★ 这是"严格限额"真正生效的地方（2026-10-17 补）：

    第一版只把 ``DEFAULT_MAX_ROUNDS_PER_DAY`` 写在文档里，
    **代码从不检查** —— 也就是说"限额"是假的，agent 想跑几轮就跑几轮。
    推荐流是**算法喂的**，跑得越多画像越被带跑，所以这个限额必须是真的。

    Returns:
        ``(能不能跑, 人话说明)``。
    """
    used = rounds_today(db)
    if used >= max_per_day:
        return False, (
            f"今天已经跑过 {used} 轮推荐流（上限 {max_per_day}）。"
            "推荐流是算法喂的，跑太多会把小号的兴趣画像带跑 —— "
            "**这是设计，不是故障**，明天再来。"
        )
    return True, f"今天还可跑 {max_per_day - used} 轮（已用 {used}/{max_per_day}）"


def record_round(
    db: Database,
    *,
    fetched_n: int = 0,
    added_n: int = 0,
    conclusion: str = "ok",
    round_id: str = "",
) -> str:
    """记一轮推荐流（**跑完必须记**，否则限额形同虚设）。

    ⚠️ 写的是 ``trends_rounds``，**不是 ``runs``** ——
    写进 ``runs`` 会让 ``schedule.is_due()`` 误以为主流程今天已经干过活了。
    """
    import uuid

    rid = round_id or f"trends_{uuid.uuid4().hex[:12]}"
    db.execute(
        "INSERT INTO trends_rounds (round_id, started_at, fetched_n, added_n, conclusion) "
        "VALUES (?,?,?,?,?)",
        (rid, _now(), int(fetched_n), int(added_n), str(conclusion)),
    )
    return rid


def rounds_summary(
    db: Database, *, max_per_day: int = DEFAULT_MAX_ROUNDS_PER_DAY
) -> dict[str, Any]:
    """轮次概况（给 ``companion_status`` 用）。"""
    used = rounds_today(db)
    row = db.query_one(
        "SELECT MAX(started_at) AS last_at FROM trends_rounds WHERE started_at >= ?",
        (_start_of_today(),),
    )
    last_at = int(row["last_at"]) if row is not None and row["last_at"] is not None else 0
    return {
        "used_today": used,
        "max_per_day": max_per_day,
        "remaining_today": max(0, max_per_day - used),
        "last_at": last_at,
    }


# ---- 小工具（延迟 import，避免顶层依赖 json/time）--------------------------


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _json_loads(text: str) -> Any:
    import json

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return []


def _now() -> int:
    import time

    return int(time.time())


def _start_of_today() -> int:
    """本地时区当天 00:00 的 Unix 秒。

    ★ **刻意复用** :func:`core.storage._start_of_today`，不自己再写一遍 ——
    "今天从几点算起"这类边界逻辑一旦有两份实现，
    早晚会一份用本地时区、一份用 UTC，于是**配额在半夜附近对不上**
    （这类 bug 极难查：白天一切正常）。
    """
    from core.storage import _start_of_today as _impl

    return _impl()
