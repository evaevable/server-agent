"""写操作工具。

三个原则：
1. **都带 dry_run**：默认只预演、不动真格。审批人看到的是「将要做什么」的具体结果。
2. **真正的副作用集中在几个小函数里**（`_service_restart` / `_kill_pid` / `_remove_files`），
   便于审计、便于测试（测试里替换掉它们，绝不真的重启服务或删文件）。
3. **参数校验不在这里**——在 `server_agent/policy/risk.py`。工具只管「怎么做」，
   策略层管「允不允许、要不要审批」。这样校验只有一处，不会漏。

所有工具的 risk 都是 high：调用前必须经过人工审批（见 registry.call 的 policy 分支）。
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from server_agent.tools.registry import ToolError, tool

ALLOWED_SIGNALS = {"TERM": "TERM", "INT": "INT", "HUP": "HUP"}


@dataclass
class CommandOutcome:
    ok: bool
    output: str
    returncode: int


# ---------- 真正干活的几个函数（测试里会被替换掉） ----------
def _run(argv: list[str], timeout: float = 30.0) -> CommandOutcome:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as e:
        return CommandOutcome(False, f"命令不存在: {e}", 127)
    except subprocess.TimeoutExpired:
        return CommandOutcome(False, f"命令执行超时（>{timeout}s）", 124)
    out = (proc.stdout or "") + (proc.stderr or "")
    return CommandOutcome(proc.returncode == 0, out.strip()[:4000], proc.returncode)


def _service_manager() -> str:
    """识别服务管理器：生产环境是 systemd；SysV 作为老系统兜底；brew 只用于 macOS 本机开发。"""
    if os.uname().sysname == "Darwin":
        return "brew"
    if Path("/run/systemd/system").is_dir():          # systemd 官方推荐的探测方式（sd_booted）
        return "systemd"
    return "sysv"


def _privileged(argv: list[str]) -> list[str]:
    """非 root 时走 sudo -n：没有免密 sudo 就立即失败，而不是卡在密码提示上。"""
    return argv if os.geteuid() == 0 else ["sudo", "-n", *argv]


def _restart_argv(name: str, manager: str) -> list[str]:
    if manager == "systemd":
        return _privileged(["systemctl", "restart", name])
    if manager == "sysv":
        return _privileged(["service", name, "restart"])
    return ["brew", "services", "restart", name]


def _service_restart(name: str) -> CommandOutcome:
    """重启服务，并在 systemd 上确认重启后确实是 active；失败时附上最近的 journal 便于判断原因。"""
    manager = _service_manager()
    outcome = _run(_restart_argv(name, manager), timeout=90)
    if manager != "systemd":
        return outcome
    state = _run(["systemctl", "is-active", name], timeout=10)
    if outcome.ok and state.output.strip() == "active":
        return CommandOutcome(True, (outcome.output + "\nis-active: active").strip(), 0)
    journal = _run(["journalctl", "-u", name, "-n", "20", "--no-pager", "-o", "short-iso"], timeout=10)
    detail = f"{outcome.output}\nis-active: {state.output.strip() or 'unknown'}\n--- journal ---\n{journal.output}"
    return CommandOutcome(False, detail.strip()[:4000], outcome.returncode or 3)


def _kill_pid(pid: int, signal: str) -> CommandOutcome:
    try:
        os.kill(pid, getattr(__import__("signal"), f"SIG{signal}"))
    except ProcessLookupError:
        return CommandOutcome(False, f"进程不存在: pid={pid}", 3)
    except PermissionError:
        return CommandOutcome(False, f"权限不足，无法向 pid={pid} 发送 {signal}", 1)
    return CommandOutcome(True, f"已向 pid={pid} 发送 SIG{signal}", 0)


def _scan_old_files(path: str, older_than_days: int, limit: int = 200) -> tuple[list[Path], int, int]:
    """列出待清理的文件：返回 (前 limit 个文件, 文件总数, 总字节数)。"""
    cutoff = time.time() - older_than_days * 86400
    root = Path(path)
    targets: list[Path] = []
    total, size = 0, 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime >= cutoff:
            continue
        total += 1
        size += st.st_size
        if len(targets) < limit:
            targets.append(p)
    return targets, total, size


def _remove_files(files: list[Path]) -> tuple[int, list[str]]:
    removed, errors = 0, []
    for f in files:
        try:
            f.unlink()
            removed += 1
        except OSError as e:
            errors.append(f"{f}: {e}")
    return removed, errors


def _human(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}P"


# ---------- 工具定义 ----------
@tool(risk="high")
def restart_service(
    name: Annotated[str, Field(min_length=1, max_length=60, description="服务名，必须在白名单内，如 nginx")],
    dry_run: Annotated[bool, Field(description="true 只预演不执行（审批前会自动预演一次）")] = True,
) -> dict:
    """重启一个系统服务（nginx、redis 等）。属于变更操作：会中断该服务的现有连接，
    必须经过人工审批才真正执行。默认 dry_run=true，只告诉你将要执行什么命令。"""
    if dry_run:
        manager = _service_manager()
        return {"dry_run": True, "action": " ".join(_restart_argv(name, manager)), "service_manager": manager,
                "impact": f"{name} 会短暂中断（数秒），现有连接被断开",
                "hint": "确认无误后由人工批准，才会真正执行"}
    outcome = _service_restart(name)
    if not outcome.ok:
        raise ToolError(f"重启 {name} 失败（退出码 {outcome.returncode}）：{outcome.output}")
    return {"dry_run": False, "ok": True, "service": name, "output": outcome.output}


@tool(risk="high")
def kill_process(
    pid: Annotated[int, Field(ge=2, description="进程号；不允许 0/1（内核与 init）")],
    signal: Annotated[Literal["TERM", "INT", "HUP"], Field(description="信号，只允许温和信号")] = "TERM",
    dry_run: Annotated[bool, Field(description="true 只预演不执行")] = True,
) -> dict:
    """向指定进程发送信号（默认 TERM，即请求它优雅退出）。属于变更操作，必须人工审批。
    不提供 SIGKILL——那是不可逆的强杀，不该由 Agent 使用。"""
    if dry_run:
        return {"dry_run": True, "action": f"kill -{signal} {pid}",
                "impact": "目标进程会收到信号并退出（TERM 允许它优雅关闭）",
                "hint": "确认这是要处理的进程再批准"}
    outcome = _kill_pid(pid, ALLOWED_SIGNALS[signal])
    if not outcome.ok:
        raise ToolError(outcome.output)
    return {"dry_run": False, "ok": True, "pid": pid, "signal": signal, "output": outcome.output}


@tool(risk="high", max_chars=6000)
def clean_directory(
    path: Annotated[str, Field(description="要清理的目录绝对路径，必须在白名单内，如 /tmp/myapp")],
    older_than_days: Annotated[int, Field(ge=1, le=365, description="只删除修改时间早于 N 天的文件")] = 7,
    dry_run: Annotated[bool, Field(description="true 只列出将删除的文件，不删除")] = True,
) -> dict:
    """清理目录下的旧文件（按修改时间，只删文件不删目录）。属于不可逆的删除操作，必须人工审批。
    默认 dry_run=true：会列出待删除文件、总数与占用空间，供你核对。"""
    files, total, size = _scan_old_files(path, older_than_days)
    if dry_run:
        return {"dry_run": True, "path": path, "older_than_days": older_than_days,
                "file_count": total, "total_size": _human(size),
                "sample": [str(f) for f in files[:50]],
                "impact": f"将删除 {total} 个文件，释放约 {_human(size)}",
                "hint": "如果 sample 里有不该删的文件，请拒绝这次操作"}
    if total == 0:
        return {"dry_run": False, "ok": True, "removed": 0, "message": "没有符合条件的文件"}
    removed, errors = _remove_files(files)
    if errors:
        raise ToolError(f"部分文件删除失败：{errors[:3]}")
    return {"dry_run": False, "ok": True, "removed": removed, "freed": _human(size)}


@tool(max_chars=6000)
def run_command(
    command: Annotated[str, Field(min_length=1, max_length=500,
                                  description="单条只读命令，如 'df -h'、'pgrep -a nginx'；不支持管道 | 与 ; > & 等")],
    timeout: Annotated[float, Field(ge=1, le=60, description="执行超时（秒）")] = 15.0,
) -> dict:
    """执行一条**只读**白名单命令（df/du/free/ps/pgrep/ss/netstat/journalctl/systemctl status 等）。
    这不是 shell：一次只能执行一条命令，**不支持管道和重定向**（| ; > & 都会被拒绝）。
    需要过滤时改用带过滤参数的工具：找进程用 top_processes 或 `pgrep -a 名字`，
    过滤日志用 tail_file 的 grep 参数，查端口用 listening_ports(port=...)。
    输出会做敏感信息脱敏。需要系统改动的操作请用专用工具（如 restart_service）。"""
    parts = shlex.split(command)
    outcome = _run(parts, timeout=timeout)
    from server_agent.policy.redact import redact

    return {"command": command, "ok": outcome.ok, "returncode": outcome.returncode,
            "output": redact(outcome.output), "at": datetime.now().isoformat(timespec="seconds")}
