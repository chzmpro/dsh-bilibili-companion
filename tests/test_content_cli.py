from __future__ import annotations

import json
from typing import Any

from typer.testing import CliRunner

from cli.app import app


def test_cli_discovery_and_read_return_short_evidence_not_raw(
    db: Any, monkeypatch: Any,
) -> None:
    from core.pipeline import Pipeline
    from core.ratelimit import RequestQueue
    from core.runtime import Runtime
    from tests.conftest import make_config
    from tests.test_content_pipeline import ReadingBackend

    config = make_config()
    queue = RequestQueue(db=db, config=config)
    pipeline = Pipeline(db=db, config=config, reader=None, queue=queue,
                        adapters={"xiaohongshu": ReadingBackend("xiaohongshu")})

    async def runtime(**kwargs: Any) -> Any:
        return Runtime(config, db, queue, None, None, pipeline)

    async def close(*args: Any) -> None:
        pass  # db fixture owns its connection; CLI still calls the real runtime lifecycle boundary.

    monkeypatch.setattr("core.runtime.build_runtime", runtime)
    monkeypatch.setattr(Runtime, "aclose", close)
    runner = CliRunner()
    found = runner.invoke(app, [
        "discover-content", "--platform", "xiaohongshu", "--keyword", "DSH",
    ])
    assert found.exit_code == 0, found.output
    key = json.loads(found.stdout)["candidates"][0]["bvid"]
    read = runner.invoke(app, ["read-content", key])
    assert read.exit_code == 0, read.output
    payload = json.loads(read.stdout)
    assert payload["platform"] == "xiaohongshu" and payload["evidence_ids"]
    assert "只留在证据内的真实原文" not in read.stdout


def test_human_can_inspect_and_explicitly_archive_blocked_outbox(db: Any, monkeypatch: Any) -> None:
    from core.storage import Database

    monkeypatch.setattr("cli.app._open_db", lambda: Database(db.path))
    run = db.start_run("host_start")
    db.save_cycle_delivery(run, session_id="duty", payload={"cards": []})
    db.update_cycle_delivery(run, state="dsh_error", prompt="不应在状态里泄露的整份材料")
    runner = CliRunner()
    status = runner.invoke(app, ["cycle-deliveries"])
    assert status.exit_code == 0, status.output
    assert run in status.output and "不应在状态里泄露" not in status.output
    missing_reason = runner.invoke(app, ["cycle-deliveries", "--abandon", run])
    assert missing_reason.exit_code != 0
    archived = runner.invoke(app, ["cycle-deliveries", "--abandon", run,
                                  "--reason", "本人已经核对，不再发送这批"])
    assert archived.exit_code == 0, archived.output
    assert db.pending_cycle_delivery() is None
    assert db.query_one("SELECT conclusion FROM runs WHERE run_id=?", (run,))[0] != "ok"
