"""执行器层：本地 / SSH、沙箱。"""

from server_agent.executors.base import ExecOutcome, Executor, ExecutorError
from server_agent.executors.inventory import HostEntry, Inventory, get_inventory, reset_inventory
from server_agent.executors.local import LocalExecutor

__all__ = ["ExecOutcome", "Executor", "ExecutorError", "HostEntry", "Inventory",
           "LocalExecutor", "get_inventory", "reset_inventory", "get_executor", "get_ssh_executor"]


def get_executor(host: str | None = None):
    """按主机名拿执行器：None / local / 本机主机名 -> 本地执行器，其它走 SSH。"""
    import socket

    from server_agent.executors.ssh import SSHExecutor

    if not host or host in ("local", "localhost", "127.0.0.1", socket.gethostname()):
        return LocalExecutor()
    entry = get_inventory().require(host)
    return SSHExecutor(entry.name, hostname=entry.hostname, port=entry.port,
                       username=entry.username, password=entry.resolve_password(),
                       key_path=entry.key_path, strict_host_key=entry.strict_host_key)


def get_ssh_executor(host: str):
    return get_executor(host)
