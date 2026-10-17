"""cli 包：**人用**命令行入口。

任务书 §3 原文：*"``review`` 模式的批准由本机人用 CLI/页面 ``companion approve <draft_id>``
写入 ``approved_at``，**不要把批准接口暴露为模型可调用的 MCP 工具**"*。

因此本包的命令**绝不**注册进 :mod:`bridge.server` 的工具列表。
恢复暂停、恢复风控停机、增加额度、改 UID 也只能由人通过这里或直接改配置完成。
"""
