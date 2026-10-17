# 定时运行接线说明（任务书 §7 任务 7）

> ⚠️ 本文由 `core/schedule.py` 的 `SCHEDULE_WIRING_NOTE` **生成**，
> 与实现保持同步（`tests/test_schedule.py` 有断言防止文档漂移）。

## 接到 DSH 的日程能力

任务书 §7 任务 7 引用官方 Schedule 说明的**三条语义**（本项目据此设计）：

1. **宿主运行期间才调度** —— DSH 关闭时不会触发；
2. **发送记录不代表模型完成** —— 定时提醒投递成功 ≠ 任务做成；
3. **不保证恰好投递一次** —— 可能重复或遗漏。

因此交接点是这样的：

| 关注点 | 由谁负责 |
|---|---|
| "什么时候唤醒" | DSH Schedule（宿主级） |
| "这一轮该不该跑" | 本项目 `is_due()`（读 `runs` 表判断今天是否跑完） |
| "有没有重复发布" | 本项目幂等键 + `uncertain` 冻结（**不依赖**调度器） |
| "任务做成没有" | 本项目 `runs`/`actions` 表（**最终事实来源**） |

**推荐接线**（人用，需主人授权改 profile）：

0. **唤醒时机（主人已定）**：`schedule.mode: on_host_start` ——
   **打开 DSH 时唤醒**，而不是每日固定时刻。
   打开后先调 `companion_status`，看 `schedule.due`：
   * `due=true` → 今天还没跑完，干活；
   * `due=false` → 今天已完成（或距上次尝试不足 `min_gap_minutes`）→ 跳过。
   `schedule.daily_time` 仅在 `mode: daily` 时生效。
1. 用 `dsh/cordis.patch.fragment.yml` 里的 `bili-companion` preset
   （只挂窄 MCP 工具，不含通用浏览器/shell）。
2. 在 DSH 的日程里设一个与 `config.yaml` 的 `schedule.daily_time` 一致的唤醒点。
3. 唤醒后在**那个 preset 的会话**里让模型先调 `companion_status`，
   再按需 `prepare_batch`。

> ⚠️ **不要**把 `@daily` 的唤醒时间既写在 DSH 里又写成系统计划任务——
> 两个调度器同时唤醒仍会被本项目的单实例锁挡住（不会双发），
> 但会产生令人困惑的失败记录。选一个。

> ⚠️ 由于"不保证恰好投递一次"，本项目**不假设**每天只被唤醒一次：
> `is_due`（含最小间隔）+ 幂等键保证重复唤醒既不重复学习也不重复发言。
