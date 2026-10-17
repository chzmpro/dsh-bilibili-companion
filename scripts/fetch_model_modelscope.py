"""用 ModelScope 镜像把 Whisper 模型预下到本地（**不依赖 HF 的 API 端点**）。

## 为什么不能直接 `snapshot_download`

实测（2026-10-17）：ModelScope 只实现了 HF 的 **``/resolve/main/`` 文件路径**，
**没有** ``/api/models/...`` 元数据端点 —— 所以 ``huggingface_hub.snapshot_download``
会在拉元数据时 JSON 解析失败。

⇒ 正确做法：**自己按文件列表逐个下**，再**手工摆成 HF 缓存的标准目录结构**：

```
<hf_cache>/models--Systran--faster-whisper-tiny/
├── refs/main                      ← 内容是 commit hash
├── snapshots/<commit>/<文件>       ← 实体文件（我们直接放真文件，不用 symlink）
```

faster-whisper 读缓存时按这个结构找文件；**不用 symlink**（本机也不支持，
见 :func:`core.asr.supports_symlink_cache`），直接放实体文件即可。

## 实测速度（同一 model.bin，各给 12 秒）

==========================  ==============================
源                          结果
==========================  ==============================
``hf-mirror.com``          ❌ 超时 / 中途断流（32.6MB 卡死）
``modelscope.cn``          ✅ 75.5 MB，**8.9 MB/s**
==========================  ==============================

用法：
    ./.venv/Scripts/python.exe scripts/fetch_model_modelscope.py --model tiny
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.asr import hf_cache_dir  # noqa: E402

#: faster-whisper 需要的文件（缺 model.bin 就没法转写）
REQUIRED_FILES: tuple[str, ...] = (
    "config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.txt",
)

MIRROR = "https://modelscope.cn/models"
#: 拿不到 refs 时用的占位 commit（faster-whisper 只按 snapshots/*/ 找文件）
FALLBACK_COMMIT = "modelscope"


def download_file(url: str, target: Path, *, timeout: float = 300.0) -> int:
    """下一个文件，返回字节数。**流式**写，避免把大文件读进内存。"""
    import httpx

    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True, trust_env=False) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        with target.open("wb") as handle:
            for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                handle.write(chunk)
                written += len(chunk)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="用 ModelScope 镜像预下 Whisper 模型")
    parser.add_argument("--model", default="tiny", help="tiny/base/small/medium")
    parser.add_argument("--commit", default=FALLBACK_COMMIT, help="缓存目录用的 commit 名")
    args = parser.parse_args()

    repo = f"Systran/faster-whisper-{args.model}"
    cache = hf_cache_dir()
    # ★★ 必须放在 `hub/` 之下（2026-10-17 真机踩到第二次）：
    #    `huggingface_hub` 的缓存根是 `$HF_HOME/hub/`，不是 `$HF_HOME/`。
    #    第一版少了这一层 ⇒ 模型明明下好了，HF 却说
    #    `LocalEntryNotFoundError: Cannot find an appropriate cached snapshot folder`
    #    ⇒ 又去联网重下（而且用旧镜像会断流）。
    model_dir = cache / "hub" / f"models--Systran--faster-whisper-{args.model}"
    snapshot = model_dir / "snapshots" / args.commit

    print(f"缓存目录：{cache}")
    print(f"目标模型：{repo}")
    print(f"镜像：{MIRROR}\n")

    ok = True
    started = time.monotonic()
    for name in REQUIRED_FILES:
        url = f"{MIRROR}/{repo}/resolve/main/{name}"
        target = snapshot / name
        if target.exists() and target.stat().st_size > 0:
            print(f"  跳过 {name}（已存在 {target.stat().st_size / 1048576:.1f} MB）")
            continue
        try:
            t0 = time.monotonic()
            size = download_file(url, target)
            elapsed = max(time.monotonic() - t0, 0.001)
            print(
                f"  ✅ {name}: {size / 1048576:.1f} MB"
                f"（{elapsed:.1f}s，{size / elapsed / 1048576:.1f} MB/s）"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            ok = False

    # refs/main：内容是什么不重要（faster-whisper 按 snapshots/*/ 找），但要存在
    refs = model_dir / "refs" / "main"
    refs.parent.mkdir(parents=True, exist_ok=True)
    refs.write_text(args.commit, encoding="utf-8")

    # 写一份最小的 tree 元数据，免得 HF 库以为缓存损坏
    (model_dir / "trees").mkdir(parents=True, exist_ok=True)
    (model_dir / "trees" / f"{args.commit}.json").write_text(
        json.dumps({"files": list(REQUIRED_FILES)}, ensure_ascii=False),
        encoding="utf-8",
    )

    total = sum(
        (snapshot / name).stat().st_size
        for name in REQUIRED_FILES
        if (snapshot / name).exists()
    )
    print(f"\n合计 {total / 1048576:.1f} MB，耗时 {time.monotonic() - started:.1f}s")
    if not ok:
        print("有文件没下成功 —— 模型可能不完整，别急着跑转写", file=sys.stderr)
        return 1
    print(f"✅ 模型已就位：{snapshot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
