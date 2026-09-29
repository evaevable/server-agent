"""沙箱层：局部 Docker（默认）与腾讯云 AGS（可选）。"""

from __future__ import annotations

from server_agent.sandbox.base import Sandbox, SandboxError, SandboxResult
from server_agent.sandbox.local_docker import DockerSandbox, docker_available

__all__ = ["Sandbox", "SandboxError", "SandboxResult", "DockerSandbox", "docker_available", "create_sandbox"]


def create_sandbox(prefer_ags: bool | None = None):
    """按环境选后端：有 AGS Key 且显式偏好时用 AGS，否则本地 Docker。

    「显式偏好」很重要：AGSSandbox 是收费且需要网络的，
    不该在你只是本地跑测试时偷偷用上。
    """
    import os

    want_ags = prefer_ags if prefer_ags is not None else bool(os.environ.get("E2B_API_KEY") and os.environ.get("AGS_ENABLED"))
    if want_ags:
        from server_agent.sandbox.ags import AGSSandbox

        try:
            return AGSSandbox()
        except SandboxError:
            pass  # 配置不全就回退，不让它挡住主流程
    return DockerSandbox()
