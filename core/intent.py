"""意图 → 关键词（搜索方式 ①，2026-10-17）。

## 为什么要有它

原来只能"给一串关键词"，主人想表达"我大概想看什么"时没法用。
这个模块把**模糊意图**（"想看看有没有人做 DSH 的记忆插件"）
交给豆包展开成**一组可搜索的关键词**，再走原有搜索链路。

## 三条设计原则（都很重要）

1. **意图展开是"建议"，不是"授权"**
   它只产出**关键词**，不碰发布模式 / 配额 / UID / 暂停。
   展开结果会被记进 ``runs`` 的日志，**可审计**。

2. **可控性优先于"聪明"**
   豆包返回的词要过**确定性清洗**（去重、去空白、长度上限、数量上限），
   **不信任模型输出的形状** —— 它给 50 个词也只取前 N 个。

3. **失败必须能降级**
   豆包不可用（没密钥 / 超时 / JSON 坏）时，**退回"把意图本身当关键词"**，
   而不是报错停摆 —— 意图本身往往就能搜（"DSH 插件"）。

## 与搜索方式 ② 的关系

② 推荐流探索发现的**新词**会回填进 ``discovery.keywords``（见
:mod:`core.trends`），然后走**同一条**搜索链路 ——
这样算法只当"词库扩展器"，**不接管内容判断**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 一次展开最多产出多少个关键词（超出丢弃）
MAX_KEYWORDS = 8
#: 单个关键词长度上限（B 站搜索框也放不下太长的）
MAX_KEYWORD_CHARS = 32
#: 关键词里不允许出现的字符（搜索词里的引号/换行会破坏请求）
_FORBIDDEN = ('"', "'", "\n", "\r", "\t", "|")

_SYSTEM_PROMPT = """\
你是 B 站搜索关键词生成器。用户会用一句话描述"大概想看什么"，
你要把它变成**一组可以直接拿去 B 站搜索框里搜的关键词**。

要求：
- 每个关键词都是**能独立搜出结果**的短词（2~12 个字最佳）；
- 覆盖不同角度：同义说法、上位词、下位词、相关工具名/技术名；
- **不要**出现"视频""推荐""看看""有没有"这类搜索框里没用的词；
- **不要**编造具体的人名、公司名或产品名（除非用户自己提到）；
- 按"最可能搜到有用内容"的顺序排列。

只输出 JSON，不要解释：
{"keywords": ["词1", "词2", "词3"], "reason": "一句话说明你怎么理解这个意图"}
"""


@dataclass(frozen=True)
class IntentExpansion:
    """意图展开的结果。"""

    keywords: list[str] = field(default_factory=list)
    reason: str = ""
    #: 真正用了豆包（False = 降级成"意图本身当关键词"）
    used_worker: bool = False
    #: 失败原因码（``None`` 表示成功或未尝试）
    error_code: str | None = None
    usage: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "keywords": self.keywords,
            "reason": self.reason,
            "used_worker": self.used_worker,
            "error_code": self.error_code,
            "usage": self.usage,
        }


def sanitize_keywords(raw: Any, *, limit: int = MAX_KEYWORDS) -> list[str]:
    """**确定性清洗**模型给的关键词（不信任它的形状）。

    规则（逐条都有测试）：

    * 只接受字符串；非字符串一律丢弃（模型可能给数字 / 嵌套对象）；
    * 去掉首尾空白与禁止字符；
    * 丢掉空串、丢掉过长的（> :data:`MAX_KEYWORD_CHARS`）；
    * **去重**（大小写不敏感，但保留首次出现的写法）；
    * 最多留 ``limit`` 个。
    """
    if not isinstance(raw, list):
        return []

    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        text = item.strip()
        for ch in _FORBIDDEN:
            text = text.replace(ch, " ")
        text = " ".join(text.split())  # 折叠内部空白
        if not text or len(text) > MAX_KEYWORD_CHARS:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def fallback_keywords(intent: str, *, limit: int = MAX_KEYWORDS) -> list[str]:
    """豆包不可用时的降级：**把意图本身当关键词**。

    做法：整句 + 拆出的有信息量的片段（长度 ≥2 的连续中文/字母数字段）。
    这样"想看看有没有人做 DSH 的记忆插件"至少能搜到 "DSH"、"记忆插件"。
    """
    text = intent.strip()
    if not text:
        return []

    out: list[str] = []
    seen: set[str] = set()

    def push(value: str) -> None:
        candidate = value.strip()
        if not candidate or len(candidate) > MAX_KEYWORD_CHARS:
            return
        lowered = candidate.lower()
        if lowered in seen:
            return
        seen.add(lowered)
        out.append(candidate)

    # 先整句（往往就能搜）
    push(text)

    # 再拆片段：连续的中文 / 字母 / 数字
    import re as _re

    for piece in _re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_.+-]{1,}", text):
        push(piece)
        if len(out) >= limit:
            break

    return out[:limit]


async def expand_intent(
    intent: str,
    *,
    worker: Any = None,
    limit: int = MAX_KEYWORDS,
) -> IntentExpansion:
    """把一句**模糊意图**展开成一组可搜索的关键词。

    Args:
        intent: 主人的原话（"想看看有没有人做 DSH 的记忆插件"）。
        worker: :class:`core.text_worker.TextWorker`；``None`` 或不可用时**降级**。
        limit: 最多要几个关键词。

    Returns:
        :class:`IntentExpansion`；**任何失败都降级**（``used_worker=False``），
        绝不抛异常 —— 意图展开只是"让搜索更准"，不该让整轮停摆。
    """
    text = intent.strip()
    if not text:
        return IntentExpansion(keywords=[], reason="", used_worker=False)

    # 没有 worker（或它明确不可用）→ 直接降级
    if worker is None:
        return IntentExpansion(
            keywords=fallback_keywords(text, limit=limit),
            reason="没有可用的文字 worker，已按意图本身当关键词",
            used_worker=False,
            error_code="TEXT_WORKER_UNAVAILABLE",
        )

    usable, code = (True, "OK")
    checker = getattr(worker, "is_usable", None)
    if callable(checker):
        usable, code = checker()
    if not usable:
        return IntentExpansion(
            keywords=fallback_keywords(text, limit=limit),
            reason=f"文字 worker 不可用（{code}），已按意图本身当关键词",
            used_worker=False,
            error_code=str(code),
        )

    parsed, error_code, usage = await worker.complete_json(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=f"用户想看的：{text}",
        max_tokens=1024,
        temperature=0.3,
    )

    if parsed is None:
        return IntentExpansion(
            keywords=fallback_keywords(text, limit=limit),
            reason=f"意图展开失败（{error_code}），已按意图本身当关键词",
            used_worker=False,
            error_code=error_code,
            usage=usage,
        )

    keywords = sanitize_keywords(parsed.get("keywords"), limit=limit)
    if not keywords:
        return IntentExpansion(
            keywords=fallback_keywords(text, limit=limit),
            reason="意图展开没产出可用关键词，已按意图本身当关键词",
            used_worker=False,
            error_code="EMPTY_EXPANSION",
            usage=usage,
        )

    return IntentExpansion(
        keywords=keywords,
        reason=str(parsed.get("reason") or ""),
        used_worker=True,
        error_code=None,
        usage=usage,
    )
