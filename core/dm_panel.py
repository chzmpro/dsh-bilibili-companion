"""单页 Web 面板 + 常驻服务（2026-10-17）。

主人：*"独立的 web 服务甚至一个独立的页面"*。

## 为什么用标准库而不是 Flask/FastAPI

| 方案 | 代价 |
|---|---|
| Flask / FastAPI | 要**新增依赖**，且要把它们塞进 uv 环境 |
| **`http.server`（标准库）** | 零新依赖、零构建 |

参考项目用 Flask + 768KB 的 HTML（35 个分区）。我们**故意只做一页**：
这个页面只回答三个问题 —— **在跑吗 / 收到几条 / 最近发生了什么**。

## 页面构成（一屏看完，不滚动）

1. **状态卡**：运行中/已停止、已跑几轮、发现几条、回复几条、错误数
2. **控制**：开始 / 停止（`POST /control`）
3. **事件流**：最近 40 条，带时间戳
4. **手动查一次**：`POST /poll` —— 不想等间隔时按一下

## 安全

* **只监听 `127.0.0.1`** —— 不暴露到局域网（面板没有鉴权，暴露 = 谁都能停你的监听）；
* 写操作只有两个：`/control`（起停）与 `/poll`（查一次），都不改配置；
* 回复内容与接收者仍由 `core/dm.py` 的门禁管，面板**绕不过**。
"""

from __future__ import annotations

import asyncio
import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from core.dm_watch import DmWatcher

#: 面板默认端口。选一个不常见的，避免和别的服务撞。
DEFAULT_PORT = 8848

#: 只监听本机 —— **不要**改成 0.0.0.0（面板没有鉴权）。
DEFAULT_HOST = "127.0.0.1"

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>小肥鱼 · 私信监听</title>
<style>
  :root {
    --bg: #0f1115; --card: #171a21; --line: #262b36;
    --text: #e6e9ef; --dim: #8b93a7; --accent: #5b9dff;
    --ok: #35c07d; --warn: #e0a33e; --err: #e5645b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px; background: var(--bg); color: var(--text);
    font: 14px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  }
  .wrap { max-width: 920px; margin: 0 auto; }
  h1 { font-size: 18px; margin: 0 0 4px; font-weight: 600; }
  .sub { color: var(--dim); font-size: 12px; margin-bottom: 20px; }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 10px; padding: 16px; margin-bottom: 14px;
  }
  .row { display: flex; gap: 12px; flex-wrap: wrap; }
  .stat { flex: 1; min-width: 110px; }
  .stat .k { color: var(--dim); font-size: 11px; text-transform: uppercase; letter-spacing: .4px; }
  .stat .v { font-size: 22px; font-weight: 600; margin-top: 2px; }
  .badge {
    display: inline-block; padding: 3px 10px;
    border-radius: 20px; font-size: 12px; font-weight: 600;
  }
  .on { background: rgba(53,192,125,.15); color: var(--ok); }
  .off { background: rgba(139,147,167,.15); color: var(--dim); }
  button {
    background: #232937; color: var(--text); border: 1px solid var(--line);
    border-radius: 7px; padding: 8px 16px; font-size: 13px; cursor: pointer;
    font-family: inherit;
  }
  button:hover { background: #2b3344; }
  button.primary {
    background: var(--accent); border-color: var(--accent);
    color: #06101f; font-weight: 600;
  }
  button.danger {
    background: rgba(229,100,91,.15);
    border-color: rgba(229,100,91,.4); color: var(--err);
  }
  button:disabled { opacity: .45; cursor: not-allowed; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th {
    text-align: left; color: var(--dim); font-weight: 500; font-size: 11px;
    text-transform: uppercase; letter-spacing: .4px; padding: 0 0 8px;
  }
  td { padding: 5px 0; border-top: 1px solid var(--line); vertical-align: top; }
  td.t {
    color: var(--dim); white-space: nowrap; width: 74px;
    font-variant-numeric: tabular-nums;
  }
  .k-found { color: var(--accent); }
  .k-replied { color: var(--ok); }
  .k-error { color: var(--err); }
  .k-skipped { color: var(--warn); }
  .k-poll { color: var(--dim); }
  .foot { color: var(--dim); font-size: 11px; margin-top: 16px; text-align: center; }
</style>
</head>
<body>
<div class="wrap">
  <h1>私信监听 <span id="badge" class="badge off">读取中…</span></h1>
  <div class="sub">只回主人 · 每 <span id="iv">–</span> 秒查一次未读 · 幂等（同一句只回一次）</div>

  <div class="card">
    <div class="row">
      <div class="stat"><div class="k">已运行</div><div class="v" id="uptime">–</div></div>
      <div class="stat"><div class="k">轮次</div><div class="v" id="rounds">–</div></div>
      <div class="stat"><div class="k">发现消息</div><div class="v" id="found">–</div></div>
      <div class="stat"><div class="k">已回复</div><div class="v" id="replied">–</div></div>
      <div class="stat"><div class="k">错误</div><div class="v" id="errors">–</div></div>
    </div>
    <div style="margin-top:14px;display:flex;gap:8px;flex-wrap:wrap">
      <button id="btnStart" class="primary">开始监听</button>
      <button id="btnStop" class="danger">停止</button>
      <button id="btnPoll">立即查一次</button>
    </div>
    <div id="errbox" style="margin-top:10px;color:var(--err);font-size:12px"></div>
  </div>

  <div class="card">
    <table>
      <thead><tr><th>时间</th><th>事件</th></tr></thead>
      <tbody id="events"><tr><td colspan="2" style="color:var(--dim)">暂无事件</td></tr></tbody>
    </table>
  </div>

  <div class="foot">数据只在本机（127.0.0.1）· 停止后不再查 B站</div>
</div>
<script>
const $ = (id) => document.getElementById(id);
let timer = null;

function cls(kind) {
  const m = {found:'found', replied:'replied', error:'error', skipped:'skipped'};
  return 'k-' + (m[kind] || 'poll');
}

async function refresh() {
  try {
    const r = await fetch('/status');
    const d = await r.json();
    $('badge').textContent = d.running ? '监听中' : '已停止';
    $('badge').className = 'badge ' + (d.running ? 'on' : 'off');
    $('iv').textContent = d.interval ?? '–';
    $('uptime').textContent = d.running ? d.uptime : '–';
    $('rounds').textContent = d.rounds ?? 0;
    $('found').textContent = d.found ?? 0;
    $('replied').textContent = d.replied ?? 0;
    $('errors').textContent = d.errors ?? 0;
    $('errbox').textContent = d.last_error ? ('最近错误：' + d.last_error) : '';

    const evs = d.events || [];
    if (!evs.length) {
      $('events').innerHTML = '<tr><td colspan="2" style="color:var(--dim)">暂无事件</td></tr>';
    } else {
      $('events').innerHTML = evs.map(e =>
        `<tr><td class="t">${e.time}</td>` +
        `<td class="${cls(e.kind)}">${escapeHtml(e.detail)}</td></tr>`
      ).join('');
    }
    $('btnStart').disabled = !!d.running;
    $('btnStop').disabled = !d.running;
  } catch (e) {
    $('badge').textContent = '连不上服务';
    $('badge').className = 'badge off';
  }
}

function escapeHtml(s) {
  const m = {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'};
  return String(s).replace(/[&<>"']/g, c => m[c]);
}

async function post(path) {
  try {
    const r = await fetch(path, {method: 'POST'});
    const d = await r.json();
    if (d.error) { $('errbox').textContent = d.error; }
  } catch (e) { $('errbox').textContent = '请求失败：' + e; }
  refresh();
}

$('btnStart').onclick = () => post('/control?action=start');
$('btnStop').onclick = () => post('/control?action=stop');
$('btnPoll').onclick = () => post('/poll');

refresh();
timer = setInterval(refresh, 2000);
</script>
</body>
</html>
"""


class _WatcherService:
    """把 `DmWatcher` 跑在**后台线程的 asyncio 事件循环**里。

    ★ 为什么不用主线程：`http.server` 是**阻塞**的（每个请求一个线程），
    而监听是 async 的。两者要分开跑，否则"停止"按钮会等监听 sleep 完才响应。
    """

    def __init__(self, watcher: DmWatcher) -> None:
        self.watcher = watcher
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, *, interval: int | None = None) -> tuple[bool, str]:
        if self.running:
            return False, "已经在跑了"
        if interval:
            self.watcher.interval = max(15, int(interval))
        self.watcher._stop = False  # noqa: SLF001 —— 重启需要清旗标
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        # 等 task 建好（最多 2 秒），否则紧接着的 /status 可能看到 running=False
        waited = 0.0
        while waited < 2.0 and not self.running:
            threading.Event().wait(0.1)
            waited += 0.1
        return True, "已启动"

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._task = self._loop.create_task(self.watcher.run())
        try:
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        finally:
            self._loop.close()

    def stop(self) -> tuple[bool, str]:
        if not self.running:
            return False, "本来就没在跑"
        self.watcher.stop()
        return True, "已请求停止（当前这一轮跑完就退出）"

    def poll_once(self) -> tuple[bool, str]:
        """手动跑一轮（不需要常驻循环）。

        ★ 真机踩到：监听在跑时 `self._loop` **已经在 run_until_complete 里**，
        再调一次会抛 `RuntimeError: This event loop is already running`。
        ⇒ 那种情况改用 `run_coroutine_threadsafe` 把协程丢给那个循环。
        """
        coro = self.watcher.poll_once_round()

        # 循环正在跑 → 线程安全地投递
        if self._loop is not None and self._loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(coro, self._loop)
                result = future.result(timeout=60)
            except Exception as exc:  # noqa: BLE001
                return False, f"查一次失败：{type(exc).__name__}: {exc}"
            return True, f"查了一次：未读 {result['unread']}，新消息 {result['found']}"

        # 没有循环在跑 → 临时建一个
        try:
            result = asyncio.new_event_loop().run_until_complete(coro)
        except Exception as exc:  # noqa: BLE001
            return False, f"查一次失败：{type(exc).__name__}: {exc}"
        return True, f"查了一次：未读 {result['unread']}，新消息 {result['found']}"


def make_handler(service: _WatcherService, watcher: DmWatcher) -> type[BaseHTTPRequestHandler]:
    """构造请求处理器（`service` 与 `watcher` 通过闭包带进去）。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "DmWatch/1.0"

        # ---- 输出 --------------------------------------------------------

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # 面板是本地单页，禁缓存免得看到旧状态
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: dict[str, Any], code: int = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8")

        # ---- 路由 --------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的约定
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._send(
                    HTTPStatus.OK, _PAGE.encode("utf-8"), "text/html; charset=utf-8"
                )
                return
            if path == "/status":
                payload = watcher.stats.to_dict()
                payload["running"] = service.running
                payload["interval"] = watcher.interval
                payload["auto_reply"] = watcher.auto_reply
                if not service.running:
                    payload["uptime"] = "–"
                self._json(payload)
                return
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            path, _, query = self.path.partition("?")
            params = dict(
                part.split("=", 1) for part in query.split("&") if "=" in part
            )

            if path == "/control":
                action = params.get("action", "")
                if action == "start":
                    ok, message = service.start(
                        interval=int(params["interval"]) if "interval" in params else None
                    )
                elif action == "stop":
                    ok, message = service.stop()
                else:
                    self._json({"error": f"未知 action：{action}"}, HTTPStatus.BAD_REQUEST)
                    return
                self._json({"ok": ok, "message": message})
                return

            if path == "/poll":
                ok, message = service.poll_once()
                self._json({"ok": ok, "message": message})
                return

            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ARG002
            """**不打印**访问日志 —— 每 2 秒轮询一次会刷爆终端。"""
            return

    return Handler


def serve(
    watcher: DmWatcher,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    autostart: bool = False,
) -> None:
    """起服务（阻塞直到 Ctrl+C）。"""
    service = _WatcherService(watcher)
    server = ThreadingHTTPServer((host, port), make_handler(service, watcher))
    server.daemon_threads = True

    print(f"面板：http://{host}:{port}/")
    print("  （只监听本机；关闭这个窗口服务就停）")
    if autostart:
        ok, message = service.start()
        print(f"  自动启动监听：{message}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在停止…")
    finally:
        service.stop()
        server.shutdown()
        server.server_close()
