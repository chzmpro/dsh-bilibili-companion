# 自动轮恢复与跨平台只读 Implementation Plan

> 实施方式：本会话直接实施；独立闭环与适配器任务按 dispatching-parallel-agents 分开，主控负责运行链集成和完整验证。

**Goal:** 修复已复现的闭环缺口，并让两平台只读结果真正经过证据、豆包、DSH交付链。

**Architecture:** 兼容现有 BV 主键，外部内容用 namespaced key + content_sources 映射；标准 MCP 后端仅允许读取工具。投递包单独持久化，不将“已结束”混同“已完成”。

**Tech Stack:** Python 3.12、现有 MCP SDK、httpx、SQLite、可选 Tesseract。

**Spec:** ../specs/2026-10-19-cycle-multiplatform-design.md

## Global Constraints

- 默认 B 站；外部适配默认禁用；不修改真实账号配置。
- 不执行真实平台写操作、付费模型调用或自动化验证码处理。
- 迁移 append-only；已有迁移16，outbox17，内容映射与OCR18。
- 新行为先回归测试失败，再实现，再运行对应测试。

## Task 1: 自动轮恢复

- [x] 修改 core/cycle.py、cycle_scheduler.py、session_status.py，必要时 dm_deliver.py。
- [x] schema17 + storage.py 保存待投递包、接收状态、完成关联、成功与尝试时间。
- [x] tests/test_cycle*.py 中复现丢候选、失败算完成、无关轮次、配置不刷新。
- [x] 红绿测试通过，并核对接收不确定不重复发送。

## Task 2: 外部适配器

- [x] 新建 core/external_config.py、external_platforms.py，更新 platforms.py。
- [x] 使用 SDK 客户端，明确只读工具名/参数，持久传递 xsec_token。
- [x] tests/test_external_platforms.py 覆盖上游完整形状、错误、生命周期。
- [x] 核对无默认网络请求、无写入工具透传、无额外依赖和上游代码复制。

## Task 3: 实际运行链

- [x] tests/test_content_pipeline.py 先覆盖同 ID 隔离、证据、缓存、健康、重启、短包。
- [x] core/content.py 封装映射、缓存、健康与正文规范化，schema18保留旧数据。
- [x] core/config.py 注册 external_platforms，discovery.platforms 默认 bilibili。
- [x] runtime.py 构造/关闭适配器；pipeline.py 发现、阅读、批次真正调用它们。
- [x] core/ocr.py 可选OCR，tests/test_ocr.py 覆盖来源页码、受限下载、缺依赖与取消。
- [x] bridge/server.py 与 cli/app.py 暴露平台选择，沿用短包而非原始工具输出。
- [x] cycle_content.py 保留原始内容来源链接及2500规则；外部内容拒绝B站发布。

## Task 4: 交付

- [x] 更新 docs/platforms-douyin-xiaohongshu.md、config.example.yaml、README及入口说明。
- [x] 当前仓库环境跑完整pytest、ruff、mypy、node plugin/selfcheck.mjs。
- [ ] 复核差异和关键数据迁移；Fork分支提交、推送、创建原仓库PR并附验证与未验证边界。
