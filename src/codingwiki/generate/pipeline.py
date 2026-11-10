"""生成流水线（计划文档 9.4、9.8.3；架构文档 v0.4 13.3）。

单任务流程：任务包 → 模型后端 → 解析输出 → 校验 → 失败则带 errors.json 重试
（最多 max_rounds 次）→ 通过则 stage 草稿；仍失败标记 failed，不覆盖旧页面。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from codingwiki.backends.protocol import BackendStatus, ModelBackend
from codingwiki.domain.facts import RepoFacts
from codingwiki.domain.module_map import ModuleMap
from codingwiki.domain.wiki import (
    CoveragePlan,
    PageDeps,
    PageStatus,
    PageTask,
    PageType,
    TaskMeta,
    VerifyIssue,
    VerifyReport,
)
from codingwiki.generate.cluster import build_module_map
from codingwiki.generate.package import TaskPackageBuilder
from codingwiki.generate.plan import scope_files
from codingwiki.publish.store import FilesystemPageStore
from codingwiki.verify.verifier import PageVerifier


class TaskOutcome:
    """单个任务的流水线结果。"""

    def __init__(
        self,
        task: PageTask,
        status: PageStatus,
        attempts: int,
        report: VerifyReport | None = None,
        reused: bool = False,
    ) -> None:
        self.task = task
        self.status = status
        self.attempts = attempts
        self.report = report
        self.reused = reused

    @property
    def ok(self) -> bool:
        return self.status in (PageStatus.DRAFT, PageStatus.VERIFIED)


class GeneratePipeline:
    def __init__(
        self,
        backend: ModelBackend,
        builder: TaskPackageBuilder,
        verifier: PageVerifier,
        store: FilesystemPageStore,
        max_rounds: int = 3,
        backend_name: str = "mock",
        model_name: str = "",
        max_workers: int = 1,
    ) -> None:
        self._backend = backend
        self._builder = builder
        self._verifier = verifier
        self._store = store
        self._max_rounds = max(1, max_rounds)
        self._backend_name = backend_name
        self._model_name = model_name
        self._max_workers = max(1, max_workers)

    def run_task(
        self,
        task: PageTask,
        facts: RepoFacts,
        related_pages: list[str] | None = None,
        module_map: ModuleMap | None = None,
    ) -> TaskOutcome:
        if task.reuse:
            return TaskOutcome(task, PageStatus.VERIFIED, attempts=0, reused=True)

        previous_errors: list[VerifyIssue] | None = None
        last_report: VerifyReport | None = None
        for attempt in range(1, self._max_rounds + 1):
            packet = self._builder.build(
                task,
                facts,
                related_pages=related_pages,
                attempt=attempt,
                previous_errors=previous_errors,
                module_map=module_map,
            )
            backend_result = self._backend.run(packet)
            if backend_result.status is not BackendStatus.SUCCEEDED:
                previous_errors = [
                    VerifyIssue(
                        rule="backend_failed",
                        message=(
                            f"后端状态 {backend_result.status.value}: "
                            f"{backend_result.error or backend_result.stderr or '无详情'}"
                        ),
                    )
                ]
                continue

            page_path = packet.output_dir / "page.md"
            result_path = packet.output_dir / "result.json"
            if not page_path.is_file() or not result_path.is_file():
                previous_errors = [
                    VerifyIssue(
                        rule="output_missing",
                        message="output/ 中缺少 page.md 或 result.json",
                        suggestion="按任务要求写 output/page.md 与 output/result.json",
                    )
                ]
                continue

            page_text = page_path.read_text(encoding="utf-8")
            result_text = result_path.read_text(encoding="utf-8")
            meta = TaskMeta.model_validate_json(
                (packet.workspace / "meta.json").read_text(encoding="utf-8")
            )
            report = self._verifier.verify(page_text, result_text, meta)
            last_report = report
            if report.status == "passed":
                self._store.stage(
                    page_text,
                    result_text,
                    report,
                    meta,
                    deps=self._build_deps(meta, facts, task, module_map),
                )
                return TaskOutcome(task, PageStatus.DRAFT, attempts=attempt, report=report)
            previous_errors = [i for i in report.issues if i.severity == "error"] or report.issues

        # 9.8.3：修复失败不覆盖旧页面，只记录失败状态
        return TaskOutcome(
            task,
            PageStatus.FAILED,
            attempts=self._max_rounds,
            report=last_report,
        )

    def run_plan(self, plan: CoveragePlan, facts: RepoFacts) -> list[TaskOutcome]:
        module_map = build_module_map(facts)
        all_page_ids = {t.page_id for t in plan.tasks} | set(self._store.list_drafts())
        related_by_page = {
            task.page_id: _related_pages(task, all_page_ids, facts, module_map)
            for task in plan.tasks
        }

        def run(task: PageTask) -> TaskOutcome:
            return self.run_task(
                task, facts, related_pages=related_by_page[task.page_id], module_map=module_map
            )

        if self._max_workers > 1:
            # 真实模型生成以网络等待为主；页面写入互不重叠，可安全并发（S17 提速）
            with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
                return list(executor.map(run, plan.tasks))
        return [run(task) for task in plan.tasks]

    # ---------------------------------------------------------------- 内部

    def _build_deps(
        self,
        meta: TaskMeta,
        facts: RepoFacts,
        task: PageTask,
        module_map: ModuleMap | None = None,
    ) -> PageDeps:
        blob_by_path = facts.blob_by_path
        dep_files: dict[str, str] = {}
        for ev in meta.evidence:
            sha = blob_by_path.get(ev.file)
            if sha:
                dep_files[ev.file] = sha
        # S17 §15.3：聚合页的依赖登记页面作用域，用于 C1 覆盖与 stale 判定
        for path in scope_files(task, facts, module_map):
            sha = blob_by_path.get(path)
            if sha:
                dep_files[path] = sha
        symbols = sorted({ev.symbol for ev in meta.evidence if ev.symbol})
        return PageDeps(
            page_id=meta.page_id,
            snapshot_id=meta.snapshot_id,
            files=dep_files,
            symbols=symbols,
            evidence_ids=[ev.id for ev in meta.evidence],
            prompt_version=meta.prompt_version,
            backend=self._backend_name,
            model=self._model_name,
        )


def _related_pages(
    task: PageTask,
    all_page_ids: set[str],
    facts: RepoFacts,
    module_map: ModuleMap,
) -> list[str]:
    """相关页面：模块页挂同目录接口页与协作模块页；接口页挂所在模块页；其余附系统页。"""
    related: list[str] = []
    if task.page_type is PageType.MODULE:
        module_dirs = {_parent_dir(path) for path in scope_files(task, facts, module_map)}
        related = sorted(
            pid
            for pid in all_page_ids
            if pid.startswith("interface/")
            and ("." if pid == "interface/root" else pid[len("interface/") :]) in module_dirs
        )[:10]
        # S37：协作模块互链（ModuleMap.collaborations）
        peers = sorted(
            pair.b if pair.a == task.scope else pair.a
            for pair in module_map.collaborations
            if pair.a == task.scope or pair.b == task.scope
        )
        related += [
            f"module/{peer}" for peer in peers if f"module/{peer}" in all_page_ids
        ][:5]
    elif task.page_type is PageType.INTERFACE:
        files = [file for file in facts.files if _parent_dir(file.path) == task.scope]
        module_page = ""
        if files:
            path_to_module: dict[str, str] = {}
            for entry in module_map.entries:
                for path in entry.files:
                    path_to_module.setdefault(path, entry.module_key)
            module = path_to_module.get(files[0].path, "")
            module_page = f"module/{module}" if module else ""
        if module_page and module_page in all_page_ids:
            related = [module_page]
    if "overview" in all_page_ids and task.page_id != "overview":
        related.append("overview")
    return related


def _parent_dir(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else "."


__all__ = ["GeneratePipeline", "TaskOutcome"]
