"""命令行入口。

只做三件事：解析参数、调用对应实现、把异常翻译成提示和退出码。
业务逻辑不写在这里，避免命令越来越多时失控。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable

from codingwiki import __version__
from codingwiki.config import load_config, load_repo_config
from codingwiki.errors import CodingWikiError, ExitCode
from codingwiki.logging_setup import configure_logging, get_logger
from codingwiki.providers import create_provider
from codingwiki.snapshot import FilesystemSnapshotStore


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

    repo_parser = subcommands.add_parser("repo", help="仓库相关命令")
    repo_subcommands = repo_parser.add_subparsers(dest="repo_command", required=True)
    sync_parser = repo_subcommands.add_parser("sync", help="同步代码并生成快照")
    sync_parser.add_argument(
        "--repo", required=True, help="仓库 ID（匹配 configs/repos/<id>.yaml）"
    )
    sync_parser.add_argument("--revision", default="", help="指定版本；空表示 latest")
    sync_parser.add_argument("--config-dir", default="configs/repos", help="仓库配置目录")
    sync_parser.set_defaults(handler=_cmd_repo_sync)

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


def _cmd_repo_sync(args: argparse.Namespace) -> int:
    logger = get_logger("cli.repo_sync")
    repo_path = os.path.join(args.config_dir, f"{args.repo}.yaml")
    repo = load_repo_config(repo_path)

    provider = create_provider(repo.provider)
    store = FilesystemSnapshotStore()

    revision = args.revision or repo.revision
    if revision == "latest":
        revision = provider.latest_revision(repo)
        logger.info("最新 revision=%s", revision)

    prev = store.latest(repo.repo_id)
    prev_id = prev.snapshot_id if prev else None
    logger.info("准备 materialize %s -> %s", repo.repo_id, revision)
    workdir = provider.materialize(repo, revision)
    logger.info("materialize 完成 workdir=%s", workdir)

    snapshot = store.create(
        repo_id=repo.repo_id,
        revision=revision,
        workdir=workdir,
        provider=repo.provider,
    )
    logger.info(
        "快照创建完成 snapshot_id=%s in_scope=%d excluded=%d",
        snapshot.snapshot_id,
        store.manifest(snapshot.snapshot_id).in_scope_count,
        store.manifest(snapshot.snapshot_id).excluded_count,
    )

    if prev_id and prev_id != snapshot.snapshot_id:
        changes = store.diff_manifests(prev_id, snapshot.snapshot_id)
        logger.info(
            "跨快照变更 added=%d modified=%d deleted=%d renamed=%d",
            len(changes.added),
            len(changes.modified),
            len(changes.deleted),
            len(changes.renamed),
        )
    elif prev_id == snapshot.snapshot_id:
        logger.info("revision 与上次相同，跳过 diff")

    print(
        f"快照就绪: repo={repo.repo_id}, snapshot_id={snapshot.snapshot_id}, "
        f"files={store.manifest(snapshot.snapshot_id).in_scope_count}"
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
