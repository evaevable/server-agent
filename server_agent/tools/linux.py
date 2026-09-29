"""面向 Linux 生产机的只读排障工具。

真实的「磁盘满」「服务挂了」排查里，df/top 只是第一步，真正定位问题靠的是：

- 哪些**大文件**占了空间（du 往下钻太慢，直接找 Top N 大文件）；
- 有没有**已删除但仍被进程占用**的文件（rm 了日志空间却不回来的经典原因）；
- systemd 眼里这个服务是什么状态、重启过几次、最近的 journal 说了什么。

这些能力依赖 /proc 与 systemd，只在 Linux 上可用；其它系统返回可读错误，而不是编造结果。
所有工具都是只读的，不需要审批。
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated

from pydantic import Field

from server_agent.tools.registry import ToolError, tool
from server_agent.tools.system import human

SERVICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:-]{0,99}$")
SKIP_DIRS = {"/proc", "/sys", "/dev", "/run"}
SCAN_ENTRY_LIMIT = 300_000       # 单次扫描最多看多少个目录项，防止在海量小文件目录里卡死
SCAN_TIME_BUDGET = 8.0           # 秒；超出就返回已找到的结果并标记 partial

SYSTEMD_PROPERTIES = ("Id", "Description", "LoadState", "ActiveState", "SubState", "Result",
                      "MainPID", "NRestarts", "ExecMainStatus", "ActiveEnterTimestamp",
                      "MemoryCurrent", "FragmentPath")


def _require_linux(feature: str) -> None:
    if not sys.platform.startswith("linux"):
        raise ToolError(f"{feature} 依赖 Linux 的 /proc 或 systemd，当前系统是 {sys.platform}")


def _run(argv: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
    """执行一条固定参数的只读命令（不经过 shell）。测试里会被替换。"""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


# ---------- 大文件 ----------
@tool(timeout=20)
def find_large_files(
    path: Annotated[str, Field(min_length=1, description="从哪个目录开始找，如 /var/log、/data")] = "/var/log",
    min_size_mb: Annotated[int, Field(ge=1, le=1_000_000, description="只列出不小于这个大小的文件（MB）")] = 100,
    limit: Annotated[int, Field(ge=1, le=50, description="最多返回多少个")] = 20,
) -> dict:
    """在一个目录下找出最大的若干个文件（相当于 find PATH -xdev -type f -size +NM 再按大小排序）。
    不跨文件系统、不跟随符号链接、跳过 /proc /sys /dev /run。用于磁盘满时直接定位「到底是哪个文件」。
    目录特别大时会在时间预算内返回部分结果（partial=true）。"""
    root = Path(path)
    if not root.is_dir():
        raise ToolError(f"不是目录或不存在: {path}")
    root_dev = root.stat().st_dev
    threshold = min_size_mb * 1024 * 1024
    found: list[tuple[int, str, float]] = []
    scanned, denied, partial = 0, 0, False
    deadline = time.monotonic() + SCAN_TIME_BUDGET
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            it = os.scandir(current)
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            denied += 1
            continue
        with it:
            for entry in it:
                scanned += 1
                if scanned > SCAN_ENTRY_LIMIT or time.monotonic() > deadline:
                    partial = True
                    stack.clear()
                    break
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    if st.st_dev == root_dev and entry.path not in SKIP_DIRS:
                        stack.append(entry.path)
                elif stat.S_ISREG(st.st_mode) and st.st_size >= threshold:
                    found.append((st.st_size, entry.path, st.st_mtime))
    found.sort(reverse=True)
    files = [{"path": p, "size": human(size), "bytes": size,
              "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))}
             for size, p, mtime in found[:limit]]
    result = {"root": str(root), "min_size": f"{min_size_mb}M", "count": len(found), "files": files,
              "scanned_entries": scanned, "partial": partial}
    if denied:
        result["unreadable_dirs"] = denied
    if not files:
        result["hint"] = f"没有 >= {min_size_mb}M 的文件；可调小 min_size_mb，或换一个挂载点再找"
    return result


# ---------- 已删除但未释放 ----------
def _proc_root() -> Path:
    return Path("/proc")


@tool(timeout=20)
def deleted_open_files(
    limit: Annotated[int, Field(ge=1, le=50, description="最多返回多少个")] = 20,
) -> dict:
    """列出「已被删除、但仍被进程打开」的文件（相当于 lsof +L1）。
    这类文件不会出现在 du 里，但仍然占着磁盘空间：典型场景是日志被 rm 掉而服务没重开文件句柄，
    df 显示满、du 却统计不出来。解决办法通常是让对应服务重新打开日志（reload/重启，需审批）。仅 Linux。"""
    _require_linux("deleted_open_files")
    proc = _proc_root()
    hits: list[dict] = []
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            fds = list((pid_dir / "fd").iterdir())
            comm = (pid_dir / "comm").read_text().strip()
        except (PermissionError, FileNotFoundError, ProcessLookupError, OSError):
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if not target.endswith(" (deleted)"):
                continue
            try:
                size = os.stat(fd).st_size
            except OSError:
                size = 0
            hits.append({"pid": int(pid_dir.name), "process": comm, "fd": int(fd.name),
                         "path": target[: -len(" (deleted)")], "bytes": size, "size": human(size)})
    hits.sort(key=lambda h: h["bytes"], reverse=True)
    total = sum(h["bytes"] for h in hits)
    return {"count": len(hits), "total": human(total), "files": hits[:limit],
            "hint": ("空间被这些句柄占着；让对应进程重新打开文件（如 nginx -s reopen / systemctl reload）即可释放"
                     if hits else "没有发现已删除未释放的文件")}


# ---------- systemd 服务状态 ----------
@tool(timeout=20)
def service_status(
    name: Annotated[str, Field(min_length=1, max_length=100, description="systemd 服务名，如 nginx 或 nginx.service")],
    journal_lines: Annotated[int, Field(ge=0, le=100, description="附带最近多少行 journal 日志（0 表示不要）")] = 20,
) -> dict:
    """查看一个 systemd 服务的状态：是否 active、子状态、主进程 pid、自启动以来重启次数、上次退出码、
    最近的 journal 日志。用于判断服务是「没起来」「反复崩溃重启」还是「在跑但不响应」。仅 systemd 系统。"""
    if not SERVICE_NAME.match(name):
        raise ToolError(f"服务名不合法: {name}")
    _require_linux("service_status")
    try:
        show = _run(["systemctl", "show", name, "--no-pager", f"--property={','.join(SYSTEMD_PROPERTIES)}"])
    except FileNotFoundError as e:
        raise ToolError("没有 systemctl：这台机器不是 systemd 系统") from e
    except subprocess.TimeoutExpired as e:
        raise ToolError("systemctl 响应超时") from e
    if show.returncode != 0:
        raise ToolError(f"systemctl show 失败：{(show.stderr or show.stdout).strip()[:300]}")
    props = dict(line.split("=", 1) for line in show.stdout.splitlines() if "=" in line)
    if props.get("LoadState") == "not-found":
        raise ToolError(f"没有这个服务: {name}")
    result: dict = {
        "service": props.get("Id", name),
        "description": props.get("Description", ""),
        "active": props.get("ActiveState", ""),
        "sub_state": props.get("SubState", ""),
        "result": props.get("Result", ""),
        "main_pid": int(props.get("MainPID") or 0),
        "restarts": int(props.get("NRestarts") or 0),
        "last_exit_status": int(props.get("ExecMainStatus") or 0),
        "active_since": props.get("ActiveEnterTimestamp", ""),
        "unit_file": props.get("FragmentPath", ""),
    }
    mem = props.get("MemoryCurrent", "")
    if mem.isdigit():
        result["memory"] = human(int(mem))
    if result["active"] != "active":
        result["warning"] = f"服务不在运行（{result['active']}/{result['sub_state']}，result={result['result']}）"
    elif result["restarts"] >= 3:
        result["warning"] = f"服务已自动重启 {result['restarts']} 次，可能在反复崩溃"
    if journal_lines:
        try:
            j = _run(["journalctl", "-u", name, "-n", str(journal_lines), "--no-pager", "-o", "short-iso"])
            result["journal"] = (j.stdout or j.stderr).strip()[-4000:]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            result["journal"] = "（读取 journal 失败）"
    return result
