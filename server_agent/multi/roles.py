"""角色定义：多 Agent 协作的分工与权限边界。

拆角色的第一动机不是「更聪明」，而是**权限隔离**：
- 诊断员只有只读工具，被骗也删不了东西；
- 执行员有写权限，但每次都要审批；
- 审查员只看结论与证据，负责挑毛病（它不执行任何东西）。

这是第 09 章「最小权限」在多 Agent 上的延伸：**让不同的 Agent 拿不同的钥匙。**
"""

from __future__ import annotations

from dataclasses import dataclass, field

READ_TOOLS = (
    "host_info", "cpu_memory_usage", "disk_usage", "top_processes", "listening_ports",
    "tail_file", "run_command", "remote_run", "remote_logs", "search_knowledge",
    "list_knowledge_docs", "list_runbooks", "load_runbook", "recall_host",
)
WRITE_TOOLS = ("restart_service", "kill_process", "clean_directory", "remote_restart_service",
               "remember_fact")


@dataclass
class Role:
    name: str
    title: str
    tools: tuple[str, ...]
    system_note: str
    extra: dict = field(default_factory=dict)

    def allows(self, tool_name: str) -> bool:
        return tool_name in self.tools


DIAGNOSTICIAN = Role(
    name="diagnostician", title="诊断员", tools=READ_TOOLS,
    system_note=("你只做**只读**排查：可以用工具查看状态、读日志、查文档与手册。"
                 "你不能修改系统；需要变更时写进建议动作并标注风险。"))

EXECUTOR = Role(
    name="executor", title="执行员", tools=WRITE_TOOLS + ("disk_usage", "listening_ports"),
    system_note=("你只负责**执行**已批准的变更动作。每次调用写操作工具都会被人工审批："
                 "如果被拒绝，不要反复重试，说明原因并停止。先 dry-run 再执行。"))

REVIEWER = Role(
    name="reviewer", title="审查员", tools=("disk_usage", "listening_ports", "tail_file",
                                            "recall_host"),
    system_note=("你是审查员：检查诊断结论有没有证据支撑、执行动作有没有越界或副作用。"
                 "不要重复排查全过程，只在必要时抽查 1-2 个工具。最后给出裁决 JSON。"))

ROLES = {r.name: r for r in (DIAGNOSTICIAN, EXECUTOR, REVIEWER)}


def registry_for(role: Role, base_registry):
    """按角色裁剪工具集：这是「最小权限」在多 Agent 下的落地方式。"""
    from server_agent.tools.registry import ToolRegistry

    filtered = ToolRegistry()
    for tool in base_registry.list():
        if role.allows(tool.name):
            filtered.register(tool)
    return filtered
