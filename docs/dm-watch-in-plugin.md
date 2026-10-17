# 在插件里启动私信监听（2026-10-17）

## 主人的问题

> 「没有在插件里启动的方式吗」

有道理 —— 之前只能开终端跑 `cli.app dm-watch`，这不合理。现在**插件设置页里就能起停**。

## 一、怎么用

1. 打开 DSH → 设置 → **B站小号** 插件页
2. 找到「**私信监听**」卡片
3. 点「**启动监听**」→ 自动起面板进程，并在新标签页打开面板
4. 点「**停止监听**」→ 进程随之退出

卡片上会显示实时状态：`运行中 · 端口 8848` 或 `已停止`，以及面板链接。

## 二、实现方式（为什么这么做）

### 后端：插件代起进程

`plugin/index.js` 新增三个接口：

| 接口 | 作用 |
|---|---|
| `GET /api/bili-companion/watch` | 现在跑没跑、pid、端口、面板 URL |
| `POST .../watch?action=start` | spawn `python -m cli.app dm-watch --port N`，**记住 pid** |
| `POST .../watch?action=stop` | kill 它（先温和，3 秒后 SIGKILL 兜底） |

**照抄了现有的 `startRun()` 模式**（同样的 `spawn` + `windowsHide` + UTF-8 环境钉死），
但有一个**关键区别**：

```js
// startRun：跑一轮就结束，不关心子进程后续
child.unref()

// startWatch：要能 kill、要知道死活 ⇒ **不能 unref**
watchChild = spawn(py, args, { ... })
watchChild.on('exit', (code) => { watchChild = null })
```

### 为什么 spawn 整个 `dm-watch` 而不是只 spawn 监听

面板与监听在**同一个进程**里（`http.server` 主线程 + asyncio 后台线程）。
杀掉这个进程，两边一起停 —— 比管两个进程简单可靠。

### 前端：设置页里的卡片

- 状态每 5 秒随 `/status` 一起轮询（`/watch` 单独拉一次，它变化更快）；
- 点启动会自动 `window.open(url)` 打开面板；
- 按钮文案随状态切换：`启动监听` / `停止监听` / `处理中…`。

## 三、★ 加了 7 条契约断言（防 D19 静默卡住）

`selfcheck.mjs` 里新增：

```
✅ GET /watch 返回 200
✅ GET /watch 含 running 字段（前端依赖它）
✅ GET /watch 含 port 字段（前端依赖它）
✅ GET /watch 含 url 字段（前端依赖它）
✅ GET /watch 的 port 是合法端口号
✅ GET /watch 的 url 指向本机（面板不对外暴露）
✅ GET /watch 初始 running=false
```

> 为什么必须有：前端靠 `watch.running` 决定按钮文案。
> **少了这个字段，界面会显示"已停止"但永远点不动** —— 而且控制台一个错都不报。
> 这正是 `AGENTS.md` 里 D19 记录的那类故障（设置页卡在"读设置中"）。

最后一条 `初始 running=false` 也很重要：防止一上来就骗人说"正在跑"。

## 四、验证

| 项 | 结果 |
|---|---|
| `node selfcheck.mjs` | ✅ 全部通过（含 7 条新断言） |
| Python 端命令 | ✅ `-m cli.app dm-watch --port 8860` 面板返回 HTTP 200 |
| 三道体检 | ✅ 孤儿行 / 形态 / 行归属全过 |
| 真机监听 | ✅ 之前已验证：`errors=0, found=8`（真查到 8 条私信） |

## 五、⚠️ 重启 DSH 后才生效

这次改的是 **host 半身**（`index.js`），必须**真实重启 DSH**，
不是刷新页面就行（`AGENTS.md` D20：改 host 只靠热重载不生效）。

重启后：
- 设置页出现「私信监听」卡片
- 按钮可点

## 六、两种启动方式都保留

| 方式 | 适合 |
|---|---|
| **插件设置页点按钮** | 日常用（推荐） |
| 终端 `cli.app dm-watch` | 调试、或想看实时日志 |

两者起的是同一个东西，互斥（重复启动会被检测到）。
