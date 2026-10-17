"""命令行：把一条 B 站视频（或本地音视频文件）转成文字。

等价于竞品 ``dsh-bili-scroll/lib/asr.py`` —— 用法照搬：:

    ./.venv/Scripts/python.exe scripts/asr_transcribe.py BV1xxxxxxxxx
    ./.venv/Scripts/python.exe scripts/asr_transcribe.py https://www.bilibili.com/video/BV1xx
    ./.venv/Scripts/python.exe scripts/asr_transcribe.py D:\\some\\clip.mp4 -o out.txt
    …还可以加 --model small / --model medium 换模型（默认 small）

行为约定（**与竞品一致**）：

* **默认不装依赖**：``faster-whisper`` / ``yt-dlp`` 缺失时给人话错误 + 退出码 3，
  绝不抛 traceback；
* ⭐ **空输出是一种结果**：这条视频没有语音（纯音乐 / 纯录屏）⇒
  标准输出为空 + **退出码 0**，只在标准错误里说明一句；
* 进度信息全部走标准错误，**标准输出只留正文**（便于重定向/管道）；
* 临时音频跑完就清掉（``core.asr`` 的 ``finally``）。

退出码：0 成功（含"没有语音"）/ 2 找不到文件 / 3 缺依赖 / 4 下载失败 / 5 其他错误 / 6 超时。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.asr import (  # noqa: E402
    DEFAULT_TIMEOUT_SEC,
    EXIT_FILE_NOT_FOUND,
    AsrResult,
    configure_stdio,
    default_model,
    exit_code_for,
    transcribe_target,
)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（抽出来是为了让测试断言参数口径）。"""
    parser = argparse.ArgumentParser(
        description="把一条视频转成文字（默认中文）。空输出 = 这条视频没有语音，不是故障。"
    )
    parser.add_argument("target", help="BV 号 / B站链接 / 本地音视频文件")
    parser.add_argument("-o", "--out", default="", help="写到这个文件（不给就打到标准输出）")
    parser.add_argument(
        "--model",
        default=default_model(),
        help="Whisper 模型大小：tiny / base / small / medium（默认 small，"
        "可用环境变量 BILI_DUTY_ASR_MODEL 覆盖）",
    )
    parser.add_argument(
        "--lang", default="zh", help="语言（默认 zh；不给就自动判断请传 auto）"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SEC,
        help=f"超时秒数（默认 {DEFAULT_TIMEOUT_SEC:.0f}；0 表示不限制，**不推荐**）",
    )
    return parser


def _emit(result: AsrResult, out_path: str) -> None:
    """输出正文：给了 ``-o`` 就写文件，否则打到标准输出。"""
    if out_path:
        Path(out_path).write_text(result.text, encoding="utf-8")
        print(f"[asr] 已写入：{out_path}", file=sys.stderr)
    else:
        print(result.text)


def _report(result: AsrResult) -> None:
    """失败/无语音的说明写在**标准错误**，标准输出保持干净。"""
    if not result.ok:
        print(f"[asr] {result.code}：{result.message}", file=sys.stderr)
    elif result.code == "NO_SPEECH":
        print(
            "[asr] 这条视频没有语音（纯音乐 / 纯录屏？）—— 属于结果，不是故障。",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    """入口。返回进程退出码（见模块 docstring）。"""
    configure_stdio()
    args = build_parser().parse_args(argv)

    if args.timeout > 0:
        # ★ 有界超时：默认走**子进程**路径（超时可真的杀掉）。
        #   代价是多一次解释器启动（约 0.2s），换来"绝不挂死"。
        import asyncio

        from core.asr import transcribe_async

        result = asyncio.run(
            transcribe_async(
                args.target, model=args.model, timeout_sec=args.timeout, lang=args.lang
            )
        )
    else:
        # ``--timeout 0``：调用方明确要不限时（本地长视频、要盯进度时）。
        # 同步路径没有硬超时，但 Ctrl-C 仍可中断，且临时音频照样被 finally 清掉。
        result = transcribe_target(args.target, model=args.model, lang=args.lang)

    if result.code == "ERROR" and "找不到音频文件" in result.message:
        print(f"[asr] {result.message}", file=sys.stderr)
        return EXIT_FILE_NOT_FOUND

    _emit(result, args.out)
    _report(result)
    return exit_code_for(result)


if __name__ == "__main__":
    raise SystemExit(main())
