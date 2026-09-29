"""记忆相关工具：让 Agent 在排查过程中主动读写长期记忆。

- `recall_host`：查这台主机的档案与历史事实（读操作）；
- `remember_fact`：把有价值的结论记下来（写本地记忆库，不影响被排查的系统）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import Field

from server_agent.memory.store import get_store
from server_agent.tools.registry import tool


@tool
def recall_host(
    host: Annotated[str, Field(description="主机名，如 web-01；本机可用 host_info 返回的 hostname")],
) -> dict:
    """查询这台主机的历史记忆：已知的关键事实（部署目录、服务名、约定）与最近几次排查的结论。
    排查一台「之前处理过」的机器时，先调用它能省掉很多重复工作。返回为空说明没有历史记录。"""
    store = get_store()
    profile = store.get_host_profile(host) or {}
    facts = store.recall_facts(host, limit=10)
    incidents = store.recent_incidents(host=host, limit=5)
    return {
        "host": host,
        "profile": profile,
        "facts": [{"key": f["key"], "value": f["value"],
                   "when": datetime.fromtimestamp(f["created_at"]).isoformat(timespec="seconds")} for f in facts],
        "recent_incidents": incidents,
        "hint": None if (profile or facts or incidents) else "没有历史记录，这是第一次排查这台主机",
    }


@tool(risk="low")
def remember_fact(
    host: Annotated[str, Field(description="主机名")],
    key: Annotated[str, Field(min_length=1, max_length=60, description="事实的名称，如 nginx_log_dir")],
    value: Annotated[str, Field(min_length=1, max_length=500, description="事实的内容，要具体")],
) -> dict:
    """把一条值得长期记住的事实写入记忆库（如「日志目录在 /data/logs/nginx」「备份脚本每天 3 点跑」）。
    只写本地记忆，不修改被排查的系统。适合记稳定的约定与路径，不要记临时状态。"""
    store = get_store()
    store.remember_fact(host, key, value)
    total = len(store.recall_facts(host, limit=100))
    return {"saved": True, "host": host, "key": key, "value": value, "total_facts": total}
