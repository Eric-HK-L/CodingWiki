"""命令行入口。

只做三件事：解析参数、调用对应实现、把异常翻译成提示和退出码。
业务逻辑不写在这里，避免命令越来越多时失控。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from codingwiki import __version__
from codingwiki.config import load_config
from codingwiki.errors import CodingWikiError, ExitCode
from codingwiki.logging_setup import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codingwiki",
        description="CodingWiki：代码仓库百科（Repo Wiki + RAG）",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("version", help="显示版本").set_defaults(handler=_cmd_version)

    config_parser = subcommands.add_parser("config", help="配置相关命令")
    config_subcommands = config_parser.add_subparsers(dest="config_command", required=True)
    validate_parser = config_subcommands.add_parser("validate", help="校验配置文件")
    validate_parser.add_argument("--file", required=True, help="YAML 配置文件路径")
    validate_parser.set_defaults(handler=_cmd_config_validate)

    return parser


def _cmd_version(_args: argparse.Namespace) -> int:
    print(f"codingwiki {__version__}")
    return int(ExitCode.OK)


def _cmd_config_validate(args: argparse.Namespace) -> int:
    config = load_config(args.file)
    gauss_state = "启用" if config.model.gauss.enabled else "未启用"
    print(
        "配置有效: "
        f"id={config.id}, "
        f"config_version={config.config_version}, "
        f"backend={config.model.backend.value}, "
        f"gauss={gauss_state}"
    )
    return int(ExitCode.OK)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging()

    handler: Callable[[argparse.Namespace], int] | None = getattr(args, "handler", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return int(ExitCode.INPUT)

    try:
        return handler(args)
    except CodingWikiError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return int(exc.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
