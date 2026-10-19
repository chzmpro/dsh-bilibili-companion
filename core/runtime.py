"""运行时组装：把 config / db / queue / reader / worker / pipeline 拼成一份。

``bridge`` 与 ``cli`` 都通过这里拿依赖，避免各自 new 一套（那会绕过统一请求队列）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.bilibili import BilibiliReader
from core.config import Config, load_config
from core.credentials import CredentialError, load_cookie
from core.external_platforms import build_external_adapters
from core.pipeline import Pipeline
from core.ratelimit import RequestQueue
from core.secrets import secret_fingerprint, secret_source
from core.storage import Database
from core.text_worker import TextWorker


@dataclass
class Runtime:
    config: Config
    db: Database
    queue: RequestQueue
    reader: BilibiliReader
    worker: TextWorker | None
    pipeline: Pipeline

    async def aclose(self) -> None:
        for key, adapter in self.pipeline.adapters.items():
            if key != "bilibili":
                await adapter.aclose()
        await self.queue.aclose()
        if self.worker is not None:
            await self.worker.aclose()
        self.db.close()


async def build_runtime(
    *,
    config_path: Path | str | None = None,
    db_path: Path | str | None = None,
    cookie_path: Path | str | None = None,
    with_worker: bool = True,
    queue: RequestQueue | None = None,
) -> Runtime:
    """组装一份运行时。

    **MCP 进程启动本身不访问 B 站**——这里只建对象、跑迁移、读本地文件。
    只有调用读取工具或已配置的定时任务才会发请求。
    """
    config = load_config(config_path)
    db = Database(db_path)
    db.migrate()

    cookies: dict[str, str] = {}
    try:
        bundle = load_cookie(cookie_path)
        cookies = bundle.values
    except CredentialError:
        # 没有凭据也要能只读启动（公开搜索不需要登录）
        cookies = {}

    reader_queue = queue or RequestQueue(db=db, config=config)
    reader = BilibiliReader(queue=reader_queue, cookies=cookies)
    worker = TextWorker(db=db, config=config) if with_worker else None
    adapters = build_external_adapters(config.external_platforms)
    pipeline = Pipeline(
        db=db, config=config, reader=reader, queue=reader_queue, worker=worker, adapters=adapters,
    )
    return Runtime(
        config=config,
        db=db,
        queue=reader_queue,
        reader=reader,
        worker=worker,
        pipeline=pipeline,
    )


def config_diagnostics(config: Config) -> dict[str, Any]:
    """``companion_status`` 用的配置诊断。**不含任何密钥值。**"""
    can_publish, publish_reason = config.can_publish()
    worker_ok, worker_code = config.text_worker_status()
    key_env = config.text_worker.api_key_env
    return {
        "agent_mid": config.account.agent_mid,
        "owner_mid": config.account.owner_mid,
        "owner_configured": config.account.owner_mid > 0,
        "keywords": config.discovery.keywords,
        "mode": config.interaction.mode,
        "dry_run": config.interaction.dry_run,
        "publish_enabled": config.interaction.publish_enabled,
        "pause": config.interaction.pause,
        "allow_third_party_threads": config.interaction.allow_third_party_threads,
        "publish_allowed": can_publish,
        "publish_reason": publish_reason,
        "text_worker_enabled": config.text_worker.enabled,
        "text_worker_usable": worker_ok,
        "text_worker_code": worker_code,
        "text_worker_model": config.text_worker.model,
        # ★ 只报变量名与来源/长度，**不报值**
        "text_worker_key_env": key_env,
        "text_worker_key_source": secret_source(key_env),
        "text_worker_key_fingerprint": secret_fingerprint(key_env),
        "max_candidates_to_dsh": config.text_worker.max_candidates_to_dsh,
        "max_excerpt_chars": config.text_worker.max_excerpt_chars,
    }
