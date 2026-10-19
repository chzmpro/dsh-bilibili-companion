"""跨平台结果必须真正经过本地证据和短包管道，而非只报告插件存在。"""
from __future__ import annotations

import json
from typing import Any

import pytest

from core.pipeline import Pipeline
from core.platforms import PlatformCapabilities, PlatformError
from core.ratelimit import RequestQueue
from core.storage import Database
from tests.conftest import make_config


class ReadingBackend:
    def __init__(self, platform: str) -> None:
        self.platform_id = platform
        self.calls: list[tuple[str, Any]] = []
        self.error = ""

    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(search=True, detail=True, comments=True)

    async def search(self, keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("search", keyword))
        if self.error:
            raise PlatformError(self.error)
        return [{"native_id": "same-id", "title": "DSH 工具", "author_id": "user-abc",
                 "url": f"https://example.com/{self.platform_id}/same-id",
                 "context": {"xsec_token": "read-context"}}]

    async def get_detail(self, native_id: str, *, context: Any = None) -> dict[str, Any]:
        self.calls.append(("detail", context))
        if self.error:
            raise PlatformError(self.error)
        return {"native_id": native_id, "title": "DSH 工具", "author_id": "user-abc",
                "description": "作者描述", "url": f"https://example.com/{native_id}",
                "evidence_blocks": [{"source_type": "asr", "text": "只留在证据内的真实原文",
                                     "start_sec": 12.0, "end_sec": 14.0}],
                "limitations": []}

    async def get_comments(self, native_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("comments", native_id))
        return [{"id": "comment-1", "author_id": "commenter-str", "text": "真实评论"}]


def pipeline_for(db: Database, **adapters: Any) -> Pipeline:
    config = make_config()
    return Pipeline(db=db, config=config, reader=None, queue=RequestQueue(db=db, config=config),
                    adapters=adapters)


async def test_same_native_id_isolated_and_context_survives_new_runtime(db: Database) -> None:
    xhs, dy = ReadingBackend("xiaohongshu"), ReadingBackend("douyin")
    pipe = pipeline_for(db, xiaohongshu=xhs, douyin=dy)
    first = await pipe.discover(platforms=["xiaohongshu", "douyin"])
    keys = [item.bvid for item in first.candidates]
    assert len(set(keys)) == 2
    restarted = pipeline_for(db, xiaohongshu=xhs, douyin=dy)
    card, blocks = await restarted.read_video(keys[0])
    assert card.to_payload()["platform"] == "xiaohongshu"
    assert card.to_payload()["native_id"] == "same-id"
    assert ("detail", {"xsec_token": "read-context"}) in xhs.calls
    assert "只留在证据内" not in json.dumps(card.to_payload(), ensure_ascii=False)
    assert db.get_evidence(blocks[-1]["evidence_id"])["text"] == "只留在证据内的真实原文"
    assert db.excerpt(blocks[-1]["evidence_id"], 5)["excerpt"] == "只留在证据"
    again = await restarted.discover(platforms=["xiaohongshu"])
    assert again.new_count == 0


async def test_external_batch_and_discussion_use_evidence_pipeline(db: Database) -> None:
    pipe = pipeline_for(db, xiaohongshu=ReadingBackend("xiaohongshu"))
    batch = await pipe.prepare_batch(platforms=["xiaohongshu"])
    assert len(batch.candidates) == 1
    assert len(json.dumps(batch.to_payload(max_batch_chars=800), ensure_ascii=False)) <= 800
    key = batch.candidates[0].bvid
    page, n = await pipe.get_discussion(bvid=key, limit=2)
    assert n == 1 and page.replies[0].message == "真实评论"
    assert any(row["source_type"] == "comment" for row in db.evidence_for_bvid(key))
    author = db.query_one("SELECT author_id FROM content_comments WHERE content_key=?", (key,))
    assert author["author_id"] == "commenter-str"
    ok, code, _ = pipe.save_analysis(bvid=key, summary="已学习", score_total=80,
                                   evidence_ids=batch.evidence_ids, evidence_level="asr")
    assert ok, code
    ok, code, draft = pipe.save_analysis(bvid=key, summary="已学习", score_total=80,
                                       evidence_ids=batch.evidence_ids, draft_text="不要发B站")
    assert not ok and code == "REJECT_PLATFORM_READ_ONLY" and draft is None


async def test_platform_risk_is_persistent_but_does_not_block_other_platform(db: Database) -> None:
    xhs, dy = ReadingBackend("xiaohongshu"), ReadingBackend("douyin")
    xhs.error = "verification_required"
    pipe = pipeline_for(db, xiaohongshu=xhs, douyin=dy)
    result = await pipe.discover(platforms=["xiaohongshu", "douyin"])
    assert result.new_count == 1 and result.errors
    xhs.error = ""
    restarted = pipeline_for(db, xiaohongshu=xhs)
    with pytest.raises(PlatformError, match="verification_required"):
        await restarted.read_content("xiaohongshu", "new-id")
    assert xhs.calls == [("search", "DeepSeek Harness")]
    assert db.health_state() == "ready"


async def test_disabled_backend_never_reports_fake_empty_success(db: Database) -> None:
    pipe = pipeline_for(db)
    with pytest.raises(PlatformError):
        await pipe.read_content("douyin", "123")


async def test_account_scope_separates_cache_and_content(db: Database) -> None:
    from core.content import ContentReader
    from core.external_config import ExternalPlatformConfig

    backend = ReadingBackend("xiaohongshu")
    one = ContentReader(db, {"xiaohongshu": backend},
                        {"xiaohongshu": ExternalPlatformConfig(account_id="one")})
    two = ContentReader(db, {"xiaohongshu": backend},
                        {"xiaohongshu": ExternalPlatformConfig(account_id="two")})
    a = await one.discover("xiaohongshu", "DSH", days=7, limit=2)
    b = await two.discover("xiaohongshu", "DSH", days=7, limit=2)
    assert a[0].bvid != b[0].bvid
    await one.discover("xiaohongshu", "DSH", days=7, limit=2)
    assert len(backend.calls) == 2  # 第三次走自己账号的缓存


async def test_bilibili_write_entry_rejects_external_content_without_reading_identity(
    db: Database,
) -> None:
    from core.publish import PublishGate
    from tests.test_publish_gates import FakeReader, RecordingExecutor, publish_ready_config

    key = "douyin:default:123"
    db.upsert_video(bvid=key, title="外部内容")
    reader = FakeReader()
    gate = PublishGate(db=db, config=publish_ready_config(), reader=reader,
                       executor=RecordingExecutor())
    liked = await gate.publish_like(bvid=key)
    favorited = await gate.publish_favorite(bvid=key, folder_name="测试", media_id=123)
    draft = db.create_draft(draft_type="top", bvid=key, text="不能发到B站", evidence_ids=[])
    result = await gate.publish(draft_id=draft)
    assert [liked.code, favorited.code, result.code] == ["REJECT_PLATFORM_READ_ONLY"] * 3
    assert reader.calls == 0
    assert db.query("SELECT * FROM actions") == []


def test_v18_migration_preserves_old_evidence_and_notes(tmp_path: Any, monkeypatch: Any) -> None:
    from core import schema

    migrations = schema.MIGRATIONS
    monkeypatch.setattr(schema, "MIGRATIONS", tuple(row for row in migrations if row[0] <= 17))
    database = Database(tmp_path / "old.db")
    database.migrate()
    database.upsert_video(bvid="BVold", title="旧数据")
    evidence_id = database.add_evidence(bvid="BVold", source_type="subtitle", text="旧证据")
    monkeypatch.setattr(schema, "MIGRATIONS", migrations)
    assert database.migrate() == 18
    assert database.get_evidence(evidence_id)["text"] == "旧证据"
    database.add_evidence(bvid="BVold", source_type="ocr", text="第一页文字")
    assert database.query("PRAGMA foreign_key_check") == []
    assert database.migrate() == 18
    database.close()


async def test_direct_xhs_canonical_link_extracts_id_and_access_context(db: Database) -> None:
    backend = ReadingBackend("xiaohongshu")
    pipe = pipeline_for(db, xiaohongshu=backend)
    card, _ = await pipe.read_content(
        "xiaohongshu", "https://www.xiaohongshu.com/explore/same-id?xsec_token=provided",
    )
    assert card.native_id == "same-id"
    assert backend.calls[0] == ("detail", {"xsec_token": "provided"})


def test_equal_ocr_text_keeps_distinct_page_and_source_provenance(db: Database) -> None:
    from core.content import register_blocks

    key = "xiaohongshu:default:pages"
    db.upsert_video(bvid=key)
    blocks = register_blocks(db, key, {"evidence_blocks": [
        {"source_type": "ocr", "text": "同样的文字",
         "source_url": "https://cdn/1", "page_index": 1},
        {"source_type": "ocr", "text": "同样的文字",
         "source_url": "https://cdn/1", "page_index": 2},
    ]})
    assert len({block["evidence_id"] for block in blocks}) == 2
    assert [db.excerpt(block["evidence_id"], 10)["page_index"] for block in blocks] == [1, 2]
    again = register_blocks(db, key, {"evidence_blocks": [
        {"source_type": "ocr", "text": "同样的文字",
         "source_url": "https://cdn/1", "page_index": 1},
    ]})
    assert again[0]["evidence_id"] == blocks[0]["evidence_id"]


async def test_bridge_discovery_does_not_return_previous_accounts(
    db: Database, monkeypatch: Any,
) -> None:
    from bridge import server
    from core.content import ContentReader
    from core.external_config import ExternalPlatformConfig
    from core.runtime import Runtime

    backend = ReadingBackend("xiaohongshu")
    old = ContentReader(db, {"xiaohongshu": backend},
                        {"xiaohongshu": ExternalPlatformConfig(account_id="old")})
    await old.discover("xiaohongshu", "DSH", days=7, limit=1)
    config = make_config(external_platforms={"xiaohongshu": {"account_id": "current"}})
    queue = RequestQueue(db=db, config=config)
    pipe = Pipeline(db=db, config=config, reader=None, queue=queue,
                    adapters={"xiaohongshu": backend})
    monkeypatch.setattr(server, "_runtime", Runtime(config, db, queue, None, None, pipe))
    result = await server.discover_videos(platforms=["xiaohongshu"], keywords=["DSH"])
    candidates = result["data"]["candidates"]
    assert [item["bvid"] for item in candidates] == ["xiaohongshu:current:same-id"]


@pytest.mark.parametrize("message", ["HTTP 403 Forbidden", "forbidden", "platform_error"])
async def test_explicit_platform_denial_freezes_only_its_account(
    db: Database, message: str,
) -> None:
    from core.content import ContentReader
    from core.external_config import ExternalPlatformConfig
    from tests.test_external_platforms import Backend, adapter

    backend = Backend({"search_feeds": {"error": message}})
    reader = ContentReader(db, {"xiaohongshu": adapter("xiaohongshu", backend)})
    with pytest.raises(PlatformError) as first:
        await reader.discover("xiaohongshu", "first", days=7, limit=1)
    assert first.value.code == "platform_error"
    with pytest.raises(PlatformError) as second:
        await reader.discover("xiaohongshu", "second", days=7, limit=1)
    assert second.value.code == "platform_error"
    assert len(backend.calls) == 1
    row = db.query_one("SELECT state FROM content_health WHERE account_id='default'")
    assert row["state"] == "platform_error"
    another = ContentReader(db, {"xiaohongshu": ReadingBackend("xiaohongshu")},
                            {"xiaohongshu": ExternalPlatformConfig(account_id="other")})
    assert len(await another.discover("xiaohongshu", "valid", days=7, limit=1)) == 1
    assert db.health_state() == "ready"
