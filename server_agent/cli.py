"""命令行入口：server-agent <子命令>。

第 01 章：version / config / serve
第 02 章：chat（纯对话，还没有工具）
第 03 章：tools list / tools call（手动调用工具）
第 04 章：ask（Agent 自主多步排查）
"""

from __future__ import annotations

import argparse
import json
import sys

from server_agent import __version__
from server_agent.config import get_settings


def _cmd_version(_: argparse.Namespace) -> int:
    print(f"server-agent {__version__}")
    return 0


def _cmd_config(_: argparse.Namespace) -> int:
    print(json.dumps(get_settings().public_dict(), ensure_ascii=False, indent=2))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from server_agent.server.app import create_app

    s = get_settings()
    host = args.host or s.host
    port = args.port or s.port
    if host not in ("127.0.0.1", "localhost", "::1") and not s.api_token:
        print("[warn] 正在监听非本机地址且未设置 SA_API_TOKEN，任何人都能访问本服务", file=sys.stderr)
    uvicorn.run(create_app(s), host=host, port=port, log_level=s.log_level)
    return 0


def _cmd_chat(args: argparse.Namespace) -> int:
    import asyncio

    return asyncio.run(_chat_loop(args))


async def _chat_loop(args: argparse.Namespace) -> int:
    from server_agent.llm import LLMError, Message, Usage, create_llm

    try:
        llm = create_llm(mock=args.mock)
    except LLMError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    history: list[Message] = [Message.system(args.system)]
    total = Usage()

    async def one_turn(question: str) -> None:
        nonlocal total
        history.append(Message.user(question))
        resp = None
        thinking = False
        async for ev in llm.stream(history):
            if ev.type == "reasoning":
                if not thinking:
                    print("[思考] ", end="", file=sys.stderr)
                    thinking = True
                print(f"\033[2m{ev.text}\033[0m", end="", flush=True, file=sys.stderr)  # 灰色，走 stderr
            elif ev.type == "text":
                if thinking:
                    print("\n", end="", file=sys.stderr)
                    thinking = False
                print(ev.text, end="", flush=True)
            else:
                resp = ev.response
        print()
        assert resp is not None
        history.append(resp.message)  # 把回答放回历史，下一轮模型才「记得」
        total = total + resp.usage
        if args.verbose:
            u = resp.usage
            print(f"  [tokens] 本轮 输入 {u.prompt_tokens} / 输出 {u.completion_tokens}；"
                  f"累计 {total.total_tokens}；历史 {len(history)} 条；finish={resp.finish_reason}", file=sys.stderr)

    try:
        if args.once:
            await one_turn(args.once)
            return 0
        print("进入对话。/reset 清空历史，/exit 退出。", file=sys.stderr)
        while True:
            try:
                q = input("\n你> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not q:
                continue
            if q in ("/exit", "/quit"):
                return 0
            if q == "/reset":
                del history[1:]
                print("[历史已清空]", file=sys.stderr)
                continue
            print("AI> ", end="", flush=True)
            await one_turn(q)
    except LLMError as e:
        print(f"\n[error] {e}", file=sys.stderr)
        return 1
    finally:
        if hasattr(llm, "aclose"):
            await llm.aclose()


def _cmd_tools_list(args: argparse.Namespace) -> int:
    from server_agent.tools import registry

    if args.schema:
        print(json.dumps(registry.schemas(), ensure_ascii=False, indent=2))
        return 0
    for t in registry.list():
        first = t.description.splitlines()[0]
        params = ", ".join(t.params.model_fields) or "-"
        print(f"{t.name:<18} [{t.risk}] ({params})\n    {first}")
    print(f"\n共 {len(registry)} 个工具。用 --schema 查看模型实际看到的 JSON Schema。", file=sys.stderr)
    return 0


def _cmd_tools_call(args: argparse.Namespace) -> int:
    """手动调用工具。

    注意：这里**必须**和 Agent 走同一条策略通道。第 09 章开发时踩过一个真实漏洞：
    `tools call run_command '{"command":"rm -rf /"}'` 直接执行了危险命令，
    因为策略只挂在 Agent 循环上，这个入口绕过了它。
    结论：安全校验要挂在**所有**能触发副作用的入口上，而不是「主要入口」上。
    """
    import asyncio

    from server_agent.policy import Policy, get_audit
    from server_agent.tools import registry

    settings = get_settings()
    audit = get_audit() if settings.audit_enabled else None
    policy = Policy(allowed_paths=tuple(settings.policy_allow_paths),
                    allowed_services=tuple(settings.policy_allow_services),
                    require_approval=settings.policy_require_approval)

    if args.no_approval:
        async def approver(tool, call_args, dry_run=None, reason=""):
            print(f"[审批] {tool} 需要批准，但指定了 --no-approval，按拒绝处理", file=sys.stderr)
            return False
    else:
        async def approver(tool, call_args, dry_run=None, reason=""):
            print(f"\n[审批] 请求执行 {tool}", file=sys.stderr)
            print(f"  参数：{json.dumps(call_args, ensure_ascii=False)}", file=sys.stderr)
            if dry_run is not None:
                print(f"  预演：{json.dumps(dry_run, ensure_ascii=False)[:600]}", file=sys.stderr)
            try:
                answer = input("  批准执行？[y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return False
            return answer in ("y", "yes")

    r = asyncio.run(registry.call(args.name, args.args, policy=policy, approver=approver,
                                 audit=audit, run_id="cli_tools_call"))
    try:
        print(json.dumps(json.loads(r.content), ensure_ascii=False, indent=2))
    except json.JSONDecodeError:
        print(r.content)
    flag = "ok" if r.ok else "error"
    extra = "，结果已截断" if r.truncated else ""
    print(f"[{flag}] {r.name} {r.elapsed_ms}ms，回喂模型 {len(r.content)} 字符{extra}", file=sys.stderr)
    return 0 if r.ok else 1


def _cmd_ask(args: argparse.Namespace) -> int:
    import asyncio

    return asyncio.run(_ask(args))


async def _ask(args: argparse.Namespace) -> int:
    from server_agent.agent import Agent
    from server_agent.llm import LLMError, create_llm

    try:
        llm = create_llm(mock=args.mock)
    except LLMError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    run_id_holder = {"id": ""}

    # 第 09 章：策略与审批。CLI 的审批人就是终端前的你（逐条确认）。
    from server_agent.policy import Policy, get_audit

    audit = get_audit() if get_settings().audit_enabled else None
    policy = Policy(allowed_paths=tuple(get_settings().policy_allow_paths),
                    allowed_services=tuple(get_settings().policy_allow_services),
                    require_approval=get_settings().policy_require_approval)
    approver = None
    if args.yes:
        async def approver(tool, call_args, dry_run=None, reason=""):
            print(f"\n[审批] {tool} {call_args} —— --yes 已自动批准", file=sys.stderr)
            return True
    elif not args.no_approval:
        async def approver(tool, call_args, dry_run=None, reason=""):
            print(f"\n[审批] 请求执行 {tool}", file=sys.stderr)
            print(f"  参数：{json.dumps(call_args, ensure_ascii=False)}", file=sys.stderr)
            if dry_run is not None:
                print(f"  预演：{json.dumps(dry_run, ensure_ascii=False)[:600]}", file=sys.stderr)
            print(f"  原因：{reason}", file=sys.stderr)
            try:
                answer = input("  批准执行？[y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n  （无法读取输入，按拒绝处理）", file=sys.stderr)
                return False
            return answer in ("y", "yes")
    else:
        async def approver(tool, call_args, dry_run=None, reason=""):
            print(f"\n[审批] {tool} 需要人工批准，但已指定 --no-approval，按拒绝处理", file=sys.stderr)
            return False

    agent = Agent(llm, system_prompt=args.system, prompt_variant=args.variant,
                  max_steps=args.max_steps, timeout=args.timeout, stream=not args.no_stream,
                  report=not args.no_report, approver=approver, policy=policy, audit=audit)

    # 第 08 章：记忆。开跑前先把历史结论注入系统提示词，跑完把本次过程落库。
    store = None
    recorder = None
    if get_settings().memory_enabled:
        from server_agent.memory import Recorder, build_memory_context, get_store

        store = get_store()
        memory_note = build_memory_context(store, host=args.host, limit=3)
        if memory_note and not args.no_memory:
            agent.system_prompt = agent.system_prompt + "\n\n" + memory_note
            if not args.quiet:
                print(f"[记忆] 已注入历史记忆（{len(memory_note)} 字符），可用 --no-memory 关闭",
                      file=sys.stderr)
        recorder = Recorder(store, pending_run_id="", user_input=args.question)
    events: list[dict] = []
    rc = 0
    try:
        async for ev in agent.run(args.question):
            if recorder:
                if ev.type == "start":
                    # 真实 run_id 由 Agent 生成，这里绑定给记录器，保证库里 id 与事件流一致
                    recorder.bind(ev.run_id)
                else:
                    recorder.event(ev)
            if args.json:
                events.append(ev.to_dict())
                continue
            _render(ev, verbose=args.verbose, quiet=args.quiet)
            if ev.type == "end" and ev.data.get("stopped") in ("error", "timeout"):
                rc = 1
        if recorder and agent.last_result:
            recorder.finish(agent.last_result, status="done" if rc == 0 else "error")
    except LLMError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[已中断]", file=sys.stderr)
        return 130
    finally:
        if hasattr(llm, "aclose"):
            await llm.aclose()

    if args.json:
        print(json.dumps(events, ensure_ascii=False, indent=2))
    return rc


def _snippet(text: str, limit: int = 160) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[:limit] + "…"


def _report_text(r: dict) -> str:
    """把结构化报告渲染成人类可读的几行。"""
    lines = [f"[{r['severity']}] {r['summary']}", f"置信度：{r['confidence']}"]
    if r.get("findings"):
        lines.append("观察与依据：")
        lines += [f"  - {f['claim']}（{f['evidence']}）" for f in r["findings"]]
    if r.get("root_cause"):
        lines.append(f"根因：{r['root_cause']}")
    if r.get("actions"):
        lines.append("建议动作：")
        for a in r["actions"]:
            cmd = f"  命令：{a['command']}" if a.get("command") else ""
            lines.append(f"  - [{a['risk']}] {a['description']}{cmd}")
    if r.get("data_gaps"):
        lines.append("还缺信息：" + "；".join(r["data_gaps"]))
    return "\n".join(lines)


def _render(ev, *, verbose: bool, quiet: bool) -> None:
    """把事件渲染到终端。stderr 放过程（思考、工具），stdout 放最终答案——便于 | jq 之类管道使用。"""
    d = ev.data
    if ev.type == "step":
        _render.suppress = False   # 每个新步骤重置「正在输出 JSON」标记
        if not quiet:
            print(f"\n── 第 {d['step']} 步 ──", file=sys.stderr)
    elif ev.type == "reasoning":
        if verbose:
            print(f"  [思考] {_snippet(d['text'], 200)}", file=sys.stderr)
    elif ev.type == "text":
        # 结构化报告是 JSON，逐字流到终端只会是一堆噪音：识别出来就只给一句提示。
        text = d["text"]
        if not getattr(_render, "suppress", False) and text.lstrip().startswith("{"):
            _render.suppress = True
            if not quiet:
                print("\n  [正在生成结构化报告…]", file=sys.stderr)
        if not quiet and not verbose and not getattr(_render, "suppress", False):
            print(text, end="", flush=True, file=sys.stderr)
    elif ev.type == "tool_call":
        if not quiet:
            print(f"\n  [调用] {d['name']} {_snippet(d['arguments'], 120)}", file=sys.stderr)
    elif ev.type == "tool_result":
        if not quiet:
            mark = "ok" if d["ok"] else "失败"
            extra = f"（{d['skipped']}）" if d.get("skipped") else ""
            ms = f" {d['elapsed_ms']}ms" if d.get("elapsed_ms") is not None else ""
            cut = "，已截断" if d.get("truncated") else ""
            print(f"  [{mark}] {d['name']}{extra}{ms}，{d['chars']} 字符{cut}", file=sys.stderr)
            if verbose:
                print("    " + _snippet(d["content"], 400), file=sys.stderr)
    elif ev.type == "report":
        if not d["parsed"]:
            print(f"\n  [报告] 未能解析为结构化报告：{d.get('error')}", file=sys.stderr)
        elif not quiet:
            r = d["report"]
            print(f"\n  [报告] {r['severity']} / 置信度 {r['confidence']}", file=sys.stderr)
            if r.get("root_cause"):
                print(f"    根因：{r['root_cause']}", file=sys.stderr)
            for a in r.get("actions", []):
                cmd = f"  → {a['command']}" if a.get("command") else ""
                print(f"    建议（{a['risk']}）：{a['description']}{cmd}", file=sys.stderr)
    elif ev.type == "error":
        print(f"\n  [错误] {d['message']}", file=sys.stderr)
    elif ev.type == "end":
        # stdout 是「交付物」：有结构化报告就输出可读报告，否则输出原始文本
        report = d.get("report")
        if report:
            print("\n" + _report_text(report))
        elif d.get("text"):
            print("\n" + d["text"])
        else:
            print(f"\n[未得出结论（{d['stopped']}）]", file=sys.stderr)
        u = d.get("usage") or {}
        print(f"[{d['stopped']}] {d['steps']} 步，{d['tool_calls']} 次工具调用，"
              f"{d['elapsed_ms']}ms，token 输入 {u.get('prompt_tokens', 0)} / 输出 {u.get('completion_tokens', 0)}",
              file=sys.stderr)


def _cmd_history(args: argparse.Namespace) -> int:
    from datetime import datetime

    from server_agent.memory import get_store

    store = get_store()
    if args.show:
        run = store.get_run(args.show)
        if not run:
            print(f"[error] 没有这条记录: {args.show}", file=sys.stderr)
            return 1
        print(json.dumps({k: v for k, v in run.items() if k != "report"}, ensure_ascii=False, indent=2))
        if run.get("report"):
            print(json.dumps(run["report"], ensure_ascii=False, indent=2))
        if args.events:
            for ev in store.get_events(args.show):
                print(f"[{ev['seq']:>3}] {ev['type']}: {json.dumps(ev['data'], ensure_ascii=False)[:160]}")
        return 0
    runs = store.list_runs(limit=args.limit)
    if not runs:
        print("还没有历史记录。跑一次 server-agent ask 之后再来看看。")
        return 0
    for r in runs:
        when = datetime.fromtimestamp(r["started_at"]).strftime("%m-%d %H:%M")
        report = r.get("report") or {}
        summary = report.get("summary") or (r.get("text") or "")[:60]
        print(f"{when}  {r['id']}  [{r['status']}]  {r['input'][:40]}")
        if summary:
            print(f"          → {summary}（置信度 {report.get('confidence', '-')}）")
    print(f"\n共 {len(runs)} 条。用 --show <run_id> 看详情，加 --events 看完整事件流。", file=sys.stderr)
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    from datetime import datetime

    from server_agent.policy import get_audit

    audit = get_audit()
    # 每次命令行调用都是新进程，内存缓冲必然是空的，所以默认从磁盘读
    records = audit.records(limit=args.limit) if args.memory else audit.read_file(limit=args.limit)
    if not records:
        print("还没有审计记录。（审计日志在 " + str(audit.path or "内存") + "）")
        return 0
    for r in records:
        when = datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M:%S")
        extra = []
        if r.get("decision"):
            extra.append(r["decision"])
        if r.get("approved") is not None:
            extra.append("approved" if r["approved"] else "denied")
        if r.get("ok") is not None:
            extra.append("ok" if r["ok"] else "failed")
        tail = f" | {' '.join(extra)}" if extra else ""
        print(f"{when}  {r['event']:<11} {r.get('tool', ''):<16}{tail}")
        if args.verbose and r.get("detail"):
            print(f"    {r['detail'][:160]}")
    print(f"\n共 {len(records)} 条。加 --verbose 看 detail，--file 从磁盘重新读。", file=sys.stderr)
    return 0


CHAT_SYSTEM = "你是一名资深 Linux 运维工程师，回答简洁、给出可执行的命令。"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="server-agent", description="服务器排障 Agent")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="打印版本").set_defaults(func=_cmd_version)
    sub.add_parser("config", help="打印当前生效配置（敏感字段打码）").set_defaults(func=_cmd_config)

    sp = sub.add_parser("serve", help="启动 HTTP 服务")
    sp.add_argument("--host", default=None, help="覆盖 SA_HOST")
    sp.add_argument("--port", type=int, default=None, help="覆盖 SA_PORT")
    sp.set_defaults(func=_cmd_serve)

    cp = sub.add_parser("chat", help="与模型流式对话（第 02 章，尚无工具）")
    cp.add_argument("--mock", action="store_true", help="使用 MockLLM（echo），不调用真实模型")
    cp.add_argument("--once", metavar="问题", help="只问一句就退出")
    cp.add_argument("--system", default=CHAT_SYSTEM, help="系统提示词")
    cp.add_argument("-v", "--verbose", action="store_true", help="打印 token 用量与历史长度")
    cp.set_defaults(func=_cmd_chat)

    tp = sub.add_parser("tools", help="查看与手动调用工具（第 03 章）")
    tsub = tp.add_subparsers(dest="tools_cmd", required=True)
    tl = tsub.add_parser("list", help="列出全部工具")
    tl.add_argument("--schema", action="store_true", help="输出发给模型的 JSON Schema")
    tl.set_defaults(func=_cmd_tools_list)
    tc = tsub.add_parser("call", help="手动调用一个工具")
    tc.add_argument("name", help="工具名")
    tc.add_argument("args", nargs="?", default="{}", help='JSON 参数，如 \'{"path": "/"}\'')
    tc.add_argument("--no-approval", action="store_true", help="需要审批的操作直接拒绝（非交互场景用）")
    tc.set_defaults(func=_cmd_tools_call)

    ap = sub.add_parser("ask", help="让 Agent 自主排查一个问题（第 04 章）")
    ap.add_argument("question", help="用自然语言描述问题")
    ap.add_argument("--mock", action="store_true", help="使用 MockLLM，不调用真实模型")
    ap.add_argument("--no-stream", action="store_true", help="不用流式，等模型一次性返回")
    ap.add_argument("--max-steps", type=int, default=None, help="覆盖 SA_AGENT_MAX_STEPS")
    ap.add_argument("--timeout", type=float, default=None, help="整次运行超时（秒）")
    ap.add_argument("--system", default=None, help="覆盖系统提示词（默认由 prompts/ 模板渲染）")
    ap.add_argument("--variant", default=None, help="提示词变体：sre（默认）或 plain（对照）")
    ap.add_argument("--no-report", action="store_true", help="不做结构化报告解析")
    ap.add_argument("--host", default=None, help="关联的主机名（用于读取/写入长期记忆）")
    ap.add_argument("--no-memory", action="store_true", help="跳过历史记忆注入")
    ap.add_argument("--yes", action="store_true", help="高危操作自动批准（仅用于演示/测试）")
    ap.add_argument("--no-approval", action="store_true", help="所有需要审批的操作直接拒绝（默认行为是交互式询问）")
    ap.add_argument("--json", action="store_true", help="输出完整事件流 JSON（供脚本/前端使用）")
    ap.add_argument("-q", "--quiet", action="store_true", help="只输出最终答案")
    ap.add_argument("-v", "--verbose", action="store_true", help="显示思考内容与工具结果片段")
    ap.set_defaults(func=_cmd_ask)

    hp = sub.add_parser("history", help="查看历史排查记录（第 08 章，来自 SQLite）")
    hp.add_argument("--limit", type=int, default=10, help="列出多少条")
    hp.add_argument("--show", metavar="RUN_ID", help="查看某次运行的详情")
    hp.add_argument("--events", action="store_true", help="配合 --show：打印完整事件流")
    hp.set_defaults(func=_cmd_history)

    au = sub.add_parser("audit", help="查看审计日志（第 09 章：谁在什么时候调了什么）")
    au.add_argument("--limit", type=int, default=30, help="显示多少条")
    au.add_argument("--memory", action="store_true", help="只看内存缓冲（默认从磁盘 JSONL 读取）")
    au.add_argument("-v", "--verbose", action="store_true", help="显示 detail 字段")
    au.set_defaults(func=_cmd_audit)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
