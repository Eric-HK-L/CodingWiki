"""命令行入口。

只做三件事：解析参数、调用对应实现、把异常翻译成提示和退出码。
业务逻辑不写在这里，避免命令越来越多时失控。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from codingwiki import __version__
from codingwiki.config import load_config, load_repo_config
from codingwiki.domain.facts import RepoFacts
from codingwiki.domain.models import RepoConfig, Snapshot
from codingwiki.errors import CodingWikiError, ExitCode, InputError, InternalError
from codingwiki.facts import FilesystemFactExtractor, FilesystemFactStore
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

    facts_parser = subcommands.add_parser("facts", help="代码事实层命令")
    facts_subcommands = facts_parser.add_subparsers(dest="facts_command", required=True)

    extract_parser = facts_subcommands.add_parser("extract", help="从快照提取代码事实")
    extract_parser.add_argument("--repo", required=True, help="仓库 ID")
    extract_parser.add_argument("--revision", default="", help="指定快照版本；空表示最近一次")
    extract_parser.add_argument("--snapshot", default="", help="直接指定 snapshot_id")
    extract_parser.add_argument("--config-dir", default="configs/repos", help="仓库配置目录")
    extract_parser.add_argument("--workdir", default="", help="覆盖工作树路径")
    extract_parser.add_argument("--force", action="store_true", help="忽略单文件解析缓存")
    extract_parser.set_defaults(handler=_cmd_facts_extract)

    query_parser = facts_subcommands.add_parser("query", help="查询已落盘的事实")
    query_parser.add_argument("--repo", required=True, help="仓库 ID")
    query_parser.add_argument("--revision", default="", help="指定版本；空表示最近一次")
    query_parser.add_argument(
        "--kind",
        default="symbol",
        choices=["symbol", "call", "test", "config", "build", "entry", "all"],
        help="事实类别",
    )
    query_parser.add_argument("--file", default="", help="按文件路径前缀过滤")
    query_parser.add_argument("--module", default="", help="按模块精确过滤")
    query_parser.add_argument("--symbol", default="", help="按名称/限定名子串过滤")
    query_parser.add_argument("--limit", type=int, default=50, help="最多输出条数")
    query_parser.set_defaults(handler=_cmd_facts_query)

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
        config=repo,
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


def _cmd_facts_extract(args: argparse.Namespace) -> int:
    logger = get_logger("cli.facts_extract")
    repo_path = os.path.join(args.config_dir, f"{args.repo}.yaml")
    repo = load_repo_config(repo_path)

    snapshots = FilesystemSnapshotStore()
    snapshot = _resolve_snapshot(snapshots, repo.repo_id, args.revision, args.snapshot)
    workdir = _facts_workdir(repo, args.workdir)
    _assert_workdir_revision(workdir, snapshot.revision)

    store = FilesystemFactStore()
    logger.info("开始提取事实 snapshot=%s workdir=%s", snapshot.snapshot_id, workdir)
    facts = FilesystemFactExtractor(store, force=args.force).extract(snapshot, workdir)
    path = store.save(facts)
    quality = facts.quality

    print(
        f"事实就绪: repo={facts.repo_id}, snapshot={facts.snapshot_id}, "
        f"files={len(facts.files)}, symbols={facts.symbol_count}, calls={facts.call_count}, "
        f"build_targets={len(facts.build_targets)}, config={len(facts.config_facts)}, "
        f"tests={len(facts.tests)}"
    )
    print(f"落盘: {path}")
    print(
        "质量: "
        f"parse_success={_fmt_rate(quality.parse_success_rate)}, "
        f"unresolved_calls={_fmt_rate(quality.unresolved_call_ratio)}, "
        f"compdb={_fmt_rate(quality.compdb_coverage)}, "
        f"build_sources={_fmt_rate(quality.build_target_source_coverage)}"
    )
    if quality.parse_failures:
        print(f"解析失败文件 {len(quality.parse_failures)} 个（详见质量报告）")
    return int(ExitCode.OK)


def _cmd_facts_query(args: argparse.Namespace) -> int:
    store = FilesystemFactStore()
    facts = store.load(args.repo, args.revision) if args.revision else store.latest(args.repo)
    if facts is None:
        raise InputError(
            f"没有 {args.repo} 的事实记录，请先执行: codingwiki facts extract --repo {args.repo}"
        )

    rows = _query_rows(
        facts, kind=args.kind, file_prefix=args.file, module=args.module, keyword=args.symbol
    )
    limit = max(args.limit, 0)
    for row in rows[:limit]:
        print(row)
    if not rows:
        print("没有匹配的事实")
    elif len(rows) > limit:
        print(f"... 仅显示前 {limit} 条，共 {len(rows)} 条")
    return int(ExitCode.OK)


def _resolve_snapshot(
    store: FilesystemSnapshotStore, repo_id: str, revision: str, snapshot_id: str
) -> Snapshot:
    if snapshot_id:
        return store.snapshot(snapshot_id)
    if revision:
        try:
            return store.snapshot(f"{repo_id}@{revision}")
        except InternalError as exc:
            raise InputError(f"快照 {repo_id}@{revision} 不存在: {exc}") from exc
    latest = store.latest(repo_id)
    if latest is None:
        raise InputError(
            f"仓库 {repo_id} 还没有快照，请先执行: codingwiki repo sync --repo {repo_id}"
        )
    return latest


def _facts_workdir(repo: RepoConfig, override: str) -> Path:
    if override:
        return Path(override).resolve()
    base = Path(os.environ.get("CODINGWIKI_HOME", ".codingwiki")).resolve()
    workdir = (
        Path(repo.workdir).resolve() if repo.workdir else base / "repos" / repo.repo_id / "work"
    )
    return (workdir / repo.root).resolve() if repo.root else workdir


def _assert_workdir_revision(workdir: Path, revision: str) -> None:
    """workdir 必须是快照对应的版本，否则事实会张冠李戴。"""
    if not (workdir / ".git").exists():
        return
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(workdir),
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    current = result.stdout.decode("utf-8", errors="replace").strip()
    if result.returncode == 0 and current and current != revision:
        raise InputError(
            f"工作树当前版本 {current} 与快照 {revision} 不一致；"
            f"请先执行 codingwiki repo sync --repo <id> --revision {revision}"
        )


def _fmt_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _query_rows(
    facts: RepoFacts, *, kind: str, file_prefix: str, module: str, keyword: str
) -> list[str]:
    rows: list[tuple[str, int, str, str, str]] = []
    module_paths = {f.path for f in facts.files if module and f.module == module}
    for file in facts.files:
        if module and file.module != module:
            continue
        for symbol in file.symbols:
            rows.append(
                (
                    symbol.path,
                    symbol.line,
                    "symbol",
                    symbol.qualified_name,
                    f"{symbol.kind.value} {symbol.signature}",
                )
            )
        for call in file.calls:
            target = call.targets[0] if call.targets else "(未解析)"
            rows.append((call.path, call.line, "call", call.callee, f"{call.kind} -> {target}"))
        for entry in file.entries:
            rows.append((entry.path, entry.line, "entry", entry.name, entry.kind))
    for build in facts.build_targets:
        rows.append((build.path, build.line, "build", build.name, build.kind))
    for config in facts.config_facts:
        rows.append((config.path, config.line, "config", config.key, config.kind))
    for test in facts.tests:
        rows.append((test.path, test.line, "test", test.name, test.framework))

    selected = [
        row
        for row in rows
        if (kind == "all" or row[2] == kind)
        and (not file_prefix or row[0].startswith(file_prefix))
        and (not module or row[0] in module_paths)
        and (not keyword or keyword in row[3] or keyword in row[4])
    ]
    return [
        f"{path}:{line}\t{category}\t{name}\t{detail}"
        for path, line, category, name, detail in selected
    ]


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
