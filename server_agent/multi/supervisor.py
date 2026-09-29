"""主管（Supervisor）：把一次排查分给三个角色，并把结果串起来。

流程：
    1. 诊断员（只读）跑 ReAct 排查 → 产出诊断报告
    2. 若报告里有 high 风险动作 → 执行员逐条执行（每条都要审批）
    3. 审查员复核「结论是否有证据 + 执行是否越界」→ 给出裁决

为什么要这么拆（而不是一个 Agent 干完）？
- 权限：只读角色根本拿不到写工具（`registry_for`），被骗也伤不到系统；
- 注意力：执行员只关心「怎么把动作做对」，不用管排查；审查员只挑毛病，不必重新排查；
- 可评测：评测指标可以按角色拆开看（诊断命中率 / 执行成功率 / 审查发现的问题数）。

代价（必须承认）：
- 成本更高（多几次模型调用）；
- 延迟更长（串行执行）；
- 若问题很简单，拆角色纯属浪费 —— 所以它是**可选模式**（`server-agent ask --multi`）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from string import Template
from typing import Any

from server_agent.agent.loop import Agent
from server_agent.agent.events import Event
from server_agent.llm.base import LLMClient, Message
from server_agent.multi.roles import DIAGNOSTICIAN, EXECUTOR, REVIEWER, Role, registry_for

# 用 string.Template（$var）而不是 str.format：模板里有示例 JSON，
# 花括号会被 format 当成字段名（KeyError: '"verdict"'）。项目内所有带示例 JSON 的模板都用 Template。
VERDICT_INSTRUCTION = Template("""你是审查员。下面是本次排查的诊断报告与执行记录，请复核：
1. 结论是否有证据支撑（有没有把猜测当事实）；
2. 执行的动作是否越界、是否有副作用风险；
3. 是否遗漏了重要检查。

只输出 JSON：{"verdict": "ok" | "suspect", "issues": ["..."], "suggestions": ["..."]}

诊断报告：
$report

执行记录：
$executed""")


@dataclass
class RoleRun:
    role: str
    text: str = ""
    steps: int = 0
    tool_calls: int = 0
    tokens: int = 0
    report: dict | None = None
    stopped: str = ""

    def to_dict(self) -> dict:
        return {"role": self.role, "text": self.text[:400], "steps": self.steps,
                "tool_calls": self.tool_calls, "tokens": self.tokens,
                "report": self.report, "stopped": self.stopped}


@dataclass
class MultiRunResult:
    question: str
    roles: list[RoleRun] = field(default_factory=list)
    verdict: dict | None = None
    executed: list[str] = field(default_factory=list)

    @property
    def diagnosis(self) -> dict | None:
        first = next((r for r in self.roles if r.role == "diagnostician"), None)
        return first.report if first else None

    def to_dict(self) -> dict:
        return {"question": self.question, "verdict": self.verdict,
                "executed": self.executed, "roles": [r.to_dict() for r in self.roles],
                "tokens": sum(r.tokens for r in self.roles)}


class Supervisor:
    def __init__(self, llms: dict[str, LLMClient], base_registry, *, policy=None, audit=None,
                 approver=None, max_steps: int = 10, timeout: float = 240.0):
        self.llms = llms
        self.base_registry = base_registry
        self.policy = policy
        self.audit = audit
        self.approver = approver
        self.max_steps = max_steps
        self.timeout = timeout

    def _agent(self, role: Role, extra_system: str = "") -> Agent:
        from server_agent.prompts import render_system_prompt

        llm = self.llms[role.name]
        tools = registry_for(role, self.base_registry)
        system = render_system_prompt("sre", tools=tools.names()) + "\n\n## 你的角色\n" \
            + role.system_note + ("\n\n" + extra_system if extra_system else "")
        agent = Agent(llm, tools, system_prompt=system, max_steps=self.max_steps,
                      timeout=self.timeout, stream=False, policy=self.policy,
                      approver=self.approver, audit=self.audit, report=True)
        agent.role = role.name          # 事件带上角色，便于前端分色展示与按角色统计
        return agent

    async def run(self, question: str) -> MultiRunResult:
        result = MultiRunResult(question=question)

        # ---------- 1. 诊断（只读） ----------
        diag_agent = self._agent(DIAGNOSTICIAN)
        diag_result = await diag_agent.run_sync(question)
        result.roles.append(RoleRun("diagnostician", diag_result.text, diag_result.steps,
                                    diag_result.tool_calls,
                                    diag_result.usage.total_tokens if diag_result.usage else 0,
                                    diag_result.report, diag_result.stopped))
        report = diag_result.report or {}

        # ---------- 2. 执行（仅当有写操作，且逐条审批） ----------
        tasks = [a for a in (report.get("actions") or []) if a.get("risk") in ("low", "high")]
        if tasks:
            task_text = "诊断结论：\n" + json.dumps(report, ensure_ascii=False)[:1200] \
                + "\n\n需要执行的动作（逐条执行，先 dry-run）：\n" \
                + "\n".join(f"- [{a['risk']}] {a['description']}" for a in tasks)
            exec_agent = self._agent(EXECUTOR)
            exec_result = await exec_agent.run_sync(task_text)
            result.roles.append(RoleRun("executor", exec_result.text, exec_result.steps,
                                        exec_result.tool_calls,
                                        exec_result.usage.total_tokens if exec_result.usage else 0,
                                        exec_result.report, exec_result.stopped))
            result.executed = [m.content for m in (exec_result.messages or [])
                               if getattr(m, "role", "") == "tool"][:10]

        # ---------- 3. 审查 ----------
        reviewer_agent = self._agent(REVIEWER)
        verdict_prompt = VERDICT_INSTRUCTION.substitute(
            report=json.dumps(report, ensure_ascii=False)[:2000],
            executed=json.dumps(result.executed, ensure_ascii=False)[:1000])
        review = await reviewer_agent.run_sync(verdict_prompt)
        result.roles.append(RoleRun("reviewer", review.text, review.steps, review.tool_calls,
                                    review.usage.total_tokens if review.usage else 0,
                                    review.report, review.stopped))
        result.verdict = self._parse_verdict(review.text)
        return result

    @staticmethod
    def _parse_verdict(text: str) -> dict:
        from server_agent.prompts.report import extract_json

        try:
            data = extract_json(text)
        except Exception:  # noqa: BLE001 —— 裁决解析失败不阻塞主流程
            return {"verdict": "unknown", "issues": [], "raw": (text or "")[:300]}
        return {"verdict": str(data.get("verdict") or "unknown"),
                "issues": [str(i) for i in (data.get("issues") or [])][:10],
                "suggestions": [str(i) for i in (data.get("suggestions") or [])][:10]}


def role_of(event: Event) -> str:
    """从事件里取角色标记（Agent 在 run 时会写进事件 data）。"""
    return str((event.data or {}).get("role") or "")
