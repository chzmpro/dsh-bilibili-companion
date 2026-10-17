"""bridge 包：对 DSH 的唯一稳定界面（MCP）。

``bridge`` 只依赖 ``core``，**不依赖** ``cli``。
所有工具的返回统一为 ``{ok, code, data, message}`` 四字段。
"""
