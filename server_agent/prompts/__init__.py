"""提示词层：系统提示词模板、变体与结构化报告。

- 模板用 `string.Template`（$var 占位），不引入 Jinja2：报告 schema 里全是花括号，
  用 `str.format` 会与之冲突，Template 的 `$` 语法更省心且零依赖。
- 变体（variant）用于 A/B 对比：`sre` 是带方法论的正经提示词，`plain` 是反面教材，
  第 15 章的评估会量化两者的差距。
"""

from __future__ import annotations

import socket
import platform
from pathlib import Path
from string import Template

from server_agent.prompts.report import (
    Action,
    DiagnosticReport,
    Evidence,
    ReportParseError,
    extract_json,
    json_schema_text,
    parse_report,
)

PROMPTS_DIR = Path(__file__).resolve().parent
VARIANTS = ("sre", "plain")
DEFAULT_VARIANT = "sre"

REPAIR_INSTRUCTION = """你上一条回答不是合法的 JSON 报告。请把它改写成符合下面结构的 JSON 对象，
只输出 JSON，不要任何解释、不要 markdown 代码块：

$schema

上一条回答：
$previous"""


def load_template(variant: str = DEFAULT_VARIANT) -> str:
    if variant not in VARIANTS:
        raise ValueError(f"未知的提示词变体: {variant}（可用：{', '.join(VARIANTS)}）")
    return (PROMPTS_DIR / f"system_{variant}.md").read_text(encoding="utf-8")


def render_system_prompt(variant: str = DEFAULT_VARIANT, *, tools: list[str] | None = None,
                         hostname: str | None = None, os_name: str | None = None) -> str:
    """把模板渲染成最终的系统提示词。工具清单与报告 schema 会写进去。"""
    return Template(load_template(variant)).safe_substitute(
        hostname=hostname or socket.gethostname(),
        os=f"{os_name or platform.system()} {platform.release()}",
        tools=", ".join(tools or []) or "（无）",
        report_schema=json_schema_text(),
    )


def repair_prompt(previous: str, schema_model: type = DiagnosticReport) -> str:
    """构造「把你上一条回答改写成 JSON」的指令。

    注意 Template.substitute 要求所有占位符都传值，所以 previous 必须在这里就给出来
    （曾经写成返回 Template 对象，调用方只补了 schema，直接 KeyError: 'previous'）。
    """
    return Template(REPAIR_INSTRUCTION).substitute(
        schema=json_schema_text(schema_model), previous=previous[:4000])


__all__ = [
    "Action", "DiagnosticReport", "Evidence", "ReportParseError",
    "DEFAULT_VARIANT", "VARIANTS", "load_template", "render_system_prompt",
    "parse_report", "extract_json", "json_schema_text", "repair_prompt",
]
