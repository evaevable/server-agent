"""面向 Linux 生产机的排障工具：大文件、已删除未释放、systemd 状态、服务重启确认。

全部离线：/proc 用临时目录模拟，systemctl/journalctl 用替身，不依赖本机是不是 Linux。
"""

import os
from types import SimpleNamespace

import pytest

from server_agent.tools import linux, ops, registry


def _proc(stdout="", stderr="", code=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)


# ---------- find_large_files ----------

async def test_find_large_files_sorted_and_filtered(tmp_path):
    (tmp_path / "nginx").mkdir()
    (tmp_path / "nginx" / "access.log").write_bytes(b"x" * 3 * 1024 * 1024)
    (tmp_path / "app.log").write_bytes(b"x" * 2 * 1024 * 1024)
    (tmp_path / "small.log").write_bytes(b"x" * 1024)
    os.symlink(tmp_path / "nginx" / "access.log", tmp_path / "link.log")   # 符号链接不能重复计数

    r = await registry.call("find_large_files", {"path": str(tmp_path), "min_size_mb": 1})
    assert r.ok, r.error
    paths = [f["path"] for f in r.data["files"]]
    assert paths == [str(tmp_path / "nginx" / "access.log"), str(tmp_path / "app.log")]
    assert r.data["partial"] is False and r.data["files"][0]["size"] == "3.0M"


async def test_find_large_files_empty_and_bad_path(tmp_path):
    r = await registry.call("find_large_files", {"path": str(tmp_path), "min_size_mb": 1})
    assert r.ok and r.data["count"] == 0 and "hint" in r.data
    r = await registry.call("find_large_files", {"path": str(tmp_path / "nope")})
    assert not r.ok and "不存在" in r.error


async def test_find_large_files_respects_scan_budget(tmp_path, monkeypatch):
    for i in range(20):
        (tmp_path / f"f{i}").write_text("x")
    monkeypatch.setattr(linux, "SCAN_ENTRY_LIMIT", 5)
    r = await registry.call("find_large_files", {"path": str(tmp_path), "min_size_mb": 1})
    assert r.ok and r.data["partial"] is True


# ---------- deleted_open_files ----------

def _fake_proc(tmp_path):
    proc = tmp_path / "proc"
    held = tmp_path / "var" / "access.log (deleted)"
    held.parent.mkdir(parents=True)
    held.write_bytes(b"x" * 4096)
    pid = proc / "77"
    (pid / "fd").mkdir(parents=True)
    (pid / "comm").write_text("nginx\n")
    os.symlink(held, pid / "fd" / "5")
    os.symlink(tmp_path / "var", pid / "fd" / "6")          # 正常文件句柄，不应计入
    (proc / "self").mkdir()                                    # 非数字目录要跳过
    return proc


async def test_deleted_open_files_finds_held_space(tmp_path, monkeypatch):
    monkeypatch.setattr(linux.sys, "platform", "linux")
    monkeypatch.setattr(linux, "_proc_root", lambda: _fake_proc(tmp_path))
    r = await registry.call("deleted_open_files", {})
    assert r.ok, r.error
    assert r.data["count"] == 1
    hit = r.data["files"][0]
    assert hit["pid"] == 77 and hit["process"] == "nginx" and hit["fd"] == 5
    assert hit["path"].endswith("access.log") and hit["bytes"] == 4096
    assert "reopen" in r.data["hint"]


async def test_linux_only_tools_refuse_on_other_platforms(monkeypatch):
    monkeypatch.setattr(linux.sys, "platform", "darwin")
    r = await registry.call("deleted_open_files", {})
    assert not r.ok and "Linux" in r.error
    r = await registry.call("service_status", {"name": "nginx"})
    assert not r.ok and "Linux" in r.error


# ---------- service_status ----------

SHOW_FAILED = """Id=nginx.service
Description=A high performance web server
LoadState=loaded
ActiveState=failed
SubState=failed
Result=exit-code
MainPID=0
NRestarts=5
ExecMainStatus=1
ActiveEnterTimestamp=Tue 2026-09-29 10:00:00 CST
MemoryCurrent=[not set]
FragmentPath=/lib/systemd/system/nginx.service
"""


async def test_service_status_parses_systemd_and_journal(monkeypatch):
    calls = []

    def fake_run(argv, timeout=10.0):
        calls.append(argv)
        if argv[0] == "systemctl":
            return _proc(SHOW_FAILED)
        return _proc("2026-09-29T10:00:00 nginx[77]: bind() to 0.0.0.0:80 failed (98: Address already in use)")

    monkeypatch.setattr(linux.sys, "platform", "linux")
    monkeypatch.setattr(linux, "_run", fake_run)
    r = await registry.call("service_status", {"name": "nginx"})
    assert r.ok, r.error
    d = r.data
    assert d["active"] == "failed" and d["restarts"] == 5 and d["last_exit_status"] == 1
    assert "不在运行" in d["warning"] and "Address already in use" in d["journal"]
    assert "memory" not in d                          # [not set] 不能被当成数字
    assert calls[0][:3] == ["systemctl", "show", "nginx"] and calls[1][:3] == ["journalctl", "-u", "nginx"]


async def test_service_status_unknown_and_invalid_name(monkeypatch):
    monkeypatch.setattr(linux.sys, "platform", "linux")
    monkeypatch.setattr(linux, "_run", lambda argv, timeout=10.0: _proc("Id=foo.service\nLoadState=not-found\n"))
    r = await registry.call("service_status", {"name": "foo"})
    assert not r.ok and "没有这个服务" in r.error
    r = await registry.call("service_status", {"name": "nginx; rm -rf /"})
    assert not r.ok and "不合法" in r.error


async def test_service_status_without_systemctl(monkeypatch):
    def missing(argv, timeout=10.0):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(linux.sys, "platform", "linux")
    monkeypatch.setattr(linux, "_run", missing)
    r = await registry.call("service_status", {"name": "nginx"})
    assert not r.ok and "不是 systemd" in r.error


# ---------- restart_service：systemd 优先 + 重启后确认 ----------

def test_restart_argv_prefers_systemd_and_sudo(monkeypatch):
    monkeypatch.setattr(ops.os, "geteuid", lambda: 1000)
    assert ops._restart_argv("nginx", "systemd") == ["sudo", "-n", "systemctl", "restart", "nginx"]
    assert ops._restart_argv("nginx", "sysv") == ["sudo", "-n", "service", "nginx", "restart"]
    monkeypatch.setattr(ops.os, "geteuid", lambda: 0)
    assert ops._restart_argv("nginx", "systemd") == ["systemctl", "restart", "nginx"]


@pytest.mark.parametrize("state,ok", [("active", True), ("failed", False)])
def test_service_restart_verifies_is_active(monkeypatch, state, ok):
    calls = []

    def fake_run(argv, timeout=30.0):
        calls.append(argv)
        if "is-active" in argv:
            return ops.CommandOutcome(state == "active", state, 0 if state == "active" else 3)
        if argv[0] == "journalctl":
            return ops.CommandOutcome(True, "nginx: [emerg] unknown directive", 0)
        return ops.CommandOutcome(True, "", 0)       # restart 命令本身返回成功

    monkeypatch.setattr(ops, "_service_manager", lambda: "systemd")
    monkeypatch.setattr(ops, "_run", fake_run)
    outcome = ops._service_restart("nginx")
    assert outcome.ok is ok
    if not ok:
        assert "unknown directive" in outcome.output and "is-active: failed" in outcome.output
        assert any(c[0] == "journalctl" for c in calls)


async def test_restart_dry_run_shows_real_command(monkeypatch):
    monkeypatch.setattr(ops, "_service_manager", lambda: "systemd")
    monkeypatch.setattr(ops.os, "geteuid", lambda: 0)
    r = await registry.call("restart_service", {"name": "nginx", "dry_run": True})
    assert r.ok and r.data["action"] == "systemctl restart nginx" and r.data["service_manager"] == "systemd"


def test_new_tools_are_read_only():
    for name in ("find_large_files", "deleted_open_files", "service_status"):
        assert registry.get(name).risk == "read"


def test_disk_usage_reports_inodes():
    from server_agent.tools.system import _usage_row

    row = _usage_row("/")
    assert "percent" in row
    if hasattr(os, "statvfs") and os.statvfs("/").f_files:
        assert 0 <= row["inodes_percent"] <= 100


def test_linux_tools_never_use_shell():
    import inspect

    assert "shell=True" not in inspect.getsource(linux)
