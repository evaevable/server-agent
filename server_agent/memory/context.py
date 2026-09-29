"""上下文预算与压缩。

一个 Agent 的上下文窗口是「预算」，不是「免费的抽屉」。占用它的一共三样东西：
1. 系统提示词（固定成本，约 3-4k 字符）；
2. 工具结果（大头：一次端口列表、一次 tail 就可能几千字符）；
3. 对话历史（每一步的模型回复 + 工具结果，全程累积）。

策略分三层，从便宜到贵：
- L1 工具结果治理：头尾保留（日志的关键信息往往在开头和结尾），中间省略；
- L2 历史压缩：把老的 tool 消息替换成一行摘要（保留 "哪个工具、返回多少字符、开头是什么"）；
- L3 兜底：仍然超预算就整段丢弃最老的 tool 消息，只留「已省略」标记。

为什么不用分词器精确计数？引入 tiktoken 之类会带来模型相关的依赖，而预算控制只需要「量级正确」。
这里用字符启发式估算：CJK 约 1 字符 1 token，其它约 4 字符 1 token。
"""

from __future__ import annotations

from dataclasses import dataclass

from server_agent.llm.base import Message

CJK_RANGES = ((0x3000, 0x30FF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0xFF00, 0xFFEF))


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数。宁可高估，也不要低估（低估会让请求直接被服务端拒绝）。"""
    if not text:
        return 0
    cjk = 0
    for ch in text:
        o = ord(ch)
        if any(lo <= o <= hi for lo, hi in CJK_RANGES):
            cjk += 1
    others = len(text) - cjk
    return cjk + (others + 3) // 4      # 向上取整：宁可高估


def messages_tokens(messages: list[Message]) -> int:
    """估算一组消息的 token 数（含每条消息的固定开销）。"""
    total = 0
    for m in messages:
        total += 4  # role / tool_call_id 等结构开销
        total += estimate_tokens(m.content or "")
        for tc in m.tool_calls or []:
            total += estimate_tokens(tc.name) + estimate_tokens(tc.arguments) + 4
    return total


def shrink_text(text: str, *, head: int = 1200, tail: int = 800) -> str:
    """头尾保留、中间省略。日志类内容的有效信息通常在首尾（错误摘要 + 最近的记录）。"""
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return (f"{text[:head]}\n...[中间省略 {omitted} 字符]...\n{text[-tail:]}")


def summarize_tool_message(m: Message) -> Message:
    """把一条 tool 消息压成一行摘要（保留工具痕迹，丢掉正文）。"""
    body = " ".join((m.content or "").split())
    preview = body[:120]
    return Message("tool", f"[已压缩的工具结果] {len(m.content or '')} 字符，开头：{preview}",
                   tool_call_id=m.tool_call_id)


@dataclass
class ContextBudget:
    """预算配置。available 是留给输入的 token 数。"""

    max_tokens: int = 32000          # 模型上下文窗口（按你用的模型改）
    reserve_output: int = 2000       # 留给模型输出的部分
    keep_recent_tool_msgs: int = 4   # 最近 N 条工具结果保持原样

    @property
    def available(self) -> int:
        return max(1000, self.max_tokens - self.reserve_output)


def fit_messages(messages: list[Message], budget: ContextBudget) -> tuple[list[Message], dict]:
    """把消息列表压进预算。返回 (新消息列表, 统计信息)。

    规则（按顺序执行，直到满足预算）：
    1. 先对「较老的 tool 消息」做头尾保留；
    2. 再把更老的 tool 消息换成一行摘要；
    3. 仍不够就把最老的 tool 消息整条丢弃（保留占位）。
    system 消息与 user 消息永不删除（前者是规则，后者是任务本身）。
    """
    stats = {"before_tokens": messages_tokens(messages), "stage": "none", "changed": 0}
    if stats["before_tokens"] <= budget.available:
        return list(messages), stats

    result = list(messages)
    tool_idx = [i for i, m in enumerate(result) if m.role == "tool"]
    protected = set(tool_idx[-budget.keep_recent_tool_msgs:])

    def shrink_msg(m: Message) -> Message:
        return Message("tool", shrink_text(m.content or ""), tool_call_id=m.tool_call_id)

    def drop_msg(m: Message) -> Message:
        return Message("tool", "[更早的工具结果已省略]", tool_call_id=m.tool_call_id)

    def apply(fn, indexes: list[int]) -> None:
        for i in indexes:
            new = fn(result[i])
            if new.content != result[i].content:
                result[i] = new
                stats["changed"] += 1

    # 三级策略，从便宜到激进；每级之后都检查是否已进预算
    stages = [("tail_head", shrink_msg), ("summarize", summarize_tool_message), ("drop", drop_msg)]
    unprotected = [i for i in tool_idx if i not in protected]
    for name, fn in stages:
        apply(fn, unprotected)
        stats["stage"] = name
        if messages_tokens(result) <= budget.available:
            stats["after_tokens"] = messages_tokens(result)
            return result, stats

    # 兜底：连「最近几条」也压——说明单个工具结果本身就超预算了。
    # 宁可牺牲一点新鲜度，也不能让请求超上下文（超了服务端会直接拒绝）。
    for name, fn in stages[1:]:
        apply(fn, tool_idx)
        stats["stage"] = name + "+protected"
        if messages_tokens(result) <= budget.available:
            break
    stats["after_tokens"] = messages_tokens(result)
    return result, stats
