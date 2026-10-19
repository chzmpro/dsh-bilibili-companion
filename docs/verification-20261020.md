# 自动轮恢复与跨平台只读：离线验证记录

验证基线：上游 `eb857a0b738c5bb06bc667342d7eb30466b00379`；Windows / Python 3.12，2026-10-20。

## 最终结果

- 完整 pytest：**1231 passed, 6 warnings，88.22 秒**。
- Ruff：全部通过。
- mypy：64 个源文件，无错误。
- 插件 `selfcheck.mjs`：全部通过，包括新设置控件及发布授权字段拒绝。
- 独立审查指出的外部 403 分类停机缺口已修复，3 个参数化回归及范围复核通过。
- 6 条 warning 均为原有 httpx 逐请求 cookies 弃用提示，不是失败。

## 可重复命令（Git Bash，项目根目录）

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 \
  TEMP="$PWD" TMP="$PWD" \
  .venv/Scripts/python.exe -m pytest tests/ -p no:cacheprovider -o addopts="" -q --tb=short
.venv/Scripts/python.exe -m ruff check . --no-cache
.venv/Scripts/python.exe -m mypy core bridge cli --no-incremental --cache-dir=nul
TEMP="$PWD" TMP="$PWD" node plugin/selfcheck.mjs
```

## 关键回归

- 候选 checkpoint 与 read 标记事务提交，异常/崩溃恢复不重复分析已保存卡。
- 整理后的投递包持久保存；确定未接收才有界重试，接收不确定不自动重发。
- 对应完整投递消息 → 消息 ID → 同一处理 turn 的完成关联；无关私信不算学习完成。
- 失败与成功调度分开，恢复不被当日已完成挡住，常驻调度重读配置。
- 两平台实际工具参数/返回形状；MCP SDK 2.x 字段、HTTP/stdio 生命周期与取消。
- 平台/账号/原生 ID 隔离、访问上下文跨重启、缓存隔离和平台风险持久停机。
- bridge 不返回旧账号候选；外部评论字符串作者 ID 不挤入 B 站数字 UID。
- 旧库升级保留 evidence ID 与外键，OCR 相同文本跨页仍保留页码来源。
- 本地 OCR 下载/字节限制、缺依赖、超时和取消回收；SRT/VTT 真实时段与来源。
- 外部内容写入在 B 站发布入口被拒绝，且拒绝发生在身份读取与配额预留之前。
- 原文留在本地证据/豆包；MCP 短包受预算约束，值班会话沿用 2500 规则。

## 不代表已经验收的事项

测试使用隔离数据目录、MockTransport、假后端/假投递，不连接真实账号，不调用付费模型。
未验收真实平台登录、访问可用率、真实 DSH 推荐投递与真实 token/费用。
外部后端默认禁用，需本人按[接入手册](platforms-douyin-xiaohongshu.md)单独部署、授权。
外部评论、点赞收藏、原生私信与主动 @ 未开放；本轮仅提供只读学习和现有 B 站汇报。
