"""沙箱与 run_python 测试（不需要 Docker）。"""

import pytest

from server_agent.policy import Policy
from server_agent.sandbox import DockerSandbox, SandboxError, create_sandbox
from server_agent.sandbox.base import SandboxResult
from server_agent.tools import registry


class FakeSandbox:
    backend = "fake"

    def __init__(self, result: SandboxResult | None = None, raises: Exception | None = None):
        self.result = result or SandboxResult(True, "42\n", "", 0, self.backend, "sb_1", 12.0)
        self.raises = raises
        self.calls: list[dict] = []

    async def run(self, code, *, files=None, timeout=30.0):
        self.calls.append({"code": code, "files": files, "timeout": timeout})
        if self.raises:
            raise self.raises
        return self.result

    async def close(self):
        return None


# ---------- Docker 后端的隔离参数（不需要 daemon，只检查命令行） ----------

def test_docker_sandbox_argv_has_all_isolation_flags():
    argv = DockerSandbox()._argv("print(1)", "sa-sb-test")
    joined = " ".join(argv)
    assert "--network none" in joined                 # 不联网
    assert "--read-only" in argv                      # 根文件系统只读
    assert "--cap-drop ALL" in joined                 # 去掉内核能力
    assert "--memory 256m" in joined and "--cpus 1" in joined and "--pids-limit 128" in joined
    assert "--user 65534:65534" in joined             # 非 root
    assert argv[-3:] == ["python", "-I", "-c", "print(1)"][-3:] or argv[-1] == "print(1)"
    assert argv[-2] == "-c" and argv[-1] == "print(1)"


def test_docker_sandbox_mounts_input_data_read_only(tmp_path):
    sb = DockerSandbox()
    argv = sb._argv("print(open('/data/x').read())", "sa-sb-test", str(tmp_path))
    assert "-v" in argv and f"{tmp_path}:/data:ro" in argv


def test_docker_sandbox_allow_network_flag():
    assert "--network bridge" in " ".join(DockerSandbox(allow_network=True)._argv("x", "id"))


def test_create_sandbox_defaults_to_local_docker(monkeypatch):
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.delenv("AGS_ENABLED", raising=False)
    assert isinstance(create_sandbox(), DockerSandbox)
    assert isinstance(create_sandbox(prefer_ags=False), DockerSandbox)


def test_create_sandbox_falls_back_when_ags_config_missing(monkeypatch):
    """显式要 AGS 但没配 Key 时，回退到本地 Docker，而不是报错崩掉。"""
    monkeypatch.delenv("E2B_DOMAIN", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    assert isinstance(create_sandbox(prefer_ags=True), DockerSandbox)


def test_ags_backend_requires_credentials(monkeypatch):
    from server_agent.sandbox.ags import AGSSandbox

    monkeypatch.delenv("E2B_DOMAIN", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    with pytest.raises(SandboxError, match="E2B_DOMAIN"):
        AGSSandbox()


# ---------- run_python 工具 ----------

async def test_run_python_passes_code_and_files(monkeypatch):
    fake = FakeSandbox()
    monkeypatch.setattr("server_agent.tools.sandbox_tool._make_sandbox", lambda: fake)

    r = await registry.call("run_python", {
        "code": "print(sum(1 for _ in open('/data/log')))",
        "purpose": "统计日志行数",
        "data_files": {"log": "a\nb\nc\n"},
    })
    assert r.ok and r.data["stdout"].strip() == "42" and r.data["backend"] == "fake"
    assert fake.calls[0]["files"] == {"log": "a\nb\nc\n"}
    assert "统计日志行数" in r.data["purpose"]


async def test_run_python_reports_sandbox_failure(monkeypatch):
    fake = FakeSandbox(raises=SandboxError("Docker daemon 没在运行（macOS 上可执行 colima start）"))
    monkeypatch.setattr("server_agent.tools.sandbox_tool._make_sandbox", lambda: fake)
    r = await registry.call("run_python", {"code": "print(1)"})
    assert not r.ok and "沙箱不可用" in r.error and "colima" in r.error


async def test_run_python_timeout_and_nonzero_exit(monkeypatch):
    fake = FakeSandbox(SandboxResult(False, "", "沙箱执行超时（>30s）", 124, "fake", "sb", 30000))
    monkeypatch.setattr("server_agent.tools.sandbox_tool._make_sandbox", lambda: fake)
    r = await registry.call("run_python", {"code": "while True: pass"})
    assert not r.ok and "超时" in r.error

    fake2 = FakeSandbox(SandboxResult(False, "", "NameError: name 'x' is not defined", 1, "fake", "sb", 5))
    monkeypatch.setattr("server_agent.tools.sandbox_tool._make_sandbox", lambda: fake2)
    r = await registry.call("run_python", {"code": "print(x)"})
    assert r.ok and r.data["ok"] is False and "NameError" in r.data["stderr"]   # 执行完成但代码报错


async def test_explicit_ags_backend_does_not_silently_fall_back(monkeypatch):
    """ADR-0003 第 4 条：显式 SA_SANDBOX_BACKEND=ags 时配置不全要报错，不能悄悄换成本地 Docker。"""
    monkeypatch.delenv("E2B_DOMAIN", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.setenv("SA_SANDBOX_BACKEND", "ags")
    r = await registry.call("run_python", {"code": "print(1)"})
    assert not r.ok and "沙箱不可用" in r.error and "E2B_DOMAIN" in r.error


async def test_run_python_never_falls_back_to_host_execution(monkeypatch):
    """ADR-0003 第 1 条：Docker 不可用时直接报错，绝不退化为在宿主机上执行模型代码。"""
    import subprocess

    def boom(*a, **k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr("asyncio.create_subprocess_exec", boom)
    monkeypatch.setenv("SA_SANDBOX_BACKEND", "local_docker")
    marker = "__host_exec_marker__"
    r = await registry.call("run_python", {"code": f"print('{marker}')"})
    assert not r.ok and "沙箱不可用" in r.error
    assert marker not in (r.content or "")


async def test_run_python_rejects_oversized_input_and_disabled_sandbox(monkeypatch):
    fake = FakeSandbox()
    monkeypatch.setattr("server_agent.tools.sandbox_tool._make_sandbox", lambda: fake)
    r = await registry.call("run_python", {"code": "print(1)", "data_files": {"big": "x" * 500_000}})
    assert not r.ok and "过大" in r.error and not fake.calls

    from server_agent.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("SA_SANDBOX_ENABLED", "false")
    try:
        r = await registry.call("run_python", {"code": "print(1)"})
        assert not r.ok and "沙箱已禁用" in r.error
    finally:
        get_settings.cache_clear()


async def test_run_python_risk_and_no_shell_bypass():
    """run_python 是 low 风险：它不直接改系统，隔离由沙箱保证；但仍要过策略层与审计。"""
    t = registry.get("run_python")
    assert t.risk == "low"
    from server_agent.policy import AuditLog

    audit = AuditLog(None)

    class Fail:
        backend = "fake"

        async def run(self, code, *, files=None, timeout=30.0):
            raise SandboxError("no docker")

        async def close(self):
            return None

    import server_agent.tools.sandbox_tool as st

    original = st._make_sandbox
    st._make_sandbox = lambda: Fail()
    try:
        r = await registry.call("run_python", {"code": "print(1)"}, policy=Policy(), audit=audit)
        assert not r.ok
        assert any(rec["tool"] == "run_python" for rec in audit.records())
    finally:
        st._make_sandbox = original


@pytest.mark.skipif(not DockerSandbox().__class__, reason="always false")
def test_docker_real_execution_shapes(monkeypatch):
    """真实 Docker 执行：daemon 不可用时跳过（本机环境常见）。"""
    import asyncio
    import shutil

    if not shutil.which("docker"):
        pytest.skip("未安装 docker")
    sb = DockerSandbox()
    try:
        result = asyncio.run(sb.run("print(6*7)"))
    except SandboxError as e:
        pytest.skip(f"docker daemon 不可用：{e}")
    assert result.ok and result.stdout.strip() == "42"
