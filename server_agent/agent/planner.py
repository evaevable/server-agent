"""规划器：先出计划，再逐步执行（Plan-and-Execute）。

为什么需要它？ReAct 是「走一步看一步」，遇到复杂问题时容易：
- 绕圈（反复查同类信息）；
- 漏项（忘了检查某个资源）；
- 没有全局视野（用户看不出它打算怎么办）。

Plan-and-Execute 的取舍：
- 优点：有计划、可展示、可复规划、能对齐「专家会怎么查」；
- 代价：多一次模型调用（生成计划）；计划可能一开始就不对（所以要允许重规划）。

本章的实现刻意保持简单：
1. 生成计划（一次 LLM 调用，产出步骤列表）；
2. 把计划写进提示词，让 ReAct 循环按计划推进；
3. 每步结束后打勾；若某步失败，允许一次「重规划」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from string import Template
from typing import Any

from server_agent.llm.base import LLMClient, LLMError, Message

# 注意：模板里必须用 $question 而不是 {question}——示例 JSON 自带花括号，
# 用 str.format 会把它当成字段名（KeyError: '"steps"'）。这是第 07 章踩过的同一个坑。
PLAN_INSTRUCTION = Template("""先不要执行任何工具。请为下面的问题制定一个排查计划。

要求：
- 3-6 步，每步是一个可验证的动作（会用哪个工具、看什么指标）；
- 按「先宏观后微观」排序，先只读后变更；
- 只输出 JSON，格式：{"steps": [{"goal": "...", "tool_hint": "..."}], "rationale": "一句话说明思路"}

问题：$question""")


@dataclass
class PlanStep:
    goal: str
    tool_hint: str = ""
    status: str = "pending"      # pending | done | failed | skipped


@dataclass
class Plan:
    rationale: str = ""
    steps: list[PlanStep] = field(default_factory=list)
    replans: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"rationale": self.rationale, "replans": self.replans,
                "steps": [{"goal": s.goal, "tool_hint": s.tool_hint, "status": s.status} for s in self.steps]}

    def render(self) -> str:
        lines = ["[当前计划]"]
        if self.rationale:
            lines.append(f"思路：{self.rationale}")
        for i, s in enumerate(self.steps, 1):
            mark = {"pending": " ", "done": "x", "failed": "!", "skipped": "-"}[s.status]
            hint = f"（建议工具：{s.tool_hint}）" if s.tool_hint else ""
            lines.append(f"{i}. [{mark}] {s.goal}{hint}")
        lines.append("请按计划推进；如某步受阻，说明原因并调整计划，不要硬凑。")
        return "\n".join(lines)

    def mark_next_done(self) -> None:
        for s in self.steps:
            if s.status == "pending":
                s.status = "done"
                return

    def mark_last_failed(self) -> None:
        """标记「当前正在做的那一步」失败：计划是按顺序推进的，所以是第一个未完成步骤。"""
        for s in self.steps:
            if s.status == "pending":
                s.status = "failed"
                return

    @property
    def pending(self) -> int:
        return sum(1 for s in self.steps if s.status == "pending")


class Planner:
    """生成计划。用一次非流式 LLM 调用，失败时退化为「无计划」而不是报错。"""

    def __init__(self, llm: LLMClient, max_steps: int = 6):
        self.llm = llm
        self.max_steps = max_steps

    async def make_plan(self, question: str) -> Plan | None:
        messages = [Message.user(PLAN_INSTRUCTION.substitute(question=question))]
        try:
            resp = await self.llm.chat(messages, max_tokens=600)
        except LLMError:
            return None
        return self.parse(resp.message.content or "")

    def parse(self, text: str) -> Plan | None:
        from server_agent.prompts.report import extract_json

        try:
            data = extract_json(text)
        except Exception:  # noqa: BLE001 —— 计划解析失败不该让排查失败
            return None
        steps = []
        for item in (data.get("steps") or [])[: self.max_steps]:
            if isinstance(item, dict) and item.get("goal"):
                steps.append(PlanStep(goal=str(item["goal"])[:200], tool_hint=str(item.get("tool_hint") or "")[:60]))
            elif isinstance(item, str):
                steps.append(PlanStep(goal=item[:200]))
        if not steps:
            return None
        return Plan(rationale=str(data.get("rationale") or "")[:300], steps=steps)
