# 生产闭环（#1）—— 调度、DSH 决策、汇报怎么真正串起来

> 状态：**✅ M1/M2 已实现（2026-10-18）**；M3 的本地日报/私信已有，`@` 通道仍未接。
> 实现落点：
>
> * `core/cycle.py` —— 单轮 `run_cycle`（发现→阅读→短包+完整内容→投递→等完成）；
> * `core/cycle_scheduler.py` —— 随 DSH 启停的调度判定；
> * `core/cycle_content.py` —— 3 短包 + 2500 规则完整内容；
> * `core/session_status.py` —— 轮询会话 `turn/end`；
> * `plugin/index.js` + `cli.app dm-watch --cycle` —— 自动启动。
>
> 来源：Codex 审查 issue #1 + [`HANDOFF.md`](../HANDOFF.md) §6。
> 运行记录不再全是 manual：`runs.trigger_source` 会出现 `host_start` / `schedule`。

---

## 0. 一句话

把 **定时唤醒 → 发现/阅读 → 豆包预处理 → DSH 最终判断与落库 → 按规则汇报**
串成一个**可恢复、不重复公开写入、失败如实表达**的循环。

---

## 1. 现状核对（实现后）

| 能力 | 现状 | 证据 |
|---|---|---|
| 时间判定 | ✅ | `core/schedule.py::evaluate_due` |
| 调度执行器 | ✅ **已有** | dm-watch 的 `CycleScheduler`，到期才跑；随 DSH 启停 |
| 单轮编排 | ✅ | `core/cycle.py::run_cycle`（复用 Pipeline，不复制抓取） |
| DSH 最终判断 | ✅ **已闭环** | 投递 `followup` → 轮询 `turn/end` → 记录 conclusion |
| 主号汇报 | ⚠️ 本地日报/私信 ✅；`@` 通道未接 | `core/mention.py::try_mention` 仍零调用 |
| 恢复/幂等 | ✅ | 任务锁、未结束 run 防重入、`uncertain` 冻结、`runs` 表 |

---

## 2. 目标流程（单一入口）

```text
timer tick（插件生命周期内，随宿主启动/停止）
  └─ evaluate_due(db, config, now)          # 已有：模式/峰谷/防抖/每日轮次
       └─ run_cycle(trigger_source, run_id) # 新增：唯一编排入口
            ├─ 1. 开始记账：runs(start)     # 先落库，再干活
            ├─ 2. 健康/暂停/锁/配额检查     # 任何一道不过 → 记 skipped，不发请求
            ├─ 3. discover + read_video      # 复用 Pipeline，不复制抓取逻辑
            ├─ 4. 豆包短卡（prepare_batch） # 只产出**待审材料**，不是最终结论
            ├─ 5. 投递 DSH 工作会话         # 复用 agents.followup；必须带 role
            ├─ 6. 等 turn/end 完成          # 轮询 + 超时；只看 reason.kind=completed
            ├─ 7. 收 DSH 结论               # DSH 调 save_analysis（最终判断在它那边）
            ├─ 8. 本地日报（必有）          # 失败也要写"这轮为什么没成"
            ├─ 9. 主号通知（按配置）        # 私信/本地；公开 @ 走同一发布门禁
            └─ 10. runs(finish)            # conclusion 如实：ok / partial / failed / skipped
```

**硬约束**

* **单一入口**：任何触发方式（手动 / 定时 / 宿主启动）都走 `run_cycle`，只差
  `trigger_source`；**不复制第二套抓取逻辑**。
* **DSH 是最终决策者**：豆包草稿不得直接当结论；`save_analysis` 必须由 DSH 调。
* **完成判定**：投递成功 ≠ 完成。唯一可信信号是 `turn/end.reason.kind == "completed"`
  （HANDOFF §5.7）；超时/失败写 `conclusion`，不冒充成功。
* **不重复公开写入**：阶段化 + 幂等键 + `sent_pending_verify`/`uncertain` 冻结；
  进程重启后从最后一个**已完成阶段**继续，已发出的动作绝不重发。
* **通知分级**：本地日报永远有；主号私信走 `dm_push`；公开 `@` 必须过发布门禁，
  发后用 `ApiMentionVerifier` 核 `owner_mid`，查不到记 `uncertain`，不重发凑成功。

---

## 3. 分阶段落地（建议顺序）

| 阶段 | 内容 | 状态 |
|---|---|---|
| **M1** | `run_cycle` + `runs` 账本 + 插件内定时器（随宿主启停） | ✅ 已实现（`cycle.py` / `cycle_scheduler.py` / `dm-watch --cycle`） |
| **M2** | DSH 工作会话投递 + 完成判定 + 结论回收 | ✅ 已实现（`session_status.py` 轮询 `turn/end`） |
| **M3** | 汇报：本地日报 + 主号私信 + `@` 门禁与核验 | ⚠️ 日报/私信已接；`@` 通道仍未接 |
| **M4** | 可恢复阶段 + 跨重启续跑 | ⚠️ 有任务锁/未结束 run 防重入；阶段化续跑仍是后续项 |

> 每阶段都先做**替身端到端**测试，再做宿主只读验收；**不要把公开写作为调度验收的前置步骤**。

---

## 4. 必须新增的验收测试（从 issue #1 提炼）

- 假时钟推进触发两轮，`run_id` 不同；第二轮不把已读当新发现。
- 双重 tick、宿主反复启动、上一轮未完成时不并发开两轮。
- DSH 只投递未结束时不能标完成；失败/取消有明确 conclusion。
- `observe` 模式完整跑到"DSH 最终笔记 + 本地日报"，公开执行器调用次数严格为 0。
- 三态通知与日志一致；"只写了 @昵称 / 取得 rpid / 真实成员匹配"三阶段不得混为一谈。
- 退出宿主后 timer / 子进程停止；平台 blocking 时下一轮不发请求。
- `runs` 账本：`read_n` 用视频数（不是 evidence 数）；真实只读读取不得标 `simulated=True`。

---

## 5. 明确不做

- 不为此重写技术栈（Python bridge / 插件结构保持）。
- 不把"定时跑通"当作"真实发布已验收"。
- 不加通用浏览器 / 任意 shell 到定时 preset（沿用 `dsh/restrict-bili-agent-tools.mjs`）。
- 不轮换 IP/账号、不伪造客户端、不代解验证码。

---

## 6. 开放问题（需要主人拍板）

1. **调度载体**：复用 dm-watch 进程，还是由插件另起一个 timer？两者都要保证随宿主启停。
2. **值班员 vs 工作会话**：生产循环是投给固定值班员会话，还是每轮新会话？
   （固定会话会累积上下文；新会话需要保证模型选择已写入 `model/selection`）
3. **一轮的边界**：连续跑时，上一轮投递未完成是否允许下一轮开始？（建议：不允许，记 skipped）
4. **通知频次**：日报 + 私信 + `@` 三者是否都开？默认建议只开本地日报与主人私信。

---

## 7. 与现有文档的关系

- 调度语义与接线：`docs/schedule-wiring.md`
- 专用会话/记忆/唤醒：`docs/dedicated-conversation.md`
- 发布状态机：`core/publish.py` 顶部 docstring
- 验收现状：`docs/acceptance.md`（本轮审查整改见 §6）
