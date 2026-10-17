"""一次性脚本：把 Whisper tiny 模型下到项目的 HF 缓存（可观测进度）。

**为什么单独一个脚本**：`faster_whisper` 的下载进度被它自己的日志吞掉，
一旦卡住看不出进展。这里用 `huggingface_hub` 直接拉，明确报"下到多少 MB"。

用法：
    ./.venv/Scripts/python.exe scripts/prefetch_asr_model.py --model tiny
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import time
from pathlib import Path

# ★ 环境变量必须在 import huggingface_hub / faster_whisper **之前**设好
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.asr import configure_hf_environment, hf_cache_dir, prepare_runtime_dirs  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="预下 Whisper 模型（可观测进度）")
    parser.add_argument("--model", default="tiny", help="tiny/base/small/medium")
    args = parser.parse_args()

    configure_hf_environment()
    prepare_runtime_dirs()
    cache = hf_cache_dir()
    print(f"HF 缓存目录：{cache}")

    repo = f"Systran/faster-whisper-{args.model}"
    print(f"目标模型：{repo}")

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("缺 huggingface_hub（应该随 faster-whisper 一起装了）", file=sys.stderr)
        return 2

    started = time.monotonic()

    def size_of(path: Path) -> int:
        total = 0
        for item in path.rglob("*"):
            if item.is_file():
                with contextlib.suppress(OSError):
                    total += item.stat().st_size
        return total

    # 后台线程每 20 秒报一次"已下多少 MB" —— 卡住时一眼可见
    import threading

    stop = threading.Event()

    def reporter() -> None:
        while not stop.wait(20):
            mb = size_of(cache) / 1048576
            print(f"  …已下 {mb:.1f} MB（{time.monotonic() - started:.0f}s）", flush=True)

    thread = threading.Thread(target=reporter, daemon=True)
    thread.start()

    try:
        path = snapshot_download(repo_id=repo, cache_dir=str(cache))
    except Exception as exc:  # noqa: BLE001
        stop.set()
        print(f"下载失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        stop.set()

    elapsed = time.monotonic() - started
    mb = size_of(cache) / 1048576
    print(f"\n✅ 模型已就位：{path}")
    print(f"   缓存合计 {mb:.1f} MB，耗时 {elapsed:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
