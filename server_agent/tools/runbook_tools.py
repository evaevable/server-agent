"""Runbook 工具：让 Agent 主动查询与加载排障手册。

设计要点：**不要把手册整本塞进系统提示词**。
手册加起来几千字，全塞进去等于每次都花这份 token，而且和当前问题无关的手册会干扰判断。
正确做法是「按症状检索 → 只加载命中的那一本」。
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from server_agent.knowledge.runbooks import get_library
from server_agent.tools.registry import ToolError, tool


@tool
def list_runbooks(
    question: Annotated[str, Field(default="", description="当前要排查的问题（用于按症状匹配；留空则列出全部手册）")] = "",
    limit: Annotated[int, Field(ge=1, le=10, description="最多返回几本")] = 3,
) -> dict:
    """查询可用的排障手册（Runbook）。传入问题描述会按症状匹配最相关的几本；
    返回手册名与标题（不含正文）。拿到名字后用 load_runbook 加载具体步骤。"""
    lib = get_library()
    if not lib.names():
        return {"count": 0, "runbooks": [], "hint": "没有找到任何手册（runbooks/ 目录为空）"}
    if question.strip():
        hits = lib.search(question, limit=limit)
        if hits:
            return {"count": len(hits), "runbooks": [r.to_dict(with_body=False) for r in hits],
                    "hint": "用 load_runbook 加载其中一本的完整步骤"}
    return {"count": len(lib.names()), "runbooks": [
        r.to_dict(with_body=False) for r in list(lib.runbooks.values())[:limit]]}


@tool(max_chars=6000)
def load_runbook(
    name: Annotated[str, Field(min_length=1, max_length=60, description="手册名，如 disk-full")],
) -> dict:
    """加载一本排障手册的完整步骤。手册是「专家会怎么查」的流程化知识，
    请按它的顺序执行，并结合本机实际情况调整；手册不是命令清单，不要盲目照抄。"""
    lib = get_library()
    rb = lib.get(name)
    if rb is None:
        raise ToolError(f"没有这本手册: {name}（可用：{', '.join(lib.names()) or '无'}）")
    return {"name": rb.name, "title": rb.title, "symptoms": rb.symptoms, "body": rb.body}
