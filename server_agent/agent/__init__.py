"""Agent 核心：事件模型与 ReAct 循环。

系统提示词不在这里——它属于 `server_agent.prompts`（第 07 章）。
Agent 只负责循环；提示词由调用方传入或由模板渲染。
"""

from server_agent.agent.events import AgentResult, Event
from server_agent.agent.loop import Agent

__all__ = ["Agent", "AgentResult", "Event"]
