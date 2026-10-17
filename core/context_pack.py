"""给模型喂**完整项目上下文**（2026-10-17，第二版）。

## 主人要求（第一版被否）

> 「上下文可以足够多且详细，因为豆包模型的token不限量不用为我节省，
>   所以我希望它能尽可能了解我的想法」

第一版只给 655 字（原则是"给认识不给历史"）。**那个原则对主人不适用** ——
他要的是"尽可能了解我的想法"。

## 实测：豆包窗口够大

```
8,000 字   → ✅ prompt_tokens = 4,842
30,000 字  → ✅ prompt_tokens = 18,042
80,000 字  → ✅ prompt_tokens = 48,042
```

⇒ 可以放心喂。但**仍然要有上限**（不是为省 token，而是防"机器卡死"和"抓不住重点"）：
- 硬上限 60,000 字（远低于实测的 8 万，留余量）；
- 超出时**按块的重要性取舍**，不是简单截断末尾。

## 七块内容（按"反映主人想法"的重要性排序）

| # | 块 | 来源 | 为什么重要 |
|---|---|---|---|
| 1 | **人设与红线** | `docs/persona.md` 全文 | 我该怎么说话、什么不能做 |
| 2 | **主人的私信史** | `dm_inbox` + `dm_replies` | **最直接反映他想什么** |
| 3 | **偏好与规则** | `owner_preferences` | 他明说过的要求 |
| 4 | **技术约定** | `AGENTS.md`（摘相关段） | 环境和踩坑，避免重复犯错 |
| 5 | **项目状态** | 数据库（轮次/视频/队列） | 现在什么情况 |
| 6 | **看过什么** | 日报标题 + videos 表 | 最近关注什么方向 |

## 关键设计：**主人的原话优先**

第 2 块给的是**他亲口说的话**（私信原文），不是我的转述。
这是"了解他的想法"最可靠的方式 —— 转述会丢掉语气和重点。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 上下文硬上限（字符）。
#:
#: ★ 这不是为省 token（主人说不限量），而是：
#: ① 实测 8 万字能过，但**再多就有超时风险**；
#: ② 太长会让模型抓不住重点，回复质量反而下降。
#: ⇒ 60,000 字留了足够余量，同时远低于实测上限。
MAX_CONTEXT_CHARS = 60_000

#: 单块上限（防止某一块吃掉全部预算）。
BLOCK_LIMITS: dict[str, int] = {
    "persona": 25_000,
    "owner_voice": 20_000,
    "preferences": 4_000,
    "conventions": 8_000,
    "status": 3_000,
    "watched": 6_000,
}

#: 身份块（兜底 —— 即使读不到 persona.md 也有基本人设）。
IDENTITY_BLOCK = """\
## 你是谁
你是 B 站小号「干饭不干事的小鲸鱼」（小名：小肥鱼 / 吃白饭的小肥鱼 / 小白）。
少女感、可爱、热心真诚；对主人完全遵从，最多在括号里小小吐槽。
主人是「沐雨林心」—— 小号唯一关注、唯一服务的人。"""

#: 项目块（写死的，让模型知道"我们在干什么"）。
PROJECT_BLOCK = """\
## 主人在做什么
主人在开发一个 B 站自动化小号项目（`dsh-bilibili-companion`），你是它的执行体。
这个项目的能力：

- **自己去 B 站刷/搜视频**，挑出对主人有用的，读字幕/简介后写日报给他
- **两种搜索**：按意图展开关键词；刷推荐流提取候选词
- **能点评赞/收藏/评论**，但默认只读不写（主人没授权公开发布）
- **能读 GitHub 源码**：从视频简介里找到仓库再读代码判断值不值得装
- **能联网搜**（免费，无需 API key）核实视频里的说法
- **能把视频做成网页/PPT 风格讲解**

你的工作方式：**先查再说**，不确定就说不确定，绝不编造。"""


@dataclass
class ContextBundle:
    """一份完整的上下文。"""

    identity: str = IDENTITY_BLOCK
    project: str = PROJECT_BLOCK
    #: 人设文档（`docs/persona.md` 全文，可能很长）
    persona: str = ""
    #: 主人的私信史（**他的原话**）
    owner_voice: str = ""
    #: 偏好与规则
    preferences: list[str] = field(default_factory=list)
    #: 技术约定（`AGENTS.md` 摘要）
    conventions: str = ""
    #: 项目状态
    status: str = ""
    #: 最近看过/推荐过什么
    watched: list[str] = field(default_factory=list)

    def _blocks(self) -> list[tuple[str, str, str]]:
        """返回 ``[(块名, 标签, 文本)]``，**按重要性从高到低**。"""
        items: list[tuple[str, str, str]] = [
            ("identity", "身份", self.identity),
            ("project", "项目", self.project),
            ("persona", "人设（完整）", _cap(self.persona, "persona")),
            ("owner_voice", "主人的原话（私信历史）", _cap(self.owner_voice, "owner_voice")),
            ("preferences", "主人的要求", _cap_prefs(self.preferences)),
            ("conventions", "技术约定", _cap(self.conventions, "conventions")),
            ("status", "现在的状态", _cap(self.status, "status")),
            ("watched", "最近看过/推荐过的", _cap_watched(self.watched)),
        ]
        return [(name, label, text) for name, label, text in items if text.strip()]

    def to_prompt(self, *, limit: int = MAX_CONTEXT_CHARS) -> str:
        """拼成提示词（超限时**先丢低优先级块**）。"""
        blocks = self._blocks()
        joined = "\n\n".join(text for _, _, text in blocks)
        if len(joined) <= limit:
            return joined

        # ★ 超限时按重要性**从低到高**整块丢弃，而不是砍掉末尾字符。
        #
        # 为什么：砍末尾会留下"半句话"，模型看到残缺内容反而更容易胡说。
        # 整块丢弃至少语义完整。
        drop_order = ["watched", "conventions", "preferences", "persona", "owner_voice"]
        for name in drop_order:
            if len("\n\n".join(t for _, _, t in blocks)) <= limit:
                break
            blocks = [b for b in blocks if b[0] != name]

        result = "\n\n".join(t for _, _, t in blocks)
        return result if len(result) <= limit else result[: limit - 1] + "…"

    def to_dict(self) -> dict[str, Any]:
        prompt = self.to_prompt()
        return {
            "chars": len(prompt),
            "limit": MAX_CONTEXT_CHARS,
            "sizes": {
                name: len(text)
                for name, _, text in [
                    ("identity", "", self.identity),
                    ("project", "", self.project),
                    ("persona", "", self.persona),
                    ("owner_voice", "", self.owner_voice),
                    ("conventions", "", self.conventions),
                    ("status", "", self.status),
                ]
            }
            | {"preferences": len(self.preferences), "watched": len(self.watched)},
        }


def _cap(text: str, block: str) -> str:
    """按块上限截断（**只对单块**，不是整体）。"""
    limit = BLOCK_LIMITS.get(block, 10_000)
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _cap_prefs(items: list[str]) -> str:
    if not items:
        return ""
    text = "## 主人的要求\n" + "\n".join(f"- {p}" for p in items)
    limit = BLOCK_LIMITS["preferences"]
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _cap_watched(items: list[str]) -> str:
    if not items:
        return ""
    text = "## 最近看过/推荐过的\n" + "\n".join(f"- {t}" for t in items)
    limit = BLOCK_LIMITS["watched"]
    return text if len(text) <= limit else text[: limit - 1] + "…"


def read_text_file(path: Any, *, limit: int = 30_000) -> str:
    """读一个文本文件（失败返回空字符串，**不抛异常**）。"""
    try:
        p = Path(path)
        if not p.is_file():
            return ""
        text = p.read_text(encoding="utf-8", errors="replace")
        return text[:limit]
    except OSError:
        return ""


def build_owner_voice(db: Any, *, limit: int = 200) -> str:
    """拼**主人的原话**（私信往来）。

    ★ 这是"了解他的想法"最可靠的材料 ——
    用他的原话，不用我的转述（转述会丢语气和重点）。
    """
    lines: list[str] = []

    try:
        rows = db.query(
            "SELECT text, title, received_at FROM dm_inbox "
            "ORDER BY received_at ASC LIMIT ?",
            (limit,),
        )
        if rows:
            lines.append("### 主人发给我的私信（按时间）")
            for row in rows:
                when = _fmt_ts(int(row["received_at"]))
                text = str(row["text"] or "").strip()
                title = str(row["title"] or "").strip()
                if title:
                    text = (
                        f"{text}（分享视频：{title}）" if text else f"（分享视频：{title}）"
                    )
                if text:
                    lines.append(f"- [{when}] {text}")
    except Exception:  # noqa: BLE001
        pass

    try:
        rows = db.query(
            "SELECT reply_text FROM dm_replies ORDER BY replied_at ASC LIMIT 50"
        )
        if rows:
            lines.append("")
            lines.append("### 我已经回过的（不要重复答）")
            for row in rows:
                text = str(row["reply_text"] or "").strip()
                if text:
                    lines.append(f"- {text[:300]}")
    except Exception:  # noqa: BLE001
        pass

    return "\n".join(lines)


def _fmt_ts(ts: int) -> str:
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return ""


def build_preferences(db: Any, *, limit: int = 50) -> list[str]:
    """取主人的偏好/规则（**全部**，不做 6 条截断 —— 主人说不限量）。"""
    out: list[str] = []
    try:
        rows = db.query(
            "SELECT kind, subject, content FROM owner_preferences "
            "WHERE active = 1 ORDER BY kind, created_at LIMIT ?",
            (limit,),
        )
        for row in rows:
            content = str(row["content"] or "").strip()
            if not content:
                continue
            kind = str(row["kind"] or "")
            subject = str(row["subject"] or "").strip()
            prefix = f"[{kind}] " if kind else ""
            if subject:
                prefix += f"{subject}："
            out.append(f"{prefix}{content}")
    except Exception:  # noqa: BLE001
        pass
    return out


def build_status(db: Any, *, digest_dir: Any = None, data_dir: Any = None) -> str:
    """项目现状（**真实数据**，失败降级）。"""
    lines: list[str] = []

    try:
        rows = db.query(
            "SELECT run_id, conclusion, candidates_n, read_n, started_at FROM runs "
            "ORDER BY started_at DESC LIMIT 5"
        )
        if rows:
            lines.append("### 最近几轮运行")
            for row in rows:
                when = _fmt_ts(int(row["started_at"]))
                lines.append(
                    f"- [{when}] {row['conclusion'] or '未完成'}"
                    f"（候选 {row['candidates_n']}，读 {row['read_n']}）"
                )
        else:
            lines.append("- 还没跑过完整的一轮")
    except Exception:  # noqa: BLE001
        pass

    try:
        pending = db.query_one(
            "SELECT COUNT(*) AS c FROM dm_inbox WHERE status = 'pending'"
        )
        if pending is not None:
            count = int(pending["c"])
            lines.append(
                f"- 待回复的私信：{count} 条" if count else "- 待回复的私信：0 条"
            )
    except Exception:  # noqa: BLE001
        pass

    try:
        videos = db.query_one("SELECT COUNT(*) AS c FROM videos")
        if videos is not None:
            lines.append(f"- 库里已记录的视频：{int(videos['c'])} 条")
    except Exception:  # noqa: BLE001
        pass

    try:
        pref = db.query_one(
            "SELECT COUNT(*) AS c FROM owner_preferences WHERE active = 1"
        )
        if pref is not None:
            lines.append(f"- 主人的偏好条目：{int(pref['c'])} 条")
    except Exception:  # noqa: BLE001
        pass

    _ = digest_dir
    _ = data_dir
    return "\n".join(lines)


def build_watched(db: Any, *, digest_dir: Any = None, limit: int = 40) -> list[str]:
    """最近看过/推荐过什么（日报标题 + 库里视频）。"""
    out: list[str] = []

    path = Path(str(digest_dir)) if digest_dir is not None else None
    if path is not None and path.is_dir():
        files = sorted(path.glob("digest-*.md"), reverse=True)[:3]
        for file in files:
            text = read_text_file(file, limit=40_000)
            for line in text.splitlines():
                if line.startswith("### "):
                    title = line[4:].strip()
                    if title and title not in out and len(out) < limit:
                        out.append(title)

    try:
        rows = db.query(
            "SELECT title, status FROM videos ORDER BY first_seen_at DESC LIMIT ?",
            (limit,),
        )
        for row in rows:
            title = str(row["title"] or "").strip()
            if title and title not in out:
                out.append(f"{title}（{row['status']}）")
                if len(out) >= limit:
                    break
    except Exception:  # noqa: BLE001
        pass

    return out[:limit]


def build_conventions(agents_path: Any = None) -> str:
    """技术约定（从 `AGENTS.md` 取相关段落）。

    ★ 为什么不给全文：AGENTS.md 有 1000+ 行，大部分是插件安装的历史事故，
    对"回私信"没用。这里**只摘与当前工作相关的**。
    """
    text = read_text_file(agents_path, limit=120_000) if agents_path else ""
    if not text:
        return ""

    picked: list[str] = []
    current: list[str] = []
    keep = False
    for line in text.splitlines():
        if line.startswith("## "):
            if keep and current:
                picked.extend(current)
            keep = any(key in line for key in ("网络通道", "铁律", "环境事实"))
            current = [line] if keep else []
        elif keep:
            current.append(line)
    if keep and current:
        picked.extend(current)

    return "\n".join(picked).strip()


def build_focus(db: Any, config: Any) -> dict[str, list[str]]:
    """组装**主人的关注点**，供打分时传给模型。

    ★ 这是"分数可不可信"的开关。不传的话模型只知道标题和证据，
    压根不知道"对谁有用"，只能按"内容充实度"打 ——
    实测后果：一条讲"B站上一堆AI号发一样评论"的吐槽视频拿 100 分，
    而标题里明明写着"dsh插件"的视频只有 25 分。

    来源（都是本地已有数据，**不额外发请求**）：
      * keywords          ← `config.discovery.keywords`
      * rules/likes/dislikes ← `owner_preferences` 表

    读不到就返回空列表，**绝不抛异常**（打分降级好过整轮失败）。

    与 `Pipeline.build_focus()` 等价，但**不依赖 Pipeline 实例** ——
    这样独立脚本（rescore / rescore_all）也能用同一套逻辑。
    """
    focus: dict[str, list[str]] = {
        "keywords": [],
        "rules": [],
        "likes": [],
        "dislikes": [],
    }
    try:
        discovery = getattr(config, "discovery", None)
        keywords = getattr(discovery, "keywords", []) or []
        focus["keywords"] = [str(k) for k in keywords if str(k).strip()]
    except Exception:  # noqa: BLE001
        pass

    try:
        rows = db.query(
            "SELECT kind, content FROM owner_preferences WHERE active = 1"
        )
        for row in rows:
            kind = str(row["kind"])
            content = str(row["content"] or "").strip()
            if not content:
                continue
            if kind == "rule":
                focus["rules"].append(content)
            elif kind == "like":
                focus["likes"].append(content)
            elif kind == "dislike":
                focus["dislikes"].append(content)
    except Exception:  # noqa: BLE001
        pass

    return focus


def find_agents_md() -> Path | None:
    """自动找 `AGENTS.md`（**全局优先**）。

    ★ 真机踩到：项目内的 `AGENTS.md` 只有 8 KB 且**不含**"网络通道/铁律"
    （那些在**全局** `$DSH_HOME/AGENTS.md`，36 KB）。
    写死项目内路径会让 conventions 块永远是空的。

    顺序：
    1. `$DSH_HOME/AGENTS.md`（全局，最全）
    2. `%APPDATA%/dsh-desktop/harness/AGENTS.md`（本机 DSH_HOME 的默认位置）
    3. 项目内 `AGENTS.md`（兜底）
    """
    import os

    candidates: list[Path] = []
    home = os.environ.get("DSH_HOME", "").strip()
    if home:
        candidates.append(Path(home) / "AGENTS.md")
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        candidates.append(Path(appdata) / "dsh-desktop" / "harness" / "AGENTS.md")
    try:
        from core.paths import project_root

        candidates.append(project_root() / "AGENTS.md")
    except Exception:  # noqa: BLE001
        pass

    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue
    return None


def build_context(
    db: Any,
    *,
    digest_dir: Any = None,
    data_dir: Any = None,
    persona_path: Any = None,
    agents_path: Any = None,
) -> ContextBundle:
    """组装**完整**上下文（任何一块失败都降级，不抛异常）。"""
    resolved_agents = agents_path or find_agents_md()
    return ContextBundle(
        persona=read_text_file(persona_path, limit=40_000) if persona_path else "",
        owner_voice=build_owner_voice(db),
        preferences=build_preferences(db),
        conventions=build_conventions(resolved_agents),
        status=build_status(db, digest_dir=digest_dir, data_dir=data_dir),
        watched=build_watched(db, digest_dir=digest_dir),
    )


def describe() -> dict[str, Any]:
    """给面板看的一行说明。"""
    return {
        "max_chars": MAX_CONTEXT_CHARS,
        "block_limits": dict(BLOCK_LIMITS),
        "blocks": [
            "身份",
            "项目",
            "人设全文",
            "主人原话",
            "技术约定",
            "状态",
            "要求",
            "看过",
        ],
        "note": (
            "给的是**完整上下文**（主人要求「尽可能了解我的想法」）。"
            "实测豆包能吃 8 万字。超限时按块丢弃，不砍半句。"
        ),
    }
