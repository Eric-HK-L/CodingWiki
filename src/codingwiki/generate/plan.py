"""覆盖计划器：从代码事实生成「人可读 Wiki」的页面清单（S6.5-b）。

粒度原则（架构文档 v0.7 §17.2）：
- 模块页按目录聚合，不按符号逐页；
- 接口页按头文件/组件聚合，不按符号逐页；
- 流程页只保留端到端入口，按可达调用规模排序；
- 配置/构建/测试/术语各一页，不再为每个目标/配置键开页。

页面数由确定性规则控制：模块 ≤ 60、接口 ≤ 30、流程 ≤ 15，总量落在 50–120。
"""

from __future__ import annotations

from collections import Counter

from codingwiki.domain.facts import CallFact, EntryFact, FileFacts, RepoFacts
from codingwiki.domain.wiki import (
    CoverageLevel,
    CoveragePlan,
    PageDeps,
    PageTask,
    PageType,
)
from codingwiki.generate.modules import (
    is_header,
    is_public_symbol,
    module_groups,
)

_MODULE_STANDALONE = 44
_MODULE_ROLLUP_LIMIT = 15
_INTERFACE_LIMIT = 100  # S17：目录聚合，硬上限（cuphy 实际约 43 页）
_INTERFACE_MIN = 10  # 覆盖面已达标时仍保留的多样性下限
_INTERFACE_COVERAGE = 0.95  # C2 目标：公开符号累计覆盖
_FLOW_LIMIT = 120  # S17：入口按目录去重后全量（cuphy 93 个入口目录）
_FLOW_HOPS = 3


class CoveragePlanner:
    """输入事实与已有页面依赖，输出覆盖计划。"""

    def __init__(self, max_tasks: int = 0) -> None:
        self._max_tasks = max_tasks  # 0 表示不限

    def plan(
        self,
        facts: RepoFacts,
        existing_deps: dict[str, PageDeps] | None = None,
    ) -> CoveragePlan:
        if not facts.files:
            return CoveragePlan(
                repo_id=facts.repo_id,
                snapshot_id=facts.snapshot_id,
                tasks=[],
            )

        tasks: list[PageTask] = []
        tasks += self._system_tasks(facts)
        tasks += self._module_tasks(facts)
        tasks += self._interface_tasks(facts)
        tasks += self._flow_tasks(facts)
        tasks += self._misc_tasks(facts)

        tasks = self._mark_reuse(tasks, facts, existing_deps or {})

        skipped: list[str] = []
        if self._max_tasks > 0:
            fresh = [t for t in tasks if not t.reuse]
            reused = [t for t in tasks if t.reuse]
            if len(fresh) > self._max_tasks:
                skipped = [t.page_id for t in fresh[self._max_tasks :]]
                fresh = fresh[: self._max_tasks]
            tasks = fresh + reused

        tasks.sort(key=lambda task: (task.priority, task.page_id))
        return CoveragePlan(
            repo_id=facts.repo_id,
            snapshot_id=facts.snapshot_id,
            tasks=tasks,
            skipped=skipped,
        )

    # ---------------------------------------------------------------- 系统页

    def _system_tasks(self, facts: RepoFacts) -> list[PageTask]:
        return [
            PageTask(
                task_id=_task_id("home"),
                page_id="home",
                page_type=PageType.HOME,
                title_hint="首页",
                subject="home",
                coverage_level=CoverageLevel.C1,
                priority=10,
            ),
            PageTask(
                task_id=_task_id("overview"),
                page_id="overview",
                page_type=PageType.OVERVIEW,
                title_hint="项目总览",
                subject="overview",
                coverage_level=CoverageLevel.C3,
                priority=11,
            ),
            PageTask(
                task_id=_task_id("architecture"),
                page_id="architecture",
                page_type=PageType.ARCHITECTURE,
                title_hint="架构",
                subject="architecture",
                coverage_level=CoverageLevel.C3,
                priority=12,
            ),
        ]

    # ---------------------------------------------------------------- 模块页

    def _module_tasks(self, facts: RepoFacts) -> list[PageTask]:
        groups = module_groups(facts)
        symbol_module: dict[str, str] = {}
        module_by_path: dict[str, str] = {}
        for module, files in groups.items():
            for file in files:
                module_by_path[file.path] = module
                for symbol in file.symbols:
                    symbol_module.setdefault(symbol.symbol_id, module)

        incoming: Counter[str] = Counter()
        for file in facts.files:
            source_module = module_by_path.get(file.path, "")
            for call in file.calls:
                for target in call.targets:
                    target_module = symbol_module.get(target)
                    if target_module and target_module != source_module:
                        incoming[target_module] += 1

        weights = {
            module: _module_weight(files, incoming[module]) for module, files in groups.items()
        }
        ranked = sorted(groups, key=lambda module: (-weights[module], module))

        if len(ranked) <= _MODULE_STANDALONE + _MODULE_ROLLUP_LIMIT:
            selected = ranked
            leftovers: list[str] = []
        else:
            selected = ranked[:_MODULE_STANDALONE]
            leftovers = ranked[_MODULE_STANDALONE:]

        tasks = [
            PageTask(
                task_id=_task_id(f"module/{module}"),
                page_id=f"module/{module}",
                page_type=PageType.MODULE,
                title_hint=f"模块 {module}",
                subject=module,
                scope=module,
                coverage_level=CoverageLevel.C2,
                priority=20 + index,
            )
            for index, module in enumerate(selected)
        ]

        for index, category in enumerate(_rollup_categories(leftovers)):
            tasks.append(
                PageTask(
                    task_id=_task_id(f"module/{category}/other"),
                    page_id=f"module/{category}/other",
                    page_type=PageType.MODULE,
                    title_hint=f"{category} 模块补遗",
                    subject=f"{category} 模块补遗",
                    scope=f"{category}/",
                    rollup=True,
                    exclude=sorted(module for module in selected if _category(module) == category),
                    coverage_level=CoverageLevel.C1,
                    priority=20 + len(selected) + index,
                )
            )
        return tasks

    # ---------------------------------------------------------------- 接口页

    def _interface_tasks(self, facts: RepoFacts) -> list[PageTask]:
        """按目录聚合公开符号；累计覆盖 ≥95% 的目录优先成页（S17 §15.2）。

        单文件一页无法在页面预算内达到 C2 ≥95%（cuphy 需 338 页），
        目录聚合后 43 页即可覆盖 95% 的公开符号。
        """
        dir_public: Counter[str] = Counter()
        dir_header: dict[str, bool] = {}
        seen: set[str] = set()
        for file in facts.files:
            if file.path in seen:  # 事实层可能存在同路径重复条目，覆盖统计按唯一文件
                continue
            seen.add(file.path)
            public = [symbol for symbol in file.symbols if is_public_symbol(symbol)]
            if not public:
                continue
            directory = _parent_dir(file.path)
            dir_public[directory] += len(public)
            dir_header[directory] = dir_header.get(directory, False) or is_header(file.path)

        if not dir_public:
            return []

        total_public = sum(dir_public.values())
        target = int(total_public * _INTERFACE_COVERAGE + 0.999999)  # ceil，避免浮点截断
        ranked = sorted(
            dir_public,
            key=lambda directory: (
                -dir_public[directory],
                -int(dir_header[directory]),
                directory,
            ),
        )

        chosen: list[str] = []
        covered = 0
        for directory in ranked:
            if len(chosen) >= _INTERFACE_LIMIT:
                break
            if covered >= target and len(chosen) >= _INTERFACE_MIN:
                break
            chosen.append(directory)
            covered += dir_public[directory]

        tasks: list[PageTask] = []
        for index, directory in enumerate(chosen):
            page_id = f"interface/{directory if directory != '.' else 'root'}"
            tasks.append(
                PageTask(
                    task_id=_task_id(page_id),
                    page_id=page_id,
                    page_type=PageType.INTERFACE,
                    title_hint=f"接口 {directory}",
                    subject=directory,
                    scope=directory,
                    coverage_level=CoverageLevel.C2,
                    priority=200 + index,
                )
            )
        return tasks

    # ---------------------------------------------------------------- 流程页

    def _flow_tasks(self, facts: RepoFacts) -> list[PageTask]:
        entries = _entry_candidates(facts)
        if not entries:
            return []

        calls_by_file = {file.path: file.calls for file in facts.files if file.calls}
        symbol_file: dict[str, str] = {}
        for file in facts.files:
            for symbol in file.symbols:
                symbol_file.setdefault(symbol.symbol_id, file.path)

        scored: list[tuple[int, EntryFact]] = []
        for entry in entries:
            reachable = _reachable_calls(entry.path, calls_by_file, symbol_file)
            scored.append((reachable, entry))
        scored.sort(key=lambda item: (-item[0], item[1].path))

        tasks: list[PageTask] = []
        for index, (_reach, entry) in enumerate(scored[:_FLOW_LIMIT]):
            stem = entry.path.rsplit(".", 1)[0]
            page_id = f"flow/{stem}"
            tasks.append(
                PageTask(
                    task_id=_task_id(page_id),
                    page_id=page_id,
                    page_type=PageType.FLOW,
                    title_hint=f"流程 {entry.name}（{entry.path}）",
                    subject=f"{entry.path}::{entry.name}",
                    scope=entry.path,
                    coverage_level=CoverageLevel.C3,
                    priority=300 + index,
                )
            )
        return tasks

    # ---------------------------------------------------------------- 配置/构建/测试/术语

    def _misc_tasks(self, facts: RepoFacts) -> list[PageTask]:
        tasks: list[PageTask] = []
        if facts.build_targets:
            tasks.append(
                PageTask(
                    task_id=_task_id("build/overview"),
                    page_id="build/overview",
                    page_type=PageType.BUILD,
                    title_hint="构建与运行",
                    subject="overview",
                    coverage_level=CoverageLevel.C2,
                    priority=400,
                )
            )
        if facts.config_facts:
            tasks.append(
                PageTask(
                    task_id=_task_id("config/overview"),
                    page_id="config/overview",
                    page_type=PageType.CONFIG,
                    title_hint="配置项",
                    subject="overview",
                    coverage_level=CoverageLevel.C2,
                    priority=401,
                )
            )
        if facts.tests:
            tasks.append(
                PageTask(
                    task_id=_task_id("test/overview"),
                    page_id="test/overview",
                    page_type=PageType.TEST,
                    title_hint="测试",
                    subject="overview",
                    coverage_level=CoverageLevel.C1,
                    priority=402,
                )
            )
        if facts.symbol_count:
            tasks.append(
                PageTask(
                    task_id=_task_id("glossary"),
                    page_id="glossary",
                    page_type=PageType.GLOSSARY,
                    title_hint="术语表",
                    subject="glossary",
                    coverage_level=CoverageLevel.C1,
                    priority=403,
                )
            )
        return tasks

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
            # S17 §15.3：复用还要求旧 deps 已登记当前作用域，否则无法补上 C1 覆盖
            expected = set(scope_files(task, facts))
            unchanged = (
                bool(deps.files)
                and expected <= set(deps.files)
                and all(blob_by_path.get(path) == sha for path, sha in deps.files.items())
            )
            if unchanged:
                marked.append(task.model_copy(update={"reuse": True}))
            else:
                marked.append(task)
        return marked


# ---------------------------------------------------------------- 内部


def _module_weight(files: list[FileFacts], incoming: int) -> int:
    symbols = sum(len(file.symbols) for file in files)
    public = sum(1 for file in files for symbol in file.symbols if is_public_symbol(symbol))
    return len(files) * 2 + symbols + public + incoming * 3


def _rollup_categories(leftovers: list[str]) -> list[str]:
    if not leftovers:
        return []
    counts: Counter[str] = Counter(_category(module) for module in leftovers)
    return [category for category, _count in counts.most_common(_MODULE_ROLLUP_LIMIT)]


def _category(module: str) -> str:
    return module.split("/", 1)[0] if "/" in module else module


def _parent_dir(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else "."


def scope_files(task: PageTask, facts: RepoFacts) -> list[str]:
    """聚合页作用域内的文件（S17 §15.3），用于依赖登记与增量复用判定。"""
    if task.page_type is PageType.MODULE:
        groups = module_groups(facts)
        if task.rollup:
            excluded = set(task.exclude)
            return [
                file.path
                for module, files in groups.items()
                if module.startswith(task.scope) and module not in excluded
                for file in files
            ]
        return [file.path for file in groups.get(task.scope, [])]
    if task.page_type is PageType.INTERFACE:
        files = [file for file in facts.files if _parent_dir(file.path) == task.scope]
        if not files:
            files = [file for file in facts.files if file.path == task.scope]
        return [file.path for file in files]
    if task.page_type is PageType.FLOW:
        entry_path = task.subject.partition("::")[0]
        return [entry_path] if facts.blob_by_path.get(entry_path) else []
    return []


def _entry_candidates(facts: RepoFacts) -> list[EntryFact]:
    """每个入口目录只保留一个 main，避免同一示例的多个 kernel_launch 占页。"""
    by_dir: dict[str, list[EntryFact]] = {}
    for file in facts.files:
        for entry in file.entries:
            if entry.kind not in {"main", "python_main"}:
                continue
            parent = entry.path.rsplit("/", 1)[0] if "/" in entry.path else "."
            by_dir.setdefault(parent, []).append(entry)

    chosen: list[EntryFact] = []
    for entries in by_dir.values():
        entries.sort(key=lambda item: (item.name not in {"main", "__main__"}, item.path, item.line))
        chosen.append(entries[0])
    return chosen


def _reachable_calls(
    entry_path: str,
    calls_by_file: dict[str, list[CallFact]],
    symbol_file: dict[str, str],
) -> int:
    """从入口文件沿调用边 BFS，统计可达调用数（限 _FLOW_HOPS 跳）。"""
    frontier = list(calls_by_file.get(entry_path, []))
    seen_files = {entry_path}
    count = 0
    for _hop in range(_FLOW_HOPS):
        next_frontier: list[CallFact] = []
        for call in frontier:
            count += 1
            for target in call.targets:
                target_file = symbol_file.get(target)
                if target_file and target_file not in seen_files:
                    seen_files.add(target_file)
                    next_frontier.extend(calls_by_file.get(target_file, []))
        if count > 500:
            break
        frontier = next_frontier
    return count


def _task_id(page_id: str) -> str:
    return page_id.replace("/", "__").replace(":", "_")


__all__ = ["CoveragePlanner"]
