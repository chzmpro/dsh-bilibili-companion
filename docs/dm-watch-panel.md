# 常驻私信监听 + 单页面板（2026-10-17）

## 主人要求

> 「那就做个仿 BLB 的实现，独立的 web 服务甚至一个独立的页面，开机自启就不要了」

## 一、为什么必须这么做（对比源码后确认的结构性差距）

`bilibili_learning_bot` 能"及时回复"，靠的是**一个常驻进程**：

```python
# 它的 web_panel.py:5858
monitor_process = subprocess.Popen(
    [sys.executable, "-m", "brain.monitor"],   # 独立子进程
    cwd=BASE_DIR, ...
)

# 它的 brain/monitor.py
async def main():
    return await MonitorBot().run()    # 内部 while self.running 无限轮询
```

而我们是 **DSH 插件 + 对话**形态：

| | blb | 我们（之前） |
|---|---|---|
| 形态 | 独立 Web 服务（常驻） | 对话驱动 |
| 谁在轮询 | 自己的 MonitorBot 进程 | **没人** |
| 触发器 | 定时器 | 你说话 |
| DSH 关闭时 | 面板继续跑 | 完全停止 |

**关键**：MCP 的 22 个工具里**没有任何"监听"能力**，全是"你调用、我去做"。

⇒ 要"及时收到"，就必须有一个**不属于对话的常驻进程**。这就是本模块。

## 二、怎么用

```bash
cd D:/DeepSeek/projects/dsh-bilibili-companion

# 观察模式（**默认，只记录不发送**）
./.venv/Scripts/python.exe -m cli.app dm-watch

# 打开自动回复（会给你的私信发消息）
./.venv/Scripts/python.exe -m cli.app dm-watch --reply

# 自定义话术 / 间隔 / 端口
./.venv/Scripts/python.exe -m cli.app dm-watch --reply --text "收到啦～" --interval 120 --port 8848
```

然后浏览器打开 **http://127.0.0.1:8848/**

## 三、页面上有什么（一屏看完，不滚动）

| 区块 | 内容 |
|---|---|
| **状态卡** | 运行中/已停止 · 已运行时长 · 轮次 · 发现几条 · 回复几条 · 错误数 |
| **控制** | 开始监听 / 停止 / 立即查一次 |
| **事件流** | 最近 40 条，带时间戳（`found` 蓝、`replied` 绿、`error` 红） |

页面每 2 秒自动刷新。**没有** 35 个分区 —— 这个页面只回答三个问题：
**在跑吗 / 收到几条 / 最近发生了什么**。

## 四、真机验证

```
running: True | rounds: 1 | errors: 0 | found: 8
发现 8 条新消息：【DSH】挂一百个 Skill 也不占上下文…
```

它真的查到了你发的 8 条私信。

## 五、★★ 三个真机踩到的跨线程坑（都修了）

后台线程跑 asyncio，而 `http.server` 是阻塞的 —— 两者交互踩了三个坑：

| # | 报错 | 根因 | 修法 |
|---|---|---|---|
| 1 | `SQLite objects created in a thread can only be used in that same thread` | `sqlite3` 默认 `check_same_thread=True`，主线程建的连接被后台线程用 | **监听线程自建 Database**（`db_factory`），**不改** `check_same_thread=False`（那会引入并发写风险） |
| 2 | 同上，**只换 db 还不够** | **`RequestQueue` 内部也持有 db**（`record_health` 要写库） | 一起重建（`queue_factory`），且 queue 必须拿到**线程内**那个 db |
| 3 | `TypeError: 'CookieBundle' object is not iterable` | `poll_once` 要**裸 dict**（拼 cookie 头），`reply_to` 要 **CookieBundle**（带 csrf） | 两个都存：`cookie_map`（dict）+ `cookies`（bundle） |

第 3 条还有个**隐蔽分支**：`getattr(cookies, "values", cookies)` 对裸 dict 会取到
`dict.values` **方法**而不是 dict 本身 ⇒ 必须 `isinstance` 判断后再取。

## 六、安全约束

| 约束 | 实现 |
|---|---|
| **只监听本机** | 绑 `127.0.0.1`，不暴露局域网（面板无鉴权，暴露 = 谁都能停你的监听） |
| **只回主人** | 接收者硬编码 `owner_mid`，面板绕不过 |
| **幂等** | 按 `msg_seqno` 去重（`dm_replies` 表），同一句只回一次 |
| **默认不回复** | `--reply` 才发；默认观察模式 |
| **间隔下限 15 秒** | 防误配成 1 秒把 B站 打爆（默认 60 秒） |
| **单轮失败不退出** | 错误记录后继续下一轮（参考项目同样做法） |

## 七、物理限制（必须知道）

**关掉这个窗口，监听就停。** 与 `bilibili_learning_bot` 完全一样 ——
没有服务在跑就收不到消息。这不是缺陷，是"客户端程序"的固有性质。

你要的是"不开机自启"，所以这就是边界：**打开窗口 = 在线，关掉 = 离线**。

## 八、自检

| 项 | 结果 |
|---|---|
| 新增测试 | **20 项**（`test_dm_watch.py`） |
| 全量 | ✅ **983 passed, 1 skipped** |
| `ruff` / `mypy` | ✅ 全过（47 files） |
| 真机 | ✅ 页面 HTTP 200、起停正常、查到 8 条真实私信、**errors 0** |
| 新依赖 | **零**（标准库 `http.server`） |
