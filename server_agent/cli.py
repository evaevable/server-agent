"""命令行入口：server-agent <子命令>。

第 01 章：version / config / serve
第 02 章起陆续增加：chat / tools / ask ...
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="server-agent", description="服务器排障 Agent")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="打印版本").set_defaults(func=_cmd_version)
    sub.add_parser("config", help="打印当前生效配置（敏感字段打码）").set_defaults(func=_cmd_config)

    sp = sub.add_parser("serve", help="启动 HTTP 服务")
    sp.add_argument("--host", default=None, help="覆盖 SA_HOST")
    sp.add_argument("--port", type=int, default=None, help="覆盖 SA_PORT")
    sp.set_defaults(func=_cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
