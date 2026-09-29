"""知识检索工具：让 Agent 查团队文档。

使用姿势（写进工具描述里，模型才知道怎么用）：
先搜 → 看命中片段与来源 → 把来源写进结论。
不要凭记忆回答「你们公司的备份目录在哪」——那正是 RAG 存在的意义。
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from server_agent.knowledge.retriever import get_knowledge_base
from server_agent.tools.registry import tool


@tool(max_chars=6000)
def search_knowledge(
    query: Annotated[str, Field(min_length=1, max_length=200,
                                description="检索词，尽量用文档里会出现的**具体词**，如 '备份目录'、'logrotate'、'502'")],
    top_k: Annotated[int, Field(ge=1, le=10, description="返回几个片段")] = 4,
) -> dict:
    """检索团队内部文档（部署说明、历史故障复盘、运维约定）。
    用在「这台机器的约定是什么」「上次类似故障怎么处理的」这类问题上。
    返回每个命中片段的正文、来源（文件#标题）与匹配分。**回答时请引用来源**，
    并注意文档可能过期——与工具查到的实时事实冲突时，以实时事实为准。"""
    kb = get_knowledge_base()
    if kb.size == 0:
        return {"hits": [], "hint": "知识库为空（knowledge/ 目录下没有文档）"}
    hits = kb.search(query, top_k=top_k)
    return {"query": query, "hits": [h.to_dict() for h in hits],
            "documents": kb.documents()[:20],
            "hint": None if hits else "没有命中，试试更具体的词（工具名、路径、错误码）"}


@tool
def list_knowledge_docs() -> dict:
    """列出知识库里有哪些文档（只看文件名，不看内容）。
    不确定该搜什么词时，先用它看看有哪些资料可用。"""
    kb = get_knowledge_base()
    return {"count": len(kb.documents()), "documents": kb.documents()}
