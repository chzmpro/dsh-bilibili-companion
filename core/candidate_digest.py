"""豆包**深度提炼**视频内容——不截断，直接读全文（2026-10-18）。

## 主人要求

> 「我既然有不要钱的豆包模型，那么就可以不采用截断的方式，
>   而是让豆包采取最有价值的字幕等信息，评论区中有许多@其主人介绍的
>   类似于本项目的大肥鱼，这些也可以让豆包分辨后总结它们的观点，
>   然后查看真实人类评论中的有效信息，最后再发给值班员」

## 为什么这比截断好

| | 截断（旧） | **豆包提炼（新）** |
|---|---|---|
| 信息完整性 | 只看到前 2500 字 | **读全文** |
| 关键结论 | 常在中间/后半段，**被截掉** | 能抓到 |
| 成本 | 省 token | 豆包免费，**几乎为 0** |
| 交给值班员的量 | 7500 字/视频 | **约 600 字/视频，更干净** |

## 四类提炼（对应主人的四个关注点）

1. **core_value** —— 这视频到底解决了什么问题（1-2 句）
2. **highlights** —— 字幕里最有价值的要点（具体做法/数字/结论）
3. **peers** —— 评论区的**同类项目/同类 AI 号**（自称"大肥鱼"、
   @主人介绍自己项目的那种）的观点与特点
4. **human_bits** —— **真实人类**评论里的有效信息：
   踩过的坑、补充的事实、反驳的意见
5. **caveats** —— 需要注意的（未核实/有争议/作者没说清）

## 关键：区分「同类号推广」和「真实人类经验」

这两类在评论区混在一起，但价值完全不同：
* 同类号（"我也做了个 DSH 插件…"）—— 是**竞品情报**，要单独拎出来
* 真实人类（"我装了之后发现 XX 会冲突"）—— 是**踩坑经验**，最有含金量

⇒ 提示词里明确要求分开，不许混为一谈。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from core.secrets import resolve_secret

#: 单次调用的超时（秒）。谈全文可能很长，给足。
CALL_TIMEOUT = 120

#: 每个视频最多喂多少字的「全文」（**这不是业务截断，是防极端**）。
#:
#: 豆包实测能吃 8 万字。一条视频"字幕+评论+简介"极少超过 5 万，
#: 所以这个上限基本不会触发；它只防"某个视频字幕 20 万字"这种极端情况。
SAFETY_CHARS = 80_000

#: 提炼结果里每条列表最多保留几项（要短、要准）。
MAX_ITEMS = 6

SYSTEM_PROMPT = """你是内容提炼助手。给你一个 B 站视频的**完整内容**（简介/字幕/热评），
请提炼成结构化信息。

**必须严格区分这两类评论**（这是最重要的要求）：
- **同类号/竞品**：那些"我也做了个类似插件"、"@某人 看我的项目"、
  自称"大肥鱼/小号/助手"的账号，在评论区介绍**自己的项目**。
  ⇒ 单独放进 peers，记录它们做了什么、有什么特点、和本视频的关系。
- **真实人类**：普通用户的实际使用反馈。
  ⇒ 放进 human_bits，**重点抓踩坑、冲突、和作者说法不一致的地方**。

只输出一个 JSON 对象，不要 markdown 代码块，不要解释。结构：

{
  "core_value": "1-2 句话：这视频解决了什么问题（不要复述标题）",
  "highlights": ["字幕里最有价值的要点，要具体，3-5 条"],
  "peers": [{"name": "账号名", "what": "它做了什么", "relation": "与本视频差异"}],
  "human_bits": ["真实人类评论里的有效信息：踩过的坑、补充事实、反驳意见"],
  "caveats": ["需要警惕的：未核实、有争议、作者没说清的"]
}

硬性要求：
1. **只基于给你的内容**，不要编造。没有的类别就返回空数组 []。
2. 每条尽量短（25 字以内），说事实不说形容词。
3. 不要把"同类号的自我推广"当成 valid 的内容价值写进 highlights。
4. 评论里明显是刷屏/表情/无意义内容，忽略。"""


@dataclass
class Digest:
    """一个视频的提炼结果。"""

    bvid: str
    core_value: str = ""
    highlights: list[str] = field(default_factory=list)
    peers: list[dict[str, str]] = field(default_factory=list)
    human_bits: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    model: str = ""
    error: str = ""

    def to_prompt(self) -> str:
        """渲染成给值班员看的文本。"""
        lines = [f"【核心价值】{self.core_value or '（未提炼）'}"]
        if self.highlights:
            lines.append("【字幕干货】")
            lines.extend(f"- {h}" for h in self.highlights)
        if self.peers:
            lines.append("【评论区里的同类项目（竞品情报）】")
            for item in self.peers:
                name = item.get("name", "?")
                what = item.get("what", "")
                relation = item.get("relation", "")
                lines.append(f"- {name}：{what}" + (f"（{relation}）" if relation else ""))
        if self.human_bits:
            lines.append("【真实人类的经验】")
            lines.extend(f"- {h}" for h in self.human_bits)
        if self.caveats:
            lines.append("【需要注意】")
            lines.extend(f"- {c}" for c in self.caveats)
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(
            {
                "core_value": self.core_value,
                "highlights": self.highlights,
                "peers": self.peers,
                "human_bits": self.human_bits,
                "caveats": self.caveats,
            },
            ensure_ascii=False,
        )


def _as_list(value: Any, *, limit: int = MAX_ITEMS) -> list[str]:
    """把任意输入收成短字符串列表。"""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value[:limit]:
        text = str(item).strip()
        if text:
            out.append(text[:120])
    return out


def _as_peers(value: Any, *, limit: int = MAX_ITEMS) -> list[dict[str, str]]:
    """把 peers 收成 ``[{name, what, relation}]``。"""
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value[:limit]:
        if isinstance(item, dict):
            out.append(
                {
                    "name": str(item.get("name") or "")[:40],
                    "what": str(item.get("what") or "")[:120],
                    "relation": str(item.get("relation") or "")[:80],
                }
            )
        elif isinstance(item, str) and item.strip():
            out.append({"name": item.strip()[:40], "what": "", "relation": ""})
    return out


def build_payload_text(
    *,
    title: str,
    description: str,
    subtitle: str,
    comments: list[str],
) -> str:
    """把完整内容拼成喂给豆包的文本（**不按业务截断**）。"""
    parts = [f"视频标题：{title}", ""]
    if description.strip():
        parts.extend(["## 简介（全文）", description.strip(), ""])
    if subtitle.strip():
        parts.extend(["## 字幕（全文）", subtitle.strip(), ""])
    if comments:
        parts.append(f"## 热评（{len(comments)} 条，含同类号与真实人类）")
        parts.extend(f"- {c.strip()}" for c in comments if c.strip())
    text = "\n".join(parts)
    # 只防极端（正常视频远小于此）
    return text[:SAFETY_CHARS]


async def digest_video(
    *,
    bvid: str,
    title: str,
    description: str,
    subtitle: str,
    comments: list[str],
    config: Any = None,
    client: httpx.AsyncClient | None = None,
) -> Digest:
    """调豆包提炼一个视频（**失败返回带 error 的 Digest，不抛异常**）。"""
    key_env = str(getattr(getattr(config, "text_worker", None), "api_key_env", "") or "")
    key = resolve_secret(key_env) if key_env else None
    if not key:
        return Digest(bvid=bvid, error=f"找不到密钥（环境变量 {key_env}）")

    base_url = str(getattr(getattr(config, "text_worker", None), "base_url", "") or "")
    model = str(getattr(getattr(config, "text_worker", None), "model", "") or "")

    body_text = build_payload_text(
        title=title, description=description, subtitle=subtitle, comments=comments
    )
    if len(body_text) < 20:
        return Digest(bvid=bvid, error="内容太少，无法提炼")

    owns = client is None
    http = client or httpx.AsyncClient(timeout=CALL_TIMEOUT, trust_env=False)
    try:
        response = await http.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": body_text},
                ],
                "max_tokens": 1500,
                "temperature": 0.3,
            },
        )
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return Digest(bvid=bvid, error=f"调用失败：{type(exc).__name__}")
    finally:
        if owns:
            await http.aclose()

    if response.status_code != 200:
        return Digest(bvid=bvid, error=f"HTTP {response.status_code}")

    content = ""
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            content = str(message.get("content") or "")

    parsed = _parse_json(content)
    if parsed is None:
        return Digest(bvid=bvid, error="模型没返回合法 JSON")

    return Digest(
        bvid=bvid,
        core_value=str(parsed.get("core_value") or "")[:300],
        highlights=_as_list(parsed.get("highlights")),
        peers=_as_peers(parsed.get("peers")),
        human_bits=_as_list(parsed.get("human_bits")),
        caveats=_as_list(parsed.get("caveats")),
        model=model,
    )


def _parse_json(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON（可能带 ```json 包裹）。"""
    body = (text or "").strip()
    if body.startswith("```"):
        # 去掉 ```json ... ```
        body = body.strip("`")
        if body.lower().startswith("json"):
            body = body[4:]
    start = body.find("{")
    end = body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(body[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def save_digest(db: Any, digest: Digest) -> bool:
    """把提炼结果写进 `candidate_digests`（幂等：同 bvid 覆盖）。"""
    if digest.error:
        return False
    try:
        db.execute(
            "INSERT OR REPLACE INTO candidate_digests "
            "(bvid, core_value, highlights, peers, human_bits, caveats, "
            " digest_json, model, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                digest.bvid,
                digest.core_value,
                json.dumps(digest.highlights, ensure_ascii=False),
                json.dumps(digest.peers, ensure_ascii=False),
                json.dumps(digest.human_bits, ensure_ascii=False),
                json.dumps(digest.caveats, ensure_ascii=False),
                digest.to_json(),
                digest.model,
                int(__import__("time").time()),
            ),
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def load_digest(db: Any, bvid: str) -> Digest | None:
    """从库里读回一个提炼结果。"""
    try:
        row = db.query_one("SELECT * FROM candidate_digests WHERE bvid = ?", (bvid,))
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    return Digest(
        bvid=str(row["bvid"]),
        core_value=str(row["core_value"] or ""),
        highlights=_load_list(row["highlights"]),
        peers=_load_peers(row["peers"]),
        human_bits=_load_list(row["human_bits"]),
        caveats=_load_list(row["caveats"]),
        model=str(row["model"] or ""),
    )


def _load_list(raw: Any) -> list[str]:
    try:
        value = json.loads(str(raw or "[]"))
    except ValueError:
        return []
    return [str(x) for x in value] if isinstance(value, list) else []


def _load_peers(raw: Any) -> list[dict[str, str]]:
    try:
        value = json.loads(str(raw or "[]"))
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    return [x for x in value if isinstance(x, dict)]


def describe() -> dict[str, Any]:
    """给面板/CLI 看的一行说明。"""
    return {
        "purpose": "豆包读全文提炼，替代截断",
        "outputs": ["core_value", "highlights", "peers", "human_bits", "caveats"],
        "safety_chars": SAFETY_CHARS,
        "note": "peers = 评论区里的同类项目/同类号；human_bits = 真实人类的踩坑经验",
    }
