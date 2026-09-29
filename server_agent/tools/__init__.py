"""工具层。导入本包即注册全部内置工具到默认注册表 registry。"""

from server_agent.tools.registry import Tool, ToolError, ToolRegistry, ToolResult, registry, tool
from server_agent.tools import memory, system  # noqa: F401  —— 触发 @tool 注册

__all__ = ["Tool", "ToolError", "ToolRegistry", "ToolResult", "registry", "tool"]
