"""外部只读后端的持久身份、证据、缓存和账号级停机状态。

旧 ``bvid`` 列保留为内部不透明主键；只有 B 站内容才是真 BV。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from core.bilibili import VideoMeta
from core.external_config import ExternalPlatformConfig
from core.platforms import PlatformError
from core.storage import Database, now_ts

EXTERNAL_PLATFORMS = ("xiaohongshu", "douyin")


def normalize_content_input(
    platform: str, native_id: str, context: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    ctx = dict(context or {})
    if native_id.startswith(("http://", "https://")):
        parsed = urlsplit(native_id)
        domain = "xiaohongshu.com" if platform == "xiaohongshu" else "douyin.com"
        host = (parsed.hostname or "").lower()
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or not (host == domain or host.endswith("." + domain))):
            raise PlatformError("INVALID_CONTENT_URL")
        parts = parsed.path.strip("/").split("/")
        if len(parts) < 2 or parts[-2] not in {"explore", "item", "video", "note"}:
            raise PlatformError("CANONICAL_LINK_REQUIRED")
        native_id = parts[-1]
        if platform == "xiaohongshu":
            token = parse_qs(parsed.query).get("xsec_token", [""])[0]
            if token:
                ctx.setdefault("xsec_token", token)
    return native_id, ctx


def content_key(platform: str, account: str, native_id: str) -> str:
    if platform not in EXTERNAL_PLATFORMS or not native_id.strip():
        raise PlatformError("INVALID_CONTENT_ID")
    return ":".join(quote(part, safe="") for part in (platform, account, native_id))


def source_ref(db: Database, key: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM content_sources WHERE content_key = ?", (key,))
    if row is None:
        if not key.startswith("BV"):
            raise PlatformError("CONTENT_NOT_FOUND")
        return {"platform": "bilibili", "native_id": key, "account_id": "", "context": {}}
    result = dict(row)
    result["context"] = json.loads(str(result.pop("context_json")))
    return result


def is_external(db: Database, key: str) -> bool:
    # Namespaced keys are rejected even if a malformed caller did not register them.
    return key.startswith(tuple(f"{p}:" for p in EXTERNAL_PLATFORMS)) or bool(
        db.query_one("SELECT 1 FROM content_sources WHERE content_key = ?", (key,))
    )


def recover_content_health(db: Database, platform: str, account_id: str) -> bool:
    """Only the human CLI exposes recovery; callers must specify the exact scope."""
    cursor = db.execute(
        "UPDATE content_health SET state = 'ready', error_code = '' "
        "WHERE platform = ? AND account_id = ? AND state != 'ready'",
        (platform, account_id),
    )
    return cursor.rowcount > 0


class ContentReader:
    def __init__(
        self, db: Database, adapters: dict[str, Any],
        configs: dict[str, ExternalPlatformConfig] | None = None,
    ) -> None:
        self.db, self.adapters = db, adapters
        self.configs = configs or {}
        self.last_new_keys: set[str] = set()

    def config(self, platform: str) -> ExternalPlatformConfig:
        return self.configs.get(platform) or ExternalPlatformConfig()

    def adapter(self, platform: str) -> Any:
        if platform not in EXTERNAL_PLATFORMS:
            raise PlatformError("UNKNOWN_PLATFORM")
        if platform not in self.adapters:
            raise PlatformError("NOT_CONFIGURED", f"{platform}: 请先配置并启用外部只读后端")
        return self.adapters[platform]

    async def _read(
        self, platform: str, operation: str, arguments: dict[str, Any],
        call: Callable[[], Awaitable[Any]],
    ) -> Any:
        self.adapter(platform)
        cfg = self.config(platform)
        scope = (platform, cfg.account_id)
        digest = hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()
        key = f"{platform}:{cfg.account_id}:{operation}:{digest}"
        cached = self.db.query_one(
            "SELECT value_json FROM content_cache WHERE cache_key = ? AND expires_at > ?",
            (key, now_ts()),
        )
        if cached:
            return json.loads(str(cached["value_json"]))
        # Reserve a request slot transactionally so multiple bridge processes share pacing.
        with self.db.transaction() as tx:
            row = tx.execute(
                "SELECT * FROM content_health WHERE platform = ? AND account_id = ?", scope
            ).fetchone()
            if row and row["state"] != "ready":
                raise PlatformError(str(row["state"]), f"{platform}: {row['state']}，需人工恢复")
            slot = max(time.time(), float(row["next_request_at"]) if row else 0)
            tx.execute(
                "INSERT INTO content_health(platform,account_id,state,next_request_at) "
                "VALUES (?,?,'ready',?) ON CONFLICT(platform,account_id) DO UPDATE SET "
                "next_request_at = excluded.next_request_at",
                (*scope, slot + cfg.min_interval_seconds),
            )
        await asyncio.sleep(max(0, slot - time.time()))
        # A preceding concurrent call may have frozen the account while we waited.
        state = self.db.query_one(
            "SELECT state FROM content_health WHERE platform = ? AND account_id = ?", scope
        )
        if state and state["state"] != "ready":
            raise PlatformError(str(state["state"]))
        try:
            result = await call()
        except PlatformError as exc:
            code = exc.code.lower()
            state_name = next((state for state, markers in {
                "verification_required": ("verification", "captcha", "risk_control"),
                "auth_expired": ("auth_expired", "login_required", "not_logged", "unauthorized"),
                "rate_limited": ("rate_limit", "too_many", "429"),
                "platform_error": ("platform_error", "forbidden"),
            }.items() if any(marker in code for marker in markers)), "")
            if state_name:
                self.db.execute(
                    "UPDATE content_health SET state = ?, error_code = ? "
                    "WHERE platform = ? AND account_id = ?", (state_name, exc.code, *scope),
                )
            raise
        if cfg.cache_ttl_seconds:
            self.db.execute(
                "INSERT OR REPLACE INTO content_cache(cache_key,value_json,expires_at) "
                "VALUES (?,?,?)",
                (key, json.dumps(result, ensure_ascii=False), now_ts() + cfg.cache_ttl_seconds),
            )
        return result

    def register(self, platform: str, item: dict[str, Any]) -> tuple[str, bool]:
        cfg = self.config(platform)
        native = str(item.get("native_id") or "")
        key = content_key(platform, cfg.account_id, native)
        new = self.db.upsert_video(
            bvid=key, title=str(item.get("title") or ""),
            up_name=str(item.get("author") or ""), source_url=str(item.get("url") or ""),
            published_at=int(item.get("published_at") or 0) or None,
            duration_sec=int(item.get("duration_sec") or 0) or None,
        )
        previous = self.db.query_one("SELECT * FROM content_sources WHERE content_key = ?", (key,))
        context = json.loads(str(previous["context_json"])) if previous else {}
        context.update(item.get("context") or {})
        author_id = str(item.get("author_id") or (previous["author_id"] if previous else ""))
        self.db.execute(
            "INSERT INTO content_sources(content_key,platform,account_id,native_id,author_id,"
            "context_json) VALUES (?,?,?,?,?,?) ON CONFLICT(content_key) DO UPDATE SET "
            "author_id=excluded.author_id, context_json=excluded.context_json",
            (key, platform, cfg.account_id, native, author_id, json.dumps(context)),
        )
        return key, new

    async def discover(
        self, platform: str, keyword: str, *, days: int, limit: int,
    ) -> list[VideoMeta]:
        adapter = self.adapter(platform)
        items = await self._read(
            platform, "search", {"keyword": keyword, "days": days, "limit": limit},
            lambda: adapter.search(keyword, days=days, limit=limit),
        )
        self.last_new_keys = set()
        result: list[VideoMeta] = []
        for item in items[:limit]:
            published = int(item.get("published_at") or 0)
            if published and published < now_ts() - days * 86400:
                continue
            key, new = self.register(platform, item)
            self.db.execute("UPDATE videos SET discovery_keyword=? "
                            "WHERE bvid=? AND discovery_keyword=''", (keyword, key))
            if new:
                self.last_new_keys.add(key)
            result.append(VideoMeta(
                bvid=key, aid=0, title=str(item.get("title") or ""), up_mid=0,
                up_name=str(item.get("author") or ""), published_at=published,
                duration_sec=int(item.get("duration_sec") or 0),
                source_url=str(item.get("url") or ""),
            ))
        return result

    def _scope(self, key: str) -> dict[str, Any]:
        ref = source_ref(self.db, key)
        if ref["account_id"] != self.config(ref["platform"]).account_id:
            raise PlatformError("ACCOUNT_SCOPE_MISMATCH", "该内容属于另一个后端账号配置")
        return ref

    async def detail(self, key: str) -> dict[str, Any]:
        ref = self._scope(key)
        platform = str(ref["platform"])
        adapter = self.adapter(platform)
        item: dict[str, Any] = await self._read(
            platform, "detail", {"id": ref["native_id"], "context": ref["context"]},
            lambda: adapter.get_detail(ref["native_id"], context=ref["context"]),
        )
        # A backend is not allowed to silently substitute a different content ID.
        if str(item.get("native_id") or "") != ref["native_id"]:
            raise PlatformError("CONTENT_ID_MISMATCH")
        self.register(platform, item)
        return item

    async def comments(self, key: str, *, limit: int) -> list[dict[str, Any]]:
        ref = self._scope(key)
        adapter = self.adapter(str(ref["platform"]))
        result: list[dict[str, Any]] = await self._read(
            ref["platform"], "comments", {"id": ref["native_id"], "limit": limit,
                                         "context": ref["context"]},
            lambda: adapter.get_comments(ref["native_id"], limit=limit, context=ref["context"]),
        )
        return result[:limit]


def register_blocks(db: Database, key: str, item: dict[str, Any]) -> list[dict[str, Any]]:
    source_url = str(item.get("url") or "")
    raw = list(item.get("evidence_blocks") or [])
    if str(item.get("description") or "").strip():
        raw.insert(0, {"source_type": "description", "text": item["description"]})
    blocks: list[dict[str, Any]] = []
    for block in raw:
        kind = str(block.get("source_type") or "description")
        if kind not in {"description", "subtitle", "asr", "ocr"}:
            raise PlatformError("INVALID_EVIDENCE_TYPE")
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        url = str(block.get("source_url") or source_url)
        start, end = block.get("start_sec"), block.get("end_sec")
        eid = db.add_evidence(bvid=key, source_type=kind, text=text, source_url=url,
                              start_sec=start, end_sec=end, page_index=block.get("page_index"))
        blocks.append({"evidence_id": eid, "source_type": kind, "text": text, "start_sec": start})
    return blocks
