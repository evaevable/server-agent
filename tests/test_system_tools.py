import json

import pytest

from server_agent.tools import registry
from server_agent.tools.system import human

EXPECTED = {"host_info", "cpu_memory_usage", "disk_usage", "top_processes", "listening_ports", "tail_file"}


MEMORY_TOOLS = {"recall_host", "remember_fact"}
LOW_RISK_TOOLS = {"run_python"}     # 沙箱执行：不改系统，隔离由沙箱保证
HIGH_RISK_TOOLS = {"restart_service", "kill_process", "clean_directory",
                   "remote_restart_service"}


def test_all_six_tools_registered_readonly_and_schemas_valid():
    assert set(registry.names()) >= EXPECTED
    for t in registry.list():
        if t.name in MEMORY_TOOLS:
            assert t.risk in ("read", "low")   # 记忆工具只写本地库，不属于系统写操作
            continue
        if t.name in HIGH_RISK_TOOLS:
            assert t.risk == "high"            # 写操作必须走审批
            continue
        if t.name in LOW_RISK_TOOLS:
            assert t.risk == "low"
            continue
        assert t.risk == "read"
        fn = t.schema()["function"]
        assert fn["description"] and fn["parameters"]["type"] == "object"
        json.dumps(fn)  # 可序列化
        for prop in fn["parameters"]["properties"].values():
            assert prop.get("description"), f"{t.name} 有参数缺少描述"


def test_human():
    assert human(512) == "512B" and human(1536) == "1.5K" and human(3 * 1024**3) == "3.0G"


async def test_host_info_and_cpu_memory():
    r = await registry.call("host_info")
    assert r.ok and {"hostname", "os", "cpu_logical", "uptime"} <= r.data.keys()
    r = await registry.call("cpu_memory_usage", {"interval": 0.1})
    assert r.ok and 0 <= r.data["cpu_percent"] <= 100 and "memory" in r.data


async def test_disk_usage():
    r = await registry.call("disk_usage", {"path": "/"})
    assert r.ok and 0 <= r.data["partitions"][0]["percent"] <= 100
    r = await registry.call("disk_usage")
    assert r.ok and len(r.data["partitions"]) >= 1
    r = await registry.call("disk_usage", {"path": "/definitely/not/here"})
    assert not r.ok and "路径不存在" in r.error


@pytest.mark.parametrize("sort_by", ["cpu", "memory"])
async def test_top_processes_sorted_and_limited(sort_by):
    r = await registry.call("top_processes", {"sort_by": sort_by, "limit": 3})
    assert r.ok
    procs = r.data["processes"]
    key = "cpu_percent" if sort_by == "cpu" else "memory_percent"
    assert 1 <= len(procs) <= 3 and [p[key] for p in procs] == sorted((p[key] for p in procs), reverse=True)


async def test_listening_ports_bounded():
    r = await registry.call("listening_ports")
    assert r.ok and len(r.data["ports"]) <= 30
    r = await registry.call("listening_ports", {"limit": 2})
    assert r.ok and len(r.data["ports"]) <= 2 and not r.truncated
    r = await registry.call("listening_ports", {"port": 1})
    assert r.ok and all(p["port"] == 1 for p in r.data["ports"])


@pytest.fixture
def log_file(tmp_path):
    p = tmp_path / "app.log"
    p.write_text("\n".join(f"line {i} {'ERROR' if i % 10 == 0 else 'info'}" for i in range(1, 101)) + "\n")
    return p


async def test_tail_file_lines_and_grep(log_file):
    r = await registry.call("tail_file", {"path": str(log_file), "lines": 3})
    assert r.ok and r.data["content"].splitlines() == ["line 98 info", "line 99 info", "line 100 ERROR"]
    r = await registry.call("tail_file", {"path": str(log_file), "lines": 2, "grep": "error"})
    assert r.data["content"].splitlines() == ["line 90 ERROR", "line 100 ERROR"]


async def test_tail_file_rejections(tmp_path, log_file):
    cases = {
        "app.log": "绝对路径",
        str(tmp_path / "nope.log"): "不存在",
        str(tmp_path): "目录",
    }
    for path, needle in cases.items():
        r = await registry.call("tail_file", {"path": path})
        assert not r.ok and needle in r.error
    b = tmp_path / "bin.dat"
    b.write_bytes(b"\x7fELF\x00\x00\x01")
    r = await registry.call("tail_file", {"path": str(b)})
    assert not r.ok and "二进制" in r.error
    r = await registry.call("tail_file", {"path": str(log_file), "lines": 5000})
    assert not r.ok and "lines" in r.error


async def test_tail_file_large_file_scans_only_tail(tmp_path):
    p = tmp_path / "big.log"
    with p.open("w") as f:
        for i in range(200_000):  # 约 3MB，超过 2MB 扫描窗口
            f.write(f"row {i:06d} padding-padding\n")
    r = await registry.call("tail_file", {"path": str(p), "lines": 1})
    assert r.ok and r.data["content"] == "row 199999 padding-padding"
