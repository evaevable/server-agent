"""OpenAI 兼容 Chat Completions 客户端，用 httpx 直接发请求，不依赖任何厂商 SDK。

适用于 DeepSeek、通义千问（百炼兼容模式）、混元、vLLM、Ollama 等提供
POST {base_url}/chat/completions 接口的服务。
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any, AsyncIterator

import httpx

from server_agent.llm.base import ChatResponse, LLMError, Message, StreamEvent, ToolCall, Usage

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class OpenAICompatClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        *,
        temperature: float = 0.2,
        timeout: float = 60.0,
        max_retries: int = 2,
        stream_usage: bool = True,
        extra_body: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,  # 测试注入假网络
    ):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.temperature = temperature
        self.max_retries = max_retries
        self.stream_usage = stream_usage
        self.extra_body = dict(extra_body or {})  # 厂商私有参数，如 {"thinking": {"type": "disabled"}}
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(headers=headers, timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------- 请求体 ----------
    def _payload(self, messages: list[Message], tools: list[dict] | None, stream: bool, opts: dict) -> dict:
        body: dict[str, Any] = {
            "model": opts.pop("model", self.model),
            "messages": [m.to_dict() for m in messages],
            "temperature": opts.pop("temperature", self.temperature),
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = opts.pop("tool_choice", "auto")
        if stream:
            body["stream"] = True
            if self.stream_usage:
                body["stream_options"] = {"include_usage": True}
        body.update(self.extra_body)
        body.update(opts)  # max_tokens 等调用时参数优先级最高
        return body

    # ---------- 重试 ----------
    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if retry_after and retry_after.replace(".", "", 1).isdigit():
            delay = min(float(retry_after), 30.0)
        else:
            delay = min(0.5 * 2**attempt, 8.0) + random.uniform(0, 0.25)
        await asyncio.sleep(delay)

    @staticmethod
    def _error_from(resp: httpx.Response) -> LLMError:
        text = resp.text[:500]
        return LLMError(
            f"LLM 接口返回 {resp.status_code}: {text}",
            status=resp.status_code,
            retryable=resp.status_code in RETRYABLE_STATUS,
        )

    # ---------- 非流式 ----------
    async def chat(self, messages: list[Message], tools: list[dict] | None = None, **opts: Any) -> ChatResponse:
        body = self._payload(messages, tools, stream=False, opts=dict(opts))
        last: LLMError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._client.post(self.url, json=body)
            except httpx.TimeoutException as e:
                last = LLMError(f"LLM 请求超时: {e}", retryable=True)
            except httpx.TransportError as e:
                last = LLMError(f"LLM 网络错误: {e}", retryable=True)
            else:
                if resp.status_code == 200:
                    return parse_response(resp.json())
                last = self._error_from(resp)
                if not last.retryable:
                    raise last
                if attempt < self.max_retries:
                    await self._backoff(attempt, resp.headers.get("retry-after"))
                continue
            if attempt < self.max_retries:
                await self._backoff(attempt, None)
        assert last is not None
        raise last

    # ---------- 流式 ----------
    async def stream(
        self, messages: list[Message], tools: list[dict] | None = None, **opts: Any
    ) -> AsyncIterator[StreamEvent]:
        """流式输出。只在收到第一个字节之前重试；一旦开始输出就不再重试，避免重复内容。"""
        body = self._payload(messages, tools, stream=True, opts=dict(opts))
        for attempt in range(self.max_retries + 1):
            try:
                async with self._client.stream("POST", self.url, json=body) as resp:
                    if resp.status_code != 200:
                        await resp.aread()
                        err = self._error_from(resp)
                        if err.retryable and attempt < self.max_retries:
                            await self._backoff(attempt, resp.headers.get("retry-after"))
                            continue
                        raise err
                    acc = _StreamAccumulator()
                    async for line in resp.aiter_lines():
                        text, reasoning = acc.feed_line(line)
                        if reasoning:
                            yield StreamEvent("reasoning", text=reasoning)
                        if text:
                            yield StreamEvent("text", text=text)
                    yield StreamEvent("done", response=acc.result())
                    return
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt < self.max_retries:
                    await self._backoff(attempt, None)
                    continue
                raise LLMError(f"LLM 流式请求失败: {e}", retryable=True) from e


def parse_response(data: dict) -> ChatResponse:
    """把非流式响应 JSON 解析为 ChatResponse。"""
    try:
        choice = data["choices"][0]
        msg = choice["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"无法解析的 LLM 响应: {str(data)[:300]}") from e
    tool_calls = [
        ToolCall(id=tc.get("id", ""), name=tc["function"]["name"], arguments=tc["function"].get("arguments") or "{}")
        for tc in (msg.get("tool_calls") or [])
    ] or None
    u = data.get("usage") or {}
    return ChatResponse(
        message=Message("assistant", msg.get("content"), tool_calls=tool_calls),
        finish_reason=choice.get("finish_reason"),
        usage=Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0), u.get("total_tokens", 0)),
        model=data.get("model"),
        reasoning=_reasoning_of(msg) or None,
    )


def _reasoning_of(obj: dict) -> str:
    """思考内容的字段名因服务而异：DeepSeek / 百炼用 reasoning_content，vLLM 0.10+ 与 OpenRouter 用 reasoning。"""
    value = obj.get("reasoning_content") or obj.get("reasoning")
    return value if isinstance(value, str) else ""


class _StreamAccumulator:
    """把 SSE 分片一片片拼回完整消息。

    SSE 格式：每个事件一行 `data: {json}`，以空行分隔，最后是 `data: [DONE]`。
    文本在 choices[0].delta.content；工具调用在 delta.tool_calls，按 index 分片到达，
    arguments 是一段段 JSON 字符串碎片，需要拼接。
    思考模型（如 DeepSeek V4 默认思考模式）另有 delta.reasoning_content，与 content 分开。
    """

    def __init__(self) -> None:
        self.text: list[str] = []
        self.reasoning: list[str] = []
        self.tools: dict[int, dict[str, str]] = {}
        self.finish_reason: str | None = None
        self.usage = Usage()
        self.model: str | None = None

    def feed_line(self, line: str) -> tuple[str, str]:
        """吃进一行 SSE，返回 (本行新增回答文本, 本行新增思考文本)。"""
        line = line.strip()
        if not line.startswith("data:"):
            return "", ""  # 空行、注释行（以 : 开头的心跳）直接忽略
        payload = line[5:].strip()
        if payload == "[DONE]":
            return "", ""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return "", ""
        self.model = data.get("model", self.model)
        if data.get("usage"):
            u = data["usage"]
            self.usage = Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0), u.get("total_tokens", 0))
        out, think = "", ""
        for choice in data.get("choices") or []:
            delta = choice.get("delta") or {}
            piece = _reasoning_of(delta)
            if piece:
                think += piece
                self.reasoning.append(piece)
            if delta.get("content"):
                out += delta["content"]
                self.text.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                slot = self.tools.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
        return out, think

    def result(self) -> ChatResponse:
        tool_calls = [
            ToolCall(id=s["id"], name=s["name"], arguments=s["arguments"] or "{}")
            for _, s in sorted(self.tools.items())
        ] or None
        return ChatResponse(
            message=Message("assistant", "".join(self.text) or None, tool_calls=tool_calls),
            finish_reason=self.finish_reason,
            usage=self.usage,
            model=self.model,
            reasoning="".join(self.reasoning) or None,
        )
