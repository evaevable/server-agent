"""LLM 调用层的公共数据结构与协议。

整个项目只通过这里定义的类型和模型打交道：换模型厂商、换成 MockLLM，
上层（第 04 章的 Agent 循环）一行都不用改。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol

Role = Literal["system", "user", "assistant", "tool"]


class LLMError(RuntimeError):
    """调用模型失败。retryable 表示这类错误重试可能成功（限流、5xx、超时）。"""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass
class ToolCall:
    """模型提议的一次工具调用。arguments 保留模型给的原始 JSON 字符串。"""

    id: str
    name: str
    arguments: str = "{}"

    def parsed_arguments(self) -> dict[str, Any]:
        """解析参数。模型可能给出非法 JSON，这里抛 ValueError，由上层决定如何回喂。"""
        try:
            data = json.loads(self.arguments or "{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"工具参数不是合法 JSON: {e}") from e
        if not isinstance(data, dict):
            raise ValueError("工具参数必须是 JSON 对象")
        return data


@dataclass
class Message:
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None  # role=tool 时，指明回应的是哪次调用
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转成 OpenAI Chat Completions 的消息格式。"""
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in self.tool_calls
            ]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        return d

    @classmethod
    def system(cls, text: str) -> "Message":
        return cls("system", text)

    @classmethod
    def user(cls, text: str) -> "Message":
        return cls("user", text)

    @classmethod
    def assistant(cls, text: str | None = None, tool_calls: list[ToolCall] | None = None) -> "Message":
        return cls("assistant", text, tool_calls=tool_calls)

    @classmethod
    def tool(cls, tool_call_id: str, content: str) -> "Message":
        return cls("tool", content, tool_call_id=tool_call_id)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.total_tokens + other.total_tokens,
        )


@dataclass
class ChatResponse:
    message: Message
    finish_reason: str | None = None  # stop / tool_calls / length / content_filter
    usage: Usage = field(default_factory=Usage)
    model: str | None = None
    reasoning: str | None = None  # 思考模型的推理过程（如 DeepSeek 的 reasoning_content），不回传给模型


@dataclass
class StreamEvent:
    """流式输出的事件。

    text=一段增量回答；reasoning=一段增量思考过程（仅思考模型）；done=流结束，response 为拼装好的完整结果。
    """

    type: Literal["text", "reasoning", "done"]
    text: str = ""
    response: ChatResponse | None = None


class LLMClient(Protocol):
    """所有模型客户端（真实的、Mock 的）都实现这两个方法。"""

    async def chat(self, messages: list[Message], tools: list[dict] | None = None, **opts: Any) -> ChatResponse: ...

    def stream(
        self, messages: list[Message], tools: list[dict] | None = None, **opts: Any
    ) -> AsyncIterator[StreamEvent]: ...
