"""远程工具：把只读排障能力伸到别的机器上。

核心取舍：**不在远端装任何依赖**（不装 psutil、不装 agent）。
远端只跑白名单命令，结构化由本地解析完成。代价是解析逻辑要跟着命令输出格式走，
因此这里只解析「输出格式稳定」的几条命令，剩下的留给 run_command 原样返回。

写操作（重启服务）复用同一套策略层：审批 + 审计一样不少，只是执行器换成了 SSH。
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from server_agent.executors import get_executor
from server_agent.executors.base import ExecutorError
from server_agent.executors.inventory import get_inventory
from server_agent.policy.redact import redact
from server_agent.tools.registry import ToolError, tool

REMOTE_READONLY = {"uptime", "df", "du", "free", "ps", "ss", "netstat", "journalctl",
                   "systemctl", "tail", "head", "wc", "grep", "ls", "uname", "who", "date", "cat", "hostname"}


def _parse_df(text: str) -> list[dict]:
    """解析 df -P 的输出（POSIX 格式最稳定）。"""
    rows = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 6:
            rows.append({"device": parts[0], "total": parts[1], "used": parts[2],
                         "free": parts[3], "percent": parts[4], "mount": parts[5]})
    return rows


def _parse_free(text: str) -> dict:
    for line in text.splitlines():
        if line.strip().startswith("Mem:"):
            parts = line.split()
            return {"total_kb": int(parts[1]), "used_kb": int(parts[2]), "free_kb": int(parts[3]),
                    "available_kb": int(parts[6]) if len(parts) > 6 else None}
    return {}


def _parse_uptime(text: str) -> dict:
    return {"raw": text.strip()}


def _parse_ss(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        rows.append({"proto": parts[0], "state": parts[1], "local": parts[3],
                     "process": parts[-1] if parts[-1].startswith("users:") or "pid=" in parts[-1] else None})
    return rows


def _parse_ps(text: str, limit: int) -> list[dict]:
    rows = []
    for line in text.splitlines()[1:]:
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        rows.append({"user": parts[0], "pid": int(parts[1]), "cpu": float(parts[2]),
                     "mem": float(parts[3]), "command": parts[10][:200]})
    return rows[:limit]


@tool(max_chars=8000)
async def remote_run(
    host: Annotated[str, Field(description="目标主机名（必须是清单里的名字，如 web-01）")],
    command: Annotated[str, Field(min_length=1, max_length=400,
                                  description="白名单内的只读命令，如 'df -h'、'free -m'")],
    timeout: Annotated[float, Field(ge=1, le=60, description="执行超时（秒）")] = 20.0,
) -> dict:
    """在**远程主机**上执行一条只读命令（走 SSH）。命令白名单与本地 run_command 相同：
    不含 shell 元字符、首词必须在白名单内。用于查看远端机器的磁盘、内存、进程、端口、日志。
    需要改动的操作（重启服务、清理文件）请用对应专用工具并传 host。"""
    import shlex

    head = (shlex.split(command) or [""])[0]
    if head not in REMOTE_READONLY:
        raise ToolError(f"命令 {head} 不在远端执行白名单内（允许：{', '.join(sorted(REMOTE_READONLY))}）")
    try:
        executor = get_executor(host)
        outcome = await executor.run(shlex.split(command), timeout=timeout)
    except ExecutorError as e:
        raise ToolError(str(e)) from e
    except KeyError as e:
        raise ToolError(str(e)) from e

    parsed = None
    if head == "df":
        parsed = {"partitions": _parse_df(outcome.stdout)}
    elif head == "free":
        parsed = _parse_free(outcome.stdout)
    elif head == "ss":
        parsed = {"listening": _parse_ss(outcome.stdout)}
    elif head == "uptime":
        parsed = _parse_uptime(outcome.stdout)
    elif head == "ps":
        parsed = {"processes": _parse_ps(outcome.stdout, 15)}
    return {"host": host, "command": command, "ok": outcome.ok, "returncode": outcome.returncode,
            "elapsed_ms": outcome.elapsed_ms, "parsed": parsed,
            "output": redact(outcome.text())[:4000]}


@tool(max_chars=8000)
async def remote_logs(
    host: Annotated[str, Field(description="目标主机名")],
    path: Annotated[str, Field(description="远端日志文件的绝对路径，如 /var/log/nginx/error.log")],
    lines: Annotated[int, Field(ge=1, le=500, description="读取最后多少行")] = 50,
    grep: Annotated[str | None, Field(description="只保留包含该关键字的行（不区分大小写）")] = None,
) -> dict:
    """读取**远程主机**上的日志文件末尾若干行（相当于远端 tail，可选 grep 过滤）。
    会用该主机清单里的 allowed_paths 做白名单校验，防止读到任意文件。"""
    inventory_host = get_inventory().get(host)
    if inventory_host and inventory_host.allowed_paths:
        if not any(path.startswith(p) for p in inventory_host.allowed_paths):
            raise ToolError(f"{host} 的日志白名单不含该路径（允许：{', '.join(inventory_host.allowed_paths)}）")
    cmd = ["tail", "-n", str(lines), path]
    try:
        outcome = await get_executor(host).run(cmd, timeout=15.0)
    except ExecutorError as e:
        raise ToolError(str(e)) from e
    text = outcome.stdout
    if grep:
        g = grep.lower()
        text = "\n".join(ln for ln in text.splitlines() if g in ln.lower())
    return {"host": host, "path": path, "ok": outcome.ok, "lines_returned": len(text.splitlines()),
            "content": redact(text)[:6000], "stderr": redact(outcome.stderr)[:500]}


@tool(risk="high")
async def remote_restart_service(
    host: Annotated[str, Field(description="目标主机名（必须是清单里的名字）")],
    name: Annotated[str, Field(min_length=1, max_length=60, description="服务名，需在该主机的白名单内")],
    dry_run: Annotated[bool, Field(description="true 只预演不执行")] = True,
) -> dict:
    """在**远程主机**上重启一个服务。与本地版本一样属于变更操作，必须人工审批；
    服务名要在该主机清单的 allowed_services 里（不同机器授权不同）。"""
    entry = get_inventory().get(host)
    if entry is None:
        raise ToolError(f"清单里没有这台主机: {host}")
    if entry.allowed_services and name not in entry.allowed_services:
        raise ToolError(f"{host} 未授权重启 {name}（允许：{', '.join(entry.allowed_services)}）")
    if dry_run:
        return {"dry_run": True, "host": host, "action": f"ssh {entry.hostname} systemctl restart {name}",
                "impact": f"{host} 上的 {name} 会短暂中断并重新加载配置"}
    outcome = await get_executor(host).run(["systemctl", "restart", name], timeout=60.0)
    if not outcome.ok:
        raise ToolError(f"远端重启失败（{outcome.returncode}）：{redact(outcome.text())[:500]}")
    return {"dry_run": False, "ok": True, "host": host, "service": name,
            "output": redact(outcome.text())[:1000]}
