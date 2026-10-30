"""覆盖计划器：从代码事实生成"必须覆盖清单"（计划文档 9.3、4.6）。

由确定性规则决定写哪些页面，不由模型决定。
排序：接口页 > 模块页 > 构建/配置/测试页 > 流程页 > 系统页；同类型按被引用数降序。
增量：drafts 区已有页面且依赖未变化时标记 reuse，不重复生成（9.10.3）。
"""

from __future__ import annotations

from codingwiki.domain.facts import RepoFacts, SymbolKind
from codingwiki.domain.wiki import (
    CoverageLevel,
    CoveragePlan,
    PageDeps,
    PageTask,
    PageType,
)

_HEADER_LANGS = {"c", "cpp", "cuda"}
_HEADER_SUFFIXES = (".h", ".hpp", ".hh", ".cuh")
_PUBLIC_KINDS = {
    SymbolKind.FUNCTION,
    SymbolKind.CLASS,
    SymbolKind.STRUCT,
    SymbolKind.KERNEL,
    SymbolKind.ENUM,
}
_FLOW_MAX_CALLS = 20  # 流程页最多步骤数（9.4.3 跨模块证据，限 2 跳由构建器控制）


class CoveragePlanner:
    """输入事实与已有页面依赖，输出覆盖计划。"""

    def __init__(self, max_tasks: int = 0) -> None:
        self._max_tasks = max_tasks  # 0 表示不限

    def plan(
        self,
        facts: RepoFacts,
        existing_deps: dict[str, PageDeps] | None = None,
    ) -> CoveragePlan:
        tasks: list[PageTask] = []
        tasks += self._interface_tasks(facts)
        tasks += self._module_tasks(facts)
        tasks += self._build_tasks(facts)
        tasks += self._config_tasks(facts)
        tasks += self._test_tasks(facts)
        tasks += self._flow_tasks(facts)
        tasks += self._system_tasks(facts)

        tasks = self._mark_reuse(tasks, facts, existing_deps or {})

        skipped: list[str] = []
        if self._max_tasks > 0:
            fresh = [t for t in tasks if not t.reuse]
            reused = [t for t in tasks if t.reuse]
            if len(fresh) > self._max_tasks:
                skipped = [t.page_id for t in fresh[self._max_tasks :]]
                fresh = fresh[: self._max_tasks]
            tasks = fresh + reused

        return CoveragePlan(
            repo_id=facts.repo_id,
            snapshot_id=facts.snapshot_id,
            tasks=tasks,
            skipped=skipped,
        )

    # ---------------------------------------------------------------- 各类任务

    def _interface_tasks(self, facts: RepoFacts) -> list[PageTask]:
        """公开符号：头文件符号 / CUDA kernel / 被 ≥2 个文件调用的符号。"""
        called_files: dict[str, set[str]] = {}
        for file in facts.files:
            for call in file.calls:
                for target in call.targets:
                    called_files.setdefault(target, set()).add(file.path)

        tasks: list[PageTask] = []
        seen: set[str] = set()
        for file in facts.files:
            for sym in file.symbols:
                if sym.kind not in _PUBLIC_KINDS:
                    continue
                is_header = file.path.endswith(_HEADER_SUFFIXES) or file.language in _HEADER_LANGS
                ref_count = len(called_files.get(sym.symbol_id, set()))
                if not (is_header or sym.kind is SymbolKind.KERNEL or ref_count >= 2):
                    continue
                if sym.symbol_id in seen:
                    continue
                seen.add(sym.symbol_id)
                page_id = f"interface/{sym.path}/{sym.name}"
                tasks.append(
                    PageTask(
                        task_id=_task_id(page_id),
                        page_id=page_id,
                        page_type=PageType.INTERFACE,
                        title_hint=sym.qualified_name,
                        subject=sym.symbol_id,
                        coverage_level=CoverageLevel.C2,
                        priority=100 - min(ref_count, 99),
                    )
                )
        return sorted(tasks, key=lambda t: (t.priority, t.page_id))

    def _module_tasks(self, facts: RepoFacts) -> list[PageTask]:
        modules: dict[str, int] = {}
        for file in facts.files:
            modules[file.module] = modules.get(file.module, 0) + len(file.symbols)
        return [
            PageTask(
                task_id=_task_id(f"module/{name}"),
                page_id=f"module/{name}",
                page_type=PageType.MODULE,
                title_hint=f"模块 {name}",
                subject=name,
                coverage_level=CoverageLevel.C2,
                priority=200 - min(count, 99),
            )
            for name, count in sorted(modules.items())
        ]

    def _build_tasks(self, facts: RepoFacts) -> list[PageTask]:
        tasks: list[PageTask] = []
        for target in facts.build_targets:
            if not target.name:
                continue
            page_id = f"build/{target.name}"
            tasks.append(
                PageTask(
                    task_id=_task_id(page_id),
                    page_id=page_id,
                    page_type=PageType.BUILD,
                    title_hint=f"构建目标 {target.name}",
                    subject=target.name,
                    coverage_level=CoverageLevel.C2,
                    priority=300,
                )
            )
        return tasks

    def _config_tasks(self, facts: RepoFacts) -> list[PageTask]:
        by_path: dict[str, int] = {}
        for cfg in facts.config_facts:
            if cfg.kind != "file" or not cfg.path:
                continue
            by_path[cfg.path] = by_path.get(cfg.path, 0) + 1
        return [
            PageTask(
                task_id=_task_id(f"config/{path}"),
                page_id=f"config/{path}",
                page_type=PageType.CONFIG,
                title_hint=f"配置文件 {path}",
                subject=path,
                coverage_level=CoverageLevel.C2,
                priority=310,
            )
            for path in sorted(by_path)
        ]

    def _test_tasks(self, facts: RepoFacts) -> list[PageTask]:
        file_module = {f.path: f.module for f in facts.files}
        by_module: dict[str, int] = {}
        for test in facts.tests:
            module = file_module.get(test.path, "(root)")
            by_module[module] = by_module.get(module, 0) + 1
        return [
            PageTask(
                task_id=_task_id(f"test/{module}"),
                page_id=f"test/{module}",
                page_type=PageType.TEST,
                title_hint=f"模块 {module} 的测试",
                subject=module,
                coverage_level=CoverageLevel.C1,
                priority=320,
            )
            for module in sorted(by_module)
        ]

    def _flow_tasks(self, facts: RepoFacts) -> list[PageTask]:
        tasks: list[PageTask] = []
        for file in facts.files:
            for entry in file.entries:
                page_id = f"flow/{entry.name}"
                tasks.append(
                    PageTask(
                        task_id=_task_id(page_id),
                        page_id=page_id,
                        page_type=PageType.FLOW,
                        title_hint=f"流程 {entry.name}",
                        subject=entry.name,
                        coverage_level=CoverageLevel.C3,
                        priority=400,
                    )
                )
        return tasks

    def _system_tasks(self, facts: RepoFacts) -> list[PageTask]:
        if not facts.files:
            return []
        return [
            PageTask(
                task_id=_task_id("overview"),
                page_id="overview",
                page_type=PageType.OVERVIEW,
                title_hint="系统概览",
                subject="overview",
                coverage_level=CoverageLevel.C3,
                priority=500,
            ),
            PageTask(
                task_id=_task_id("home"),
                page_id="home",
                page_type=PageType.HOME,
                title_hint="首页",
                subject="home",
                coverage_level=CoverageLevel.C1,
                priority=510,
            ),
        ]

    # ---------------------------------------------------------------- 增量复用

    def _mark_reuse(
        self,
        tasks: list[PageTask],
        facts: RepoFacts,
        existing_deps: dict[str, PageDeps],
    ) -> list[PageTask]:
        if not existing_deps:
            return tasks
        blob_by_path = facts.blob_by_path
        marked: list[PageTask] = []
        for task in tasks:
            deps = existing_deps.get(task.page_id)
            if deps is None:
                marked.append(task)
                continue
            # 依赖文件全部存在且 blob 未变 → 复用（9.10.3）
            unchanged = bool(deps.files) and all(
                blob_by_path.get(path) == sha for path, sha in deps.files.items()
            )
            if unchanged:
                marked.append(task.model_copy(update={"reuse": True}))
            else:
                marked.append(task)
        return marked


def _task_id(page_id: str) -> str:
    return page_id.replace("/", "__").replace(":", "_")


__all__ = ["CoveragePlanner"]
