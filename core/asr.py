"""本地语音转写（ASR）：把一条 B 站视频转成文字。**默认不引入 ~1GB 依赖。**

移植自竞品 ``dsh-bili-scroll`` 的 ``lib/asr.py``（138 行，**实测踩坑逐条保留**）。
本项目把它拆成两层：

* :func:`transcribe_target` —— **同步**能力，命令行入口
  （``scripts/asr_transcribe.py``）直接调用；
* :func:`transcribe_bvid` —— **异步**包装，转写跑在**独立子进程**里
  ⇒ 超时能**真的杀掉**（线程杀不掉；而且模型常驻几百 MB，
  不应该塞进长期运行的 MCP server 进程）。

## 竞品实测踩坑清单（**照抄，不是理论**）

1. ``HF_ENDPOINT`` / ``HF_HOME`` / ``HF_HUB_DISABLE_XET`` 必须在
   ``import faster_whisper`` **之前** ``setdefault`` ——
   否则它**已经去连 HuggingFace 了**。本机 ``~/.cache`` **拒绝写入**；
   Xet（HuggingFace 新版存储）**国内镜像不代理 ⇒ 401**。
2. 取音频用 ``yt-dlp``，``format='bestaudio/best'``，**不用 ffmpeg 合并**
   （B 站 DASH 已音视频分离）。
3. **沙箱适配三连**：``paths={'home': tmp, 'temp': tmp}``（别往 ``%TEMP%`` 写，沙箱拒绝）、
   ``nopart=True``（沙箱里"新建文件 + 改名"会被拒）、``cachedir=False``。
4. 临时音频文件名**用 BV 号**：竞品写死过 ``audio.m4a`` ⇒
   两轮同时跑互相覆盖，转出来的其实是另一个视频。
5. **刻意不用 ``mkdtemp``** —— 沙箱只允许写"子进程启动前就存在"的目录；
   目录由宿主**预建**（:func:`asr_tmp_dir`），文件名固定，跑完自己清。
6. ``WhisperModel(model, device='cpu', compute_type='int8')`` + ``vad_filter=True``。
7. 默认模型 ``small``，可用环境变量 ``BILI_DUTY_ASR_MODEL`` 覆盖。
8. ⭐ **空输出是一种结果，不是坏了**：转写返回空串 ⇒ 这条视频**没有语音**
   （纯音乐 / 纯录屏）⇒ ``ok=True`` + ``code=NO_SPEECH``；
   竞品的做法是**空串 + exit 0**，本模块保持同一语义，
   让调用方能把"没语音"和"ASR 坏了"分开。
9. ``sys.stdout.reconfigure(encoding='utf-8', errors='replace')`` ——
   Windows 默认 GBK，中文/emoji 打进管道会直接炸（见 :func:`configure_stdio`）。
10. ``finally`` 里清理临时音频。

## 与本机项目的**有意不同**（其余照抄）

* ``HF_HOME`` 落在 :func:`core.paths.data_dir` 下的 ``.hf-cache``：
  竞品放在自己的脚本目录（= 插件家）；本项目的数据目录是**探测出来的**
  （``%LOCALAPPDATA%`` 与 ``%APPDATA%`` 本机都不可写，
  见 ``docs/compatibility.md`` §7.5），所以不能用写死的"插件家"。
* 依赖检查**提前到下载之前**（竞品是先下音频、再 import 模型）：
  少一次白下载，结果不变。
* 失败一律变成 :class:`AsrResult`（**不抛异常**），
  并给出人话 ``message`` 与原因码 —— 与 ``core/text_worker.py`` 的错误码风格一致。
* 真正崩掉的转写线程/进程不会把调用方挂死：异步入口用**子进程 + 超时**兜底。

## 默认关闭

ASR 会拖下约 1GB 依赖（``faster-whisper`` + Whisper ``small`` 模型约 538MB），
所以它**不在** ``pyproject.toml`` 的必需依赖里，代码路径也只在**显式调用**时才跑：
``is_available()`` 发现依赖缺失就返回 ``(False, "NO_DEPENDENCY")``，**不会**去装、不会去下。

想彻底关掉（例如主人不想让任何进程碰 HuggingFace）：
把环境变量 ``COMPANION_ASR_ENABLED`` 置为 ``0`` / ``false`` / ``no`` / ``off``，
``is_available()`` 便返回 ``(False, "ASR_DISABLED")``。
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core import paths

__all__ = [
    "ASR_LEVEL",
    "CODE_ASR_DISABLED",
    "CODE_DOWNLOAD_FAILED",
    "CODE_ERROR",
    "CODE_NO_DEPENDENCY",
    "CODE_NO_SPEECH",
    "CODE_OK",
    "CODE_TIMEOUT",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT_SEC",
    "MODEL_ENV",
    "AsrResult",
    "asr_tmp_dir",
    "availability_message",
    "configure_hf_environment",
    "configure_stdio",
    "check_dependency_versions",
    "default_model",
    "exit_code_for",
    "fetch_audio",
    "hf_cache_dir",
    "is_available",
    "missing_dependencies",
    "prepare_runtime_dirs",
    "status",
    "to_url",
    "transcribe_async",
    "transcribe_bvid",
    "transcribe_target",
    "worker_main",
]

#: 证据等级：与 :data:`core.pipeline.LEVEL_ASR` **必须一致**
#: （见 ``tests/test_asr.py`` 的等值断言）。
ASR_LEVEL = "asr"

# ---- 原因码（公开 API 的 ``AsrResult.code``）---------------------------------
CODE_OK = "OK"
#: 空串 = 这次**没有语音**，不是失败（竞品：空串 + exit 0）
CODE_NO_SPEECH = "NO_SPEECH"
CODE_NO_DEPENDENCY = "NO_DEPENDENCY"
CODE_DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
CODE_TIMEOUT = "TIMEOUT"
CODE_ERROR = "ERROR"

#: 仅 :func:`is_available` 会返回（``AsrResult.code`` 里不出现）
CODE_ASR_DISABLED = "ASR_DISABLED"
#: ★ 依赖装了但**版本不兼容**（2026-10-17 实测新增：
#: 裸装最新版 av / huggingface_hub 会报 metadata_errors，转写必失败）
CODE_BAD_VERSION = "BAD_VERSION"

#: 依赖 / 状态码 → 子进程退出码（竞品 ``asr.py`` 用 2 = 找不到文件）
EXIT_OK = 0
EXIT_FILE_NOT_FOUND = 2
EXIT_NO_DEPENDENCY = 3
EXIT_DOWNLOAD_FAILED = 4
EXIT_ERROR = 5
EXIT_TIMEOUT = 6

DEFAULT_MODEL = "small"
MODEL_ENV = "BILI_DUTY_ASR_MODEL"
ENABLED_ENV = "COMPANION_ASR_ENABLED"

#: 竞品默认超时口径：一次转写最长 15 分钟（**必须有界，不能挂死**）
DEFAULT_TIMEOUT_SEC = 900.0

#: ⭐ 必须与竞品**逐字一致**：国内镜像 + 关掉 Xet
#:
#: ⚠️ **2026-10-17 实测修正**（比竞品更进一步）：
#: ``hf-mirror.com`` 在本机**极慢且会中途断流** —— 实测下到 32.6 MB / 75 MB
#: 后**连接消失、文件 10 分钟不再增长**（不是慢，是断了）。
#:
#: 实测三个源拉同一个 ``model.bin``（各给 12 秒）：
#:
#: ==========================  ==============================
#: 源                          12 秒结果
#: ==========================  ==============================
#: ``hf-mirror.com``          ❌ 超时
#: ``huggingface.co``         ❌ 不通（本机已知）
#: **``modelscope.cn``**      ✅ **75.5 MB，8.9 MB/s**（一次就下完）
#: ==========================  ==============================
#:
#: ModelScope（魔搭，国内官方）完整实现了 HF 的 ``/resolve/main/`` 路径协议
#: （``config.json`` / ``tokenizer.json`` / ``vocabulary.txt`` / ``model.bin``
#: 四个文件实测全部 HTTP 200），因此可以直接当 ``HF_ENDPOINT`` 用，
#: **快约 90 倍**。
#:
#: ⇒ 默认改用 ModelScope；``HF_ENDPOINT`` 已设时**不覆盖**（尊重用户选择）。
HF_ENDPOINT_MIRROR = "https://modelscope.cn/models"
#: 备选镜像（实测较慢且会断流，仅作兜底记录）
HF_ENDPOINT_FALLBACK = "https://hf-mirror.com"
HF_CACHE_DIR_NAME = ".hf-cache"
BILIBILI_REFERER = "https://www.bilibili.com"
BILIBILI_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

#: 依赖体积提示（写进状态输出，让主人知道为什么默认不装）
MODEL_DOWNLOAD_HINT = "faster-whisper + Whisper small（模型约 538MB，首次转写时下载）"

#: ★★ 依赖版本约束（**2026-10-17 真机踩到，必须钉住**）
#:
#: ``faster-whisper`` 的依赖声明**过宽**（``av>=11`` / ``huggingface-hub>=0.21``），
#: 装最新版会直接**跑不起来**：
#:
#: ====================  ==========  ============================================
#: 包                    装到        症状
#: ====================  ==========  ============================================
#: ``av``               **19.0.1**  ``open() got an unexpected keyword argument
#:                                  'metadata_errors'`` ——
#:                                  ``faster_whisper/audio.py`` 仍在传这个参数，
#:                                  而新版 PyAV 已移除它 ⇒ **转写必失败**
#: ``huggingface_hub``  **2.2.0**   同类问题（``metadata_errors`` 被移除）
#: ====================  ==========  ============================================
#:
#: **实测可用的组合**（本机跑通真实转写）：
#:
#: * ``faster-whisper==1.2.1``
#: * ``av==15.1.0``（``<16``）
#: * ``huggingface-hub==1.33.0``（``<2``）
#:
#: ⇒ 安装时按 :data:`ASR_REQUIREMENTS` 来，**别裸装最新版**。
ASR_REQUIREMENTS: tuple[str, ...] = (
    "faster-whisper",
    "yt-dlp",
    "av<16",
    "huggingface_hub<2",
)

#: 安装提示（把上面的约束写进人话，供 status / 错误信息引用）
ASR_INSTALL_HINT = (
    "pip install faster-whisper yt-dlp 'av<16' 'huggingface_hub<2'"
    "（⚠️ 别装 av>=16 / huggingface_hub>=2：faster-whisper 还在用被移除的 "
    "metadata_errors 参数，会报 open() got an unexpected keyword argument）"
)

_REQUIRED_MODULES: tuple[str, ...] = ("faster_whisper", "yt_dlp")

_DISABLED_TOKENS = frozenset({"0", "false", "no", "off"})

#: 子进程启动串：把项目根塞进 ``sys.path``，再跑 :func:`worker_main`。
#: 路径用 ``as_posix()`` 规范化 —— ``repr`` 一个含反斜杠的 Windows 路径会写成
#: ``'D:\\\\a\\\\b'``，虽然能跑但读起来容易看错；正斜杠在 Windows 上同样有效。
_WORKER_BOOTSTRAP = (
    "import sys; sys.path.insert(0, {root!r}); "
    "from core.asr import worker_main; raise SystemExit(worker_main(sys.argv[1:]))"
)


@dataclass(frozen=True)
class AsrResult:
    """一次转写的结果。

    ``text`` 为空串表示**这次没有语音**（``code=NO_SPEECH``，``ok=True``），
    **不是**失败 —— 调用方据此登记"跳过（无语音）"，而不是重试。
    """

    ok: bool
    text: str
    code: str
    message: str = ""
    duration_sec: float = 0.0


def configure_stdio() -> None:
    """把标准输出/错误钉成 UTF-8。

    Windows 上 Python 默认按 GBK 编/解码标准输出 —— 中文或 emoji 打进管道会炸
    （竞品原注释：有用户反馈装依赖 / 跑转写时报 ``UnicodeDecodeError`` 或乱码）。
    拿不到 ``reconfigure``（如已被重定向的流）时**静默跳过**，绝不因此失败。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(Exception):
            reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 环境变量：⭐ 必须在 import faster_whisper **之前**生效
# ---------------------------------------------------------------------------


def hf_cache_dir() -> Path:
    """模型缓存目录（本机 ``~/.cache`` **拒绝写入**，所以放在数据目录下）。"""
    return paths.data_dir() / HF_CACHE_DIR_NAME


def asr_tmp_dir() -> Path:
    """临时音频目录。

    ⚠️ **刻意不用 ``mkdtemp``**：沙箱只允许写"子进程启动前就存在"的目录，
    因此这个目录由宿主**预建**（本函数每次都会 ``mkdir``），文件名固定为 BV 号。
    """
    directory = paths.data_dir() / "tmp" / "asr"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def prepare_runtime_dirs() -> Path:
    """预建运行时目录，并把**整进程的临时目录**钉到可写位置。

    竞品原注释：值班那一轮跑在沙箱里（只允许写工作区/cwd），
    yt-dlp 默认往 ``%TEMP%`` 写、会被直接拒掉
    （``unable to open for writing: [Errno 13] Permission denied``）。
    所以除了 yt-dlp 的 ``paths`` 选项，这里还把 ``tempfile.tempdir`` 一起钉住。

    Returns:
        可写的临时音频目录。
    """
    directory = asr_tmp_dir()
    hf_cache_dir().mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        tempfile.tempdir = str(directory)
    return directory


#: 已知**会断流**的镜像（实测，2026-10-17）。
#:
#: ``hf-mirror.com`` 在本机下到 32.6 MB / 75 MB 后**连接消失**，
#: 10 分钟不再增长（不是慢，是断了）。用 ``small`` 模型（483 MB）时
#: 表现更糟：下到 61.8 MB 就 `peer closed connection`。
#:
#: ⇒ 即使环境变量把它设成这个值，也要**纠正**成 ModelScope，
#: 否则用户会遇到"转写卡住半小时然后失败"，而**根本不知道是镜像的问题**。
_KNOWN_BAD_ENDPOINTS: frozenset[str] = frozenset(
    {
        "https://hf-mirror.com",
        "http://hf-mirror.com",
        "https://hf-mirror.com/",
    }
)

#: 归一化（去末尾斜杠、转小写）后的集合，供 `configure_hf_environment` 比对
_KNOWN_BAD_NORMALIZED: frozenset[str] = frozenset(
    value.rstrip("/").lower() for value in _KNOWN_BAD_ENDPOINTS
)


def configure_hf_environment(
    env: MutableMapping[str, str] | None = None,
) -> MutableMapping[str, str]:
    """设置 HuggingFace 镜像相关环境变量。

    ★ **对"已知坏掉的镜像"做纠正，其余尊重既有设置。**

    抽成纯函数是为了能断言"**在 import faster_whisper 之前**就已设好"。

    * ``HF_ENDPOINT`` —— 默认 :data:`HF_ENDPOINT_MIRROR`（ModelScope）。
      ⚠️ 若环境里是 :data:`_KNOWN_BAD_ENDPOINTS` 里的值（``hf-mirror.com``），
      **改成 ModelScope 并打一行警告** —— 理由见 :data:`_KNOWN_BAD_ENDPOINTS`：
      那个镜像在本机**会中途断流**，用户只会看到"转写卡住然后失败"，
      而**不可能猜到是镜像问题**（2026-10-17 真机踩到：483 MB 模型下到 61.8 MB 断掉）。
    * ``HF_HOME=<数据目录>/.hf-cache`` —— 默认的 ``~/.cache`` 在本机拒绝写入；
    * ``HF_HUB_DISABLE_XET=1`` —— Xet 通道国内镜像不代理 ⇒ **401**，关掉走老 CDN；
    * ``HF_HUB_DISABLE_SYMLINKS_WARNING=1`` —— 本机 Windows 未开开发者模式 ⇒
      不支持 symlink，HF 会降级成复制并每次刷长警告。
      ⚠️ 这里**只静音警告，不改变降级行为** —— 真正要让人知道的
      "这台机器不支持 symlink"由 :func:`status()` 如实报出来（``symlink_cache``）。

    Args:
        env: 要写入的映射；默认 ``os.environ``。

    Returns:
        同一个映射对象（便于链式断言）。
    """
    target: MutableMapping[str, str] = os.environ if env is None else env

    existing = target.get("HF_ENDPOINT", "").strip()
    # 归一化后判断（容忍末尾斜杠与大小写）
    normalized = existing.rstrip("/").lower()
    if normalized and normalized in _KNOWN_BAD_NORMALIZED:
        # 已知坏镜像 → 纠正（并留一行可解释的警告，**不静默改**）
        print(
            f"[asr] ⚠️ HF_ENDPOINT={existing} 在本机会中途断流，"
            f"已改用 {HF_ENDPOINT_MIRROR}",
            file=sys.stderr,
        )
        target["HF_ENDPOINT"] = HF_ENDPOINT_MIRROR
    else:
        target.setdefault("HF_ENDPOINT", HF_ENDPOINT_MIRROR)

    target.setdefault("HF_HOME", str(hf_cache_dir()))
    target.setdefault("HF_HUB_DISABLE_XET", "1")
    target.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    return target


def supports_symlink_cache(directory: Path | None = None) -> bool:
    """本机是否支持 symlink（决定 HF 缓存是"软链共享"还是"复制多占磁盘"）。

    **实测背景**（2026-10-17）：Windows 未开开发者模式时，
    ``huggingface_hub`` 会打一段长警告并降级。与其让用户从警告里猜，
    不如直接探测一次并在 ``status()`` 里报出来。

    做法：在一个已存在的目录里**真建一个软链再删掉**（最可靠）。
    """
    target_dir = Path(directory) if directory is not None else hf_cache_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    probe_source = target_dir / ".symlink-probe-source"
    probe_link = target_dir / ".symlink-probe-link"
    try:
        probe_source.write_text("probe", encoding="utf-8")
        try:
            if probe_link.exists() or probe_link.is_symlink():
                probe_link.unlink()
            probe_link.symlink_to(probe_source)
            return True
        except (OSError, NotImplementedError):
            return False
        finally:
            with contextlib.suppress(OSError):
                if probe_link.is_symlink():
                    probe_link.unlink()
            with contextlib.suppress(OSError):
                probe_source.unlink()
    except OSError:
        return False


def check_dependency_versions() -> tuple[bool, str]:
    """检查已装的依赖版本是否**兼容**（返回 ``(ok, 说明)``）。

    ★ 为什么要单独查（2026-10-17 真机踩到）：``faster-whisper`` 的依赖声明过宽，
    装最新版 ``av`` / ``huggingface_hub`` 会**直接跑不起来**，
    而报错信息（``open() got an unexpected keyword argument``）
    **完全看不出是版本问题**。

    ⇒ 与其等转写时才炸，不如在 ``status()`` 里提前说清楚。
    """
    problems: list[str] = []

    # av：>=16 移除了 metadata_errors
    try:
        import av

        major = int(str(av.__version__).split(".")[0])
        if major >= 16:
            problems.append(
                f"av=={av.__version__} 太新（faster-whisper 需要 av<16，"
                "新版移除了 metadata_errors 参数）"
            )
    except ImportError:
        pass  # 缺依赖由 is_available 报
    except (AttributeError, ValueError):
        pass

    # huggingface_hub：>=2 同类问题
    try:
        import huggingface_hub

        major = int(str(huggingface_hub.__version__).split(".")[0])
        if major >= 2:
            problems.append(
                f"huggingface_hub=={huggingface_hub.__version__} 太新"
                "（建议 <2，避免 metadata_errors 类不兼容）"
            )
    except ImportError:
        pass
    except (AttributeError, ValueError):
        pass

    if problems:
        return False, "；".join(problems) + f"。修法：{ASR_INSTALL_HINT}"
    return True, "OK"


def default_model() -> str:
    """默认模型：``small``，可被 ``BILI_DUTY_ASR_MODEL`` 覆盖（竞品口径）。"""
    return os.environ.get(MODEL_ENV, DEFAULT_MODEL).strip() or DEFAULT_MODEL


# ---------------------------------------------------------------------------
# 依赖可用性
# ---------------------------------------------------------------------------


def _module_available(name: str) -> bool:
    """依赖是否可用。

    先看 ``sys.modules``（测试注入的假模块也算），再用 ``find_spec``，
    **不真的 import** —— import ``faster_whisper`` 会拉起 ctranslate2 并可能去连
    HuggingFace，``companion_status`` 那种只读路径不该付这个代价。
    """
    if name in sys.modules:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def missing_dependencies() -> list[str]:
    """缺失的可选依赖名列表（**给人看的顺序固定**）。"""
    return [name for name in _REQUIRED_MODULES if not _module_available(name)]


def _env_enabled() -> bool:
    """``COMPANION_ASR_ENABLED`` 没被显式关掉就算启用（缺依赖时另有 NO_DEPENDENCY）。"""
    return os.environ.get(ENABLED_ENV, "").strip().lower() not in _DISABLED_TOKENS


def is_available() -> tuple[bool, str]:
    """ASR 能不能用。返回 ``(可用, 原因码)``，**不抛异常**。

    原因码：``OK`` / ``ASR_DISABLED`` / ``NO_DEPENDENCY`` / ``BAD_VERSION``。
    人话解释见 :func:`availability_message`。
    """
    if not _env_enabled():
        return False, CODE_ASR_DISABLED
    if missing_dependencies():
        return False, CODE_NO_DEPENDENCY
    # ★ 依赖装了但版本不兼容 —— 单独一个码，否则会等到转写才炸
    versions_ok, _ = check_dependency_versions()
    if not versions_ok:
        return False, CODE_BAD_VERSION
    return True, CODE_OK


def availability_message(code: str) -> str:
    """原因码 → 人话（**不含任何凭据**，可安全进 ``companion_status``）。"""
    if code == CODE_OK:
        return ""
    if code == CODE_ASR_DISABLED:
        return f"ASR 已被 {ENABLED_ENV} 显式关闭。"
    if code == CODE_NO_DEPENDENCY:
        missing = "、".join(missing_dependencies()) or "（无）"
        return (
            f"ASR 不可用：未安装 {missing}。这是**默认状态**（约 1GB 依赖，不强制安装）。"
            f"需要时执行：{ASR_INSTALL_HINT}"
            f"；首次转写还会自动下模型（{MODEL_DOWNLOAD_HINT}）。"
        )
    if code == CODE_BAD_VERSION:
        _, note = check_dependency_versions()
        return f"ASR 不可用：依赖版本不兼容 —— {note}"
    return f"ASR 状态未知：{code}"


def status() -> dict[str, object]:
    """给 ``companion_status`` 用的可安全输出诊断（**不含任何凭据**）。"""
    usable, code = is_available()
    versions_ok, versions_note = check_dependency_versions()
    return {
        "available": usable,
        "code": code,
        "message": availability_message(code),
        "model": default_model(),
        "missing": missing_dependencies(),
        # ★ 版本兼容性（实测：裸装最新版 av / huggingface_hub 会跑不起来）
        "versions_ok": versions_ok,
        "versions_note": versions_note,
        "requirements": list(ASR_REQUIREMENTS),
        "tmp_dir": str(asr_tmp_dir()),
        "hf_cache_dir": str(hf_cache_dir()),
        # ★ 本机是否支持 symlink 缓存（实测：Windows 未开开发者模式 ⇒ False，
        #   此时 HF 会降级成"复制"，模型多占一份磁盘）。
        "symlink_cache": supports_symlink_cache(),
        "dependency_hint": MODEL_DOWNLOAD_HINT,
    }


# ---------------------------------------------------------------------------
# 音频获取（yt-dlp）
# ---------------------------------------------------------------------------


def to_url(target: str) -> str:
    """``BVxxxx`` → 完整视频链接；其他原样返回（竞品同名函数）。"""
    text = target.strip()
    if text.upper().startswith("BV") and len(text) <= 20:
        return "https://www.bilibili.com/video/" + text
    return text


def _looks_remote(target: str) -> bool:
    text = target.strip()
    return text.lower().startswith("http") or text.upper().startswith("BV")


def _sanitize_tag(name: str) -> str:
    """把 BV 号变成安全的文件名标签（竞品：去掉扩展名 + 白名单过滤 + 截断 40）。"""
    tag = os.path.basename(name).rsplit(".", 1)[0] or "clip"
    tag = re.sub(r"[^0-9A-Za-z_-]", "_", tag)[:40]
    return tag or "clip"


def fetch_audio(url: str, tmp: str, tag: str) -> str:
    """用 yt-dlp 把音频拉下来，返回落盘路径。

    B 站是 DASH **音视频分离**，取 ``bestaudio`` 即可，**不需要 ffmpeg 合并**。

    三个沙箱适配参数（竞品踩过，逐条保留）：

    * ``paths={'home': tmp, 'temp': tmp}`` —— 别让 yt-dlp 往 ``%TEMP%`` 写（沙箱会拒）；
    * ``nopart: True`` —— 不写 ``.part`` 再改名（沙箱里"新建文件 + 改名"会被拒）；
    * ``cachedir: False`` —— 顺手别去动用户目录里的缓存。

    文件名用 ``tag``（= BV 号）：写死名字会让并发的两轮互相覆盖。

    ★ **本机实测新增（2026-10-17，竞品没有这条）**：``HTTP 416 Range Not Satisfiable``。

    现象：上一轮中途失败会留下**同名的残缺音频**，下一次 yt-dlp 做
    **断点续传**时带上 ``Range: bytes=N-``，而 B 站的 CDN 对那个范围回 **416**
    ⇒ 整次下载直接失败，而且**每次重试都失败**（残留还在）。

    处理（两道）：

    1. **下载前先清掉同 tag 的残留**（``_cleanup_tmp``）—— 断根；
    2. 若仍遇到 416，**清干净再重试一次**（应对"这一轮自己产生的残留"）。
    """
    from yt_dlp import YoutubeDL  # 延迟 import：缺依赖时由调用方给人话错误

    opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmp, tag + ".%(ext)s"),
        "paths": {"home": tmp, "temp": tmp},
        "cachedir": False,
        "nopart": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": {
            "Referer": BILIBILI_REFERER,
            "User-Agent": BILIBILI_USER_AGENT,
        },
    }

    # ★ 第 1 道：下载前清残留（416 的根因就是它）
    _cleanup_tmp(Path(tmp), tag)

    def _attempt() -> str:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return str(ydl.prepare_filename(info))

    try:
        return _attempt()
    except Exception as exc:  # noqa: BLE001
        # ★ 第 2 道：只在 416（range 不满足）时清干净重试一次，别的错误直接上抛
        if "416" not in str(exc):
            raise
        _cleanup_tmp(Path(tmp), tag)
        return _attempt()


def _cleanup_tmp(tmp: Path, tag: str) -> None:
    """``finally`` 里清掉这一轮的临时音频（竞品第 10 条）。"""
    for path in glob.glob(os.path.join(str(tmp), tag + ".*")):
        with contextlib.suppress(OSError):
            os.remove(path)


# ---------------------------------------------------------------------------
# 转写本体（同步）
# ---------------------------------------------------------------------------


class _AsrFailure(Exception):
    """内部失败：带原因码，由 :func:`transcribe_target` 转成 :class:`AsrResult`。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message or code)


def _local_model_dir(model: str) -> Path | None:
    """找本地已下载的 faster-whisper 模型目录（找不到返回 None）。

    结构（本机实测）::

        .local-data/.hf-cache/hub/models--Systran--faster-whisper-small/
            snapshots/modelscope/{config.json, model.bin, tokenizer.json, vocabulary.txt}

    ★ 为什么必须找本地：`HF_ENDPOINT` 指向 ModelScope 后，
    `WhisperModel("small")` 走下载会失败（ModelScope 没有元数据端点）。
    详见 `_transcribe_file` 里的注释。
    """
    if not model:
        return None
    # 已经是本地目录就直接用
    candidate = Path(model)
    if candidate.is_dir() and (candidate / "model.bin").is_file():
        return candidate

    home = os.environ.get("HF_HOME", "").strip()
    roots: list[Path] = []
    if home:
        roots.append(Path(home))
    try:
        from core.paths import data_dir

        roots.append(data_dir() / ".hf-cache")
    except Exception:  # noqa: BLE001
        pass
    roots.extend([Path.home() / ".cache" / "huggingface"])

    repo = f"models--Systran--faster-whisper-{model}"
    for root in roots:
        base = root / "hub" / repo / "snapshots"
        if not base.is_dir():
            continue
        for snapshot in sorted(base.iterdir(), reverse=True):
            if (snapshot / "model.bin").is_file():
                return snapshot
    return None


def _transcribe_file(path: str, *, model: str, lang: str) -> str:
    """加载模型并转写，返回**纯文本**（可能是空串）。

    ⭐ ``configure_hf_environment()`` 必须在 ``import faster_whisper`` **之前**：
    否则它已经去连 HuggingFace 了（竞品第一条踩坑）。
    """
    configure_hf_environment()
    from faster_whisper import WhisperModel

    # ★★ 优先用**本地已下载**的模型目录（2026-10-18 真机踩到）
    #
    # 现象：`WhisperModel("small")` 走下载路径，报
    #     `加载 Whisper 模型失败：Expecting value: line 1 column 1 (char 0)`
    # 根因：`HF_ENDPOINT` 被改成 ModelScope，而 **ModelScope 没有 `/api/models/`
    #       元数据端点** ⇒ `huggingface_hub` 去解析 JSON 时拿到空/HTML ⇒ 解析失败。
    #       （`AGENTS.md` 的「网络通道」一节早记过这个坑）
    #
    # 但本机 `.local-data/.hf-cache/hub/models--Systran--faster-whisper-<model>/`
    # 里**模型其实已经下好了**。所以只要把本地目录直接传给 WhisperModel，
    # 就完全绕开下载和那个坏掉的元数据接口。
    local = _local_model_dir(model)
    target = str(local) if local is not None else model
    if local is not None:
        print(f"[asr] 用本地模型 {local}", file=sys.stderr)
    else:
        print(f"[asr] 加载模型 {model}（本地没有，会去下）…", file=sys.stderr)
    try:
        engine = WhisperModel(target, device="cpu", compute_type="int8")
    except Exception as exc:  # 模型下载失败 / 磁盘满 / 显存问题……
        raise _AsrFailure(CODE_ERROR, f"加载 Whisper 模型失败：{exc}") from exc

    print("[asr] 开始转写…", file=sys.stderr)
    try:
        # ``segments`` 是**惰性生成器**：join 的时候才真的开始转写
        segments, _info = engine.transcribe(
            path,
            language=None if lang == "auto" else lang,
            vad_filter=True,  # 掐掉静音段，省时间也少出错
        )
        return "".join(segment.text for segment in segments).strip()
    except Exception as exc:
        raise _AsrFailure(CODE_ERROR, f"转写失败：{exc}") from exc


def transcribe_target(
    target: str, *, model: str = DEFAULT_MODEL, lang: str = "zh"
) -> AsrResult:
    """**同步**转写一条视频（BV 号 / B 站链接 / 本地音视频文件）。

    **不抛异常**：所有失败都变成带原因码的 :class:`AsrResult`。

    Args:
        target: BV 号、B 站链接，或本地文件路径。
        model: Whisper 模型大小；空串表示用 :func:`default_model`（读环境变量）。
        lang: 语言，``zh`` 为默认；传 ``auto`` 让模型自己判断。

    Returns:
        :class:`AsrResult`；``text`` 为空串时 ``code=NO_SPEECH`` 且 ``ok=True``。
    """
    started = time.monotonic()

    usable, availability_code = is_available()
    if not usable:
        return AsrResult(
            ok=False,
            text="",
            code=CODE_NO_DEPENDENCY,
            message=availability_message(availability_code),
            duration_sec=_elapsed(started),
        )

    if not target.strip():
        return AsrResult(
            ok=False,
            text="",
            code=CODE_ERROR,
            message="未给出视频（BV 号 / 链接 / 文件路径都为空）。",
            duration_sec=_elapsed(started),
        )

    effective_model = model.strip() or default_model()
    tmp = prepare_runtime_dirs()
    tag = _sanitize_tag(target)

    src = target
    try:
        if _looks_remote(target):
            print("[asr] 先下音频…", file=sys.stderr)
            try:
                src = fetch_audio(to_url(target), str(tmp), tag)
            except Exception as exc:
                return AsrResult(
                    ok=False,
                    text="",
                    code=CODE_DOWNLOAD_FAILED,
                    message=f"下载音频失败（{type(exc).__name__}）：{exc}",
                    duration_sec=_elapsed(started),
                )

        if not os.path.exists(src):
            return AsrResult(
                ok=False,
                text="",
                code=CODE_ERROR,
                message=f"找不到音频文件：{src}",
                duration_sec=_elapsed(started),
            )

        try:
            text = _transcribe_file(src, model=effective_model, lang=lang).strip()
        except _AsrFailure as exc:
            return AsrResult(
                ok=False,
                text="",
                code=exc.code,
                message=str(exc),
                duration_sec=_elapsed(started),
            )
    finally:
        # 临时音频不留在磁盘上
        _cleanup_tmp(tmp, tag)

    if not text:
        # ⭐ 空输出是一种结果，不是坏了：这条视频没有语音（纯音乐 / 纯录屏）
        return AsrResult(
            ok=True,
            text="",
            code=CODE_NO_SPEECH,
            message="这条视频没有语音（纯音乐 / 纯录屏？）—— 属于结果，不是故障。",
            duration_sec=_elapsed(started),
        )
    return AsrResult(
        ok=True, text=text, code=CODE_OK, message="", duration_sec=_elapsed(started)
    )


# ---------------------------------------------------------------------------
# 异步入口：跑在子进程里，超时可真的杀掉
# ---------------------------------------------------------------------------


def _worker_argv(target: str, *, model: str, lang: str) -> list[str]:
    """构造转写子进程的命令行（``--json`` 回传结构化结果）。"""
    bootstrap = _WORKER_BOOTSTRAP.format(root=paths.project_root().as_posix())
    return [
        sys.executable,
        "-c",
        bootstrap,
        target,
        "--model",
        model,
        "--lang",
        lang,
        "--json",
    ]


def _worker_env() -> dict[str, str]:
    """子进程环境：继承 + 显式补 HF 三件套 + ``PYTHONPATH`` 保证 ``core`` 可导入。"""
    env = dict(os.environ)
    configure_hf_environment(env)
    root = str(paths.project_root())
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([root, existing]) if existing else root
    return env


async def _spawn_worker(argv: Sequence[str]) -> asyncio.subprocess.Process:
    """启动转写子进程。**测试接缝**：monkeypatch 本函数即可伪造子进程。"""
    return await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_worker_env(),
    )


def _parse_worker_stdout(raw: bytes) -> dict[str, Any] | None:
    """从子进程 stdout 里取**最后一行**合法 JSON 对象（倒着找，忽略进度输出）。"""
    text = raw.decode("utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "code" in parsed:
            return parsed
    return None


def _result_from_payload(payload: dict[str, Any], *, elapsed: float) -> AsrResult:
    """把子进程回报的 JSON 变成 :class:`AsrResult`（并**兜底修正**空文本语义）。"""
    code = str(payload.get("code", "") or CODE_ERROR)
    text = str(payload.get("text", "") or "").strip()
    message = str(payload.get("message", "") or "")
    raw_duration = payload.get("duration_sec", elapsed)
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        duration = elapsed

    ok = code in (CODE_OK, CODE_NO_SPEECH)
    if ok and not text:
        # ⭐ 无论子进程怎么标，空文本一律是"没有语音"，不是失败
        code = CODE_NO_SPEECH
        ok = True
    elif ok and text:
        code = CODE_OK
    return AsrResult(ok=ok, text=text, code=code, message=message, duration_sec=duration)


async def transcribe_async(
    target: str,
    *,
    model: str = DEFAULT_MODEL,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    lang: str = "zh",
) -> AsrResult:
    """**异步**转写任意目标（BV 号 / 链接 / 本地文件）。**不抛异常**。

    转写跑在独立子进程里 ⇒ 超时可**真的杀掉**（``code=TIMEOUT``），
    模型内存也随进程一起回收，不会常驻在 MCP server 里。

    Args:
        target: BV 号、B 站链接，或本地音视频文件路径。
        model: Whisper 模型大小；空串表示用环境变量 / ``small``。
        timeout_sec: 有界超时，默认 900 秒。
        lang: 语言，``zh`` 为默认；``auto`` 让模型自己判断。

    Returns:
        :class:`AsrResult`；``ok=True`` 且 ``text=""`` 表示**这次没有语音**。
    """
    started = time.monotonic()

    usable, availability_code = is_available()
    if not usable:
        return AsrResult(
            ok=False,
            text="",
            code=CODE_NO_DEPENDENCY,
            message=availability_message(availability_code),
            duration_sec=_elapsed(started),
        )

    if not target.strip():
        return AsrResult(
            ok=False,
            text="",
            code=CODE_ERROR,
            message="未给出视频（BV 号 / 链接 / 文件路径都为空）。",
            duration_sec=_elapsed(started),
        )

    if timeout_sec <= 0:
        return AsrResult(
            ok=False,
            text="",
            code=CODE_ERROR,
            message=f"timeout_sec 必须是正数，收到 {timeout_sec!r}。",
            duration_sec=_elapsed(started),
        )

    effective_model = model.strip() or default_model()
    prepare_runtime_dirs()  # ★ 目录必须**先于子进程**存在（沙箱只允许写已存在的目录）

    argv = _worker_argv(target, model=effective_model, lang=lang)
    try:
        proc = await _spawn_worker(argv)
    except OSError as exc:
        return AsrResult(
            ok=False,
            text="",
            code=CODE_ERROR,
            message=f"无法启动转写子进程：{exc}",
            duration_sec=_elapsed(started),
        )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except TimeoutError:
        _terminate(proc)
        return AsrResult(
            ok=False,
            text="",
            code=CODE_TIMEOUT,
            message=(
                f"转写超过 {timeout_sec:.0f} 秒仍未完成，已终止子进程。"
                "（首次转写要下模型，可先用 scripts/asr_transcribe.py 预热）"
            ),
            duration_sec=_elapsed(started),
        )

    payload = _parse_worker_stdout(stdout)
    if payload is None:
        return AsrResult(
            ok=False,
            text="",
            code=CODE_ERROR,
            message=(
                "转写子进程没有回传可解析的结果"
                f"（exit={proc.returncode}）：{_tail(stderr)}"
            ),
            duration_sec=_elapsed(started),
        )
    return _result_from_payload(payload, elapsed=_elapsed(started))


async def transcribe_bvid(
    bvid: str, *, model: str = DEFAULT_MODEL, timeout_sec: float = DEFAULT_TIMEOUT_SEC
) -> AsrResult:
    """转写一条 B 站视频（BV 号或链接）。**不抛异常**。

    这是主人指定的公开入口，等价于 :func:`transcribe_async` 的
    "BV 号 + 中文"特化：

    >>> await transcribe_bvid("BV1xx411c7mD")          # doctest: +SKIP
    AsrResult(ok=True, text='…', code='OK')

    ``ok=True`` 且 ``text=""`` 表示**这条视频没有语音**（``code=NO_SPEECH``），
    **不是失败** —— 调用方应据此登记"跳过（无语音）"，不要重试。

    Args:
        bvid: BV 号，或完整视频链接。
        model: Whisper 模型大小；空串表示用环境变量 ``BILI_DUTY_ASR_MODEL`` / ``small``。
        timeout_sec: 有界超时，默认 900 秒。

    Returns:
        :class:`AsrResult`。
    """
    return await transcribe_async(bvid, model=model, timeout_sec=timeout_sec, lang="zh")


def _terminate(proc: asyncio.subprocess.Process) -> None:
    """尽力杀掉子进程（可能已经被超时机制收走，失败就忽略）。"""
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()


def _tail(raw: bytes, limit: int = 240) -> str:
    """stderr 末尾一小段，用于人话报错（**不含凭据**；yt-dlp 只会打公开 URL）。"""
    text = raw.decode("utf-8", errors="replace").strip()
    return text[-limit:] if text else "（无输出）"


def _elapsed(started: float) -> float:
    return round(time.monotonic() - started, 3)


# ---------------------------------------------------------------------------
# 子进程入口 / 退出码
# ---------------------------------------------------------------------------


def exit_code_for(result: AsrResult) -> int:
    """结果 → 进程退出码。

    ⭐ **``NO_SPEECH`` 是 0**：竞品的做法就是"空串 + exit 0"，
    这样调用脚本不会把它当成故障（本项目 AGENTS.md §0.1 也要求如此）。
    """
    if result.code in (CODE_OK, CODE_NO_SPEECH):
        return EXIT_OK
    if result.code == CODE_NO_DEPENDENCY:
        return EXIT_NO_DEPENDENCY
    if result.code == CODE_DOWNLOAD_FAILED:
        return EXIT_DOWNLOAD_FAILED
    if result.code == CODE_TIMEOUT:
        return EXIT_TIMEOUT
    return EXIT_ERROR


def worker_main(argv: Sequence[str]) -> int:
    """子进程入口（内部）：转写一条目标，把结果以 **JSON 单行**打到 stdout。

    进度信息一律走 stderr，保证 stdout 只有那一行 JSON。
    """
    import argparse

    configure_stdio()
    parser = argparse.ArgumentParser(description="把一条视频转成文字（内部 worker）")
    parser.add_argument("target", help="BV 号 / B站链接 / 本地音视频文件")
    parser.add_argument("--model", default=default_model())
    parser.add_argument("--lang", default="zh")
    parser.add_argument("--json", action="store_true", help="以 JSON 单行回传结果")
    parser.add_argument("-o", "--out", default="", help="写到这个文件（不给就打到标准输出）")
    args = parser.parse_args(list(argv))

    result = transcribe_target(args.target, model=args.model, lang=args.lang)

    if args.json:
        payload = {
            "ok": result.ok,
            "text": result.text,
            "code": result.code,
            "message": result.message,
            "duration_sec": result.duration_sec,
        }
        print(json.dumps(payload, ensure_ascii=False))
    elif args.out:
        Path(args.out).write_text(result.text, encoding="utf-8")
        print(f"[asr] 已写入：{args.out}", file=sys.stderr)
    else:
        print(result.text)

    if not result.ok:
        print(f"[asr] {result.code}：{result.message}", file=sys.stderr)
    elif result.code == CODE_NO_SPEECH:
        print("[asr] 没有语音（这是结果，不是故障）", file=sys.stderr)
    return exit_code_for(result)
