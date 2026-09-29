"""MockLLM：按剧本回放的假模型。

用途：让 Agent 行为测试确定、免费、离线可跑。
- 剧本里每一项对应一次 chat/stream 调用的返回值（str 表示纯文本回答）。
- calls 记录每次收到的消息与工具列表，测试可以断言「Agent 给模型看了什么」。
- 剧本为空时进入 echo 模式，便于没有 API Key 时体验 CLI。
"""

from __future__ import annotations

import copy
import itertools
import json
from typing import Any, AsyncIterator, Callable

from server_agent.llm.base import ChatResponse, LLMError, Message, StreamEvent, ToolCall, Usage

ScriptItem = str | ChatResponse | Callable[[list[Message]], ChatResponse | str]
_ids = itertools.count(1)


def text(content: str) -> ChatResponse:
    return ChatResponse(Message.assistant(content), finish_reason="stop")


def tool_call(name: str, arguments: dict | None = None, *, call_id: str | None = None) -> ChatResponse:
    tc = ToolCall(id=call_id or f"call_{next(_ids)}", name=name, arguments=json.dumps(arguments or {}))
    return ChatResponse(Message.assistant(None, [tc]), finish_reason="tool_calls")


class MockLLM:
    def __init__(self, script: list[ScriptItem] | None = None, *, echo: bool | None = None, chunk_size: int = 4):
        self.script = list(script or [])
        self.echo = (not self.script) if echo is None else echo
        self.chunk_size = chunk_size
        self.calls: list[dict[str, Any]] = []

    def _next(self, messages: list[Message], tools: list[dict] | None) -> ChatResponse:
        self.calls.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)})
        if self.script:
            item = self.script.pop(0)
            if callable(item):
                item = item(messages)
        elif self.echo:
            last = next((m.content for m in reversed(messages) if m.role == "user"), "")
            item = f"（mock）你说的是：{last}"
        else:
            raise LLMError("MockLLM 剧本已用完：Agent 调用模型的次数比预期多")
        resp = text(item) if isinstance(item, str) else item
        chars = sum(len(m.content or "") for m in messages)
        out = len(resp.message.content or "")
        resp.usage = Usage(chars, out, chars + out)  # 用字符数粗略模拟 token
        resp.model = "mock"
        return resp

    async def chat(self, messages: list[Message], tools: list[dict] | None = None, **opts: Any) -> ChatResponse:
        return self._next(messages, tools)

    async def stream(
        self, messages: list[Message], tools: list[dict] | None = None, **opts: Any
    ) -> AsyncIterator[StreamEvent]:
        resp = self._next(messages, tools)
        content = resp.message.content or ""
        for i in range(0, len(content), self.chunk_size):
            yield StreamEvent("text", text=content[i : i + self.chunk_size])
        yield StreamEvent("done", response=resp)
