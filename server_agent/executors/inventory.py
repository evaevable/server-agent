"""主机清单：有哪些机器、怎么连、允许对它做什么。

一个刻意的小设计：**授权范围写在清单里，跟着主机走**。
因为「web-01 上可以重启 nginx，db-01 上连只读命令都要谨慎」这种差异，
属于环境知识，不该散落在代码的 if-else 里。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


@dataclass
class HostEntry:
    name: str
    hostname: str = ""
    port: int = 22
    username: str | None = None
    password: str | None = None
    key_path: str | None = None
    strict_host_key: bool = False          # 演练环境默认关；真机上请打开
    groups: list[str] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=list)     # 该主机上允许清理/读取的路径
    allowed_services: list[str] = field(default_factory=list)  # 该主机上允许重启的服务
    tags: dict[str, Any] = field(default_factory=dict)

    def resolve_password(self) -> str | None:
        """支持 password_env：密码从环境变量读，不落盘到清单里。"""
        if self.password:
            return self.password
        env_key = self.tags.get("password_env")
        return os.environ.get(env_key) if env_key else None


class Inventory:
    def __init__(self, hosts: dict[str, HostEntry]):
        self.hosts = hosts

    def get(self, name: str) -> HostEntry | None:
        return self.hosts.get(name)

    def names(self) -> list[str]:
        return list(self.hosts)

    def by_group(self, group: str) -> list[HostEntry]:
        return [h for h in self.hosts.values() if group in h.groups]

    def require(self, name: str) -> HostEntry:
        host = self.get(name)
        if host is None:
            raise KeyError(f"清单里没有这台主机: {name}（可用：{', '.join(self.names()) or '无'}）")
        return host

    def summary(self) -> list[dict]:
        return [{"name": h.name, "hostname": h.hostname or h.name, "port": h.port,
                 "groups": h.groups, "allowed_paths": h.allowed_paths,
                 "allowed_services": h.allowed_services} for h in self.hosts.values()]

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Inventory":
        if yaml is None:  # pragma: no cover
            raise RuntimeError("未安装 pyyaml：pip install pyyaml")
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        defaults = data.get("defaults") or {}
        hosts: dict[str, HostEntry] = {}
        for name, cfg in (data.get("hosts") or {}).items():
            merged = {**defaults, **(cfg or {})}
            hosts[name] = HostEntry(
                name=name, hostname=merged.get("hostname", name), port=merged.get("port", 22),
                username=merged.get("username"), password=merged.get("password"),
                key_path=merged.get("key_path"), strict_host_key=merged.get("strict_host_key", False),
                groups=list(merged.get("groups") or []),
                allowed_paths=list(merged.get("allowed_paths") or []),
                allowed_services=list(merged.get("allowed_services") or []),
                tags=dict(merged.get("tags") or {}))
        return cls(hosts)


_cache: Inventory | None = None


def get_inventory(path: str | Path | None = None) -> Inventory:
    """进程内缓存的清单。文件不存在时返回空清单（本机模式）。"""
    global _cache
    if path is not None:
        return Inventory.from_yaml(path)
    if _cache is None:
        from server_agent.config import get_settings

        p = Path(get_settings().inventory_path)
        _cache = Inventory.from_yaml(p) if p.exists() else Inventory({})
    return _cache


def reset_inventory(inv: Inventory | None = None) -> None:
    global _cache
    _cache = inv
