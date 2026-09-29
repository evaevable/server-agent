"""执行器、主机清单与远程工具测试（全部离线：用假执行器，不真连 SSH）。"""

import pytest

from server_agent.executors import ExecOutcome, Inventory, LocalExecutor, get_inventory, reset_inventory
from server_agent.executors.base import ExecutorError
from server_agent.executors.inventory import HostEntry
from server_agent.executors.ssh import SSHExecutor
from server_agent.tools import registry


class FakeExecutor:
    """记录被执行的命令，返回预设输出。"""

    def __init__(self, host="web-01", stdout="", stderr="", ok=True, returncode=0):
        self.host = host
        self.stdout, self.stderr, self.ok, self.returncode = stdout, stderr, ok, returncode
        self.calls: list[list[str]] = []

    async def run(self, argv, *, timeout=30.0):
        self.calls.append(list(argv))
        return ExecOutcome(self.ok, self.stdout, self.stderr, self.returncode, self.host, 1.0)


@pytest.fixture
def inventory():
    inv = Inventory({
        "web-01": HostEntry(name="web-01", hostname="127.0.0.1", port=2201, username="root",
                            allowed_paths=["/var/log/nginx", "/tmp"], allowed_services=["nginx"]),
        "db-01": HostEntry(name="db-01", hostname="127.0.0.1", port=2203, username="root",
                           allowed_paths=["/var/log"], allowed_services=["redis-server"]),
    })
    reset_inventory(inv)
    yield inv
    reset_inventory(Inventory({}))


# ---------- 执行器 ----------

async def test_local_executor_runs_and_reports():
    out = await LocalExecutor().run(["echo", "hello"])
    assert out.ok and out.stdout.strip() == "hello" and out.host == "local"

    bad = await LocalExecutor().run(["/definitely/not/a/command"])
    assert not bad.ok and bad.returncode == 127 and "命令不存在" in bad.stderr

    slow = await LocalExecutor().run(["sleep", "2"], timeout=0.2)
    assert not slow.ok and "超时" in slow.stderr


def test_ssh_executor_quotes_arguments():
    from server_agent.executors.ssh import _quote

    assert _quote("df") == "df" and _quote("-h") == "-h" and _quote("/var/log/x") == "/var/log/x"
    assert _quote("a b") == "'a b'"
    assert _quote("it's") == "'it'\\''s'"          # 单引号被安全转义
    assert _quote("") == "''"


async def test_ssh_executor_drops_connection_on_error(monkeypatch):
    """远端执行异常时应丢弃连接，下次重连（否则会一直用一个坏连接）。"""
    ex = SSHExecutor("web-01", hostname="127.0.0.1", port=2201)

    class BadConn:
        async def run(self, cmd, check=False):
            raise OSError("connection reset")

    ex._conn = BadConn()
    out = await ex.run(["df", "-h"])
    assert not out.ok and "执行失败" in out.stderr and ex._conn is None


async def test_ssh_executor_connect_failure_becomes_executor_error(monkeypatch):
    ex = SSHExecutor("web-01", hostname="127.0.0.1", port=1)

    async def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr("server_agent.executors.ssh.asyncssh", type("M", (), {"connect": staticmethod(boom)}))
    with pytest.raises(ExecutorError, match="失败"):
        await ex.run(["df"])


# ---------- 清单 ----------

def test_inventory_from_yaml(tmp_path, monkeypatch):
    path = tmp_path / "inv.yaml"
    path.write_text("""
defaults:
  username: root
  port: 2222
  tags: {password_env: LAB_SSH_PASSWORD}
hosts:
  web-01:
    hostname: 127.0.0.1
    port: 2201
    groups: [web]
    allowed_paths: ["/var/log/nginx"]
    allowed_services: ["nginx"]
  db-01:
    groups: [db]
""", encoding="utf-8")
    inv = Inventory.from_yaml(path)
    assert inv.names() == ["web-01", "db-01"]
    assert inv.require("web-01").port == 2201 and inv.require("web-01").username == "root"
    assert inv.by_group("db")[0].name == "db-01"
    assert inv.require("db-01").hostname == "db-01"          # 没写 hostname 就用名字

    monkeypatch.setenv("LAB_SSH_PASSWORD", "labpass")
    assert inv.require("web-01").resolve_password() == "labpass"
    monkeypatch.delenv("LAB_SSH_PASSWORD")
    assert inv.require("web-01").resolve_password() is None

    with pytest.raises(KeyError, match="没有这台主机"):
        inv.require("nope")
    assert {h["name"] for h in inv.summary()} == {"web-01", "db-01"}


def test_get_executor_local_vs_ssh(inventory):
    from server_agent.executors import get_executor

    assert isinstance(get_executor(None), LocalExecutor)
    assert isinstance(get_executor("local"), LocalExecutor)
    ssh = get_executor("web-01")
    assert isinstance(ssh, SSHExecutor) and ssh.port == 2201
    with pytest.raises(KeyError):
        get_executor("unknown-host")


# ---------- 远程工具 ----------

async def test_remote_run_executes_and_parses(inventory, monkeypatch):
    fake = FakeExecutor(stdout="Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                               "/dev/vda1 40960000 38000000 900000 98% /\n")
    monkeypatch.setattr("server_agent.tools.remote.get_executor", lambda host=None: fake)

    r = await registry.call("remote_run", {"host": "web-01", "command": "df -h"})
    assert r.ok and fake.calls == [["df", "-h"]]
    part = r.data["parsed"]["partitions"][0]
    assert part["percent"] == "98%" and part["mount"] == "/"

    free = FakeExecutor(stdout="              total  used  free  shared  buff/cache  available\n"
                               "Mem:           8000  6000  1000     100        1000       1500\n")
    monkeypatch.setattr("server_agent.tools.remote.get_executor", lambda host=None: free)
    r = await registry.call("remote_run", {"host": "web-01", "command": "free -m"})
    assert r.data["parsed"]["available_kb"] == 1500


async def test_remote_run_rejects_non_whitelisted(inventory, monkeypatch):
    fake = FakeExecutor()
    monkeypatch.setattr("server_agent.tools.remote.get_executor", lambda host=None: fake)
    r = await registry.call("remote_run", {"host": "web-01", "command": "rm -rf /"})
    assert not r.ok and "白名单" in r.error and not fake.calls


async def test_remote_logs_enforces_host_path_whitelist(inventory, monkeypatch):
    fake = FakeExecutor(stdout="line1 ERROR boom\nline2 ok\n")
    monkeypatch.setattr("server_agent.tools.remote.get_executor", lambda host=None: fake)

    r = await registry.call("remote_logs", {"host": "web-01", "path": "/var/log/nginx/error.log", "grep": "error"})
    assert r.ok and "boom" in r.data["content"] and r.data["lines_returned"] == 1

    r = await registry.call("remote_logs", {"host": "web-01", "path": "/etc/shadow"})
    assert not r.ok and "白名单" in r.error


async def test_remote_restart_requires_host_authorization_and_approval(inventory, monkeypatch):
    fake = FakeExecutor(stdout="ok")
    monkeypatch.setattr("server_agent.tools.remote.get_executor", lambda host=None: fake)

    # 未授权服务
    r = await registry.call("remote_restart_service", {"host": "web-01", "name": "ssh"})
    assert not r.ok and "未授权" in r.error

    # 未登记主机
    r = await registry.call("remote_restart_service", {"host": "nope", "name": "nginx"})
    assert not r.ok and "清单里没有" in r.error

    # dry_run 不执行
    r = await registry.call("remote_restart_service", {"host": "web-01", "name": "nginx"})
    assert r.ok and r.data["dry_run"] is True and not fake.calls

    # 真执行（走策略层 + 审批）
    from server_agent.policy import AuditLog, Policy

    async def approver(*_a, **_k):
        return True

    r = await registry.call("remote_restart_service", {"host": "web-01", "name": "nginx", "dry_run": False},
                            policy=Policy(), approver=approver, audit=AuditLog(None))
    assert r.ok and fake.calls == [["systemctl", "restart", "nginx"]]


async def test_remote_restart_denied_without_approval(inventory, monkeypatch):
    fake = FakeExecutor()
    monkeypatch.setattr("server_agent.tools.remote.get_executor", lambda host=None: fake)
    from server_agent.policy import AuditLog, Policy

    r = await registry.call("remote_restart_service", {"host": "web-01", "name": "nginx", "dry_run": False},
                            policy=Policy(), approver=None, audit=AuditLog(None))
    assert not r.ok and "审批" in r.error and not fake.calls


async def test_connection_error_is_readable(inventory, monkeypatch):
    class Broken:
        host = "web-01"

        async def run(self, argv, *, timeout=30.0):
            raise ExecutorError("连接 web-01（127.0.0.1:2201）失败: connection refused")

    monkeypatch.setattr("server_agent.tools.remote.get_executor", lambda host=None: Broken())
    r = await registry.call("remote_run", {"host": "web-01", "command": "df -h"})
    assert not r.ok and "connection refused" in r.error


def test_remote_tools_are_registered_with_expected_risk():
    names = registry.names()
    assert {"remote_run", "remote_logs", "remote_restart_service"} <= set(names)
    assert registry.get("remote_run").risk == "read"
    assert registry.get("remote_restart_service").risk == "high"


def test_inventory_missing_file_falls_back_to_empty(monkeypatch):
    from server_agent.executors.inventory import get_inventory, reset_inventory

    reset_inventory(None)
    monkeypatch.chdir("/tmp")
    monkeypatch.setenv("SA_INVENTORY_PATH", "/tmp/definitely-not-here.yaml")
    from server_agent.config import get_settings
    get_settings.cache_clear()
    inv = get_inventory()
    assert inv.names() == []
    get_settings.cache_clear()
    reset_inventory(Inventory({}))
