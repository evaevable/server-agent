"""本地 Docker 沙箱：默认后端，不需要任何云服务。

隔离手段（缺一不可）：
    --network none          不联网（默认的「沙箱内网」策略：连不出去）
    --read-only             根文件系统只读
    --tmpfs /work           只给一块可写的临时空间
    --memory / --cpus / --pids-limit   资源配额，防止把宿主机拖垮
    --cap-drop ALL          去掉所有内核能力
    --user 65534            以 nobody 运行
    -v 数据目录:ro           输入数据只读挂载

启动一个容器大约 1-3 秒（首次还要拉镜像），这就是「本地 Docker」与 AGS「毫秒级」的差距来源。
"""

from __future__ import annotations

import asyncio
import shlex
import time
import uuid

from server_agent.sandbox.base import SandboxError, SandboxResult

DEFAULT_IMAGE = "python:3.12-slim"


class DockerSandbox:
    backend = "local_docker"

    def __init__(self, image: str = DEFAULT_IMAGE, *, memory: str = "256m", cpus: str = "1",
                 pids_limit: int = 128, network: str = "none", allow_network: bool = False):
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.network = "bridge" if allow_network else network

    def _argv(self, code: str, sandbox_id: str, data_dir: str | None = None) -> list[str]:
        mount = ["-v", f"{data_dir}:/data:ro"] if data_dir else []
        return [
            "docker", "run", "--rm", "--name", sandbox_id,
            "--network", self.network,
            "--read-only",
            "--tmpfs", "/work:rw,size=64m,noexec=0",
            "--memory", self.memory, "--cpus", self.cpus, "--pids-limit", str(self.pids_limit),
            "--cap-drop", "ALL",
            "--user", "65534:65534",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            *mount,
            "-w", "/work",
            self.image,
            "python", "-I", "-c", code,
        ]

    async def run(self, code: str, *, files: dict[str, str] | None = None,
                  timeout: float = 30.0) -> SandboxResult:
        sandbox_id = f"sa-sb-{uuid.uuid4().hex[:8]}"
        data_dir = None
        if files:
            # 输入数据放进临时目录，只读挂载到 /data —— 沙箱里只读得到我们给的东西
            import tempfile
            from pathlib import Path

            data_dir = tempfile.mkdtemp(prefix="sa-sb-data-")
            for name, content in files.items():
                target = Path(data_dir) / Path(name).name      # 只取文件名，防止路径穿越
                target.write_text(content, encoding="utf-8")
        argv = self._argv(code, sandbox_id, data_dir)
        started = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await self._kill(sandbox_id)
            return SandboxResult(False, "", f"沙箱执行超时（>{timeout}s）", 124, self.backend,
                                 sandbox_id, round((time.perf_counter() - started) * 1000, 1))
        except FileNotFoundError as e:
            raise SandboxError(f"未找到 docker 命令：{e}。请安装 Docker 或改用其它后端") from e
        except Exception as e:  # noqa: BLE001
            raise SandboxError(f"沙箱启动失败：{e}") from e

        elapsed = round((time.perf_counter() - started) * 1000, 1)
        err = stderr.decode("utf-8", errors="replace")
        if proc.returncode != 0 and "Cannot connect to the Docker daemon" in err:
            raise SandboxError("Docker daemon 没在运行（macOS 上可执行 colima start）")
        if data_dir:
            import shutil

            shutil.rmtree(data_dir, ignore_errors=True)
        return SandboxResult(proc.returncode == 0,
                             stdout.decode("utf-8", errors="replace")[:20000],
                             err[:8000], proc.returncode or 0, self.backend, sandbox_id, elapsed)

    async def _kill(self, sandbox_id: str) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "kill", sandbox_id,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), timeout=10)
        except Exception:  # noqa: BLE001
            pass

    async def close(self) -> None:
        """容器是 --rm 的一次性容器，无需清理；这里保留接口以便与 AGS 对齐。"""
        return None


def docker_available() -> bool:
    """粗略探测：docker 命令在不在（不检查 daemon）。"""
    import shutil

    return shutil.which("docker") is not None
