"""回流闭环的六个阶段（架构文档 v0.9 §18.2）。

每个阶段函数接收 `JobContext`，返回一句话摘要（进入 JobRecord）；
失败抛 `CodingWikiError` 子类，由 runner 记 failed 并停止后续阶段。
阶段逻辑与对应 CLI 命令保持同一条能力层调用路径，不复制业务规则。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from codingwiki.config import load_config, load_repo_config
from codingwiki.domain.facts import RepoFacts
from codingwiki.domain.models import RepoConfig, Snapshot
from codingwiki.errors import EnvironmentIssue, InputError, InternalError, RetryableError

if TYPE_CHECKING:
    from codingwiki.backends.protocol import ModelBackend
    from codingwiki.publish.repo_wiki import RepoWikiTarget
    from codingwiki.snapshot import FilesystemSnapshotStore


@dataclass
class JobContext:
    """一次 run 的共享上下文：参数 + 跨阶段产物。"""

    repo: RepoConfig
    config_dir: str = "configs/repos"
    revision: str = ""  # 覆盖 repo.revision；空表示用配置值
    backend: str = "mock"  # wiki 生成后端：mock / api
    app_config: str = "configs/default.yaml"
    snapshot: Snapshot | None = field(default=None)
    facts: RepoFacts | None = field(default=None)


# ------------------------------------------------- 共享辅助（自 cli 下沉，cli 反向 import）


def resolve_snapshot(
    store: FilesystemSnapshotStore, repo_id: str, revision: str, snapshot_id: str
) -> Snapshot:
    """定位快照：snapshot_id 优先，其次 revision，最后 latest。"""
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


def facts_workdir(repo: RepoConfig, override: str) -> Path:
    if override:
        return Path(override).resolve()
    base = Path(os.environ.get("CODINGWIKI_HOME", ".codingwiki")).resolve()
    workdir = (
        Path(repo.workdir).resolve() if repo.workdir else base / "repos" / repo.repo_id / "work"
    )
    return (workdir / repo.root).resolve() if repo.root else workdir


def assert_workdir_revision(workdir: Path, revision: str) -> None:
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


def load_facts_or_raise(repo_id: str) -> RepoFacts:
    from codingwiki.facts import FilesystemFactStore

    facts = FilesystemFactStore().latest(repo_id)
    if facts is None:
        raise InputError(
            f"没有 {repo_id} 的事实记录，请先执行: codingwiki facts extract --repo {repo_id}"
        )
    return facts


def wiki_workdir(repo_id: str, config_dir: str, override: str) -> Path:
    repo_path = os.path.join(config_dir, f"{repo_id}.yaml")
    repo = load_repo_config(repo_path)
    return facts_workdir(repo, override)


def resolve_wiki_backend(backend: str, app_config: str) -> tuple[ModelBackend, str, str]:
    """按 backend 构造模型后端；API 缺配置时报可操作错误，绝不静默退回 mock。"""
    if backend == "mock":
        from codingwiki.backends import MockBackend
        from codingwiki.backends.mock import BACKEND_NAME, MODEL_NAME

        return MockBackend(), BACKEND_NAME, MODEL_NAME

    from codingwiki.backends import ApiBackend

    config = load_config(app_config)
    api = config.model.api
    problems: list[str] = []
    if not api.enabled:
        problems.append("model.api.enabled 为 false")
    if not api.base_url.strip():
        problems.append("model.api.base_url 为空")
    if not api.model.strip():
        problems.append("model.api.model 为空")
    if problems:
        raise InputError(
            "无法启动 API 后端：\n"
            + "\n".join(f"- {p}" for p in problems)
            + "\n修法：复制 configs/default.yaml 到已 gitignore 的 configs/api.local.yaml，"
            "填写 model.api.base_url/model/api_key_file，然后用 "
            "--backend api --app-config configs/api.local.yaml"
        )

    if api.api_key_file:
        key_path = Path(api.api_key_file).expanduser()
        if not key_path.is_file():
            raise EnvironmentIssue(
                f"API 密钥文件不存在: {key_path}；"
                "请检查 model.api.api_key_file（只写路径，不读内容）"
            )
    elif not os.environ.get(api.api_key_env, "").strip():
        raise EnvironmentIssue(
            f"未找到 API 密钥：请配置 model.api.api_key_file，或设置环境变量 {api.api_key_env}"
        )

    backend_obj = ApiBackend(
        base_url=api.base_url,
        model=api.model,
        api_key_file=api.api_key_file,
        api_key_env=api.api_key_env,
        timeout_s=api.timeout_s,
        max_retries=api.max_retries,
        temperature=api.temperature,
        max_tokens=api.max_tokens,
    )
    return backend_obj, "api", api.model


# ---------------------------------------------------------------- 六个阶段


def stage_sync(ctx: JobContext) -> str:
    """拉取代码生成快照；同版本复跑幂等（不产生新快照）。"""
    import logging

    from codingwiki.providers import create_provider
    from codingwiki.snapshot import FilesystemSnapshotStore

    logger = logging.getLogger("pipeline.stage_sync")
    repo = ctx.repo
    provider = create_provider(repo.provider)
    store = FilesystemSnapshotStore()

    revision = ctx.revision or repo.revision
    if revision == "latest":
        revision = provider.latest_revision(repo)
        logger.info("最新 revision=%s", revision)

    prev = store.latest(repo.repo_id)
    prev_id = prev.snapshot_id if prev else None
    workdir = provider.materialize(repo, revision)
    snapshot = store.create(
        repo_id=repo.repo_id,
        revision=revision,
        workdir=workdir,
        provider=repo.provider,
        config=repo,
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
    ctx.snapshot = snapshot
    in_scope = store.manifest(snapshot.snapshot_id).in_scope_count
    return f"snapshot={snapshot.snapshot_id} files={in_scope}"


def stage_facts(ctx: JobContext) -> str:
    """从快照提取代码事实；未变更文件复用缓存（extractor 内置）。"""
    from codingwiki.facts import FilesystemFactExtractor, FilesystemFactStore
    from codingwiki.snapshot import FilesystemSnapshotStore

    repo = ctx.repo
    store = FilesystemSnapshotStore()
    snapshot = ctx.snapshot or resolve_snapshot(store, repo.repo_id, "", "")
    workdir = facts_workdir(repo, "")
    assert_workdir_revision(workdir, snapshot.revision)

    facts = FilesystemFactExtractor(FilesystemFactStore(), force=False).extract(snapshot, workdir)
    FilesystemFactStore().save(facts)
    ctx.facts = facts
    return (
        f"snapshot={facts.snapshot_id} files={len(facts.files)} "
        f"symbols={facts.symbol_count} calls={facts.call_count}"
    )


def stage_wiki(ctx: JobContext) -> str:
    """生成并校验页面（含修复循环）；有失败页即阶段失败，阻断发布。"""
    from codingwiki.generate import CoveragePlanner, GeneratePipeline, TaskPackageBuilder
    from codingwiki.generate.cluster import ensure_module_map
    from codingwiki.publish import FilesystemPageStore
    from codingwiki.verify import PageVerifier

    facts = ctx.facts or load_facts_or_raise(ctx.repo.repo_id)
    workdir = wiki_workdir(ctx.repo.repo_id, ctx.config_dir, "")
    store = FilesystemPageStore(ctx.repo.repo_id)
    backend, backend_name, model_name = resolve_wiki_backend(ctx.backend, ctx.app_config)
    # S37：模块聚类产物落盘（同快照复用），规划按 ModuleMap 进行
    module_map = ensure_module_map(facts, store.root)
    plan = CoveragePlanner().plan(facts, store.load_deps(), module_map)
    # S17 §15.5：run 的 wiki 阶段总是全量计划，清理不在计划内的旧页
    store.prune_drafts({t.page_id for t in plan.tasks})

    known_pages = {t.page_id for t in plan.tasks} | set(store.list_drafts())
    pipeline = GeneratePipeline(
        backend=backend,
        builder=TaskPackageBuilder(workdir),
        verifier=PageVerifier(facts, workdir, known_pages=known_pages),
        store=store,
        backend_name=backend_name,
        model_name=model_name,
    )
    outcomes = pipeline.run_plan(plan, facts)

    ok = sum(1 for o in outcomes if o.ok and not o.reused)
    reused = sum(1 for o in outcomes if o.reused)
    failed = [o.task.page_id for o in outcomes if not o.ok]
    if failed:
        raise RetryableError(
            f"页面生成失败 {len(failed)} 页: {', '.join(failed[:10])}；不进入 verify/publish"
        )
    return f"生成 {ok}，复用 {reused}，失败 0"


def stage_verify(ctx: JobContext) -> str:
    """复验草稿区全部页面；有失败即阶段失败。"""
    from codingwiki.publish import FilesystemPageStore
    from codingwiki.verify import PageVerifier

    facts = ctx.facts or load_facts_or_raise(ctx.repo.repo_id)
    workdir = wiki_workdir(ctx.repo.repo_id, ctx.config_dir, "")
    store = FilesystemPageStore(ctx.repo.repo_id)
    page_ids = store.list_drafts()
    if not page_ids:
        raise InputError(f"仓库 {ctx.repo.repo_id} 草稿区为空，请先执行 wiki generate")

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
    if failed:
        raise RetryableError(f"复验失败 {len(failed)} 页: {', '.join(failed[:10])}")
    return f"复验 {len(page_ids)} 页全部通过"


def _repo_wiki_target(ctx: JobContext, snapshot_id: str) -> RepoWikiTarget:
    from codingwiki.publish.repo_wiki import RepoWikiTarget

    revision = snapshot_id.split("@", 1)[-1] or ctx.repo.revision
    return RepoWikiTarget.from_repo(ctx.repo.repo_id, ctx.repo.url, revision, ctx.repo.root)


def stage_publish(ctx: JobContext) -> str:
    """门禁检查后发布新版本并生成站点（失败时 current 指针不动）。"""
    from codingwiki.publish import FilesystemPageStore

    facts = ctx.facts or load_facts_or_raise(ctx.repo.repo_id)
    workdir = wiki_workdir(ctx.repo.repo_id, ctx.config_dir, "")
    store = FilesystemPageStore(ctx.repo.repo_id)
    profile: dict[str, object] = {
        "files": len(facts.files),
        "symbols": facts.symbol_count,
        "calls": facts.call_count,
        "modules": len({file.module for file in facts.files}),
        "build_targets": len(facts.build_targets),
        "tests": len(facts.tests),
        "config_facts": len(facts.config_facts),
    }
    index = store.publish(
        facts.snapshot_id,
        workdir=workdir,
        site_profile=profile,
        facts=facts,
        repo_wiki=_repo_wiki_target(ctx, facts.snapshot_id),
    )
    counts = index.status_counts
    return (
        f"version={index.version_id} pages={len(index.pages)} verified={counts.get('verified', 0)}"
    )


def stage_index(ctx: JobContext) -> str:
    """重建检索索引（wiki 块 + 代码/事实块），写入新 index 版本。"""
    from codingwiki.publish import FilesystemPageStore
    from codingwiki.retrieval import Bm25Index, ChunkBuilder, FilesystemIndexStore
    from codingwiki.retrieval.registry import create_embedder

    facts = ctx.facts or load_facts_or_raise(ctx.repo.repo_id)
    workdir = wiki_workdir(ctx.repo.repo_id, ctx.config_dir, "")
    wiki_pages = FilesystemPageStore(ctx.repo.repo_id).load_published_pages()

    chunks = ChunkBuilder(workdir).build(facts, wiki_pages)
    if not chunks:
        raise InputError(f"仓库 {ctx.repo.repo_id} 没有任何可索引的内容")

    config = load_config(ctx.app_config)
    settings = config.embedding
    backend = settings.backend
    is_hash = backend.strip().lower() in {"hash", "hash-embed"}
    embedder = create_embedder(
        backend,
        model_path=settings.model_path,
        device=settings.device,
        batch_size=settings.batch_size,
        max_length=settings.max_length,
        dim=0 if is_hash else settings.dimension,
    )
    texts = [f"{c.title}\n{c.text}" for c in chunks]
    vocab_size = Bm25Index(texts).vocab_size
    vectors = embedder.embed(texts)

    store = FilesystemIndexStore(ctx.repo.repo_id)
    meta = store.save(chunks, vectors, embedder.name, embedder.dim, vocab_size)
    return (
        f"index={meta.index_id} snapshot={meta.snapshot_id} chunks={meta.chunk_count} "
        f"model={meta.embedding_model}"
    )


# 阶段名 → 函数；顺序由 domain.job.STAGE_ORDER 决定，这里只做查找表
STAGES: dict[str, object] = {
    "sync": stage_sync,
    "facts": stage_facts,
    "wiki": stage_wiki,
    "verify": stage_verify,
    "publish": stage_publish,
    "index": stage_index,
}

__all__ = [
    "STAGES",
    "JobContext",
    "assert_workdir_revision",
    "facts_workdir",
    "load_facts_or_raise",
    "resolve_snapshot",
    "resolve_wiki_backend",
    "stage_facts",
    "stage_index",
    "stage_publish",
    "stage_sync",
    "stage_verify",
    "stage_wiki",
    "wiki_workdir",
]
