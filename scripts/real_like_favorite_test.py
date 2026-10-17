"""真实点赞 + 收藏验收（**一次性**，跑完自动把配置改回安全值）。

用法::

    ./.venv/Scripts/python.exe scripts/real_like_favorite_test.py

它做四件事：
1. 备份当前 ``config.yaml``；
2. 临时打开 ``review`` + ``dry_run=false`` + ``publish_enabled=true``；
3. 走**完整门禁**真实点赞 + 收藏一个视频；
4. **无论成败**都把配置恢复原样，并打印前后对比。

⚠️ 这是**公开写入**：会在小号上留下真实的赞与收藏。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config  # noqa: E402
from core.credentials import load_cookie  # noqa: E402
from core.paths import default_config_path  # noqa: E402
from core.publish import PublishGate  # noqa: E402
from core.runtime import build_runtime  # noqa: E402

BVID = "BV1nFp46qECm"  # 任务书里的参考视频（已读过，评论/点赞均公开）
FOLDER = "工具与技术"


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _write_publish_config(path: Path) -> None:
    """把配置改成"允许真实发布"（临时）。"""
    text = path.read_text(encoding="utf-8")
    text = text.replace('mode: "observe"', 'mode: "review"', 1)
    text = text.replace("dry_run: true", "dry_run: false", 1)
    text = text.replace("publish_enabled: false", "publish_enabled: true", 1)
    path.write_text(text, encoding="utf-8")


async def run() -> int:
    _configure_stdio()
    config_path = default_config_path()
    backup = config_path.with_suffix(".yaml.real-test-backup")
    shutil.copy2(config_path, backup)
    print(f"已备份配置 → {backup.name}")

    exit_code = 0
    try:
        _write_publish_config(config_path)
        config = load_config(config_path)
        can, reason = config.can_publish()
        print(f"临时发布门禁: {'允许' if can else '拒绝'}（{reason}）")
        if not can:
            print("门禁未打开，放弃真实测试。", file=sys.stderr)
            return 2

        runtime = await build_runtime(with_worker=False)
        try:
            from core.bilibili_write import HttpWriteExecutor

            detail = await runtime.reader.get_video_detail(BVID)
            runtime.db.upsert_video(
                bvid=BVID, aid=detail.aid, title=detail.title,
                up_mid=detail.up_mid, up_name=detail.up_name,
                published_at=detail.published_at or None,
                duration_sec=detail.duration_sec or None,
                source_url=detail.source_url,
            )
            print(f"\n视频: {detail.title[:50]}\n  aid={detail.aid}")

            executor = HttpWriteExecutor(cookies=load_cookie())
            try:
                folders = await executor.list_favorite_folders(up_mid=executor._self_mid())
                gate = PublishGate(
                    db=runtime.db, config=config,
                    reader=runtime.reader, executor=executor,
                )
                media_id = gate.resolve_favorite_folder(
                    folder_name=FOLDER, folders=folders
                )
                print(f"收藏夹: {FOLDER} → media_id={media_id}")

                print("\n=== 真实点赞 ===")
                like = await gate.publish_like(bvid=BVID)
                print(f"  ok={like.ok} code={like.code} action={like.action_id}")
                if like.detail:
                    print(f"  detail={like.detail}")

                print("\n=== 真实收藏 ===")
                fav = await gate.publish_favorite(
                    bvid=BVID, folder_name=FOLDER, media_id=media_id
                )
                print(f"  ok={fav.ok} code={fav.code} action={fav.action_id}")
                if fav.detail:
                    print(f"  detail={fav.detail}")

                print("\n=== 落库状态 ===")
                state = runtime.db.get_video_state(BVID)
                print(f"  like_state={state['like_state']} fav_state={state['fav_state']} "
                      f"fav_media_ids={state['fav_media_ids']}")

                print("\n=== B站 侧回读 ===")
                print(f"  has_liked = {await executor.has_liked(bvid=BVID)}")
                after = await executor.list_favorite_folders(up_mid=executor._self_mid())
                for folder in after:
                    if str(folder.get("id")) == str(media_id):
                        print(f"  收藏夹 {folder.get('title')} count="
                              f"{folder.get('media_count')}")

                if not (like.ok and fav.ok):
                    exit_code = 1
            finally:
                await executor.aclose()
        finally:
            await runtime.aclose()
    finally:
        shutil.copy2(backup, config_path)
        backup.unlink(missing_ok=True)
        restored = load_config(config_path)
        can, reason = restored.can_publish()
        print(f"\n配置已恢复：mode={restored.interaction.mode} "
              f"dry_run={restored.interaction.dry_run} "
              f"publish_enabled={restored.interaction.publish_enabled}")
        print(f"发布门禁: {'允许' if can else '拒绝'}（{reason}）")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
