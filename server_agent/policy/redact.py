"""敏感信息脱敏。

两个使用场景：
1. 审计日志：记录工具参数时，密码/Token 不能落盘；
2. 工具输出：`run_command` 的输出可能包含密钥（如打印环境变量、配置文件内容），
   回喂给模型前先脱敏——模型不需要知道真实密码，而这段文本会进历史、进数据库。

设计原则：宁可多打码，也不要漏。正则覆盖面偏向保守（可能误伤一些普通文本），
因为「把密码发给模型」的代价远高于「把普通词打码」。
"""

from __future__ import annotations

import re

PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\b\s*[:=]\s*)(\S+)"),
     r"\1***"),
    (re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)(\S+)"), r"\1***"),
    (re.compile(r"\bsk-[A-Za-z0-9]{8,}\b"), "sk-***"),
    (re.compile(r"\bAKID[A-Za-z0-9]{8,}\b"), "AKID***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), "ghp_***"),
    (re.compile(r"(?i)(-----BEGIN [A-Z ]*PRIVATE KEY-----)[\s\S]+?(-----END [A-Z ]*PRIVATE KEY-----)"),
     r"\1 [已脱敏] \2"),
    (re.compile(r"(?i)(mongodb|mysql|postgres(?:ql)?|redis)://[^\s\"']+"), r"\1://***"),
]


def redact(text: str) -> str:
    if not text:
        return text
    for pattern, repl in PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact_args(args: dict) -> dict:
    """脱敏工具参数（审计用）。"""
    out = {}
    for k, v in args.items():
        out[k] = redact(v) if isinstance(v, str) else v
    return out


def contains_secret(text: str) -> bool:
    """判断文本里是否出现了敏感模式（用于审计告警与测试断言）。"""
    return redact(text) != text
