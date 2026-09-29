"""命令行入口：server-agent <子命令>。

第 01 章：version / config / serve
第 02 章：chat（纯对话，还没有工具）
第 03 章起陆续增加：tools / ask ...
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


DEFAULT_SYSTEM = "你是一名资深 Linux 运维工程师，回答简洁、给出可执行的命令。"


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
    cp.add_argument("--system", default=DEFAULT_SYSTEM, help="系统提示词")
    cp.add_argument("-v", "--verbose", action="store_true", help="打印 token 用量与历史长度")
    cp.set_defaults(func=_cmd_chat)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
