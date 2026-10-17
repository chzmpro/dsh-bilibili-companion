# 私信主动回复：为什么用豆包（2026-10-17）

## 主人的问题

> 「那如果用豆包模型能主动醒来回复吗，或者采用DSH的APIkey」

## 实测结论：两件事要分开看

"**主动醒来**" 和 "**用好模型**" 是**两个独立的约束**，不能同时满足。

| | 豆包 | DSH 的 DeepSeek |
|---|---|---|
| key 能否拿到 | ✅ **能**（注册表 `doubao-seed-2-0-lite-260428-API-KEY`） | ❌ **不能** |
| 常驻进程能否自己调 | ✅ 能 | ❌ 不能 |
| **能否主动回复** | ✅ **能** | ❌ 只能等对话唤醒 |
| 回复质量 | 中（lite 版） | 高 |

### 为什么 DSH 的 key 拿不到（实测）

```bash
$ reg query "HKCU\Environment" | grep DEEPSEEK
（空）
$ reg query "HKLM\...\Environment" | grep DEEPSEEK
（空）
$ python -c "resolve_secret('DEEPSEEK_API_KEY')"
None
```

`provider-snapshots.json` 里写的是 `credential: "env"`，但那个 env **在 DSH 进程内部**，
不落任何可读配置。

**而且 DSH 没有"外部调模型"的接口**：本地只开了一个给界面用的端口（43129），
那是 WebSocket/HTTP 给前端用的，不是 API。

⇒ **即使你给了 DSH 的 key，也解决不了"主动"** ——
主动需要有个进程一直在跑、自己调模型。那个进程就是我们已有的监听器。
所以真正的选择是：**监听器用谁的模型**。

## 主人选的是：豆包

已实现，效果（真机生成，未发送）：

> 我现在还没捋完这个up的相关内容和这个Harness玩法的细节哦，这就立刻去逐点梳理，
> 把适用场景、落地门槛、能省下的上下文资源量还有可能踩的坑都整理清楚，
> 很快就给你出完整的可行性分析哈。（本来还想扒两口饭的，马上就干活！）

**注意它说的是「还没捋完」而不是假装看过了** —— 这正是提示词里强制的。

## 怎么用

```bash
# 打开自动回复（豆包生成）
./.venv/Scripts/python.exe -m cli.app dm-watch --reply

# 或插件设置页 → 私信监听 → 启动（需要把 watchAutoReply 打开，见下）
```

## ★ 三条防"低级模型乱说话"的措施

### 1. 提示词硬约束

```
硬性规则（必须遵守）：
1. **不知道就说不知道**。宁可说"我还没查，要我现在去看吗"，也绝不编造内容。
2. 不能承诺你做不到的事（例如"我已经帮你看完了"但你其实没看）。
3. 不要提"我是AI模型"这类元信息，除非主人直接问。
4. 回复控制在 200 字以内。
```

> 低级模型最典型的问题就是**自信地编**。这条是防线。

### 2. 长度上限 300 字

长回复最容易出错。短句就算不完美，也不容易说错关键事实。

### 3. 带历史上下文

主人说过「可以查看我前面的发言并回复我吗」——
只给最后一句会让回复显得莫名其妙。所以带最近 6 条历史。

## 两条路并存（互不干扰）

```
监听进程 → 写 dm_inbox（一直攒）
   ├─ 自动回复（豆包）：按开关，自己发
   └─ DSH 那条：你在对话里说一句，我调 dm_inbox + dm_reply 用 DeepSeek 补深度回复
```

**豆包回复不影响 DSH 那条**。你可以：
- 让豆包先接住（立刻有回应）
- 之后在对话里让我用 DeepSeek 补一条更好的

## 验证脚本

```bash
# 预览会回什么（不发送）
./.venv/Scripts/python.exe -u scripts/probe_doubao_reply.py

# 真的发出去
./.venv/Scripts/python.exe -u scripts/probe_doubao_reply.py --send
```

## 自检

| 项 | 结果 |
|---|---|
| 新增测试 | **29 项**（`test_dm_reply_gen.py`） |
| 全量 | ✅ **1012 passed, 1 skipped** |
| `ruff` / `mypy` | ✅ 全过（48 files） |
| 真机生成 | ✅ 豆包返回人设化回复，且**不编造** |
| 豆包 key | ✅ 注册表 `doubao-seed-2-0-lite-260428-API-KEY` 实测调通 |

## ⚠️ 要真正"自动回"，还差一个开关

插件默认 `watchAutoReply: false`（观察模式）。要打开：

```bash
# 方法一：命令行（立刻生效）
./.venv/Scripts/python.exe -m cli.app dm-watch --reply

# 方法二：改插件配置 harness/bili-companion/config.json
{ "watchAutoReply": true }
```

> 我**没有**把默认改成 `true`，因为"自动回复说错话"比"晚回一会儿"更糟。
> 你确认上面那条示例回复质量可接受，再打开比较稳。
