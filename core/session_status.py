"""读取 DSH 会话的最后一个 ``turn/end``（用于自动调度的完成判定，R1）。

## 为什么不能只看投递返回

DSH 的投递只是把消息塞进会话并唤醒；**投递成功 ≠ 模型跑完**。
唯一可信的成功信号是会话事件流里最后一个 ``turn/end`` 的
``reason.kind == "completed"``（HANDOFF §5.7 实测结论）。

## 会话文件是 zstd 多帧

DSH 的会话文件是 ``session.v4.jsonl.zstd``，第一帧只有 header。
必须按 magic（``0x28B52FFD``）逐帧解压；这里复用项目里已经在用的
Node ``zlib.zstdDecompressSync`` 方案。

## 边界

* 只读；不修改任何会话文件；
* 找不到文件 / 解压失败 → 返回 ``unknown``，**绝不**据此宣布成功。
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: 逐帧解压（与 scripts/verify_deliver.py 同一套，已验证可用）。
NODE_DECOMPRESS = r"""
const fs=require('fs'), zlib=require('zlib');
const buf=fs.readFileSync(process.argv[1]);
const MAGIC = Buffer.from([0x28,0xB5,0x2F,0xFD]);
let pos = 0, text = '';
while (pos < buf.length) {
  const idx = buf.indexOf(MAGIC, pos);
  if (idx < 0) break;
  const next = buf.indexOf(MAGIC, idx + 4);
  const end = next < 0 ? buf.length : next;
  try { text += zlib.zstdDecompressSync(buf.subarray(idx, end)).toString('utf8'); } catch {}
  pos = end;
}
process.stdout.write(text);
"""


@dataclass(frozen=True)
class TurnStatus:
    """一个会话的事件流摘要。"""

    found: bool
    turns: int
    completed: bool
    last_kind: str
    error: str
    matched: bool = False
    message_id: str = ""
    turn_id: int | None = None


def sessions_home() -> Path:
    """DSH 会话根目录（``$DSH_HOME/sessions``）。"""
    home = os.environ.get("DSH_HOME", "").strip()
    if home:
        return Path(home) / "sessions"
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        return Path(appdata) / "dsh-desktop" / "harness" / "sessions"
    return Path.home() / ".dsh" / "sessions"


def find_session_file(session_id: str, *, home: Path | None = None) -> Path | None:
    """按 session id 找 ``session.v4.jsonl.zstd``（只读）。"""
    if not session_id:
        return None
    root = home or sessions_home()
    for candidate in root.glob(f"*/{session_id}/session.v4.jsonl.zstd"):
        return candidate
    return None


def decompress(path: Path, *, timeout: float = 60.0) -> str:
    """逐帧解压；失败返回空串（**不抛异常**）。"""
    try:
        result = subprocess.run(  # noqa: S603, S607 —— node 是本项目既有依赖
            ["node", "-e", NODE_DECOMPRESS, str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout or ""


def parse_turns(text: str) -> tuple[int, str, bool, str]:
    """从事件流里解析 ``(turns, last_kind, completed, last_error)``。"""
    turns = 0
    last_kind = ""
    last_error = ""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "turn/end":
            continue
        turns += 1
        data = event.get("data") or {}
        reason = data.get("reason") or {}
        last_kind = str(reason.get("kind") or "?")
        if last_kind == "error":
            error = reason.get("error") or {}
            last_error = str(error.get("message") or "")[:160]
        else:
            last_error = ""
    return turns, last_kind, last_kind == "completed", last_error


def parse_delivery_turn(text: str, expected_text: str) -> TurnStatus:
    """按 DSH 的 turn/start → user/message → turn/end 契约关联精确投递。

    followup 先进入 inbox，只有被处理时才在当前 turn 中落 user/message。
    不以总轮数、片段命中或后续主人的消息代替本条消息。
    """
    active: int | None = None
    matched_turn: int | None = None
    message_id = ""
    ended = 0
    kind = error = ""
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
            continue
        data = event["data"]
        if event.get("type") == "turn/start":
            active = data.get("turn") if isinstance(data.get("turn"), int) else None
        elif event.get("type") == "user/message" and active is not None:
            content = data.get("content") or []
            body = "\n".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
            if (
                expected_text
                and body == expected_text
                and data.get("role") == "user"
                and data.get("id")
            ):
                # Duplicate matching messages are ambiguous; never guess which one completed.
                if message_id:
                    return TurnStatus(True, ended, False, "", "duplicate delivery message")
                matched_turn, message_id = active, str(data["id"])
        elif event.get("type") == "turn/end":
            ended += 1
            if matched_turn is not None and data.get("turn") == matched_turn:
                reason = data.get("reason") or {}
                kind = str(reason.get("kind") or "")
                error = str((reason.get("error") or {}).get("message") or "")[:160]
            if data.get("turn") == active:
                active = None
    return TurnStatus(
        True, ended, kind == "completed", kind, error, bool(message_id), message_id, matched_turn
    )


def read_turn_status(
    session_id: str, *, home: Path | None = None, expected_text: str = ""
) -> TurnStatus:
    """读一个会话当前的最后一个 turn 状态（找不到文件 → ``found=False``）。"""
    path = find_session_file(session_id, home=home)
    if path is None:
        return TurnStatus(found=False, turns=0, completed=False, last_kind="", error="")
    text = decompress(path)
    if not text:
        return TurnStatus(found=False, turns=0, completed=False, last_kind="", error="")
    if expected_text:
        return parse_delivery_turn(text, expected_text)
    turns, last_kind, completed, error = parse_turns(text)
    return TurnStatus(
        found=True,
        turns=turns,
        completed=completed,
        last_kind=last_kind,
        error=error,
    )
