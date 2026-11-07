"""命令行入口。

只做三件事：解析参数、调用对应实现、把异常翻译成提示和退出码。
业务逻辑不写在这里，避免命令越来越多时失控。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codingwiki.domain.eval import EvalReport, QuestionResult
    from codingwiki.domain.job import JobRecord
    from codingwiki.retrieval import HybridSearcher

from codingwiki import __version__
from codingwiki.config import load_config, load_repo_config
from codingwiki.config.models import AppConfig, EmbeddingSettings
from codingwiki.domain.facts import RepoFacts
from codingwiki.domain.job import StageName, StageStatus
from codingwiki.errors import (
    CodingWikiError,
    ExitCode,
    InputError,
)
from codingwiki.facts import FilesystemFactExtractor, FilesystemFactStore
from codingwiki.logging_setup import configure_logging, get_logger
from codingwiki.pipeline.stages import (
    assert_workdir_revision,
    facts_workdir,
    load_facts_or_raise,
    resolve_snapshot,
    resolve_wiki_backend,
    wiki_workdir,
)
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

    wiki_parser = subcommands.add_parser("wiki", help="Wiki 生成、校验与发布（S4）")
    wiki_subcommands = wiki_parser.add_subparsers(dest="wiki_command", required=True)

    plan_parser = wiki_subcommands.add_parser("plan", help="生成覆盖计划")
    plan_parser.add_argument("--repo", required=True, help="仓库 ID")
    plan_parser.add_argument("--max-tasks", type=int, default=0, help="任务预算；0 表示不限")
    plan_parser.add_argument("--config-dir", default="configs/repos", help="仓库配置目录")
    plan_parser.set_defaults(handler=_cmd_wiki_plan)

    generate_parser = wiki_subcommands.add_parser("generate", help="生成并校验页面（含修复循环）")
    generate_parser.add_argument("--repo", required=True, help="仓库 ID")
    generate_parser.add_argument("--max-tasks", type=int, default=0, help="任务预算；0 表示不限")
    generate_parser.add_argument("--max-rounds", type=int, default=3, help="单页最大修复轮数")
    generate_parser.add_argument("--config-dir", default="configs/repos", help="仓库配置目录")
    generate_parser.add_argument("--workdir", default="", help="覆盖工作树路径")
    generate_parser.add_argument(
        "--backend",
        choices=["mock", "api"],
        default="mock",
        help="模型后端：mock=CI 地板；api=OpenAI 兼容真实模型",
    )
    generate_parser.add_argument(
        "--app-config",
        default="configs/default.yaml",
        help="主配置路径（--backend api 时读取 model.api 段；本机私有配置放 configs/*.local.yaml）",
    )
    generate_parser.set_defaults(handler=_cmd_wiki_generate)

    verify_parser = wiki_subcommands.add_parser("verify", help="复验草稿区全部页面")
    verify_parser.add_argument("--repo", required=True, help="仓库 ID")
    verify_parser.add_argument("--config-dir", default="configs/repos", help="仓库配置目录")
    verify_parser.add_argument("--workdir", default="", help="覆盖工作树路径")
    verify_parser.set_defaults(handler=_cmd_wiki_verify)

    publish_parser = wiki_subcommands.add_parser("publish", help="门禁检查后发布新版本")
    publish_parser.add_argument("--repo", required=True, help="仓库 ID")
    publish_parser.add_argument("--config-dir", default="configs/repos", help="仓库配置目录")
    publish_parser.set_defaults(handler=_cmd_wiki_publish)

    status_parser = wiki_subcommands.add_parser("status", help="Wiki 质量看板摘要")
    status_parser.add_argument("--repo", required=True, help="仓库 ID")
    status_parser.set_defaults(handler=_cmd_wiki_status)

    coverage_parser = wiki_subcommands.add_parser(
        "coverage", help="覆盖率看板：模块 × C0–C3 矩阵 + 评测/任务摘要（S11）"
    )
    coverage_parser.add_argument("--repo", required=True, help="仓库 ID")
    coverage_parser.set_defaults(handler=_cmd_wiki_coverage)

    stale_parser = wiki_subcommands.add_parser(
        "stale", help="按最新快照与当前版本的差异，把受影响页面标 stale"
    )
    stale_parser.add_argument("--repo", required=True, help="仓库 ID")
    stale_parser.set_defaults(handler=_cmd_wiki_stale)

    rollback_parser = wiki_subcommands.add_parser("rollback", help="把 current 指针切回历史版本")
    rollback_parser.add_argument("--repo", required=True, help="仓库 ID")
    rollback_parser.add_argument("--to", dest="to_version", required=True, help="目标版本 ID")
    rollback_parser.set_defaults(handler=_cmd_wiki_rollback)

    index_parser = subcommands.add_parser("index", help="检索索引（S5）")
    index_subcommands = index_parser.add_subparsers(dest="index_command", required=True)
    index_build_parser = index_subcommands.add_parser(
        "build", help="从最新事实 + 当前 Wiki 版本构建索引"
    )
    index_build_parser.add_argument("--repo", required=True, help="仓库 ID")
    index_build_parser.add_argument("--config-dir", default="configs/repos", help="仓库配置目录")
    index_build_parser.add_argument("--workdir", default="", help="覆盖工作树路径")
    index_build_parser.add_argument(
        "--app-config",
        default="",
        help="主配置路径（embedding 参数来源）；为空时用内置默认（local/bge-m3）",
    )
    index_build_parser.add_argument(
        "--embedding",
        choices=["hash", "bge-m3", "local"],
        default="",
        help="覆盖配置里的嵌入后端；hash=离线占位，bge-m3/local=真实语义模型",
    )
    index_build_parser.add_argument("--model-path", default="", help="覆盖嵌入模型目录")
    index_build_parser.add_argument(
        "--device", choices=["auto", "cpu", "mps", "cuda"], default="", help="覆盖推理设备"
    )
    index_build_parser.add_argument("--batch-size", type=int, default=0, help="覆盖批大小")
    index_build_parser.add_argument("--max-length", type=int, default=0, help="覆盖最大序列长度")
    index_build_parser.add_argument("--dim", type=int, default=0, help="覆盖向量维度")
    index_build_parser.set_defaults(handler=_cmd_index_build)

    search_parser = subcommands.add_parser("search", help="混合检索：BM25 + 向量 + RRF（S5）")
    search_parser.add_argument("--repo", required=True, help="仓库 ID")
    search_parser.add_argument("--query", required=True, help="查询文本")
    search_parser.add_argument("--top-k", type=int, default=10, help="返回条数")
    search_parser.add_argument("--app-config", default="", help="主配置路径（模型/重排参数来源）")
    search_parser.add_argument("--no-rerank", action="store_true", help="关闭 cross-encoder 重排")
    search_parser.add_argument("--module", default="", help="按模块精确过滤（S7 SearchFilters）")
    search_parser.add_argument(
        "--file-prefix", default="", help="按文件路径前缀过滤（S7 SearchFilters）"
    )
    search_parser.set_defaults(handler=_cmd_search)

    ask_parser = subcommands.add_parser("ask", help="带来源出处的问答（S5）")
    ask_parser.add_argument("--repo", required=True, help="仓库 ID")
    ask_parser.add_argument("--question", required=True, help="问题文本")
    ask_parser.add_argument("--top-k", type=int, default=8, help="检索资料条数")
    ask_parser.add_argument("--app-config", default="", help="主配置路径（模型/重排参数来源）")
    ask_parser.add_argument("--no-rerank", action="store_true", help="关闭 cross-encoder 重排")
    ask_parser.set_defaults(handler=_cmd_ask)

    eval_parser = subcommands.add_parser(
        "eval", help="金标准集评测：Recall@k / MRR / nDCG / 拒答 / 出处（S9）"
    )
    eval_parser.add_argument("--repo", required=True, help="仓库 ID")
    eval_parser.add_argument("--golden", required=True, help="金标准集 YAML 路径")
    eval_parser.add_argument("--top-k", type=int, default=10, help="检索深度 k")
    eval_parser.add_argument("--app-config", default="", help="主配置路径（模型/重排参数来源）")
    eval_parser.add_argument("--no-rerank", action="store_true", help="关闭 cross-encoder 重排")
    eval_parser.set_defaults(handler=_cmd_eval)

    run_parser = subcommands.add_parser(
        "run", help="日更回流闭环：sync→facts→wiki→verify→publish→index（S10，供 cron 调用）"
    )
    run_parser.add_argument("--repo", required=True, help="仓库 ID，或 all 表示全部仓库")
    run_parser.add_argument("--revision", default="", help="覆盖仓库配置的 revision")
    run_parser.add_argument("--config-dir", default="configs/repos", help="仓库配置目录")
    run_parser.add_argument(
        "--backend",
        choices=["mock", "api"],
        default="mock",
        help="wiki 生成后端：mock=CI 地板；api=OpenAI 兼容真实模型",
    )
    run_parser.add_argument(
        "--app-config", default="configs/default.yaml", help="主配置路径（wiki/index 的模型来源）"
    )
    run_parser.add_argument(
        "--from",
        dest="from_stage",
        choices=[s.value for s in StageName],
        default="",
        help="从指定阶段开始（更早的阶段不执行不记录）",
    )
    run_parser.add_argument(
        "--skip",
        choices=[s.value for s in StageName],
        action="append",
        default=[],
        help="跳过指定阶段（可重复）",
    )
    run_parser.set_defaults(handler=_cmd_run)

    spec_parser = subcommands.add_parser("spec", help="开发 Spec 生成（S12 起）")
    spec_subcommands = spec_parser.add_subparsers(dest="spec_command", required=True)

    spec_extract_parser = spec_subcommands.add_parser(
        "extract", help="需求文档 → 原子需求（模型只做原子化与 AC 草拟）"
    )
    spec_extract_parser.add_argument("--repo", required=True, help="仓库 ID")
    spec_extract_parser.add_argument("--doc", required=True, help="需求文档 Markdown 路径")
    spec_extract_parser.add_argument(
        "--snapshot", default="", help="绑定快照 ID；空表示取该仓库最近一次 facts 快照"
    )
    spec_extract_parser.add_argument(
        "--backend",
        choices=["mock", "api"],
        default="api",
        help="模型后端：api=真实模型；mock=离线链路测试（不用于质量指标）",
    )
    spec_extract_parser.add_argument(
        "--app-config", default="configs/default.yaml", help="主配置路径（--backend api 时读取）"
    )
    spec_extract_parser.set_defaults(handler=_cmd_spec_extract)

    spec_list_parser = spec_subcommands.add_parser("list", help="列出已抽取的需求文档")
    spec_list_parser.add_argument("--repo", required=True, help="仓库 ID")
    spec_list_parser.set_defaults(handler=_cmd_spec_list)

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
    snapshot = resolve_snapshot(snapshots, repo.repo_id, args.revision, args.snapshot)
    workdir = facts_workdir(repo, args.workdir)
    assert_workdir_revision(workdir, snapshot.revision)

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


def _fmt_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


# ---------------------------------------------------------------- wiki 命令


def _cmd_wiki_plan(args: argparse.Namespace) -> int:
    from codingwiki.generate import CoveragePlanner
    from codingwiki.publish import FilesystemPageStore

    facts = load_facts_or_raise(args.repo)
    store = FilesystemPageStore(args.repo)
    plan = CoveragePlanner(max_tasks=args.max_tasks).plan(facts, store.load_deps())

    plans_dir = (
        Path(os.environ.get("CODINGWIKI_HOME", ".codingwiki")).resolve()
        / "wiki"
        / args.repo
        / "plans"
    )
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / f"{plan.created_at.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")

    fresh = sum(1 for t in plan.tasks if not t.reuse)
    reused = len(plan.tasks) - fresh
    print(
        f"覆盖计划: repo={plan.repo_id}, snapshot={plan.snapshot_id}, "
        f"tasks={len(plan.tasks)}（新生成 {fresh}，复用 {reused}），跳过 {len(plan.skipped)}"
    )
    print(f"落盘: {path}")
    return int(ExitCode.OK)


def _cmd_wiki_generate(args: argparse.Namespace) -> int:
    logger = get_logger("cli.wiki_generate")
    from codingwiki.generate import CoveragePlanner, GeneratePipeline, TaskPackageBuilder
    from codingwiki.publish import FilesystemPageStore
    from codingwiki.verify import PageVerifier

    facts = load_facts_or_raise(args.repo)
    workdir = wiki_workdir(args.repo, args.config_dir, args.workdir)
    store = FilesystemPageStore(args.repo)
    backend, backend_name, model_name = resolve_wiki_backend(args.backend, args.app_config)
    plan = CoveragePlanner(max_tasks=args.max_tasks).plan(facts, store.load_deps())

    known_pages = {t.page_id for t in plan.tasks} | set(store.list_drafts())
    pipeline = GeneratePipeline(
        backend=backend,
        builder=TaskPackageBuilder(workdir),
        verifier=PageVerifier(facts, workdir, known_pages=known_pages),
        store=store,
        max_rounds=args.max_rounds,
        backend_name=backend_name,
        model_name=model_name,
    )
    outcomes = pipeline.run_plan(plan, facts)

    ok = sum(1 for o in outcomes if o.ok and not o.reused)
    reused = sum(1 for o in outcomes if o.reused)
    failed = [o for o in outcomes if not o.ok]
    for outcome in failed:
        logger.warning(
            "页面 %s 生成失败（%d 轮）：%s",
            outcome.task.page_id,
            outcome.attempts,
            "; ".join(i.message for i in (outcome.report.issues[:3] if outcome.report else [])),
        )
    print(f"生成完成: 成功 {ok}，复用 {reused}，失败 {len(failed)}")
    if failed:
        print("失败页面: " + ", ".join(o.task.page_id for o in failed[:10]))
        return int(ExitCode.RETRYABLE)
    return int(ExitCode.OK)


def _cmd_wiki_verify(args: argparse.Namespace) -> int:
    from codingwiki.publish import FilesystemPageStore
    from codingwiki.verify import PageVerifier

    facts = load_facts_or_raise(args.repo)
    workdir = wiki_workdir(args.repo, args.config_dir, args.workdir)
    store = FilesystemPageStore(args.repo)
    page_ids = store.list_drafts()
    if not page_ids:
        raise InputError(f"仓库 {args.repo} 草稿区为空，请先执行 wiki generate")

    verifier = PageVerifier(facts, workdir, known_pages=set(page_ids))
    failed: list[str] = []
    for page_id in page_ids:
        loaded = store.load_draft(page_id)
        meta = store.draft_meta(page_id)
        if loaded is None or meta is None:
            failed.append(page_id)
            continue
        report = verifier.verify(loaded[0], loaded[1], meta)
        if report.status != "passed":
            failed.append(page_id)
    print(
        f"复验完成: 共 {len(page_ids)} 页，通过 {len(page_ids) - len(failed)}，失败 {len(failed)}"
    )
    if failed:
        print("失败页面: " + ", ".join(failed[:10]))
        return int(ExitCode.RETRYABLE)
    return int(ExitCode.OK)


def _cmd_wiki_publish(args: argparse.Namespace) -> int:
    from codingwiki.publish import FilesystemPageStore

    facts = load_facts_or_raise(args.repo)
    workdir = wiki_workdir(args.repo, args.config_dir, "")
    store = FilesystemPageStore(args.repo)
    profile: dict[str, object] = {
        "files": len(facts.files),
        "symbols": facts.symbol_count,
        "calls": facts.call_count,
        "modules": len({file.module for file in facts.files}),
        "build_targets": len(facts.build_targets),
        "tests": len(facts.tests),
        "config_facts": len(facts.config_facts),
    }
    index = store.publish(facts.snapshot_id, workdir=workdir, site_profile=profile, facts=facts)
    counts = index.status_counts
    site = store.version_dir(index.version_id) / "site" / "index.html"
    print(
        f"发布成功: version={index.version_id}, pages={len(index.pages)}, "
        f"verified={counts.get('verified', 0)}"
    )
    print(f"站点: {site}（离线打开 index.html，首页 2 次点击可达模块页）")
    print(f"看板: {site.parent / 'dashboard.html'}")
    return int(ExitCode.OK)


def _cmd_wiki_coverage(args: argparse.Namespace) -> int:
    from codingwiki.publish import FilesystemPageStore
    from codingwiki.publish.dashboard import build_current_dashboard

    facts = load_facts_or_raise(args.repo)
    store = FilesystemPageStore(args.repo)
    data = build_current_dashboard(args.repo, facts, store)

    print(f"看板: repo={data.repo_id}, version={data.version_id}, snapshot={data.snapshot_id}")
    print(
        f"页面: {data.pages_total}（verified {data.pages_verified} / stale {data.pages_stale}），"
        f"证据总数 {data.evidence_total}"
    )
    print("覆盖率矩阵（缺口 = 每格未覆盖数）:")
    header = f"  {'模块':<40} {'C0 登记':>10} {'C1 说明':>12} {'C2 接口':>14} {'C3 流程':>10}"
    print(header)
    for row in data.coverage:
        print(
            f"  {row.module:<40} "
            f"{row.files_total:>10} "
            f"{row.files_covered:>6}/{row.files_total:<5} "
            f"{row.public_covered:>7}/{row.public_total:<6} "
            f"{row.entries_covered:>5}/{row.entries_total:<4}"
        )
    if data.eval_summary is not None:
        evaluation = data.eval_summary
        print(
            f"评测: set={evaluation.set_id}, "
            f"Recall@k={_fmt_rate(evaluation.recall_at_k)}, "
            f"拒答准确率={_fmt_rate(evaluation.refusal_accuracy)}, "
            f"出处有效率={_fmt_rate(evaluation.citation_valid_rate)}"
        )
    else:
        print("评测: 未评测（codingwiki eval 后重新发布可见）")
    if data.job_summary is not None:
        print(
            f"最近任务: {data.job_summary.job_id}（{data.job_summary.status}）"
            + (f"，错误: {data.job_summary.error}" if data.job_summary.error else "")
        )
    else:
        print("最近任务: 未运行（codingwiki run 后重新发布可见）")
    return int(ExitCode.OK)


def _cmd_wiki_stale(args: argparse.Namespace) -> int:
    from codingwiki.publish import FilesystemPageStore
    from codingwiki.snapshot import FilesystemSnapshotStore

    store = FilesystemPageStore(args.repo)
    current = store.current_version()
    if current is None:
        raise InputError(f"仓库 {args.repo} 还没有发布版本，请先执行 wiki publish")
    index = store.version_index(current)
    if index is None:
        raise InputError(f"当前版本 {current} 的 index.json 不存在")

    snapshots = FilesystemSnapshotStore()
    latest = snapshots.latest(args.repo)
    if latest is None:
        raise InputError(f"仓库 {args.repo} 还没有快照，请先执行 repo sync")
    if latest.snapshot_id == index.snapshot_id:
        print(f"快照未变化（{index.snapshot_id}），没有页面需要标 stale")
        return int(ExitCode.OK)

    changes = snapshots.diff_manifests(index.snapshot_id, latest.snapshot_id)
    marked = store.mark_stale(changes)
    print(
        f"stale 标记: {len(marked)} 页（快照 {index.snapshot_id} → {latest.snapshot_id}，"
        f"变更 modified={len(changes.modified)} deleted={len(changes.deleted)} "
        f"renamed={len(changes.renamed)}）"
    )
    if marked:
        print("受影响页面: " + ", ".join(marked[:10]))
    return int(ExitCode.OK)


def _cmd_wiki_rollback(args: argparse.Namespace) -> int:
    from codingwiki.publish import FilesystemPageStore

    store = FilesystemPageStore(args.repo)
    version = store.rollback(args.to_version)
    index = store.version_index(version)
    pages = len(index.pages) if index else 0
    print(f"已回滚: current -> {version}（{pages} 页）")
    return int(ExitCode.OK)


def _cmd_wiki_status(args: argparse.Namespace) -> int:
    from codingwiki.publish import FilesystemPageStore

    store = FilesystemPageStore(args.repo)
    drafts = store.list_drafts()
    current = store.current_version()
    print(f"仓库: {args.repo}")
    print(f"草稿区: {len(drafts)} 页")
    print(f"当前版本: {current or '（未发布）'}")
    versions = store.list_versions()
    print(f"历史版本: {len(versions)} 个")
    if current:
        index = store.version_index(current)
        if index is not None:
            print(f"当前快照: {index.snapshot_id}")
            counts = index.status_counts
            print("页面状态: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
            evidence = sum(p.evidence_count for p in index.pages)
            print(f"证据总数: {evidence}（出处有效率 100%，校验器已逐条核对）")
    return int(ExitCode.OK)


# ---------------------------------------------------------------- S12 需求抽取


def _cmd_spec_extract(args: argparse.Namespace) -> int:
    from codingwiki.domain.spec import RequirementKind
    from codingwiki.spec import extract_requirements
    from codingwiki.spec.ingest import doc_bundle_dir

    snapshot_id = args.snapshot
    if not snapshot_id:
        snapshot_id = load_facts_or_raise(args.repo).snapshot_id

    backend, backend_name, model_name = resolve_wiki_backend(args.backend, args.app_config)
    result = extract_requirements(
        repo_id=args.repo,
        doc_path=args.doc,
        backend=backend,
        snapshot_id=snapshot_id,
        backend_name=backend_name,
        model_name=model_name,
    )
    functional = sum(1 for r in result.requirements if r.kind is RequirementKind.FUNCTIONAL)
    non_functional = len(result.requirements) - functional
    print(
        f"已抽取: doc_id={result.doc_id}, requirements={len(result.requirements)}"
        f"（R={functional}, N={non_functional}）, questions={len(result.questions)}, "
        f"backend={result.backend}/{result.model}"
    )
    print(f"产物: {doc_bundle_dir(args.repo, result.doc_id)}")
    return int(ExitCode.OK)


def _cmd_spec_list(args: argparse.Namespace) -> int:
    from codingwiki.domain.spec import RequirementKind
    from codingwiki.spec import load_extractions

    records = load_extractions(args.repo)
    if not records:
        print(f"仓库 {args.repo} 暂无需求抽取记录；先执行: codingwiki spec extract --repo ...")
        return int(ExitCode.OK)
    for record in records:
        functional = sum(1 for r in record.requirements if r.kind is RequirementKind.FUNCTIONAL)
        non_functional = len(record.requirements) - functional
        print(
            f"{record.doc_id}\tR={functional}\tN={non_functional}\tQ={len(record.questions)}\t"
            f"backend={record.backend}/{record.model}\t{record.source_path}"
        )
    return int(ExitCode.OK)


def _app_config_or_default(path: str) -> AppConfig:
    """读取主配置；未指定路径时用内置默认（embedding=local，重排关闭）。"""
    if path:
        return load_config(path)
    return AppConfig(config_version="1.0", embedding=EmbeddingSettings())


def _embed_progress(total_hint: int = 0) -> Callable[[int, int], None]:
    """把嵌入进度按 5% 粒度打到 stdout，便于长任务观察。"""
    last_bucket = [-1]

    def report(done: int, total: int) -> None:
        effective = total or total_hint
        if effective <= 0:
            return
        bucket = min(20, int(done * 20 / effective))
        if bucket > last_bucket[0] or done >= effective:
            last_bucket[0] = bucket
            print(f"嵌入进度: {done}/{effective} ({done * 100 // effective}%)", flush=True)

    return report


def _cmd_index_build(args: argparse.Namespace) -> int:
    from codingwiki.publish import FilesystemPageStore
    from codingwiki.retrieval import Bm25Index, ChunkBuilder, FilesystemIndexStore
    from codingwiki.retrieval.registry import create_embedder
    from codingwiki.spec import load_doc_chunks

    facts = load_facts_or_raise(args.repo)
    workdir = wiki_workdir(args.repo, args.config_dir, args.workdir)
    wiki_pages = FilesystemPageStore(args.repo).load_published_pages()

    chunks = ChunkBuilder(workdir).build(facts, wiki_pages)
    # 需求文档块与代码块共用索引，通过 source_types=("doc",) 隔离（架构 阶段2 §4）
    doc_chunks = load_doc_chunks(args.repo, facts.snapshot_id)
    chunks += doc_chunks
    if not chunks:
        raise InputError(f"仓库 {args.repo} 没有任何可索引的内容")

    config = _app_config_or_default(args.app_config)
    settings = config.embedding
    backend = args.embedding or settings.backend
    is_hash = backend.strip().lower() in {"hash", "hash-embed"}
    embedder = create_embedder(
        backend,
        model_path=args.model_path or settings.model_path,
        device=args.device or settings.device,
        batch_size=args.batch_size or settings.batch_size,
        max_length=args.max_length or settings.max_length,
        dim=args.dim or (0 if is_hash else settings.dimension),
        progress=_embed_progress(len(chunks)),
    )
    texts = [f"{c.title}\n{c.text}" for c in chunks]
    vocab_size = Bm25Index(texts).vocab_size
    vectors = embedder.embed(texts)

    store = FilesystemIndexStore(args.repo)
    meta = store.save(chunks, vectors, embedder.name, embedder.dim, vocab_size)
    tiers: dict[str, int] = {}
    for chunk in chunks:
        tiers[chunk.trust_tier] = tiers.get(chunk.trust_tier, 0) + 1
    tier_text = ", ".join(f"{k}={v}" for k, v in sorted(tiers.items()))
    print(
        f"索引就绪: index_id={meta.index_id}, snapshot={meta.snapshot_id}, "
        f"chunks={meta.chunk_count}（{tier_text}）, dim={meta.dim}, model={meta.embedding_model}"
    )
    if not wiki_pages:
        print("提示: 尚未发布 Wiki 版本，索引只含代码与事实块；wiki publish 后重建可补齐")
    if doc_chunks:
        print(f"需求文档块: {len(doc_chunks)}（source_type=doc）")
    return int(ExitCode.OK)


def _load_searcher(
    repo_id: str, *, app_config: str = "", no_rerank: bool = False
) -> HybridSearcher:
    from codingwiki.retrieval import FilesystemIndexStore, HybridSearcher
    from codingwiki.retrieval.registry import HASH_PREFIX, create_reranker, embedder_for_name

    config = _app_config_or_default(app_config)
    store = FilesystemIndexStore(repo_id)
    meta, chunks, vectors = store.load()
    embedder = embedder_for_name(
        meta.embedding_model,
        model_path=config.embedding.model_path,
        device=config.embedding.device,
    )
    reranker = None
    if config.reranker.enabled and not no_rerank:
        reranker = create_reranker(
            model_path=config.reranker.model_path,
            device=config.reranker.device,
            batch_size=config.reranker.batch_size,
        )
    # 哈希后端无语义能力，拒答门槛必须走规模自适应公式（传 None）
    threshold = (
        None if meta.embedding_model.startswith(HASH_PREFIX) else config.retrieval.min_vector_score
    )
    return HybridSearcher(
        chunks,
        vectors,
        embedder,
        reranker=reranker,
        rerank_pool=config.retrieval.rerank_pool,
        min_vector_score=threshold,
    )


def _cmd_search(args: argparse.Namespace) -> int:
    from codingwiki.domain.rag import SearchFilters

    searcher = _load_searcher(args.repo, app_config=args.app_config, no_rerank=args.no_rerank)
    filters = SearchFilters(module=args.module, file_prefix=args.file_prefix)
    hits = searcher.search(args.query, top_k=max(args.top_k, 1), filters=filters)
    if not hits:
        print("没有匹配的资料")
        return int(ExitCode.OK)
    for rank, hit in enumerate(hits, start=1):
        chunk = hit.chunk
        print(f"{rank}. [{chunk.trust_tier}] {hit.reference}  score={hit.score:.4f}  {chunk.title}")
    return int(ExitCode.OK)


def _cmd_ask(args: argparse.Namespace) -> int:
    from codingwiki.backends import MockBackend
    from codingwiki.retrieval import AnswerPipeline

    searcher = _load_searcher(args.repo, app_config=args.app_config, no_rerank=args.no_rerank)
    pipeline = AnswerPipeline(backend=MockBackend(), searcher=searcher)
    outcome = pipeline.ask(args.repo, args.question, top_k=max(args.top_k, 1))
    print(outcome.answer)
    if not outcome.refused:
        print(
            f"---\nconfidence={outcome.result.confidence}  "
            f"citations={len(outcome.result.citations)}  attempts={outcome.attempts}"
        )
    return int(ExitCode.OK)


def _cmd_eval(args: argparse.Namespace) -> int:
    from codingwiki.backends import MockBackend
    from codingwiki.eval import EvalHarness, load_golden, load_previous_report, save_report
    from codingwiki.publish import FilesystemPageStore
    from codingwiki.retrieval import AnswerPipeline, FilesystemIndexStore

    golden = load_golden(args.golden)
    if golden.repo_id != args.repo:
        raise InputError(
            f"金标准集 {golden.set_id} 属于仓库 {golden.repo_id}，与 --repo {args.repo} 不一致"
        )

    facts = load_facts_or_raise(args.repo)
    searcher = _load_searcher(args.repo, app_config=args.app_config, no_rerank=args.no_rerank)
    _meta, chunks, _vectors = FilesystemIndexStore(args.repo).load()
    store = FilesystemPageStore(args.repo)
    known_pages: set[str] = set()
    current = store.current_version()
    if current:
        index = store.version_index(current)
        if index:
            known_pages = {p.page_id for p in index.pages}

    pipeline = AnswerPipeline(MockBackend(), searcher)
    harness = EvalHarness(
        searcher,
        chunks,
        pipeline,
        known_pages=known_pages,
        known_paths=set(facts.blob_by_path),
    )
    report = harness.run(golden, top_k=max(args.top_k, 1))
    report = report.model_copy(update={"snapshot_id": facts.snapshot_id})

    previous = load_previous_report(args.repo, golden.set_id)
    path = save_report(report, args.repo)

    def _delta(name: str, now: float | None, before: float | None) -> str:
        if now is None:
            return f"{name}=n/a"
        line = f"{name}={now:.1%}"
        if before is not None:
            diff = now - before
            line += f"（上次 {before:.1%}，{'+' if diff >= 0 else ''}{diff:.1%}）"
        return line

    print(
        f"评测完成: set={report.set_id}, repo={report.repo_id}, model={report.index_model}, "
        f"top_k={report.top_k}, questions={report.question_count}"
    )
    print(
        "  "
        + _delta("Recall@k", report.recall_at_k, previous.recall_at_k if previous else None)
        + "  "
        + _delta("MRR", report.mrr, previous.mrr if previous else None)
        + "  "
        + _delta("nDCG@k", report.ndcg_at_k, previous.ndcg_at_k if previous else None)
    )
    print(
        "  "
        + _delta(
            "拒答准确率", report.refusal_accuracy, previous.refusal_accuracy if previous else None
        )
        + f"（拒答正确 {report.refusal_correct}/{report.refusal_total}，"
        f"可答题误拒 {report.false_refusal}/{report.answerable_total}）"
    )
    print(
        "  "
        + _delta(
            "出处有效率",
            report.citation_valid_rate,
            previous.citation_valid_rate if previous else None,
        )
    )
    if report.misses:
        print("未命中题: " + ", ".join(q.question_id for q in report.misses))
    if report.wrong_refusals:
        wrong = report.wrong_refusals
        print(
            "拒答判定错误: "
            + ", ".join(
                f"{q.question_id}（期望{'拒答' if q.expect_refused else '回答'}）" for q in wrong
            )
        )
    print(_top1_distribution(report))
    print(f"报告: {path}")
    return int(ExitCode.OK)


def _cmd_run(args: argparse.Namespace) -> int:
    from codingwiki.pipeline import JobRunner

    runner = JobRunner()
    skip = tuple(args.skip)
    repo_ids = _enabled_repo_ids(args.config_dir) if args.repo == "all" else [args.repo]

    failed: list[str] = []
    first_error_exit = int(ExitCode.OK)
    for repo_id in repo_ids:
        record, exit_code = runner.run(
            repo_id,
            config_dir=args.config_dir,
            revision=args.revision,
            backend=args.backend,
            app_config=args.app_config,
            from_stage=args.from_stage,
            skip=skip,
        )
        _print_job_record(record)
        if record.status is not StageStatus.OK:
            failed.append(repo_id)
            if first_error_exit == int(ExitCode.OK):
                first_error_exit = exit_code
    if failed:
        print("失败仓库: " + ", ".join(failed))
        return first_error_exit
    return int(ExitCode.OK)


def _enabled_repo_ids(config_dir: str) -> list[str]:
    """configs/repos/ 下全部 enabled 的仓库（计划 5172：未配置的跳过）。"""
    repo_dir = Path(config_dir)
    if not repo_dir.is_dir():
        raise InputError(f"仓库配置目录不存在: {config_dir}")
    ids: list[str] = []
    for path in sorted(repo_dir.glob("*.yaml")):
        repo = load_repo_config(path)
        if not repo.enabled:
            get_logger("cli.run").info("仓库 %s enabled=false，跳过", repo.repo_id)
            continue
        ids.append(repo.repo_id)
    if not ids:
        raise InputError(f"{config_dir} 下没有启用的仓库配置")
    return ids


def _print_job_record(record: JobRecord) -> None:
    print(
        f"任务 repo={record.repo_id} job={record.job_id} "
        f"status={record.status.value} snapshot={record.snapshot_id or '-'}"
    )
    for stage in record.stages:
        line = f"  {stage.status.value:7} {stage.stage.value:8} {stage.duration_s:7.1f}s"
        if stage.summary:
            line += f"  {stage.summary}"
        print(line)
    base = Path(os.environ.get("CODINGWIKI_HOME", ".codingwiki")).resolve()
    print(f"记录: {base / 'jobs' / record.repo_id}")


def _top1_distribution(report: EvalReport) -> str:
    """两类题的 Top1 向量分分布（拒答阈值标定依据，架构 §15.5 v0.8）。"""

    def _span(rows: list[QuestionResult]) -> str:
        if not rows:
            return "n/a"
        scores = [q.top_vector_score for q in rows]
        return f"[{min(scores):.3f}, {max(scores):.3f}]"

    refused = [q for q in report.per_question if q.expect_refused]
    answerable = [q for q in report.per_question if not q.expect_refused]
    return (
        f"  Top1 向量分分布: 拒答题 {_span(refused)} / 可答题 {_span(answerable)}"
        "（阈值标定取两段之间的分隔点）"
    )


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
