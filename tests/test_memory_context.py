"""上下文预算与压缩测试。"""

from server_agent.llm import Message
from server_agent.memory.context import (
    ContextBudget,
    estimate_tokens,
    fit_messages,
    messages_tokens,
    shrink_text,
)


def test_estimate_tokens_cjk_vs_ascii():
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好世界") == 4           # CJK 一字符约一 token
    assert estimate_tokens("中文abc") == 3            # 2 个 CJK + 3 个 ASCII（向上取整为 1）
    assert estimate_tokens("a" * 40) == 10            # 非 CJK 约 4 字符一 token


def test_messages_tokens_counts_tool_calls():
    from server_agent.llm import ToolCall

    plain = [Message.user("hi")]
    with_call = [Message.assistant(None, [ToolCall("c", "disk_usage", '{"path": "/"}')])]
    assert messages_tokens(with_call) > messages_tokens(plain)


def test_shrink_text_keeps_head_and_tail():
    text = "A" * 3000 + "中间" * 500 + "Z" * 3000
    out = shrink_text(text, head=100, tail=100)
    assert out.startswith("A" * 100) and out.endswith("Z" * 100)
    assert "中间省略" in out and len(out) < len(text)
    assert shrink_text("short") == "short"


def test_fit_messages_noop_when_within_budget():
    msgs = [Message.system("s"), Message.user("u"), Message.tool("c1", "x" * 100)]
    out, stats = fit_messages(msgs, ContextBudget(max_tokens=10000, reserve_output=1000))
    assert out == msgs and stats["stage"] == "none" and stats["changed"] == 0


def test_fit_messages_stage1_shrinks_old_tool_results():
    big = "行" * 8000
    msgs = [Message.system("s"), Message.user("u")]
    for i in range(6):
        msgs.append(Message.assistant(None, [__import__("server_agent.llm", fromlist=["ToolCall"]).ToolCall(f"c{i}", "t", "{}")]))
        msgs.append(Message.tool(f"c{i}", big))
    budget = ContextBudget(max_tokens=40000, reserve_output=1000, keep_recent_tool_msgs=2)
    out, stats = fit_messages(msgs, budget)
    assert stats["stage"] == "tail_head" and stats["changed"] >= 1
    assert messages_tokens(out) <= budget.available
    assert out[-1].content == big           # 最近两条工具结果保持原样
    assert "中间省略" in out[3].content


def test_fit_messages_escalates_to_summarize_then_drop():
    huge = "字" * 20000
    msgs = [Message.system("s"), Message.user("u"),
            Message.assistant(None, [__import__("server_agent.llm", fromlist=["ToolCall"]).ToolCall("c1", "t", "{}")]),
            Message.tool("c1", huge),
            Message.assistant(None, [__import__("server_agent.llm", fromlist=["ToolCall"]).ToolCall("c2", "t", "{}")]),
            Message.tool("c2", huge)]
    tight = ContextBudget(max_tokens=6000, reserve_output=1000, keep_recent_tool_msgs=1)
    out, stats = fit_messages(msgs, tight)
    assert stats["stage"].startswith(("summarize", "drop"))
    assert len(out) == len(msgs)               # 结构不破坏，只压内容
    assert messages_tokens(out) <= tight.available   # 兜底会连「受保护」的一起压
    assert out[0].role == "system" and out[1].role == "user"   # 永不删系统与用户消息
