"""腾讯云 Agent 沙箱（AGS）后端：E2B 兼容协议，毫秒级启动。

为什么单独写一个后端？三个真实差异：
1. **启动快**：AGS 实例毫秒级启动（本地 Docker 要拉镜像 + 起容器）；
2. **隔离强**：沙箱跑在云端隔离环境里，本地机器完全不受影响；
3. **用完即毁**：沙箱工具设了生命周期（如 5m/1h），到点自动销毁，不需要自己清理。

需要两个环境变量：
    E2B_DOMAIN=ap-guangzhou.tencentags.com   （按控制台实际域名）
    E2B_API_KEY=<在 AGS 控制台创建的 API Key>
以及一个「沙箱工具」名（模板），通过 AGS_TEMPLATE 指定。

实现上按 E2B 的 REST 约定走（创建沙箱 -> 执行代码 -> 关闭），用 httpx 直连，
不额外引入 SDK —— 与本项目「依赖尽量少」的一贯取舍一致。

**注意**：本文件在没有 Key 的环境里不会被调用（工厂会回退到本地 Docker 沙箱），
因此它的正确性依赖真实 AGS 环境验证；接入前请对照官方文档核对字段名。
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx

from server_agent.sandbox.base import SandboxError, SandboxResult


class AGSSandbox:
    backend = "ags"

    def __init__(self, *, domain: str | None = None, api_key: str | None = None,
                 template: str | None = None, timeout: float = 60.0):
        self.domain = domain or os.environ.get("E2B_DOMAIN", "")
        self.api_key = api_key or os.environ.get("E2B_API_KEY", "")
        self.template = template or os.environ.get("AGS_TEMPLATE", "code-interpreter-v1")
        self.timeout = timeout
        if not self.domain or not self.api_key:
            raise SandboxError("缺少 E2B_DOMAIN / E2B_API_KEY，无法使用 AGS 后端")
        self.base = f"https://{self.domain}"
        self.headers = {"X-API-Key": self.api_key, "Content-Type": "application/json"}

    async def _create(self) -> str:
        url = f"{self.base}/sandboxes"
        payload = {"template": self.template, "timeout": int(self.timeout)}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, headers=self.headers, json=payload)
        if resp.status_code >= 300:
            raise SandboxError(f"创建 AGS 沙箱失败（{resp.status_code}）：{resp.text[:300]}")
        data = resp.json()
        sandbox_id = data.get("sandboxID") or data.get("sandbox_id") or data.get("id")
        if not sandbox_id:
            raise SandboxError(f"创建沙箱的响应里没有 id：{data}")
        return str(sandbox_id)

    async def run(self, code: str, *, files: dict[str, str] | None = None,
                  timeout: float = 30.0) -> SandboxResult:
        started = time.perf_counter()
        sandbox_id = await self._create()
        try:
            url = f"{self.base}/sandboxes/{sandbox_id}/code"
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, headers=self.headers,
                                         json={"code": code, "language": "python"})
            ok = resp.status_code < 300
            body = resp.json() if ok else {}
            return SandboxResult(ok, str(body.get("stdout", ""))[:20000],
                                 str(body.get("stderr") or resp.text)[:8000],
                                 int(body.get("exitCode", 0) or 0), self.backend, sandbox_id,
                                 round((time.perf_counter() - started) * 1000, 1))
        finally:
            await self.close(sandbox_id)

    async def close(self, sandbox_id: str | None = None) -> None:
        if not sandbox_id:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.delete(f"{self.base}/sandboxes/{sandbox_id}", headers=self.headers)
        except Exception:  # noqa: BLE001 —— 关闭失败不影响主流程（AGS 会自动回收）
            pass


async def _demo() -> None:  # pragma: no cover —— 需要真实 Key
    sb = AGSSandbox()
    try:
        result = await sb.run("print(sum(range(10)))")
        print(result.ok, result.stdout, result.backend, result.elapsed_ms)
    finally:
        await asyncio.sleep(0)
