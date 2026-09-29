"""只读排障工具（第 03 章）。基于 psutil，Linux / macOS 通用。

每个工具的 docstring 与参数描述就是模型看到的全部信息，改它们等于改 Agent 的行为。
第 10 章会把这些工具改造成支持 host 参数、在远程主机上执行。
"""

from __future__ import annotations

import os
import platform
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

import psutil
from pydantic import Field

from server_agent.tools.registry import ToolError, tool

PSEUDO_FS = {"devfs", "autofs", "tmpfs", "devtmpfs", "overlay", "squashfs", "proc", "sysfs",
             "cgroup", "cgroup2", "nullfs", "fuse.lxcfs"}
WARN_PERCENT = 90.0


def human(n: float) -> str:
    """字节数转人类可读，如 1536 -> 1.5K。"""
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}P"


def _duration(seconds: float) -> str:
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    return f"{d}天{h}小时{rem // 60}分"


@tool
def host_info() -> dict:
    """获取主机基本信息：主机名、操作系统与内核版本、CPU 核数、内存总量、开机时间与已运行时长。
    排障开始时先调用它，了解机器环境后再决定查什么。"""
    boot = psutil.boot_time()
    return {
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "platform": platform.platform(),
        "arch": platform.machine(),
        "cpu_logical": psutil.cpu_count(),
        "cpu_physical": psutil.cpu_count(logical=False),
        "memory_total": human(psutil.virtual_memory().total),
        "boot_time": datetime.fromtimestamp(boot).isoformat(timespec="seconds"),
        "uptime": _duration(time.time() - boot),
    }


@tool
def cpu_memory_usage(
    interval: Annotated[float, Field(ge=0.1, le=5, description="CPU 采样时长（秒），越长越准")] = 0.5,
) -> dict:
    """获取 CPU 与内存的使用情况：CPU 总使用率与每核使用率、1/5/15 分钟平均负载、内存与 swap 使用量。
    用于判断「机器卡不卡、资源紧不紧张」。负载持续高于 CPU 核数说明有任务在排队。"""
    per_cpu = psutil.cpu_percent(interval=interval, percpu=True)
    vm, sw = psutil.virtual_memory(), psutil.swap_memory()
    n = psutil.cpu_count() or 1
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
    return {
        "cpu_percent": round(sum(per_cpu) / len(per_cpu), 1),
        "per_cpu_percent": per_cpu,
        "cpu_count": n,
        "load_avg": {"1m": round(load[0], 2), "5m": round(load[1], 2), "15m": round(load[2], 2)},
        "load_per_cpu_1m": round(load[0] / n, 2),
        "memory": {"total": human(vm.total), "used": human(vm.used), "available": human(vm.available),
                   "percent": vm.percent},
        "swap": {"total": human(sw.total), "used": human(sw.used), "percent": sw.percent},
    }


def _inode_usage(mount: str) -> float | None:
    """inode 使用率（df -i）。有的文件系统（如 btrfs、部分网络盘）不报告 inode，返回 None。"""
    try:
        st = os.statvfs(mount)
    except (OSError, AttributeError):
        return None
    if not st.f_files:
        return None
    return round((st.f_files - st.f_ffree) / st.f_files * 100, 1)


def _usage_row(mount: str, device: str | None = None, fstype: str | None = None) -> dict:
    u = psutil.disk_usage(mount)
    row = {"mount": mount, "total": human(u.total), "used": human(u.used), "free": human(u.free),
           "percent": u.percent}
    inodes = _inode_usage(mount)
    if inodes is not None:
        row["inodes_percent"] = inodes
        if inodes >= WARN_PERCENT:
            row["inode_warning"] = f"inode 使用率 {inodes:.0f}%：还有空间也可能写不进新文件（海量小文件）"
    if device:
        row["device"] = device
    if fstype:
        row["fstype"] = fstype
    if u.percent >= WARN_PERCENT:
        row["warning"] = f"使用率超过 {WARN_PERCENT:.0f}%"
    return row


@tool
def disk_usage(
    path: Annotated[str | None, Field(description="要查看的路径，如 / 或 /var/log；不填则列出所有分区")] = None,
) -> dict:
    """查看磁盘空间使用情况（相当于 df -h + df -i）：每个分区的总量、已用、可用、使用率与 inode 使用率，
    任一项 >= 90% 会带 warning。只统计分区级别；要找具体是哪个文件，用 find_large_files / deleted_open_files。"""
    if path:
        if not Path(path).exists():
            raise ToolError(f"路径不存在: {path}")
        return {"partitions": [_usage_row(path)]}
    rows = []
    for p in psutil.disk_partitions(all=False):
        if p.fstype in PSEUDO_FS or p.mountpoint.startswith("/snap/"):
            continue
        try:
            rows.append(_usage_row(p.mountpoint, p.device, p.fstype))
        except (PermissionError, OSError):
            continue
    rows.sort(key=lambda r: r["percent"], reverse=True)
    return {"partitions": rows[:20]}


@tool
def top_processes(
    sort_by: Annotated[Literal["cpu", "memory"], Field(description="排序依据：cpu 或 memory")] = "cpu",
    limit: Annotated[int, Field(ge=1, le=50, description="返回多少个进程")] = 10,
) -> dict:
    """列出资源占用最高的进程（相当于 top / ps aux --sort）：pid、进程名、用户、CPU%、内存%、常驻内存、
    状态、命令行（截断到 200 字符）。CPU% 是约 0.5 秒内的采样值，单核满载为 100。"""
    attrs = ["pid", "name", "username", "memory_percent", "memory_info", "cmdline", "status"]
    procs = list(psutil.process_iter(attrs))
    for p in procs:  # cpu_percent 需要两次采样：第一次只是打点
        try:
            p.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    time.sleep(0.5)
    rows = []
    for p in procs:
        try:
            cpu = p.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        info = p.info
        mem = info.get("memory_info")
        rows.append({
            "pid": info["pid"],
            "name": info.get("name"),
            "user": info.get("username"),
            "cpu_percent": round(cpu, 1),
            "memory_percent": round(info.get("memory_percent") or 0.0, 2),
            "rss": human(mem.rss) if mem else None,
            "status": info.get("status"),
            "cmdline": " ".join(info.get("cmdline") or [])[:200] or None,
        })
    key = "cpu_percent" if sort_by == "cpu" else "memory_percent"
    rows.sort(key=lambda r: r[key], reverse=True)
    return {"sort_by": sort_by, "total_processes": len(procs), "processes": rows[:limit]}


def _proc_name(pid: int | None) -> str | None:
    if not pid:
        return None
    try:
        return psutil.Process(pid).name()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


@tool
def listening_ports(
    port: Annotated[int | None, Field(ge=1, le=65535, description="只查看这个端口，如 80；不填则列出全部")] = None,
    limit: Annotated[int, Field(ge=1, le=100, description="最多返回多少条")] = 30,
) -> dict:
    """列出正在监听的 TCP 端口和已绑定的 UDP 端口（相当于 ss -lntup），以及占用端口的进程 pid 与名称。
    用于排查「端口被占用」「服务起了但连不上」。已知端口号时请传 port 参数，结果更精确。
    非 root 运行时可能看不到其他用户进程，结果会标注 partial。"""
    partial = False
    items: list[tuple] = []  # (conn, pid)
    try:
        items = [(c, c.pid) for c in psutil.net_connections(kind="inet")]
    except psutil.AccessDenied:  # macOS 非 root：退化为逐进程查看自己有权限的
        partial = True
        for p in psutil.process_iter(["pid"]):
            getter = getattr(p, "net_connections", None) or p.connections
            try:
                items += [(c, p.pid) for c in getter(kind="inet")]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    seen, rows = set(), []
    for c, pid in items:
        is_tcp = c.type == socket.SOCK_STREAM
        if is_tcp and c.status != psutil.CONN_LISTEN:
            continue
        if not is_tcp and c.raddr:
            continue
        if not c.laddr or c.laddr.port == 0:
            continue
        if port is not None and c.laddr.port != port:
            continue
        key = ("tcp" if is_tcp else "udp", c.laddr.ip, c.laddr.port)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"proto": key[0], "address": c.laddr.ip, "port": c.laddr.port, "pid": pid,
                     "process": _proc_name(pid)})
    rows.sort(key=lambda r: (r["proto"], r["port"]))
    out: dict = {"count": len(rows), "returned": min(len(rows), limit), "ports": rows[:limit]}
    if len(rows) > limit:
        out["hint"] = f"共 {len(rows)} 条，仅返回前 {limit} 条；可用 port 参数精确查询"
    if partial:
        out["partial"] = "无权限查看全部进程，结果可能不完整（以 root 运行可看到全部）"
    return out


TAIL_SCAN_BYTES = 2 * 1024 * 1024


@tool(max_chars=8000)
def tail_file(
    path: Annotated[str, Field(description="文件的绝对路径，如 /var/log/nginx/error.log")],
    lines: Annotated[int, Field(ge=1, le=1000, description="返回最后多少行")] = 50,
    grep: Annotated[str | None, Field(description="只保留包含该关键字的行（不区分大小写），如 error")] = None,
) -> dict:
    """读取文本文件（通常是日志）的最后若干行（相当于 tail -n，可选 grep 过滤）。
    只扫描文件末尾 2MB；二进制文件会被拒绝。查看目录内容或文件大小请用其他工具。"""
    p = Path(path)
    if not p.is_absolute():
        raise ToolError(f"必须使用绝对路径: {path}")
    if not p.exists():
        raise ToolError(f"文件不存在: {path}")
    if p.is_dir():
        raise ToolError(f"这是目录不是文件: {path}")
    size = p.stat().st_size
    with p.open("rb") as f:
        f.seek(max(0, size - TAIL_SCAN_BYTES))
        chunk = f.read()
    if b"\x00" in chunk[:8192]:
        raise ToolError(f"疑似二进制文件，拒绝读取: {path}")
    text_lines = chunk.decode("utf-8", errors="replace").splitlines()
    if size > TAIL_SCAN_BYTES and text_lines:
        text_lines = text_lines[1:]  # 第一行大概率被截断了一半
    if grep:
        g = grep.lower()
        text_lines = [ln for ln in text_lines if g in ln.lower()]
    picked = text_lines[-lines:]
    return {
        "path": str(p),
        "size": human(size),
        "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
        "lines_returned": len(picked),
        "content": "\n".join(picked),
    }
