# 自我进化：提案制 + 四道硬闸（2026-10-17）

## 主人指令

> 「自我进化我觉得可以开始了」

## 一、它到底能做什么

**能**：基于当前配置与运行数据，**提出**一个参数的小步调整建议（如
「打分权重 0.80 → 0.82」）。

**不能**：自己把建议变成生效的配置。**必须你批准**。

```
生成提案（AI，写 pending）
   ↓
你看提案（cli.app evolve）
   ↓
你批准（cli.app evolve --apply <id>）→ 才写进 config.yaml
   或你拒绝（--reject <id>）→ 记 rejected
```

## 二、四道硬闸（照抄 `bilibili_learning_bot`）

| # | 闸 | 实现 | 为什么 |
|---|---|---|---|
| 1 | **`auto_apply` 写死 False** | 常量 `AUTO_APPLY_FORBIDDEN = False`，**没有**任何"自动应用"的代码路径 | "AI 改自己"必须人点头 |
| 2 | **单次步长 5%** | 比率型绝对 0.05；天数型相对 5%（30 天→最多 1.5 天） | 防止一步改飞 |
| 3 | **参数硬边界** | `score_topic_weight ∈ [0.5, 0.95]` 等，越界**直接拒绝**（不是夹紧） | 边界由人定 |
| 4 | **每日 2 份** | 查**数据库**计数，不是内存 | 重启也不失效 |

### 可进化的三个参数（**都不涉及授权**）

| 参数 | 范围 | 默认 | 含义 |
|---|---|---|---|
| `discovery.score_topic_weight` | 0.50 ~ 0.95 | 0.80 | 打分里"主题相关度"占多少 |
| `discovery.min_score_for_comment` | 50 ~ 95 | 75 | 低于这个分不评论 |
| `discovery.published_within_days` | 1 ~ 90 | 30 | 只看最近几天 |

### 永久禁改（授权 / 账号 / 密钥）

```
interaction.mode              interaction.dry_run
interaction.publish_enabled   interaction.pause
interaction.dm_reply_enabled  account.agent_mid
account.owner_mid             text_worker.api_key_env
```

> 这些**不会**出现在提案里，也无法通过任何参数名变体绕过（有前缀检查 + 测试）。

## 三、用法

```bash
# 看框架与待审提案
./.venv/Scripts/python.exe -m cli.app evolve

# 生成一份提案（短名/完整路径都行，留空自动挑）
./.venv/Scripts/python.exe -m cli.app evolve --propose --param score_topic_weight

# 批准并写入 config.yaml
./.venv/Scripts/python.exe -m cli.app evolve --apply prop_xxx

# 拒绝
./.venv/Scripts/python.exe -m cli.app evolve --reject prop_xxx
```

## 四、写盘怎么保证安全

应用提案时会：

1. **再查一次白名单**（防数据库被手动塞进奇怪参数）；
2. **行级替换** `config.yaml` —— 不 `yaml.dump`，所以**注释不会丢**（实测 10 条注释完好）；
3. **先备份** `config.yaml.bak-evolve`；
4. `.tmp` + `os.replace` **原子替换**；
5. 找不到该行就**返回失败不写**（擅自插入会破坏 YAML 结构）。

## 五、真机验证

```
生成提案 → score_topic_weight: 0.8 → 0.85（正好 5% 步长）
此时 config.yaml 仍是 0.80  ← 提案不自动生效 ✅
批准 → config.yaml 变成 0.85
注释 10 行完好 · .bak-evolve 已生成 · 授权字段未动 ✅
```

## 六、★ 过程中修掉一个真 bug（安全闸本来是假的）

**现象**：每日限额测试怎么都不触发——提 3 份，库里永远只有 1 份。

**根因**：提案 ID 用「时间戳 + 参数/值哈希」生成，
于是**同参数同值的第二份提案算出同一个 ID**，被 `INSERT OR REPLACE`
**覆盖**掉了 ⇒ 库里计数永远是 1 ⇒ **每日 2 份的限额形同虚设**。

**修法**：ID 已存在就递增序号（`prop_xxx_2`、`prop_xxx_3`），保证每次插入是新行。

**加了专门的回归测试** `test_duplicate_proposals_get_distinct_ids`——
提 2 份必须真有两行、计数必须是 2。

> 这条如果没测出来，限额就是"写着 2、实际无限"，而**没有任何报错**。
> 属于"静默失效的安全闸"，比崩溃危险得多。

## 七、自检

| 项 | 结果 |
|---|---|
| 新增测试 | **27 项**（`test_evolution_store.py`） |
| 全量 | ✅ **963 passed** |
| `ruff` / `mypy` | ✅ 全过 |
| schema | v8 → **v9**（`evolution_proposals` 表） |
| 真机闭环 | ✅ 生成 → 批准 → 写盘 → 注释保留 → 备份存在 |
| 限额 | ✅ 查库计数，重启不失效 |
| AI 越权 | ✅ `decided_by != "cli"` 时拒绝应用/拒绝 |
