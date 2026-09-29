"""工具层。导入本包即注册全部内置工具到默认注册表 registry。"""

from server_agent.tools.registry import Tool, ToolError, ToolRegistry, ToolResult, registry, tool
from server_agent.tools import (knowledge_tools, linux, memory, ops, remote,  # noqa: F401
                                runbook_tools, sandbox_tool, system)  # 触发 @tool 注册

__all__ = ["Tool", "ToolError", "ToolRegistry", "ToolResult", "registry", "tool"]
