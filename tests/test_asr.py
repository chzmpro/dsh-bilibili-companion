"""``core/asr.py`` 的测试：**绝不真跑模型、绝不联网**。

覆盖点（对应主人给的 8 条验收）：

1. 依赖缺失 → ``NO_DEPENDENCY`` + 人话提示，**不抛异常**；
2. ⭐ 转写返回空 → ``ok=True`` + ``NO_SPEECH``（**不是失败**）—— 最关键的一条；
3. 转写返回文字 → ``ok=True``，text 正确；
4. yt-dlp 抛错 → ``DOWNLOAD_FAILED``；
5. 子进程超时 → ``TIMEOUT``（并且**真的 kill 了**）；
6. ``is_available()`` 依赖缺失时返回 ``(False, ...)``；
7. ``HF_ENDPOINT`` 等在 **import faster_whisper 之前**被设上（纯函数 + 假模块双证）；
8. stdout reconfigure 逻辑不炸。

外加：竞品参数**逐条**断言（``format`` / ``paths`` / ``nopart`` / ``cachedir`` / BV 文件名 /
``device='cpu'`` / ``compute_type='int8'`` / ``vad_filter=True``）。

**不联网、不下模型**的手段：把两个可选依赖换成 ``sys.modules`` 里的空模块
（``_module_available`` 先看 ``sys.modules``），再把 ``_transcribe_file`` / ``_spawn_worker``
这两个接缝 monkeypatch 掉。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from core import asr
from core.pipeline import LEVEL_ASR

# ---------------------------------------------------------------------------
# 假依赖 / 假子进程
# ---------------------------------------------------------------------------


class FakeYoutubeDL:
    """假的 ``yt_dlp.YoutubeDL``：只记录 opts，不联网。"""

    instances: list[FakeYoutubeDL] = []

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts
        self.downloaded_url = ""
        FakeYoutubeDL.instances.append(self)

    def __enter__(self) -> FakeYoutubeDL:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
        self.downloaded_url = url
        self.download = download
        return {"ext": "m4a"}

    def prepare_filename(self, info: dict[str, Any]) -> str:
        """照竞品口径拼路径，并**真的建出文件**（后续流程要求它存在）。"""
        outtmpl: str = self.opts["outtmpl"]
        path = outtmpl.replace(".%(ext)s", "." + str(info.get("ext", "m4a")))
        Path(path).write_bytes(b"fake-audio")
        return path


@pytest.fixture(autouse=True)
def _reset_fake_ytdlp_records() -> None:
    """每个测试都从空的记录开始（``FakeYoutubeDL.instances`` 是类属性）。"""
    FakeYoutubeDL.instances.clear()


@pytest.fixture
def fake_youtube_dl(monkeypatch: pytest.MonkeyPatch) -> type[FakeYoutubeDL]:
    """把 ``yt_dlp`` 换成假模块（只装它，模型那边由各测试自己决定）。"""
    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = FakeYoutubeDL  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yt_dlp", module)
    return FakeYoutubeDL


@pytest.fixture
def fake_deps(monkeypatch: pytest.MonkeyPatch) -> dict[str, types.ModuleType]:
    """把**两个**可选依赖都塞进 ``sys.modules`` ⇒ ``is_available()`` 走真实代码路径。

    ``yt_dlp`` 那份是**可用**的假模块（带 ``YoutubeDL``），
    这样不需要下载的测试也能完整跑通 ``transcribe_target`` 的全流程
    （包括 ``finally`` 里的清理）。
    """
    modules: dict[str, types.ModuleType] = {}
    for name in asr._REQUIRED_MODULES:
        module = types.ModuleType(name)
        monkeypatch.setitem(sys.modules, name, module)
        modules[name] = module
    modules["yt_dlp"].YoutubeDL = FakeYoutubeDL  # type: ignore[attr-defined]
    return modules


class FakeProc:
    """假的 asyncio 子进程。"""

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", delay: float = 0.0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._delay = delay
        self.returncode: int | None = 0
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def worker_stdout(code: str, text: str = "", message: str = "") -> bytes:
    """伪造 worker 子进程的 stdout（单行 JSON，与 ``worker_main`` 一致）。"""
    payload = {
        "ok": code in (asr.CODE_OK, asr.CODE_NO_SPEECH),
        "text": text,
        "code": code,
        "message": message,
        "duration_sec": 1.25,
    }
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def patch_worker(monkeypatch: pytest.MonkeyPatch, proc: FakeProc) -> FakeProc:
    """把 ``_spawn_worker`` 换掉（**测试接缝**，不发真子进程）。"""

    async def fake_spawn(argv: list[str]) -> Any:
        proc.argv = argv  # type: ignore[attr-defined]
        return proc

    monkeypatch.setattr(asr, "_spawn_worker", fake_spawn)
    return proc


# ---------------------------------------------------------------------------
# 0. 常量与命令行口径
# ---------------------------------------------------------------------------


def test_asr_level_matches_pipeline_constant() -> None:
    """``ASR_LEVEL`` 必须与 ``core.pipeline.LEVEL_ASR`` 完全一致（登记证据时要对上）。"""
    assert asr.ASR_LEVEL == LEVEL_ASR == "asr"


def test_to_url_expands_bv_only() -> None:
    assert asr.to_url("BV1xx411c7mD") == "https://www.bilibili.com/video/BV1xx411c7mD"
    url = "https://www.bilibili.com/video/BV1xx411c7mD"
    assert asr.to_url(url) == url
    assert asr.to_url(r"D:\clip.mp4") == r"D:\clip.mp4"


def test_default_model_follows_competitor_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认 ``small``；``BILI_DUTY_ASR_MODEL`` 可覆盖（竞品第 7 条）。"""
    monkeypatch.delenv(asr.MODEL_ENV, raising=False)
    assert asr.default_model() == "small"

    monkeypatch.setenv(asr.MODEL_ENV, "medium")
    assert asr.default_model() == "medium"

    monkeypatch.setenv(asr.MODEL_ENV, "   ")
    assert asr.default_model() == "small"


# ---------------------------------------------------------------------------
# 1 & 6. 依赖缺失：人话 + NO_DEPENDENCY，不抛异常
# ---------------------------------------------------------------------------


def test_is_available_reports_missing_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asr, "_module_available", lambda name: False)
    usable, code = asr.is_available()
    assert usable is False
    assert code == asr.CODE_NO_DEPENDENCY

    message = asr.availability_message(code)
    assert "未安装" in message
    assert "faster_whisper" in message
    assert "yt_dlp" in message
    # 人话里要说明"默认不装"以及体积，否则使用者不知道代价
    assert "默认状态" in message
    assert "1GB" in message
    assert "538MB" in message
    # 给出可复制的安装命令
    assert "pip install faster-whisper yt-dlp" in message


def test_is_available_ok_with_fake_modules(fake_deps: dict[str, types.ModuleType]) -> None:
    """走**真实**代码路径：两个依赖都在 ``sys.modules`` 里 ⇒ 可用。"""
    _ = fake_deps
    assert asr.is_available() == (True, asr.CODE_OK)
    assert asr.availability_message(asr.CODE_OK) == ""
    assert asr.missing_dependencies() == []


def test_is_available_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """``COMPANION_ASR_ENABLED=0`` ⇒ 显式关闭（默认关闭之外的第二道闸）。"""
    monkeypatch.setenv(asr.ENABLED_ENV, "0")
    assert asr.is_available() == (False, asr.CODE_ASR_DISABLED)
    assert "关闭" in asr.availability_message(asr.CODE_ASR_DISABLED)


def test_check_dependency_versions_flags_too_new_av(monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ 回归（2026-10-17 真机踩到）：``av>=16`` 必须被判为不兼容。

    真机症状：``open() got an unexpected keyword argument 'metadata_errors'``
    —— ``faster_whisper/audio.py`` 仍在传这个参数，而新版 PyAV 移除了它。
    **报错完全看不出是版本问题**，所以要在 status 里提前说清。
    """
    import types

    fake_av = types.SimpleNamespace(__version__="19.0.1")
    monkeypatch.setitem(sys.modules, "av", fake_av)  # type: ignore[arg-type]

    ok, note = asr.check_dependency_versions()
    assert ok is False
    assert "av==" in note
    assert "av<16" in note, "应该给出可执行的修法"


def test_check_dependency_versions_flags_too_new_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    """``huggingface_hub>=2`` 同类问题（实测 2.2.0 会炸）。"""
    import types

    fake_hub = types.SimpleNamespace(__version__="2.2.0")
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)  # type: ignore[arg-type]

    ok, note = asr.check_dependency_versions()
    assert ok is False
    assert "huggingface_hub==" in note


def test_check_dependency_versions_accepts_known_good(monkeypatch: pytest.MonkeyPatch) -> None:
    """本机实测跑通的组合（av 15.1.0 / hub 1.33.0）必须判为 OK。"""
    import types

    monkeypatch.setitem(sys.modules, "av", types.SimpleNamespace(__version__="15.1.0"))  # type: ignore[arg-type]
    monkeypatch.setitem(
        sys.modules, "huggingface_hub", types.SimpleNamespace(__version__="1.33.0")
    )  # type: ignore[arg-type]

    ok, note = asr.check_dependency_versions()
    assert ok is True
    assert note == "OK"


def test_bad_version_makes_asr_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 版本不兼容时 ``is_available`` 返回专用原因码（而不是等转写才炸）。"""
    import types

    monkeypatch.setattr(asr, "_env_enabled", lambda: True)
    monkeypatch.setattr(asr, "missing_dependencies", lambda: [])
    monkeypatch.setitem(sys.modules, "av", types.SimpleNamespace(__version__="19.0.1"))  # type: ignore[arg-type]

    usable, code = asr.is_available()
    assert usable is False
    assert code == asr.CODE_BAD_VERSION
    assert "版本不兼容" in asr.availability_message(code)


def test_asr_requirements_pin_the_known_good_versions() -> None:
    """★ 安装约束必须钉住那两个会炸的包（否则别人裸装最新版必踩坑）。"""
    joined = " ".join(asr.ASR_REQUIREMENTS)
    assert "av<16" in joined
    assert "huggingface_hub<2" in joined
    assert "av<16" in asr.ASR_INSTALL_HINT
    assert "huggingface_hub<2" in asr.ASR_INSTALL_HINT


def test_status_payload_is_credential_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """``companion_status`` 要用的字段都在，且不含任何凭据。"""
    monkeypatch.setattr(asr, "_module_available", lambda name: False)
    payload = asr.status()
    assert payload["available"] is False
    assert payload["code"] == asr.CODE_NO_DEPENDENCY
    assert payload["model"] == asr.default_model()
    assert set(payload) == {
        "available",
        "code",
        "message",
        "model",
        "missing",
        # ★ 版本兼容性（2026-10-17 实测新增：裸装最新版 av / huggingface_hub
        #   会报 metadata_errors，转写必失败 ⇒ 提前在 status 里报出来）
        "versions_ok",
        "versions_note",
        "requirements",
        "tmp_dir",
        "hf_cache_dir",
        # ★ 本机是否支持 symlink 缓存（Windows 未开开发者模式 ⇒ False，
        #   HF 会降级成"复制"多占磁盘）
        "symlink_cache",
        "dependency_hint",
    }
    assert "cookie" not in json.dumps(payload).lower()


@pytest.mark.asyncio
async def test_missing_dependency_gives_human_error_not_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """依赖缺失 → ``NO_DEPENDENCY``，**不抛异常**（异步入口）。"""
    monkeypatch.setattr(asr, "_module_available", lambda name: False)

    result = await asr.transcribe_bvid("BV1xx411c7mD")

    assert isinstance(result, asr.AsrResult)
    assert result.ok is False
    assert result.code == asr.CODE_NO_DEPENDENCY
    assert result.text == ""
    assert "未安装" in result.message
    # 绝不能把 traceback 塞进 message
    assert "Traceback" not in result.message
    assert result.duration_sec >= 0.0


def test_missing_dependency_gives_human_error_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """同步入口同样**不抛异常**。"""
    monkeypatch.setattr(asr, "_module_available", lambda name: False)

    result = asr.transcribe_target("BV1xx411c7mD")

    assert result.ok is False
    assert result.code == asr.CODE_NO_DEPENDENCY
    assert "未安装" in result.message


# ---------------------------------------------------------------------------
# 2. ⭐ 空输出 = 没有语音，不是失败
# ---------------------------------------------------------------------------


def test_empty_transcript_is_no_speech_not_failure(
    fake_deps: dict[str, types.ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """⭐ 最关键的一条：转写返回空 ⇒ ``ok=True`` + ``NO_SPEECH``。"""
    _ = fake_deps
    monkeypatch.setattr(asr, "_transcribe_file", lambda *a, **k: "")

    result = asr.transcribe_target("BV1xx411c7mD")

    assert result.ok is True, "空输出不是失败"
    assert result.code == asr.CODE_NO_SPEECH
    assert result.text == ""
    assert "没有语音" in result.message
    # 竞品口径：空串 + **exit 0**
    assert asr.exit_code_for(result) == asr.EXIT_OK


@pytest.mark.asyncio
async def test_empty_transcript_is_no_speech_async(
    fake_deps: dict[str, types.ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """异步路径（子进程）同样把空文本判成"没有语音"。"""
    _ = fake_deps
    patch_worker(monkeypatch, FakeProc(stdout=worker_stdout(asr.CODE_NO_SPEECH, "")))

    result = await asr.transcribe_bvid("BV1xx411c7mD")

    assert result.ok is True
    assert result.code == asr.CODE_NO_SPEECH
    assert result.text == ""


def test_payload_normalization_treats_empty_text_as_no_speech() -> None:
    """即使子进程把空文本标成 ``OK``，父进程也要**兜底**改成 ``NO_SPEECH``。

    这是防止"空输出被当成故障"的最后一道闸。
    """
    result = asr._result_from_payload({"code": asr.CODE_OK, "text": "   "}, elapsed=0.5)
    assert result.ok is True
    assert result.code == asr.CODE_NO_SPEECH
    assert result.text == ""

    # 反向：真正的失败**不能**被误判成"没有语音"
    failed = asr._result_from_payload({"code": asr.CODE_ERROR, "text": ""}, elapsed=0.5)
    assert failed.ok is False
    assert failed.code == asr.CODE_ERROR

    timed_out = asr._result_from_payload({"code": asr.CODE_TIMEOUT, "text": ""}, elapsed=0.5)
    assert timed_out.ok is False
    assert timed_out.code == asr.CODE_TIMEOUT


def test_whitespace_only_transcript_counts_as_no_speech(
    fake_deps: dict[str, types.ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """只有空白（含换行）也算"没有语音" —— 竞品是 ``.strip()`` 之后判空。"""
    _ = fake_deps
    monkeypatch.setattr(asr, "_transcribe_file", lambda *a, **k: "  \n\t ")
    result = asr.transcribe_target("BV1xx411c7mD")
    assert result.ok is True
    assert result.code == asr.CODE_NO_SPEECH


# ---------------------------------------------------------------------------
# 3. 正常文字
# ---------------------------------------------------------------------------


def test_transcript_text_is_returned(
    fake_deps: dict[str, types.ModuleType], fake_youtube_dl: type[FakeYoutubeDL],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = fake_deps, fake_youtube_dl
    monkeypatch.setattr(asr, "_transcribe_file", lambda *a, **k: "  大家好，这是正文。  ")

    result = asr.transcribe_target("BV1xx411c7mD")

    assert result.ok is True
    assert result.code == asr.CODE_OK
    assert result.text == "大家好，这是正文。"
    assert asr.exit_code_for(result) == asr.EXIT_OK


@pytest.mark.asyncio
async def test_transcript_text_via_worker_payload(
    fake_deps: dict[str, types.ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = fake_deps
    patch_worker(monkeypatch, FakeProc(stdout=worker_stdout(asr.CODE_OK, "转写成功")))

    result = await asr.transcribe_bvid("BV1xx411c7mD")

    assert result.ok is True
    assert result.code == asr.CODE_OK
    assert result.text == "转写成功"


@pytest.mark.asyncio
async def test_stdout_parser_ignores_progress_lines_and_takes_last_json(
    fake_deps: dict[str, types.ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """stdout 里混进别的行时，只认**最后一个**合法 JSON 对象。"""
    _ = fake_deps
    noisy = (
        b"[asr] \xe5\x85\x88\xe4\xb8\x8b\xe9\x9f\xb3\xe9\xa2\x91\n"
        + b'{"not": "our payload"}\n'
        + worker_stdout(asr.CODE_OK, "正文")
    )
    patch_worker(monkeypatch, FakeProc(stdout=noisy))

    result = await asr.transcribe_bvid("BV1xx411c7mD")
    assert result.text == "正文"
    assert result.code == asr.CODE_OK


# ---------------------------------------------------------------------------
# 4. 下载失败
# ---------------------------------------------------------------------------


def test_download_failure_maps_to_download_failed(
    fake_deps: dict[str, types.ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """yt-dlp 抛错 → ``DOWNLOAD_FAILED``（同步路径直接覆盖真实 ``fetch_audio`` 调用点）。"""
    _ = fake_deps

    def boom(url: str, tmp: str, tag: str) -> str:
        raise RuntimeError("unable to open for writing: [Errno 13] Permission denied")

    monkeypatch.setattr(asr, "fetch_audio", boom)

    result = asr.transcribe_target("BV1xx411c7mD")

    assert result.ok is False
    assert result.code == asr.CODE_DOWNLOAD_FAILED
    assert result.text == ""
    assert "下载音频失败" in result.message
    assert "Permission denied" in result.message
    assert asr.exit_code_for(result) == asr.EXIT_DOWNLOAD_FAILED


@pytest.mark.asyncio
async def test_download_failure_via_worker_payload(
    fake_deps: dict[str, types.ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = fake_deps
    patch_worker(
        monkeypatch,
        FakeProc(stdout=worker_stdout(asr.CODE_DOWNLOAD_FAILED, "", "下载音频失败：403")),
    )

    result = await asr.transcribe_bvid("BV1xx411c7mD")
    assert result.ok is False
    assert result.code == asr.CODE_DOWNLOAD_FAILED


def test_missing_local_file_is_error(
    fake_deps: dict[str, types.ModuleType], monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """本地文件不存在 → ``ERROR``（竞品对应 exit 2）。"""
    _ = fake_deps
    monkeypatch.setattr(asr, "_transcribe_file", lambda *a, **k: "不该走到这里")

    result = asr.transcribe_target(str(tmp_path / "nope.mp4"))

    assert result.ok is False
    assert result.code == asr.CODE_ERROR
    assert "找不到音频文件" in result.message


# ---------------------------------------------------------------------------
# 5. 超时：有界，且真的杀掉子进程
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subprocess_timeout_returns_timeout_and_kills(
    fake_deps: dict[str, types.ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = fake_deps
    proc = patch_worker(monkeypatch, FakeProc(delay=5.0))

    result = await asr.transcribe_bvid("BV1xx411c7mD", timeout_sec=0.05)

    assert result.ok is False
    assert result.code == asr.CODE_TIMEOUT
    assert proc.killed is True, "超时必须真的杀掉子进程，不能只是放弃等待"
    assert "超过" in result.message
    assert asr.exit_code_for(result) == asr.EXIT_TIMEOUT


@pytest.mark.asyncio
async def test_non_positive_timeout_is_rejected_without_spawning(
    fake_deps: dict[str, types.ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``timeout_sec <= 0`` 直接拒绝，**不启动子进程**（避免无界挂死）。"""
    _ = fake_deps

    async def must_not_spawn(argv: list[str]) -> Any:
        raise AssertionError("timeout_sec 非正数时不应该启动子进程")

    monkeypatch.setattr(asr, "_spawn_worker", must_not_spawn)

    for bad in (0.0, -1.0):
        result = await asr.transcribe_bvid("BV1xx411c7mD", timeout_sec=bad)
        assert result.code == asr.CODE_ERROR
        assert "正数" in result.message


@pytest.mark.asyncio
async def test_unparseable_worker_output_is_error(
    fake_deps: dict[str, types.ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """子进程崩了（没有 JSON）→ ``ERROR`` + stderr 尾巴，绝不挂起。"""
    _ = fake_deps
    proc = patch_worker(monkeypatch, FakeProc(stdout=b"", stderr=b"boom: import failed"))
    proc.returncode = 1

    result = await asr.transcribe_bvid("BV1xx411c7mD")

    assert result.ok is False
    assert result.code == asr.CODE_ERROR
    assert "boom" in result.message


# ---------------------------------------------------------------------------
# 7. ⭐ 环境变量必须在 import faster_whisper 之前设上
# ---------------------------------------------------------------------------


def test_configure_hf_environment_is_pure_and_uses_setdefault(tmp_path: Path) -> None:
    """纯函数：只补默认值，**不覆盖**使用者已有的设置。"""
    env: dict[str, str] = {}
    returned = asr.configure_hf_environment(env)

    assert returned is env
    assert env["HF_ENDPOINT"] == asr.HF_ENDPOINT_MIRROR
    assert env["HF_HUB_DISABLE_XET"] == "1"
    assert env["HF_HOME"].endswith(asr.HF_CACHE_DIR_NAME)

    # setdefault 语义：显式给的值必须原样保留（否则会悄悄改掉主人的镜像设置）
    preset = {
        "HF_ENDPOINT": "https://example.invalid",
        "HF_HOME": "D:/mine",
        "HF_HUB_DISABLE_XET": "0",
        "HF_HUB_DISABLE_SYMLINKS_WARNING": "0",
    }
    asr.configure_hf_environment(preset)
    assert preset == {
        "HF_ENDPOINT": "https://example.invalid",
        "HF_HOME": "D:/mine",
        "HF_HUB_DISABLE_XET": "0",
        "HF_HUB_DISABLE_SYMLINKS_WARNING": "0",
    }


def test_configure_hf_environment_targets_real_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.delenv("HF_HUB_DISABLE_XET", raising=False)

    asr.configure_hf_environment()

    assert os.environ["HF_ENDPOINT"] == asr.HF_ENDPOINT_MIRROR
    assert os.environ["HF_HUB_DISABLE_XET"] == "1"
    assert Path(os.environ["HF_HOME"]).name == asr.HF_CACHE_DIR_NAME


def test_hf_env_is_set_before_faster_whisper_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⭐ 用假 ``faster_whisper`` 记录"构造模型那一刻"的环境变量。

    竞品第一条踩坑：晚设一步，它**已经去连 HuggingFace 了**。
    所以必须在 ``WhisperModel`` 被构造之前就设好 —— 这里就是那条断言。
    """
    for name in ("HF_ENDPOINT", "HF_HOME", "HF_HUB_DISABLE_XET"):
        monkeypatch.delenv(name, raising=False)

    seen: dict[str, Any] = {}

    class FakeWhisperModel:
        def __init__(self, model: str, **kwargs: Any) -> None:
            # 构造的那一刻就把环境快照下来
            seen["HF_ENDPOINT"] = os.environ.get("HF_ENDPOINT")
            seen["HF_HOME"] = os.environ.get("HF_HOME")
            seen["HF_HUB_DISABLE_XET"] = os.environ.get("HF_HUB_DISABLE_XET")
            seen["model"] = model
            seen["kwargs"] = dict(kwargs)

        def transcribe(self, path: str, **kwargs: Any) -> tuple[list[Any], Any]:
            seen["transcribe_kwargs"] = dict(kwargs)
            return [], None

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = FakeWhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)

    text = asr._transcribe_file("audio.m4a", model="small", lang="zh")

    assert text == ""
    assert seen["HF_ENDPOINT"] == asr.HF_ENDPOINT_MIRROR
    assert seen["HF_HUB_DISABLE_XET"] == "1"
    assert str(seen["HF_HOME"]).endswith(asr.HF_CACHE_DIR_NAME)
    # 竞品参数：device=cpu / compute_type=int8（第 6 条）
    assert seen["model"] == "small"
    assert seen["kwargs"] == {"device": "cpu", "compute_type": "int8"}
    # 竞品参数：vad_filter=True（第 6 条）
    assert seen["transcribe_kwargs"]["vad_filter"] is True
    assert seen["transcribe_kwargs"]["language"] == "zh"


def test_language_auto_maps_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--lang auto`` ⇒ ``language=None``（让模型自己判断），竞品口径。"""
    recorded: dict[str, Any] = {}

    class FakeWhisperModel:
        def __init__(self, model: str, **kwargs: Any) -> None:
            pass

        def transcribe(self, path: str, **kwargs: Any) -> tuple[list[Any], Any]:
            recorded.update(kwargs)
            return [], None

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = FakeWhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)

    asr._transcribe_file("audio.m4a", model="small", lang="auto")

    assert recorded["language"] is None


def test_model_load_failure_becomes_error_result(
    fake_deps: dict[str, types.ModuleType], monkeypatch: pytest.MonkeyPatch,
    fake_youtube_dl: type[FakeYoutubeDL],
) -> None:
    """模型下载/加载失败 → ``ERROR``（不是抛出去）。"""
    _ = fake_deps, fake_youtube_dl

    def boom(*args: Any, **kwargs: Any) -> str:
        raise asr._AsrFailure(asr.CODE_ERROR, "加载 Whisper 模型失败：磁盘满")

    monkeypatch.setattr(asr, "_transcribe_file", boom)

    result = asr.transcribe_target("BV1xx411c7mD")
    assert result.ok is False
    assert result.code == asr.CODE_ERROR
    assert "磁盘满" in result.message


# ---------------------------------------------------------------------------
# 8. stdout reconfigure 不炸
# ---------------------------------------------------------------------------


def test_configure_stdio_uses_reconfigure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class FakeStream:
        def reconfigure(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(sys, "stdout", FakeStream())
    monkeypatch.setattr(sys, "stderr", FakeStream())

    asr.configure_stdio()

    assert len(calls) == 2
    assert all(call == {"encoding": "utf-8", "errors": "replace"} for call in calls)


def test_configure_stdio_survives_hostile_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """流没有 ``reconfigure``／reconfigure 直接抛 ⇒ **静默跳过**，绝不因此失败。"""

    class NoReconfigure:
        def write(self, text: str) -> int:
            return len(text)

    class Exploding:
        def reconfigure(self, **kwargs: Any) -> None:
            raise OSError("pipe is closed")

    monkeypatch.setattr(sys, "stdout", NoReconfigure())
    monkeypatch.setattr(sys, "stderr", Exploding())

    asr.configure_stdio()  # 不抛就算过

    monkeypatch.setattr(sys, "stdout", object())
    monkeypatch.setattr(sys, "stderr", object())
    asr.configure_stdio()


def test_configure_stdio_on_real_streams(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """真实流（pytest 的 UTF-8 替换流）上跑一次也不炸 —— 对应 GBK 管道那条踩坑。"""
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)

    asr.configure_stdio()
    asr.configure_stdio()  # 幂等：跑两次也不能炸

    print("中文 ✅")
    captured = capsys.readouterr()
    assert "中文" in captured.out


# ---------------------------------------------------------------------------
# 竞品细节：yt-dlp 的"沙箱适配三连" + BV 文件名
# ---------------------------------------------------------------------------


def test_fetch_audio_options_match_competitor(
    fake_youtube_dl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    """逐条断言竞品踩过的参数：format / paths / nopart / cachedir / BV 文件名。"""
    tmp = tmp_path / "asr"
    tmp.mkdir()

    path = asr.fetch_audio("https://www.bilibili.com/video/BV1xx411c7mD", str(tmp), "BV1xx411c7mD")

    assert len(fake_youtube_dl.instances) == 1
    opts = fake_youtube_dl.instances[0].opts

    # ② 取 bestaudio / best，**不用 ffmpeg 合并**（B 站 DASH 已音视频分离）
    assert opts["format"] == "bestaudio/best"
    # ③ 沙箱适配三连
    assert opts["paths"] == {"home": str(tmp), "temp": str(tmp)}
    assert opts["nopart"] is True
    assert opts["cachedir"] is False
    # ④ 文件名用 BV 号（写死 audio.m4a ⇒ 并发两轮互相覆盖）
    assert opts["outtmpl"] == os.path.join(str(tmp), "BV1xx411c7mD.%(ext)s")
    assert opts["noplaylist"] is True
    assert opts["http_headers"]["Referer"] == "https://www.bilibili.com"
    assert "User-Agent" in opts["http_headers"]

    # 返回的是 yt-dlp 自己算出来的落盘路径
    assert Path(path).exists()
    assert Path(path).name == "BV1xx411c7mD.m4a"


def test_fetch_audio_clears_stale_file_before_download(
    fake_youtube_dl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    """★★ 回归（2026-10-17 真机踩到）：下载前**必须**清掉同 tag 的残留音频。

    根因：上一轮中途失败会留下**同名的残缺音频**，下一次 yt-dlp 做断点续传时
    带上 ``Range: bytes=N-``，B 站 CDN 对那个范围回 **HTTP 416**，
    整次下载失败 —— 而且**每次重试都失败**（残留还在）。

    这条断言"下载前那个残留已经没了"，正是断根的那一步。
    """
    tmp = tmp_path / "asr"
    tmp.mkdir()
    stale = tmp / "BV1xx411c7mD.m4a"
    stale.write_bytes(b"x" * 100_000)  # 伪造上一轮的残缺文件

    observed: list[bool] = []

    class ObservingYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, *, download: bool = False) -> dict[str, Any]:
            # ★ 关键断言点：yt-dlp 被调用的**那一刻**，残留应该已经不存在
            observed.append(stale.exists())
            return super().extract_info(url, download=download)

    import yt_dlp

    monkeypatch_target = ObservingYoutubeDL
    fake_youtube_dl.__class__  # noqa: B018 —— 保持类型引用，便于阅读
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(yt_dlp, "YoutubeDL", monkeypatch_target)
        asr.fetch_audio("https://www.bilibili.com/video/BV1xx411c7mD", str(tmp), "BV1xx411c7mD")

    assert observed == [False], "下载开始时残留必须已被清掉（否则会撞 416）"


def test_fetch_audio_retries_once_on_416(
    fake_youtube_dl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    """★ 回归：真的撞上 416 时，清干净再重试**一次**（且只重试一次）。"""
    tmp = tmp_path / "asr"
    tmp.mkdir()
    calls: list[int] = []

    class FlakyYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, *, download: bool = False) -> dict[str, Any]:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError(
                    "ERROR: unable to download video data: "
                    "HTTP Error 416: Requested Range Not Satisfiable"
                )
            return super().extract_info(url, download=download)

    import yt_dlp

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(yt_dlp, "YoutubeDL", FlakyYoutubeDL)
        path = asr.fetch_audio(
            "https://www.bilibili.com/video/BV1xx411c7mD", str(tmp), "BV1xx411c7mD"
        )

    assert len(calls) == 2, f"416 应该重试一次（实际调用 {len(calls)} 次）"
    assert Path(path).exists()


def test_fetch_audio_does_not_retry_other_errors(
    fake_youtube_dl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    """非 416 的错误**不重试** —— 否则会把"视频不存在"这类确定性失败变成反复请求。"""
    tmp = tmp_path / "asr"
    tmp.mkdir()
    calls: list[int] = []

    class BrokenYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, *, download: bool = False) -> dict[str, Any]:
            calls.append(1)
            raise RuntimeError("ERROR: Video unavailable")

    import yt_dlp

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(yt_dlp, "YoutubeDL", BrokenYoutubeDL)
        with pytest.raises(RuntimeError, match="Video unavailable"):
            asr.fetch_audio("https://www.bilibili.com/video/BV1xx411c7mD", str(tmp), "BV1xx411c7mD")

    assert len(calls) == 1, "非 416 不该重试"


def test_fetch_audio_options_never_touch_temp(monkeypatch: pytest.MonkeyPatch) -> None:
    """``paths`` 与 ``outtmpl`` 都不得指向 ``%TEMP%``（沙箱会拒）。"""
    recorded: dict[str, Any] = {}

    class RecordingYoutubeDL(FakeYoutubeDL):
        def prepare_filename(self, info: dict[str, Any]) -> str:
            recorded.update(self.opts)
            return "D:/nowhere/never-written.m4a"

    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = RecordingYoutubeDL  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yt_dlp", module)

    asr.fetch_audio("https://example.invalid/v", "D:/data/tmp/asr", "BV1")

    tempdir = os.environ.get("TEMP", "").replace("\\", "/")
    assert tempdir, "本机应存在 TEMP"
    assert tempdir not in recorded["outtmpl"].replace("\\", "/")
    assert tempdir not in recorded["paths"]["temp"].replace("\\", "/")
    assert recorded["paths"] == {"home": "D:/data/tmp/asr", "temp": "D:/data/tmp/asr"}


def test_tmp_dir_is_precreated_and_not_mkdtemp(monkeypatch: pytest.MonkeyPatch) -> None:
    """⑤ **刻意不用 ``mkdtemp``**：目录由宿主预建，且把 tempfile.tempdir 钉在可写处。"""
    def must_not_be_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("不允许使用 mkdtemp（沙箱只允许写预先存在的目录）")

    monkeypatch.setattr(asr.tempfile, "mkdtemp", must_not_be_called)

    directory = asr.prepare_runtime_dirs()

    assert directory.exists()
    assert directory.name == "asr"
    assert asr.tempfile.tempdir == str(directory)
    # 模型缓存目录一并预建（HF_HOME 指向它）
    assert asr.hf_cache_dir().exists()


def test_sanitize_tag_keeps_bv_and_drops_unsafe_chars() -> None:
    assert asr._sanitize_tag("BV1xx411c7mD") == "BV1xx411c7mD"
    assert asr._sanitize_tag(r"D:\a b\clip.mp4") == "clip"
    assert asr._sanitize_tag("../../etc/passwd") == "passwd"
    assert asr._sanitize_tag("!!!") == "___"
    assert asr._sanitize_tag("") == "clip"
    assert len(asr._sanitize_tag("B" * 100)) == 40


def test_tmp_audio_is_cleaned_in_finally(
    fake_deps: dict[str, types.ModuleType], fake_youtube_dl: type[FakeYoutubeDL],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⑩ ``finally`` 里清理临时音频 —— 成功路径与失败路径都要清。"""
    _ = fake_deps, fake_youtube_dl
    monkeypatch.setattr(asr, "_transcribe_file", lambda *a, **k: "正文")

    first = asr.transcribe_target("BV1xx411c7mD")
    assert first.ok is True
    leftovers = list(asr.asr_tmp_dir().glob("BV1xx411c7mD.*"))
    assert leftovers == [], f"临时音频没清掉：{leftovers}"

    def boom(*args: Any, **kwargs: Any) -> str:
        raise asr._AsrFailure(asr.CODE_ERROR, "转写失败")

    monkeypatch.setattr(asr, "_transcribe_file", boom)
    second = asr.transcribe_target("BV1xx411c7mD")
    assert second.ok is False
    assert list(asr.asr_tmp_dir().glob("BV1xx411c7mD.*")) == []


# ---------------------------------------------------------------------------
# worker 入口 / 退出码契约
# ---------------------------------------------------------------------------


def test_exit_code_contract() -> None:
    """退出码口径：**NO_SPEECH 是 0**（竞品：空串 + exit 0）。"""
    cases = {
        asr.CODE_OK: 0,
        asr.CODE_NO_SPEECH: 0,
        asr.CODE_NO_DEPENDENCY: asr.EXIT_NO_DEPENDENCY,
        asr.CODE_DOWNLOAD_FAILED: asr.EXIT_DOWNLOAD_FAILED,
        asr.CODE_TIMEOUT: asr.EXIT_TIMEOUT,
        asr.CODE_ERROR: asr.EXIT_ERROR,
    }
    for code, expected in cases.items():
        result = asr.AsrResult(ok=code in (asr.CODE_OK, asr.CODE_NO_SPEECH), text="x", code=code)
        assert asr.exit_code_for(result) == expected, code


def test_worker_main_emits_single_json_line(
    fake_deps: dict[str, types.ModuleType], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``worker_main --json`` 的 stdout 必须是**一个** JSON 对象（父进程靠它解析）。"""
    _ = fake_deps
    monkeypatch.setattr(asr, "_transcribe_file", lambda *a, **k: "正文")
    monkeypatch.setattr(
        asr, "fetch_audio", lambda url, tmp, tag: _make_fake_audio(tmp, tag)
    )

    code = asr.worker_main(["BV1xx411c7mD", "--json"])

    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out.strip())
    assert payload["ok"] is True
    assert payload["code"] == asr.CODE_OK
    assert payload["text"] == "正文"


def test_worker_main_no_speech_still_exits_zero(
    fake_deps: dict[str, types.ModuleType], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """⭐ 没有语音也要 ``exit 0``（否则调用方会把它当成故障重试）。"""
    _ = fake_deps
    monkeypatch.setattr(asr, "_transcribe_file", lambda *a, **k: "")
    monkeypatch.setattr(
        asr, "fetch_audio", lambda url, tmp, tag: _make_fake_audio(tmp, tag)
    )

    code = asr.worker_main(["BV1xx411c7mD", "--json"])

    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out.strip())
    assert payload["code"] == asr.CODE_NO_SPEECH
    assert payload["text"] == ""
    # 说明写在 stderr（pytest 的 capsys 走同一条 reconfigure 后的流），stdout 只有 JSON
    assert "没有语音" in (captured.err + captured.out)


def test_worker_main_writes_out_file(
    fake_deps: dict[str, types.ModuleType], monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``-o`` 写文件（竞品同名能力）。"""
    _ = fake_deps
    monkeypatch.setattr(asr, "_transcribe_file", lambda *a, **k: "写进文件的正文")
    monkeypatch.setattr(
        asr, "fetch_audio", lambda url, tmp, tag: _make_fake_audio(tmp, tag)
    )
    out = tmp_path / "out.txt"

    code = asr.worker_main(["BV1xx411c7mD", "-o", str(out)])

    assert code == 0
    assert out.read_text(encoding="utf-8") == "写进文件的正文"


def test_worker_argv_is_wired_for_subprocess() -> None:
    """子进程命令行：走 ``python -c`` bootstrap，并把项目根放进 sys.path。"""
    argv = asr._worker_argv("BV1x", model="tiny", lang="zh")

    assert argv[0] == sys.executable
    assert argv[1] == "-c"
    assert "core.asr" in argv[2]
    assert asr.paths.project_root().as_posix() in argv[2]
    assert argv[3:] == ["BV1x", "--model", "tiny", "--lang", "zh", "--json"]


def test_worker_env_carries_hf_defaults_and_pythonpath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子进程环境必须带上 HF 三件套（子进程不会替我们设）与 PYTHONPATH。"""
    for name in ("HF_ENDPOINT", "HF_HOME", "HF_HUB_DISABLE_XET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PYTHONPATH", "D:/existing")

    env = asr._worker_env()

    assert env["HF_ENDPOINT"] == asr.HF_ENDPOINT_MIRROR
    assert env["HF_HUB_DISABLE_XET"] == "1"
    # ★ 项目根必须**排在前面**：否则同名 ``core`` 包会解析到别处
    assert env["PYTHONPATH"].split(os.pathsep) == [str(asr.paths.project_root()), "D:/existing"]


def _make_fake_audio(tmp: str, tag: str) -> str:
    """造一个假的音频文件（不联网），并返回路径。"""
    path = Path(tmp) / f"{tag}.m4a"
    path.write_bytes(b"fake-audio")
    return str(path)


# ---------------------------------------------------------------------------
# 真子进程端到端（**不装依赖时**跑）：验证 argv / env / JSON 管道真的通
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not asr.missing_dependencies(),
    reason="本机已装齐 ASR 依赖：这条端到端回退测试没有意义（且会真的加载模型）",
)
def test_real_subprocess_pipeline_reports_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真的起一个子进程：父进程以为依赖在（被 patch），子进程发现不在 ⇒ 回传 JSON。

    这条不碰模型、不联网，只验证**子进程管道**（argv / env / stdout JSON / 退出码）。
    """
    monkeypatch.setattr(asr, "_module_available", lambda name: True)

    result = asyncio.run(asr.transcribe_bvid("BV1xx411c7mD", timeout_sec=120.0))

    assert result.ok is False
    assert result.code == asr.CODE_NO_DEPENDENCY
    assert "未安装" in result.message


# ---------------------------------------------------------------------------
# ★ 已知坏镜像纠正（2026-10-17 真机踩到）
# ---------------------------------------------------------------------------


def test_hf_endpoint_hf_mirror_is_corrected() -> None:
    """★★ 回归：环境变量把 HF_ENDPOINT 设成 ``hf-mirror.com`` 时**必须纠正**。

    真机症状（2026-10-17）：`asr_fallback: true` + 环境里
    `HF_ENDPOINT=https://hf-mirror.com` ⇒ 去下 483 MB 的 small 模型，
    **下到 61.8 MB 就 `peer closed connection`**，整轮跑满 15 分钟超时。

    ★ 关键：`setdefault` 会**尊重**已有的环境变量值，所以用户的
    "我只是设了个镜像" 会变成 "转写永远失败"，而他**不可能猜到是镜像问题**。
    ⇒ 对**已知坏掉的**值要纠正（并打警告，不静默改）。
    """
    env = {"HF_ENDPOINT": "https://hf-mirror.com"}
    asr.configure_hf_environment(env)
    assert env["HF_ENDPOINT"] == asr.HF_ENDPOINT_MIRROR


def test_hf_endpoint_correction_tolerates_trailing_slash() -> None:
    """末尾斜杠 / http 变体也要能识别。"""
    for value in ("https://hf-mirror.com/", "http://hf-mirror.com"):
        env = {"HF_ENDPOINT": value}
        asr.configure_hf_environment(env)
        assert env["HF_ENDPOINT"] == asr.HF_ENDPOINT_MIRROR, f"{value} 未被纠正"


def test_hf_endpoint_respects_custom_mirror() -> None:
    """★ 自定义镜像**要尊重**（不能只认自己的默认值）。"""
    env = {"HF_ENDPOINT": "https://my-own-mirror.example"}
    asr.configure_hf_environment(env)
    assert env["HF_ENDPOINT"] == "https://my-own-mirror.example"


def test_hf_endpoint_default_when_unset() -> None:
    env: dict[str, str] = {}
    asr.configure_hf_environment(env)
    assert env["HF_ENDPOINT"] == asr.HF_ENDPOINT_MIRROR
    assert "modelscope" in env["HF_ENDPOINT"], "默认应是 ModelScope（实测快约 100 倍）"


def test_known_bad_endpoints_include_hf_mirror() -> None:
    """已知坏镜像清单要含 hf-mirror（这是实测结论，不是猜的）。"""
    normalized = {v.rstrip("/").lower() for v in asr._KNOWN_BAD_ENDPOINTS}
    assert "https://hf-mirror.com" in normalized


# ---------------------------------------------------------------------------
# ★ 模型缓存目录层级（2026-10-17 真机踩到**两次**）
# ---------------------------------------------------------------------------


def test_modelscope_fetch_script_puts_model_under_hub() -> None:
    """★★ 回归：预下脚本必须把模型放到 ``<HF_HOME>/hub/`` 之下。

    真机症状：脚本把模型下到 ``.hf-cache/models--...``（少了 ``hub/`` 层），
    而 ``huggingface_hub`` 只认 ``.hf-cache/hub/models--...`` ⇒
    `LocalEntryNotFoundError: Cannot find an appropriate cached snapshot folder`
    ⇒ 明明下好了却**又去联网重下**（用旧镜像就断流）。

    这个坑我踩了**两次**（第一次是手工摆缓存，第二次是脚本），所以钉死。
    """
    from pathlib import Path

    script = Path("scripts/fetch_model_modelscope.py").read_text(encoding="utf-8")
    assert 'cache / "hub" / f"models--' in script, (
        "预下脚本必须把模型放到 hub/ 之下（HF 的缓存根是 $HF_HOME/hub/）"
    )
