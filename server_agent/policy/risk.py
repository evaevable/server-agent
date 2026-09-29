"""安全策略：工具调用的准入判断。

分层防御的第一层，也是最重要的一层：**不给任意 shell，只给经过校验的专用工具**。
它回答三个问题：
1. 这个调用允许吗？（forbidden -> 直接拒绝，连审批机会都没有）
2. 需要人工审批吗？（high 风险 -> 必须停下来问人）
3. 参数本身安全吗？（路径白名单、服务白名单、pid 保护、命令白名单）

为什么把「参数校验」放在这里，而不是工具内部？
- 工具内部校验属于「实现细节」，容易漏；策略层是唯一入口，漏不掉；
- 策略可配置、可审计、可测试（本章的注入攻击测试全部打在这一层）。
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Risk = Literal["read", "low", "high", "forbidden"]
NEVER_DELETE = ("/", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/dev", "/proc",
                "/sys", "/var/lib", "/System", "/Library", "/Applications", "/Users", "/home")
PROTECTED_PIDS = {0, 1}
# run_command 的白名单：只读、信息型命令。**注意这里没有 rm / mv / dd / chmod / kill**
ALLOWED_COMMANDS = {"uptime", "df", "du", "free", "ps", "ss", "netstat", "journalctl",
                    "systemctl", "tail", "head", "wc", "grep", "ls", "uname", "who", "date"}
FORBIDDEN_COMMANDS = {"rm", "rmdir", "dd", "mkfs", "shutdown", "reboot", "halt", "chmod",
                      "chown", "sudo", "su", "kill", "killall", "pkill", "mv", "useradd",
                      "userdel", "passwd", "iptables", "mount", "umount", "tee", "curl", "wget"}
SHELL_METACHARS = (";", "|", "&", ">", "<", "`", "$(", "\n", "\\")


@dataclass
class PolicyDecision:
    allowed: bool
    needs_approval: bool = False
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        if not self.allowed:
            return "forbidden"
        return "approval" if self.needs_approval else "allow"


@dataclass
class Policy:
    allowed_paths: tuple[str, ...] = ("/tmp", "/var/tmp")
    allowed_services: tuple[str, ...] = ("nginx", "redis", "redis-server")
    require_approval: bool = True
    protected_pids: set[int] = field(default_factory=lambda: set(PROTECTED_PIDS))

    # ---------- 入口 ----------
    def decide(self, tool: str, args: dict[str, Any], risk: Risk = "read") -> PolicyDecision:
        """按「先看是否禁止、再看参数、最后看风险等级」的顺序判断。"""
        if risk == "forbidden":
            return PolicyDecision(False, reason=f"工具 {tool} 被标记为禁止使用")
        checker = getattr(self, f"_check_{tool}", None)
        if checker is not None:
            decision = checker(args)
            if not decision.allowed:
                return decision
            decision.needs_approval = decision.needs_approval or (self.require_approval and risk == "high")
            return decision
        # 没有专门规则的工具：按风险等级决定
        return PolicyDecision(True, needs_approval=self.require_approval and risk == "high",
                              reason="按风险等级判定")

    @staticmethod
    def _norm(path: str) -> Path:
        """把路径归一化（展开 ~、解析符号链接），两侧都用它比较。"""
        try:
            return Path(path).expanduser().resolve()
        except (OSError, RuntimeError):
            return Path(path)

    # ---------- 各工具的参数校验 ----------
    def _check_clean_directory(self, args: dict) -> PolicyDecision:
        raw = str(args.get("path", ""))
        try:
            target = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError):
            return PolicyDecision(False, reason=f"路径无法解析: {raw}")
        for bad in NEVER_DELETE:
            if str(target) == bad:
                return PolicyDecision(False, reason=f"拒绝清理受保护路径: {target}")
        # 白名单也要 resolve：macOS 上 /tmp 实际是 /private/tmp，
        # 只归一化一边会导致「看起来允许、实际拒绝」的假阴性（本章测试踩到过）。
        allowed = [str(self._norm(p)) for p in self.allowed_paths]
        if not any(str(target) == a or str(target).startswith(a.rstrip("/") + "/") for a in allowed):
            return PolicyDecision(False, reason=f"路径不在白名单内（允许：{', '.join(self.allowed_paths)}）",
                                  details={"path": str(target)})
        days = args.get("older_than_days", 7)
        if not isinstance(days, int) or days < 1:
            return PolicyDecision(False, reason="older_than_days 必须是 >=1 的整数（避免误删新文件）")
        return PolicyDecision(True, reason="清理范围在白名单内，需人工确认")

    def _check_restart_service(self, args: dict) -> PolicyDecision:
        name = str(args.get("name", "")).strip()
        if not name:
            return PolicyDecision(False, reason="服务名不能为空")
        if name not in self.allowed_services:
            return PolicyDecision(False, reason=f"服务不在白名单内（允许：{', '.join(self.allowed_services) or '无'}）")
        return PolicyDecision(True, reason=f"服务 {name} 在白名单内，需人工确认")

    def _check_kill_process(self, args: dict) -> PolicyDecision:
        pid = args.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return PolicyDecision(False, reason="pid 必须是正整数")
        if pid in self.protected_pids or pid == os.getpid():
            return PolicyDecision(False, reason=f"拒绝操作受保护的进程: pid={pid}")
        sig = str(args.get("signal", "TERM")).upper()
        if sig not in ("TERM", "INT", "HUP"):
            return PolicyDecision(False, reason=f"只允许温和信号 TERM/INT/HUP，收到: {sig}")
        return PolicyDecision(True, reason=f"将向 pid={pid} 发送 {sig}，需人工确认")

    def _check_run_command(self, args: dict) -> PolicyDecision:
        cmd = str(args.get("command", "")).strip()
        if not cmd:
            return PolicyDecision(False, reason="命令不能为空")
        for meta in SHELL_METACHARS:
            if meta in cmd:
                return PolicyDecision(False, reason=f"命令包含不允许的 shell 元字符: {meta!r}（防止命令拼接）")
        try:
            parts = shlex.split(cmd)
        except ValueError as e:
            return PolicyDecision(False, reason=f"命令解析失败: {e}")
        if not parts:
            return PolicyDecision(False, reason="命令不能为空")
        head = Path(parts[0]).name
        if head in FORBIDDEN_COMMANDS:
            return PolicyDecision(False, reason=f"命令 {head} 在禁用列表里")
        if head not in ALLOWED_COMMANDS:
            return PolicyDecision(False, reason=f"命令 {head} 不在白名单内（允许：{', '.join(sorted(ALLOWED_COMMANDS))}）")
        return PolicyDecision(True, needs_approval=False, reason=f"命令 {head} 属于只读白名单")
