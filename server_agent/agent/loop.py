"""Agent 主循环（ReAct：Reason -> Act -> Observe）。

一轮循环做四件事：
1. 把「系统提示词 + 用户目标 + 全部历史 + 工具清单」发给模型；
2. 模型要么给出最终回答（finish_reason=stop），要么提议调用工具（tool_calls）；
3. 程序执行工具，把结果作为 tool 消息追加到历史；
4. 回到第 1 步，直到模型给出回答或触发停止条件。

设计要点：
- 这里是 async generator：每发生一件事就 yield 一个事件，第 05 章可以直接推给 WebSocket。
- 任何工具失败都变成「观察」回喂给模型，不中断循环（第 03 章 registry.call 从不抛异常）。
- 三个刹车：最大步数、整次运行超时、重复调用检测。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, AsyncIterator, Callable

from server_agent.agent.events import AgentResult, Event
from server_agent.config import get_settings
from server_agent.llm.base import LLMClient, LLMError, Message, ToolCall, Usage
from server_agent.tools.registry import ToolRegistry, registry as default_registry

DEFAULT_SYSTEM = """你是一名资深 Linux 运维工程师，正在通过工具排查一台服务器的问题。

工作方式：
- 先了解环境，再排查具体问题；每一步都基于上一步的观察结果，不要凭空猜测。
- 优先使用工具获取事实，不要凭经验直接下结论。
- 工具返回错误时，读懂错误信息并调整参数重试，不要重复同样的调用。
- 信息足够时给出结论：现象、判断依据（引用具体数据）、建议的下一步操作。
- 你目前只有只读工具，不能修改系统；需要改动时只给出建议，不要声称已经执行。"""

NO_TOOL_NOTE = ("[系统提示] 你上一条回复既没有给出最终答案也没有调用工具。"
                "请直接用文字给出最终结论，或调用合适的工具继续排查。")
REPEAT_NOTE = ("[系统提示] 你刚刚用完全相同的参数调用过 {name}，结果已在上文。"
               "请不要重复调用，改用其它参数或其它工具，或直接给出结论。")
MAX_STEPS_NOTE = "已达到最大步数 {n} 步，停止排查。以下是基于已有信息的结论："


def _norm_args(arguments: str) -> str:
    """把参数归一化，用于识别「完全相同的调用」（键顺序不同不算不同）。"""
    try:
        return json.dumps(json.loads(arguments or "{}"), sort_keys=True, ensure_ascii=False)
    except json.JSONDecodeError:
        return (arguments or "").strip()


class Agent:
    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry | None = None,
        *,
        system_prompt: str = DEFAULT_SYSTEM,
        max_steps: int | None = None,
        timeout: float | None = None,
        stream: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ):
        s = get_settings()
        self.llm = llm
        self.tools = tools or default_registry
        self.system_prompt = system_prompt
        self.max_steps = max_steps if max_steps is not None else s.agent_max_steps
        self.timeout = timeout if timeout is not None else s.agent_timeout
        self.stream = stream
        self.clock = clock
        self.usage = Usage()
        self.last_result: AgentResult | None = None

    # ---------- 单步：问一次模型 ----------
    async def _step(self, messages: list[Message], emit) -> tuple[Message, str | None, str]:
        """返回 (assistant 消息, finish_reason, 本次回答文本)。"""
        opts = {"max_tokens": get_settings().agent_max_tokens}
        if not self.stream:
            resp = await self.llm.chat(messages, tools=self.tools.schemas(), **opts)
            self.usage = self.usage + resp.usage
            if resp.reasoning:
                await emit("reasoning", {"text": resp.reasoning})
            text = resp.message.content or ""
            if text:
                await emit("text", {"text": text})
            return resp.message, resp.finish_reason, text

        parts: list[str] = []
        final = None
        async for ev in self.llm.stream(messages, tools=self.tools.schemas(), **opts):
            if ev.type == "reasoning":
                await emit("reasoning", {"text": ev.text})
            elif ev.type == "text":
                parts.append(ev.text)
                await emit("text", {"text": ev.text})
            else:
                final = ev.response
        if final is None:
            raise LLMError("模型流式响应异常结束：没有收到完整结果")
        self.usage = self.usage + final.usage
        return final.message, final.finish_reason, "".join(parts)

    # ---------- 主循环 ----------
    async def run(self, user_input: str, *, history: list[Message] | None = None) -> AsyncIterator[Event]:
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        started = self.clock()
        buf: list[Event] = []  # 事件缓冲：辅助方法往里写，主循环负责 yield 出去
        seq = 0

        async def emit(type_: str, data: dict) -> None:
            nonlocal seq
            seq += 1
            buf.append(Event(type_, run_id, seq, data))

        async def flush():
            while buf:
                yield buf.pop(0)

        messages: list[Message] = [Message.system(self.system_prompt)]
        if history:
            messages.extend(history)
        messages.append(Message.user(user_input))

        await emit("start", {"input": user_input, "max_steps": self.max_steps,
                             "tools": self.tools.names(), "model": getattr(self.llm, "model", None)})
        async for e in flush():
            yield e

        seen_calls: set[tuple[str, str]] = set()
        final_text, stopped, step = "", "max_steps", 0
        tool_call_count = 0

        step = 0
        try:
            while step < self.max_steps:
                if self.clock() - started > self.timeout:
                    stopped = "timeout"
                    await emit("error", {"message": f"运行超时（>{self.timeout} 秒），已中止"})
                    break
                step += 1
                await emit("step", {"step": step})
                last = step == self.max_steps
                step_messages = messages + ([Message.user(MAX_STEPS_NOTE.format(n=self.max_steps))]
                                            if last else [])
                assistant, finish_reason, text = await self._step(step_messages, emit)
                async for e in flush():
                    yield e
                messages.append(assistant)

                if finish_reason == "tool_calls" and assistant.tool_calls:
                    # 先落文字（模型可能一边说一边调工具）
                    if text and not last:
                        final_text = text
                    calls = assistant.tool_calls
                    for tc in calls:
                        await emit("tool_call", {"id": tc.id, "name": tc.name, "arguments": tc.arguments})
                    async for e in flush():
                        yield e
                    results = await self._run_tools(calls, seen_calls, emit)
                    for tc, result in zip(calls, results):
                        tool_call_count += 1
                        messages.append(Message.tool(tc.id, result))
                    async for e in flush():  # 工具结果事件（含被跳过/坏参数的情况）
                        yield e
                    continue

                if text and not self.stream:
                    await emit("text", {"text": text})  # 非流式模式没有逐字事件，在此补齐
                async for e in flush():
                    yield e
                if finish_reason == "length":
                    stopped = "length"
                    final_text = text + "\n[输出因达到 max_tokens 上限被截断]"
                    break
                if not text.strip():
                    # 既没有回答也没有工具调用：提醒一次，继续
                    messages.append(Message.user(NO_TOOL_NOTE))
                    continue
                final_text = text
                stopped = "final"
                break
        except LLMError as e:
            stopped = "error"
            final_text = final_text or "（模型调用失败）"
            await emit("error", {"message": str(e), "status": getattr(e, "status", None),
                                 "retryable": getattr(e, "retryable", False)})
            async for e in flush():
                yield e
        except asyncio.CancelledError:
            stopped = "cancelled"
            await emit("error", {"message": "运行被取消"})
            async for e in flush():
                yield e
            raise

        result = AgentResult(text=final_text, steps=step, tool_calls=tool_call_count,
                             usage=self.usage, stopped=stopped, messages=messages)
        self.last_result = result  # 供 run_sync 等调用方取用（含完整消息历史）
        await emit("end", {**result.to_dict(), "elapsed_ms": round((self.clock() - started) * 1000, 1)})
        async for e in flush():
            yield e

    async def _run_tools(self, calls: list[ToolCall], seen: set[tuple[str, str]], emit) -> list[str]:
        """执行本轮全部工具调用，返回与 calls 等长的「观察」文本列表。

        多个调用并行执行（asyncio.gather）；重复调用不执行，直接回一句提醒。
        """
        async def one(tc: ToolCall) -> str:
            key = (tc.name, _norm_args(tc.arguments))
            if key in seen:
                note = REPEAT_NOTE.format(name=tc.name)
                await emit("tool_result", {"id": tc.id, "name": tc.name, "ok": False, "content": note,
                                           "chars": len(note), "skipped": "repeat"})
                return note
            seen.add(key)
            try:
                args = tc.parsed_arguments()
            except ValueError as e:
                msg = f"参数不是合法 JSON：{e}。请修正后重新调用 {tc.name}"
                await emit("tool_result", {"id": tc.id, "name": tc.name, "ok": False, "content": msg,
                                           "chars": len(msg), "skipped": "bad_json"})
                return msg
            r = await self.tools.call(tc.name, args)
            await emit("tool_result", {"id": tc.id, "name": tc.name, "ok": r.ok, "content": r.content,
                                       "chars": len(r.content), "truncated": r.truncated,
                                       "elapsed_ms": r.elapsed_ms})
            return r.content

        return list(await asyncio.gather(*(one(c) for c in calls)))

    async def run_sync(self, user_input: str, *, history: list[Message] | None = None) -> AgentResult:
        """不关心中间过程时用这个：跑完直接拿结果。"""
        async for _ in self.run(user_input, history=history):
            pass
        assert self.last_result is not None, "run() 未产出 end 事件"
        return self.last_result
