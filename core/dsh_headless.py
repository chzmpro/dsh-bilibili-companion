"""用 `dsh headless` 唤醒 DSH 做深度思考（2026-10-17）。

## 主人设想

> 「难道不能让豆包读取我的私信，然后理解并调用我们的项目，
>   然后让DSH启动思考并回复我的私信么？」

**可以，实测成立。** 这个模块就是那条链路。

## 为什么需要它（约束链条）

```
监听进程（常驻）
   ├─ 豆包：能自己调（key 在注册表）→ 但只是 lite，不懂项目上下文
   └─ DSH ：懂上下文、能用全部工具 → 但 key 在 DSH 进程内，拿不到
```

⇒ 让监听进程**不自己调模型**，而是**叫醒 DSH**：

```bash
dsh --profile headless "<任务描述>"
```

DSH 醒来 → 用 DeepSeek 思考 → 调我们的 MCP 工具（dm_inbox / dm_reply）→ 发出去 → 退出。

## 实测（2026-10-17）

```
$ dsh --profile headless "调用 dm_inbox 工具，告诉我有没有待回复的私信"
[loader] still waiting ... mcp-bili-companion     ← MCP 正常加载
INFO bili_companion bridge server.py:1928 启动（stdio）
没有待回复的私信（0 条）。
```

## 三个坑（都踩过，别重犯）

| # | 错误写法 | 报错 |
|---|---|---|
| 1 | `dsh headless --profile web "任务"` | `select a profile only once` |
| 2 | `dsh --profile web headless "任务"` | `too many arguments` |
| 3 | ✅ `dsh --profile headless "任务"` | 正常 |

⇒ **`headless` 是独立 profile 名，不是 `web` 的子命令。**

## 代价

| 项 | 说明 |
|---|---|
| 启动开销 | 每次起一个完整 DSH 进程，首次约 10~20 秒 |
| 并发 | 别同时起太多个 |
| 输出 | 自然语言（不是严格 JSON），需要容错解析 |
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: DSH 桌面端可执行文件（本机固定路径）。
DSH_EXE = Path(r"D:\DSH\DSH-file\DSH Desktop\DSH Desktop.exe")

#: DSH 的 bin.js 入口（在 app.asar 内）。
DSH_ENTRY = Path(
    r"D:\DSH\DSH-file\DSH Desktop\resources\app.asar"
    r"\node_modules\@deepseek-ai\dsh\lib\bin.js"
)

#: headless profile 名（**独立 profile**，不是 web 的子命令）。
HEADLESS_PROFILE = "headless"

#: 默认超时（秒）。唤醒 + 思考 + 调用工具，给足。
DEFAULT_TIMEOUT = 300

#: 单次任务描述的长度上限（避免把整段对话塞进命令行）。
MAX_TASK_CHARS = 4000


@dataclass
class HeadlessResult:
    """一次唤醒的结果（**永远不抛异常**）。"""

    ok: bool
    text: str = ""
    error_code: str = ""
    message: str = ""
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "text": self.text,
            "error_code": self.error_code,
            "message": self.message,
            "duration_seconds": round(self.duration_seconds, 1),
        }


def available() -> tuple[bool, str]:
    """检查 `dsh headless` 是否可用（不实际启动）。

    ★ 只检查**桌面端可执行文件**，不检查 `bin.js`。

    为什么（真机踩到）：`bin.js` 在 `app.asar` **打包文件内部**，
    普通文件系统 API（`Path.is_file()` / `os.path.exists()`）**看不到它**，
    但 Electron 自己能把 `asar` 当目录读。

    ⇒ 用 `is_file()` 检查它会**永远返回 False**，把可用功能误判成"不可用"。
    """
    if not DSH_EXE.is_file():
        return False, f"找不到 DSH：{DSH_EXE}"
    return True, "OK"


def _clean(text: str) -> str:
    """去掉 loader 噪音与 INFO 日志，只留模型输出。

    ★ 为什么要清：headless 的 stdout 会混进
    `[loader] still waiting for ...` 和 `INFO bili_companion bridge ...`，
    直接当回复发出去会很难看。
    """
    lines: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("[loader]"):
            continue
        if stripped.startswith("dsh: reasoning:"):
            continue
        if re.match(r"^\[\d{1,2}/\d{1,2}/\d{2,3} .*\]\s+INFO\b", stripped):
            continue
        if stripped.startswith("INFO "):
            continue
        if stripped.startswith("(node:") and "DeprecationWarning" in stripped:
            continue
        if stripped.startswith("(Use `"):
            continue
        lines.append(stripped)
    return "\n".join(lines).strip()


def build_task(*, incoming: str, history: list[dict[str, object]] | None = None) -> str:
    """拼交给 DSH 的任务描述。

    ★ 关键：明确告诉它**用我们自己的工具**去取队列、发回复，
    而不是让它自由发挥（否则它可能只是"回答一句"而没发出去）。
    """
    parts = [
        "你是 B 站小号「干饭不干事的小鲸鱼」，现在要给主人「沐雨林心」回私信。",
        "",
        "请严格按这两步做：",
        "1. 调用 `dm_inbox` 取出待回复私信"
        "（队列为空就直接回答「没有待回复」，不要回复任何人）；",
        "2. 针对**最新一条**，用你自己的判断写一条回复，然后调用 `dm_reply` 工具发出。",
        "",
        "回复要求：",
        "- 你是小鲸鱼：少女感、真诚、简短自然，最多一个括号吐槽；",
        "- **不知道就说不知道**，绝不编造；不要承诺你做不到的事；",
        "- 200 字以内，不要写成公文或列表；",
        "- 如果对方发的是分享视频（bvid 非空），可以先用 read_video 或 find_repo 了解再回。",
        "",
    ]
    if history:
        parts.append("最近的私信（供你了解上下文）：")
        for item in history[-6:]:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            title = str(item.get("title") or "").strip()
            suffix = f"（分享视频：{title}）" if title else ""
            parts.append(f"- {text}{suffix}")
        parts.append("")
    parts.append(f"最新一条：{incoming.strip()}")
    return "\n".join(parts)[:MAX_TASK_CHARS]


def temp_dir() -> Path:
    r"""给 DSH 子进程用的临时目录 —— 必须**在可写范围内**。

    ★ 真机踩到（很隐蔽）：

    ```
    errno: -4048, code: 'EPERM', syscall: 'mkdtemp'
    path: 'C:\Users\<用户>\AppData\Local\Temp\dsh-spill-XXXXXX'
    ```

    DSH 启动时要 `mkdtemp` 做 spill 目录，但 `%LOCALAPPDATA%\Temp` 在
    **工作区之外**，本机会拒绝写 ⇒ DSH 直接 `rc=1`、stdout 全空。
    现象是"调用成功返回但没有任何输出"，极难查。

    ⇒ 显式把 `TMP`/`TEMP`/`TMPDIR` 指到项目数据目录下。
    """
    from core.paths import data_dir

    path = data_dir() / "dsh-tmp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _build_env() -> dict[str, str]:
    """构造子进程环境（DSH_HOME + UTF-8 + **可写临时目录**）。"""
    env = dict(os.environ)
    home = env.get("DSH_HOME") or str(
        Path(os.path.expanduser("~")) / "AppData" / "Roaming" / "dsh-desktop" / "harness"
    )
    env["DSH_HOME"] = home
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["ELECTRON_RUN_AS_NODE"] = "1"
    # ★ 见 temp_dir() 的说明 —— 不设这条 DSH 会 EPERM 并静默失败
    temp = str(temp_dir())
    env["TMP"] = temp
    env["TEMP"] = temp
    env["TMPDIR"] = temp
    return env


def run_headless(task: str, *, timeout: float = DEFAULT_TIMEOUT) -> HeadlessResult:
    """**同步**唤醒 DSH 跑一个任务（由 :func:`ask_dsh` 用线程池包起来）。"""
    import time

    ready, why = available()
    if not ready:
        return HeadlessResult(ok=False, error_code="DSH_MISSING", message=why)

    body = (task or "").strip()
    if not body:
        return HeadlessResult(
            ok=False, error_code="EMPTY_TASK", message="任务描述为空"
        )

    command = [
        str(DSH_EXE),
        "--expose-internals",
        str(DSH_ENTRY),
        "--profile",
        HEADLESS_PROFILE,
        body,
    ]
    started = time.time()
    try:
        completed = subprocess.run(  # noqa: S603 —— 命令与参数都由本模块固定
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_build_env(),
        )
    except subprocess.TimeoutExpired:
        return HeadlessResult(
            ok=False,
            error_code="TIMEOUT",
            message=f"DSH 超过 {timeout:.0f} 秒未完成",
            duration_seconds=time.time() - started,
        )
    except (OSError, ValueError) as exc:
        return HeadlessResult(
            ok=False,
            error_code="SPAWN_FAILED",
            message=f"启动 DSH 失败：{type(exc).__name__}: {exc}",
            duration_seconds=time.time() - started,
        )

    elapsed = time.time() - started
    cleaned = _clean(completed.stdout or "")

    if completed.returncode != 0 and not cleaned:
        return HeadlessResult(
            ok=False,
            error_code="DSH_FAILED",
            message=(completed.stderr or "")[:300] or f"退出码 {completed.returncode}",
            duration_seconds=elapsed,
        )

    return HeadlessResult(
        ok=bool(cleaned),
        text=cleaned,
        error_code="" if cleaned else "EMPTY_OUTPUT",
        message="" if cleaned else "DSH 没有输出",
        duration_seconds=elapsed,
    )


async def ask_dsh(task: str, *, timeout: float = DEFAULT_TIMEOUT) -> HeadlessResult:
    """**异步**唤醒 DSH（内部用线程池跑阻塞的子进程）。"""
    import asyncio

    return await asyncio.to_thread(run_headless, task, timeout=timeout)


async def reply_via_dsh(
    *,
    incoming: str,
    history: list[dict[str, object]] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> HeadlessResult:
    """让 DSH 自己取队列、自己想、自己发（**一步到位**）。

    这是「豆包发现消息 → 触发 DSH 深度思考并回复」的落地函数。
    """
    return await ask_dsh(build_task(incoming=incoming, history=history), timeout=timeout)


def describe() -> dict[str, object]:
    """给面板/CLI 看的能力说明。"""
    ready, why = available()
    return {
        "available": ready,
        "reason": why,
        "profile": HEADLESS_PROFILE,
        "note": (
            "用 `dsh --profile headless` 唤醒 DSH 用 DeepSeek 思考。"
            "每次要起一个完整进程（约 10~20 秒）。"
            if ready
            else why
        ),
    }
