"""结构化诊断报告：模型最终必须交出这个形状的结论。

为什么要结构化？
1. 前端能渲染成卡片（而不是一大段散文），用户一眼看到根因、置信度、建议动作；
2. 评测可以**自动判定**「根因对不对」，不用人去读自然语言；
3. 逼模型把「猜的」和「有证据的」分开写（findings 里的每条都必须引用具体数据）。

注意：模型不一定听话。所以 schema 既写进提示词（让它知道形状），
也在程序里做校验（不接受形状不对的结论）。
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

Severity = Literal["info", "warning", "critical"]
Confidence = Literal["high", "medium", "low"]


class Evidence(BaseModel):
    """一条观察 + 它的依据。evidence 必须引用工具返回的具体数据。"""

    claim: str = Field(description="观察到的结论，一句话")
    evidence: str = Field(description="支撑数据，引用工具返回的原始数值/日志片段")


class Action(BaseModel):
    """建议动作。risk 表示这个动作的危险程度，供审批人参考。"""

    description: str = Field(description="要做什么，一句话")
    risk: Literal["read", "low", "high"] = Field(description="read=只读；low=影响小；high=需要审批")
    command: str | None = Field(None, description="可直接执行的命令（只读动作才给命令）")


class DiagnosticReport(BaseModel):
    summary: str = Field(description="现象：一句话说清发生了什么")
    severity: Severity = Field(description="严重程度")
    findings: list[Evidence] = Field(default_factory=list, description="观察与依据，至少一条（除非信息完全不足）")
    root_cause: str | None = Field(None, description="根因。证据不足时留空，不要硬猜")
    confidence: Confidence = Field(description="对根因判断的置信度")
    actions: list[Action] = Field(default_factory=list, description="建议动作，按优先级排序")
    data_gaps: list[str] = Field(default_factory=list, description="还缺哪些信息才能更确定")


class ReportParseError(ValueError):
    """没能从模型输出里解析出合法报告。"""


FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def json_schema_text(model: type[BaseModel] = DiagnosticReport) -> str:
    """把 schema 转成给人/模型看的紧凑 JSON（写进提示词用）。"""
    return json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2)


def extract_json(text: str) -> dict:
    """从模型输出里抠出 JSON 对象。

    模型交出来的形态很花：纯 JSON、```json 包起来、前面还有一段解释。
    依次尝试：整段解析 -> 代码块里解析 -> 第一个平衡花括号片段。
    """
    text = (text or "").strip()
    for candidate in (text, *(m.group(1) for m in FENCE_RE.finditer(text))):
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start : i + 1])
                        if isinstance(data, dict):
                            return data
                    except json.JSONDecodeError:
                        pass
                    break
        start = text.find("{", start + 1)
    raise ReportParseError("输出里找不到合法的 JSON 对象")


def parse_report(text: str) -> tuple[DiagnosticReport | None, str | None]:
    """解析并校验报告。返回 (报告, 错误说明)；失败时报告为 None。"""
    try:
        data = extract_json(text)
    except ReportParseError as e:
        return None, str(e)
    try:
        return DiagnosticReport.model_validate(data), None
    except ValidationError as e:
        problems = "；".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()[:5])
        return None, f"报告字段不符合要求：{problems}"
