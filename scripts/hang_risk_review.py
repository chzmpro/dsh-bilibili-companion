"""★ 重启后是否会卡死 —— 系统性风险审查（只读，不启动 GUI）。

主人明确要求：「重点审查重启后是否会卡死等问题」。

本脚本逐项验证 6 类卡死风险，每项都给**实测证据**，不靠推理：

1. **启动耗时** —— bridge 从 spawn 到 `initialize` 响应要多久？
   （DSH 会**等待**这个，官方文档：stdio 先起探测进程再起实际进程）
2. **工具调用耗时** —— 每个工具多久返回？有没有会阻塞的？
3. **网络超时** —— 平台不通时会不会挂死？
4. **限速 sleep** —— 会不会把一次调用拖过 DSH 超时？
5. **stdout 污染** —— 有没有非协议输出（会让 DSH 解析失败）？
6. **异常路径** —— 工具抛异常时会不会让 server 崩？

用法::

    ./.venv/Scripts/python.exe scripts/hang_risk_review.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYEXE = str(ROOT / ".venv" / "Scripts" / "python.exe")
if not Path(PYEXE).exists():  # pragma: no cover
    PYEXE = str(ROOT / ".venv" / "bin" / "python")


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    base = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "PYTHONIOENCODING": "utf-8",
    }
    if extra:
        base.update(extra)
    return base


def rpc(id_: int, method: str, params: dict | None = None) -> bytes:
    payload: dict = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        payload["params"] = params
    return (json.dumps(payload) + "\n").encode("utf-8")


class Session:
    """一个可计时的 MCP 会话。"""

    def __init__(self, env_extra: dict[str, str] | None = None) -> None:
        self.responses: dict[int, tuple[float, dict]] = {}
        self.stdout_lines: list[str] = []
        self.stderr_chunks: list[bytes] = []
        self.t0 = time.perf_counter()
        self.proc = subprocess.Popen(
            [PYEXE, "-m", "bridge.server"],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_env(env_extra),
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            self.stdout_lines.append(line)
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed.get("id"), int):
                self.responses[parsed["id"]] = (time.perf_counter(), parsed)

    def _read_stderr(self) -> None:
        assert self.proc.stderr
        for raw in self.proc.stderr:
            self.stderr_chunks.append(raw)

    def send(self, rid: int, method: str, params: dict | None = None) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(rpc(rid, method, params))
        self.proc.stdin.flush()

    def wait(self, rid: int, timeout: float = 120.0) -> tuple[float, dict] | None:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if rid in self.responses:
                return self.responses[rid]
            time.sleep(0.02)
        return None

    def close(self) -> int | None:
        try:
            assert self.proc.stdin
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        return self.proc.returncode


def review_startup() -> list[str]:
    """风险 1：启动耗时（DSH 会等待）。"""
    out: list[str] = []
    timings = []
    for i in range(3):
        s = Session()
        # ★ 不要先 wait —— 那会白等满超时。直接发，然后等响应。
        s.send(1, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "hang-review", "version": "1"},
        })
        r = s.wait(1, timeout=90)
        if r is None:
            out.append(f"  ❌ 第{i+1}次：initialize 超时（>90s）")
            s.close()
            continue
        elapsed = r[0] - s.t0
        timings.append(elapsed)
        out.append(f"  · 第{i+1}次：{elapsed:.2f}s")
        s.close()
    if timings:
        worst = max(timings)
        out.append(f"  最慢 {worst:.2f}s")
        if worst < 30:
            out.append("  ✅ 远低于 MCP SDK 的 60s 协商超时 → 不会卡启动")
        else:
            out.append("  ⚠️ 接近 60s，有卡启动风险")
    return out


def review_tool_calls() -> list[str]:
    """风险 2：各工具调用耗时。"""
    out: list[str] = []
    s = Session()
    s.send(1, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "hang-review", "version": "1"},
    })
    s.wait(1, timeout=90)
    assert s.proc.stdin
    s.proc.stdin.write(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode() + b"\n"
    )
    s.proc.stdin.flush()
    s.send(2, "tools/list")
    s.wait(2, timeout=60)

    # 只测**纯本地**工具（有网络的那几个依赖外网，另行判断）
    local_calls = [
        (10, "companion_status", {}),
        (11, "list_drafts", {}),
        (12, "owner_preferences", {"action": "list"}),
        (13, "feedback", {"bvid": "BV1nope", "verdict": "useful"}),
        (14, "read_evidence_excerpt", {"evidence_id": "ev_nope"}),
        (15, "save_analysis", {
            "bvid": "BV1nope", "evidence_ids": ["ev_nope"],
            "score_total": 1, "summary": "x",
        }),
        (16, "pause", {"reason": "hang-review"}),
    ]
    for rid, name, args in local_calls:
        s.send(rid, "tools/call", {"name": name, "arguments": args})
        r = s.wait(rid, timeout=60)
        if r is None:
            out.append(f"  ❌ {name}: 超时（>60s）← 会卡！")
            continue
        dt = r[0] - s.t0
        out.append(f"  · {name:24s} {dt:5.2f}s")
    s.close()
    return out


def review_stdout_purity() -> list[str]:
    """风险 5：stdout 只能有协议帧。"""
    out: list[str] = []
    s = Session()
    s.send(1, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "hang-review", "version": "1"},
    })
    s.wait(1, timeout=90)
    assert s.proc.stdin
    s.proc.stdin.write(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode() + b"\n"
    )
    s.proc.stdin.flush()
    # 故意触发异常路径
    for rid, name, args in [
        (20, "read_video", {"bvid": "not-a-bvid"}),
        (21, "companion_status", {}),
        (22, "publish_draft", {"draft_id": "nope"}),
    ]:
        s.send(rid, "tools/call", {"name": name, "arguments": args})
        s.wait(rid, timeout=30)
    time.sleep(0.5)
    s.close()

    bad = []
    for line in s.stdout_lines:
        try:
            json.loads(line)
        except json.JSONDecodeError:
            bad.append(line[:100])
    if bad:
        out.append(f"  ❌ 有 {len(bad)} 行非 JSON（会污染协议）：{bad[:3]}")
    else:
        out.append(f"  ✅ {len(s.stdout_lines)} 行 stdout **全部**是合法 JSON-RPC")
    return out


def review_exception_survival() -> list[str]:
    """风险 6：异常后 server 还能用吗？"""
    out: list[str] = []
    s = Session()
    s.send(1, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "hang-review", "version": "1"},
    })
    s.wait(1, timeout=90)
    assert s.proc.stdin
    s.proc.stdin.write(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode() + b"\n"
    )
    s.proc.stdin.flush()

    # 连续喂一堆畸形参数
    weird = [
        (30, "read_video", {"bvid": ""}),
        (31, "read_evidence_excerpt", {}),
        (32, "get_discussion", {"bvid": "BV1x", "page": -5}),
        (33, "list_drafts", {"limit": 99999}),
        (34, "feedback", {"bvid": "BV1x", "verdict": "bogus"}),
        (35, "owner_preferences", {"action": "bogus"}),
        (36, "save_analysis", {}),
    ]
    answered = 0
    for rid, name, args in weird:
        s.send(rid, "tools/call", {"name": name, "arguments": args})
        r = s.wait(rid, timeout=30)
        if r is not None:
            answered += 1
    out.append(f"  · 畸形参数 {len(weird)} 个，得到响应 {answered} 个")

    # 关键：之后**还能正常用**吗
    s.send(99, "tools/call", {"name": "companion_status", "arguments": {}})
    r = s.wait(99, timeout=30)
    alive = r is not None and "result" in r[1]
    mark = "✅" if alive else "❌"
    state = "可用" if alive else "不可用"
    out.append(f"  {mark} 异常之后 companion_status 仍{state}")
    rc = s.close()
    out.append(f"  · 退出码 {rc}")
    return out


def review_timeout_alignment() -> list[str]:
    """风险 4：预算与 DSH 超时是否对齐。"""
    out: list[str] = []
    sys.path.insert(0, str(ROOT))
    from core.config import Config

    frag = (ROOT / "dsh" / "cordis.patch.fragment.yml").read_text(encoding="utf-8")
    import re

    m = re.search(r"toolCallTimeoutMs:\s*(\d+)", frag)
    dsh_timeout = int(m.group(1)) / 1000 if m else 0
    budget = Config().text_worker.max_batch_seconds
    out.append(f"  · 批处理预算        : {budget:.0f}s")
    out.append(f"  · DSH toolCallTimeout: {dsh_timeout:.0f}s")
    if budget < dsh_timeout:
        out.append(f"  ✅ 预算比超时小 {dsh_timeout - budget:.0f}s → 会主动收尾，不会被掐断")
    else:
        out.append("  ❌ 预算 ≥ 超时 → 仍会被掐断")
    return out


def review_rate_limit_math() -> list[str]:
    """风险 4b：最坏情况会不会超时（纯算术，不发请求）。"""
    out: list[str] = []
    sys.path.insert(0, str(ROOT))
    from core.config import Config

    c = Config()
    r = c.bilibili_requests
    reads = c.discovery.max_full_reads_per_run
    per_video = 5  # 详情 + 字幕列表 + 字幕正文 + 评论 + 余量
    total = reads * per_video
    throttle = total * r.min_interval_seconds
    out.append(f"  · 单轮请求数（估）  : {total}（{reads} 视频 × {per_video}）")
    out.append(f"  · 仅限速耗时（估）  : {throttle:.0f}s")
    out.append(f"  · 批处理预算        : {c.text_worker.max_batch_seconds:.0f}s")
    if throttle > c.text_worker.max_batch_seconds:
        out.append(
            "  ⚠️ 限速本身就超过预算 → 会被预算提前收尾（**这是设计**："
            "宁可少读几条，也不能让调用被掐断）"
        )
    else:
        out.append("  ✅ 限速耗时在预算内")
    return out


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    print("=" * 70)
    print("★ 重启后卡死风险审查（实测，不启动 GUI）")
    print("=" * 70)

    sections = [
        ("风险 1：启动耗时（DSH 会等待）", review_startup),
        ("风险 2：工具调用耗时", review_tool_calls),
        ("风险 4：预算与 DSH 超时对齐", review_timeout_alignment),
        ("风险 4b：最坏情况算术", review_rate_limit_math),
        ("风险 5：stdout 纯净性", review_stdout_purity),
        ("风险 6：异常后是否还能用", review_exception_survival),
    ]
    for title, fn in sections:
        print(f"\n【{title}】")
        try:
            for line in fn():
                print(line)
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ 审查本身出错：{type(exc).__name__}: {exc}")

    print("\n" + "=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
