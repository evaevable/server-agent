"""工具注册表：把普通 Python 函数变成模型可调用的工具。

核心思想：模型看不到函数实现，只看得到 name + description + parameters(JSON Schema)。
所以这里做三件事：
1. 从函数签名（类型注解 + Annotated[..., Field(...)]）自动生成 pydantic 参数模型与 JSON Schema；
2. 调用时用同一个模型校验参数，校验失败返回可读错误而不是抛异常；
3. 统一序列化与截断工具结果，给模型的永远是一段有长度上限的字符串。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal, get_type_hints

from pydantic import BaseModel, ConfigDict, ValidationError, create_model

Risk = Literal["read", "low", "high", "forbidden"]  # 第 09 章据此做审批
DEFAULT_MAX_CHARS = 4000
DEFAULT_TIMEOUT = 30.0


class ToolError(Exception):
    """工具主动抛出、希望原样告诉模型的错误（如「路径不存在」）。"""


@dataclass
class ToolResult:
    name: str
    ok: bool
    content: str  # 回喂给模型的文本（已截断）
    data: Any = None  # 原始返回值，给程序用（前端展示、测试断言）
    error: str | None = None
    truncated: bool = False
    elapsed_ms: float = 0.0


@dataclass
class Tool:
    name: str
    description: str
    func: Callable[..., Any]
    params: type[BaseModel]
    risk: Risk = "read"
    max_chars: int = DEFAULT_MAX_CHARS
    timeout: float = DEFAULT_TIMEOUT

    def schema(self) -> dict:
        """OpenAI Chat Completions 的 tools 数组元素格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": clean_schema(self.params.model_json_schema()),
            },
        }


def clean_schema(node: Any) -> Any:
    """精简 pydantic 生成的 Schema：去掉自动生成的 title；把 Optional 的 anyOf[X, null] 压平成 X。

    少一层嵌套，模型更不容易填错；也省 token（schema 每轮都要发给模型）。
    """
    if isinstance(node, list):
        return [clean_schema(x) for x in node]
    if not isinstance(node, dict):
        return node
    out = {k: clean_schema(v) for k, v in node.items() if not (k == "title" and isinstance(v, str))}
    any_of = out.get("anyOf")
    if isinstance(any_of, list) and len(any_of) == 2 and {"type": "null"} in any_of:
        other = next(x for x in any_of if x != {"type": "null"})
        out.pop("anyOf")
        out = {**other, **out}
    return out


def build_params_model(func: Callable[..., Any]) -> type[BaseModel]:
    """从函数签名生成参数模型。extra=forbid：模型编造的多余参数会被拒绝并告知。"""
    hints = get_type_hints(func, include_extras=True)
    fields: dict[str, Any] = {}
    for pname, p in inspect.signature(func).parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            raise TypeError(f"工具 {func.__name__} 不能使用 *args / **kwargs：模型需要明确的参数列表")
        if pname not in hints:
            raise TypeError(f"工具 {func.__name__} 的参数 {pname} 缺少类型注解")
        default = ... if p.default is inspect.Parameter.empty else p.default
        fields[pname] = (hints[pname], default)
    return create_model(f"{func.__name__}_params", __config__=ConfigDict(extra="forbid"), **fields)


def _format_validation_error(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "(参数整体)"
        parts.append(f"{loc}: {err['msg']}")
    return "参数校验失败：" + "；".join(parts)


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    note = f"\n...[已截断：原始 {len(text)} 字符，仅保留前 {max_chars} 字符。如需更多，请缩小查询范围]"
    return text[:max_chars] + note, True


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # ---------- 注册 ----------
    def register(self, t: Tool) -> Tool:
        if t.name in self._tools:
            raise ValueError(f"工具重名: {t.name}")
        self._tools[t.name] = t
        return t

    def tool(
        self,
        func: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        risk: Risk = "read",
        max_chars: int = DEFAULT_MAX_CHARS,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        """装饰器。函数的 docstring 就是给模型看的工具描述，必须写。"""

        def deco(f: Callable[..., Any]) -> Callable[..., Any]:
            desc = inspect.getdoc(f)
            if not desc:
                raise ValueError(f"工具 {f.__name__} 缺少 docstring：模型只能通过描述理解工具，不允许省略")
            self.register(Tool(name or f.__name__, desc, f, build_params_model(f), risk, max_chars, timeout))
            return f

        return deco(func) if func else deco

    # ---------- 查询 ----------
    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def schemas(self, names: list[str] | None = None) -> list[dict]:
        return [t.schema() for t in self._tools.values() if names is None or t.name in names]

    # ---------- 调用 ----------
    async def call(self, name: str, arguments: dict | str | None = None, *,
                   policy=None, approver=None, audit=None, run_id: str = "") -> ToolResult:
        """执行工具。任何失败都变成 ok=False 的 ToolResult，绝不向上抛异常：
        第 04 章的 Agent 循环会把错误作为「观察」回喂给模型，让它自己纠正。"""
        start = time.perf_counter()

        def fail(msg: str) -> ToolResult:
            return ToolResult(name, False, json.dumps({"error": msg}, ensure_ascii=False), error=msg,
                              elapsed_ms=round((time.perf_counter() - start) * 1000, 1))

        t = self._tools.get(name)
        if t is None:
            return fail(f"未知工具 {name}。可用工具：{', '.join(self._tools)}")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError as e:
                return fail(f"参数不是合法 JSON：{e}")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return fail("参数必须是 JSON 对象")
        try:
            params = t.params.model_validate(arguments)
        except ValidationError as e:
            return fail(_format_validation_error(e))

        kwargs = {k: getattr(params, k) for k in t.params.model_fields}

        # ---------- 第 09 章：策略层。所有副作用都要过这道关 ----------
        if policy is not None:
            decision = policy.decide(t.name, kwargs, risk=t.risk)
            if not decision.allowed:
                if audit:
                    audit.log("denied", run_id=run_id, tool=t.name, args=kwargs,
                              decision="forbidden", detail=decision.reason)
                return fail(f"策略拒绝执行：{decision.reason}")
            if decision.needs_approval:
                preview = kwargs
                if "dry_run" in t.params.model_fields and not kwargs.get("dry_run"):
                    preview = {**kwargs, "dry_run": True}
                preview_result = await self._execute(t, preview)
                dry_run_info = preview_result if isinstance(preview_result, (dict, list)) else str(preview_result)[:500]
                if audit:
                    audit.log("approval", run_id=run_id, tool=t.name, args=kwargs,
                              decision="approval", approved=None, detail=decision.reason)
                approved = False
                if approver is not None:
                    approved = await approver(t.name, kwargs, dry_run_info, decision.reason)
                if not approved:
                    if audit:
                        audit.log("denied", run_id=run_id, tool=t.name, args=kwargs,
                                  decision="approval", approved=False,
                                  detail="人工审批未通过（超时或拒绝一律视为不批准）")
                    return fail("人工审批未通过：该操作未执行。"
                                "如果你认为确有必要，请向用户说明原因并请其手动执行或重新批准。")
                if audit:
                    audit.log("approved", run_id=run_id, tool=t.name, args=kwargs,
                              decision="approval", approved=True, detail="人工已批准")
            if audit:
                audit.log("tool_call", run_id=run_id, tool=t.name, args=kwargs, decision=decision.label)

        try:
            result = await self._execute(t, kwargs)
        except asyncio.TimeoutError:
            return fail(f"工具执行超时（>{t.timeout} 秒）")
        except ToolError as e:
            if audit:
                audit.log("error", run_id=run_id, tool=t.name, args=kwargs, ok=False, detail=str(e))
            return fail(str(e))
        except Exception as e:  # noqa: BLE001 —— 工具内部任何异常都转成可读错误
            if audit:
                audit.log("error", run_id=run_id, tool=t.name, args=kwargs, ok=False,
                          detail=f"{type(e).__name__}: {e}")
            return fail(f"{type(e).__name__}: {e}")

        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        content, cut = truncate(text, t.max_chars)
        elapsed = round((time.perf_counter() - start) * 1000, 1)
        if audit:
            audit.log("tool_result", run_id=run_id, tool=t.name, args=kwargs, ok=True,
                      duration_ms=elapsed, detail=content[:300])
        return ToolResult(name, True, content, data=result, truncated=cut, elapsed_ms=elapsed)

    async def _execute(self, t: Tool, kwargs: dict):
        """真正调用工具函数。同步函数放进线程池，避免阻塞事件循环。"""
        if inspect.iscoroutinefunction(t.func):
            return await asyncio.wait_for(t.func(**kwargs), t.timeout)
        return await asyncio.wait_for(asyncio.to_thread(t.func, **kwargs), t.timeout)


# 全局默认注册表。system.py 等模块用 @tool 把工具注册到这里。
registry = ToolRegistry()
tool = registry.tool
