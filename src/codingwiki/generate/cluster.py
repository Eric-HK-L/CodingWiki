"""模块三信号聚类（S37，优化架构 v1.0 §14.1）。

从事实层确定性构建 ModuleMap：
1. 目录边界：复用 `modules.module_groups` 作为基础分区（目录是最外层边界，不被打破）；
2. 构建拆分：模块内文件按构建目标归属分组，多个大 target 组时拆子模块 `<module>--<target>`；
3. 调用协作：跨模块已解析调用聚合成模块对，超阈值的入 collaborations（互链，不重分区）；
4. 分层：模块依赖有向图 SCC 凝聚 + 最长路径深度，`layer = "L<depth>"`，环内同层。

全部规则确定性：无随机、无时间、无外部输入；同输入字节级一致。
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from codingwiki.domain.facts import FileFacts, RepoFacts
from codingwiki.domain.module_map import ModuleEntry, ModuleMap, ModulePair
from codingwiki.errors import InternalError
from codingwiki.generate.modules import module_groups

CLUSTER_VERSION = "1"

_MIN_COLLAB_CALLS = 5
_MIN_COLLAB_DENSITY = 1.0
_TARGET_KEY_SAFE = re.compile(r"[^A-Za-z0-9_.+-]+")


def build_module_map(facts: RepoFacts) -> ModuleMap:
    """三信号确定性合并（优化架构 §14.1）。构建后强制校验划分不变量。"""
    groups = module_groups(facts)
    file_targets = _file_targets(facts)
    entries = _split_by_build_target(groups, file_targets)
    file_module = {path: key for key, entry in entries.items() for path in entry.files}
    pair_calls, internal_calls = _cross_module_calls(facts, file_module)
    for key, entry in entries.items():
        entry.cohesion_score = round(internal_calls.get(key, 0) / max(1, len(entry.files)), 3)
    collaborations = _collaborations(pair_calls, entries)
    layers = _layers(pair_calls, sorted(entries))
    result = ModuleMap(
        repo_id=facts.repo_id,
        snapshot_id=facts.snapshot_id,
        cluster_version=CLUSTER_VERSION,
        entries=[
            ModuleEntry(
                module_key=key,
                layer=layers.get(key, "L0"),
                files=entry.files,
                build_targets=entry.build_targets,
                cohesion_score=entry.cohesion_score,
                source_signals=entry.source_signals,
            )
            for key, entry in sorted(entries.items())
        ],
        collaborations=collaborations,
    )
    validate_module_map(result, facts)
    return result


def validate_module_map(module_map: ModuleMap, facts: RepoFacts) -> None:
    """归属唯一性（module_membership_unique）：entries.files 是在范围文件集的划分。"""
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for entry in module_map.entries:
        for path in entry.files:
            if path in seen:
                duplicates.append(f"{path}（{seen[path]} 与 {entry.module_key}）")
            seen[path] = entry.module_key
    expected = sorted({file.path for file in facts.files})
    actual = sorted(seen)
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    problems: list[str] = []
    if duplicates:
        problems.append(f"重复归属: {', '.join(duplicates[:5])}")
    if missing:
        problems.append(f"未归属: {', '.join(missing[:5])}")
    if extra:
        problems.append(f"越界文件: {', '.join(extra[:5])}")
    if problems:
        raise InternalError("ModuleMap 划分不变量破坏——" + "；".join(problems))


def ensure_module_map(facts: RepoFacts, wiki_dir: Path) -> ModuleMap:
    """按快照复用 `wiki/<repo>/module_map.json`，不一致则重建并原子写盘。"""
    path = wiki_dir / "module_map.json"
    if path.is_file():
        try:
            cached = ModuleMap.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            cached = None  # 损坏或不兼容的旧文件：重建覆盖
        if (
            cached is not None
            and cached.snapshot_id == facts.snapshot_id
            and cached.cluster_version == CLUSTER_VERSION
        ):
            validate_module_map(cached, facts)
            return cached
    built = build_module_map(facts)
    wiki_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(built.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return built


def files_for_module(module_map: ModuleMap, module_key: str) -> list[str]:
    entry = module_map.entry(module_key)
    return list(entry.files) if entry else []


# ---------------------------------------------------------------- 内部


class _EntryDraft:
    __slots__ = ("files", "build_targets", "cohesion_score", "source_signals")

    def __init__(
        self,
        files: list[str],
        build_targets: list[str],
        source_signals: list[str],
    ) -> None:
        self.files = files
        self.build_targets = build_targets
        self.source_signals = source_signals
        self.cohesion_score = 0.0


def _file_targets(facts: RepoFacts) -> dict[str, list[str]]:
    """文件路径 → 声明它为 source 的构建目标名（排序去重）。"""
    mapping: dict[str, set[str]] = {}
    for target in facts.build_targets:
        for source in target.sources:
            mapping.setdefault(source, set()).add(target.name)
    return {path: sorted(names) for path, names in mapping.items()}


def _split_by_build_target(
    groups: dict[str, list[FileFacts]],
    file_targets: dict[str, list[str]],
) -> dict[str, _EntryDraft]:
    """信号 1+2：目录基础分区，模块内多个大 target 组时拆子模块。"""
    entries: dict[str, _EntryDraft] = {}
    for key, grouped in sorted(groups.items()):
        # 事实层允许同一路径出现多条 FileFacts（如多个构建目标各解析一次），按路径去重
        files = list({file.path: file for file in grouped}.values())
        by_target: dict[str, list[str]] = {}
        rest: list[str] = []
        for file in files:
            targets = file_targets.get(file.path, [])
            if targets:
                # 文件被多个 target 引用时归字典序首个，保证确定性与划分不变量
                by_target.setdefault(targets[0], []).append(file.path)
            else:
                rest.append(file.path)
        threshold = max(3, math.ceil(len(files) * 0.2))
        big = {name: paths for name, paths in by_target.items() if len(paths) >= threshold}
        if len(big) < 2:
            entries[key] = _EntryDraft(
                files=sorted(file.path for file in files),
                build_targets=sorted({name for paths in by_target.values() for name in paths}),
                source_signals=["dir"],
            )
            continue
        for name, paths in sorted(big.items()):
            sub_key = f"{key}--{_TARGET_KEY_SAFE.sub('-', name)}"
            entries[sub_key] = _EntryDraft(
                files=sorted(paths),
                build_targets=[name],
                source_signals=["dir", "build"],
            )
        small = [path for name, paths in by_target.items() if name not in big for path in paths]
        base = sorted(rest + small)
        if base:
            entries[key] = _EntryDraft(
                files=base,
                build_targets=sorted(name for name in by_target if name not in big),
                source_signals=["dir"],
            )
    return entries


def _cross_module_calls(
    facts: RepoFacts,
    file_module: dict[str, str],
) -> tuple[dict[tuple[str, str], int], dict[str, int]]:
    """跨模块已解析调用计数 + 模块内调用计数（后者供 cohesion_score）。"""
    symbol_module: dict[str, str] = {}
    for file in facts.files:
        module = file_module.get(file.path)
        if module is None:
            continue
        for symbol in file.symbols:
            symbol_module.setdefault(symbol.symbol_id, module)
    pairs: dict[tuple[str, str], int] = {}
    internal: dict[str, int] = {}
    for file in facts.files:
        source_module = file_module.get(file.path)
        if source_module is None:
            continue
        for call in file.calls:
            for target in call.targets:
                target_module = symbol_module.get(target)
                if not target_module:
                    continue
                if target_module == source_module:
                    internal[source_module] = internal.get(source_module, 0) + 1
                else:
                    key = (source_module, target_module)
                    pairs[key] = pairs.get(key, 0) + 1
    return pairs, internal


def _collaborations(
    pair_calls: dict[tuple[str, str], int],
    entries: dict[str, _EntryDraft],
) -> list[ModulePair]:
    """信号 3：双向合计调用数与密度均超阈值的模块对（键按字典序归一）。"""
    merged: dict[tuple[str, str], int] = {}
    for (a, b), count in pair_calls.items():
        key = (a, b) if a < b else (b, a)
        merged[key] = merged.get(key, 0) + count
    result: list[ModulePair] = []
    for (a, b), calls in sorted(merged.items()):
        size_a = len(entries[a].files)
        size_b = len(entries[b].files)
        density = calls / max(1, min(size_a, size_b))
        if calls >= _MIN_COLLAB_CALLS and density >= _MIN_COLLAB_DENSITY:
            result.append(ModulePair(a=a, b=b, calls=calls, call_density=round(density, 3)))
    return result


def _layers(
    pair_calls: dict[tuple[str, str], int],
    module_keys: list[str],
) -> dict[str, str]:
    """SCC 凝聚 + 最长路径深度：被调方在下层，环内同层。全部按字典序遍历保证确定性。"""
    edges: dict[str, set[str]] = {key: set() for key in module_keys}
    for source, target in pair_calls:
        edges[source].add(target)
    components = _tarjan_scc(edges, module_keys)
    comp_of = {key: comp for key, comp in components.items()}
    comp_count = len(set(components.values()))
    comp_succ: dict[int, set[int]] = {index: set() for index in range(comp_count)}
    for source, targets in edges.items():
        for target in targets:
            if comp_of[source] != comp_of[target]:
                comp_succ[comp_of[source]].add(comp_of[target])
    # 逆拓扑序求深度：无出边（不依赖任何模块）的为 L0
    depth = _longest_depth(comp_succ, comp_count)
    return {key: f"L{depth[comp_of[key]]}" for key in module_keys}


def _tarjan_scc(edges: dict[str, set[str]], keys: list[str]) -> dict[str, int]:
    """迭代版 Tarjan，邻居按字典序展开保证确定性输出。"""
    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: dict[str, int] = {}
    comp_count = 0
    counter = 0
    for root in keys:
        if root in index_of:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, child_index = work[-1]
            if child_index == 0:
                index_of[node] = lowlink[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            neighbors = sorted(edges[node])
            if child_index < len(neighbors):
                work[-1] = (node, child_index + 1)
                target = neighbors[child_index]
                if target not in index_of:
                    work.append((target, 0))
                elif target in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[target])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[node])
                if lowlink[node] == index_of[node]:
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        components[member] = comp_count
                        if member == node:
                            break
                    comp_count += 1
    return components


def _longest_depth(comp_succ: dict[int, set[int]], comp_count: int) -> list[int]:
    """凝聚 DAG 上「依赖链最长深度」：depth(c) = 1 + max(depth(后继))，无后继为 0。"""
    memo: dict[int, int] = {}

    def depth(comp: int) -> int:
        if comp in memo:
            return memo[comp]
        memo[comp] = 0  # 凝聚后无环，此占位防重入
        result = 0
        for succ in sorted(comp_succ[comp]):
            result = max(result, depth(succ) + 1)
        memo[comp] = result
        return result

    return [depth(comp) for comp in range(comp_count)]


__all__ = [
    "CLUSTER_VERSION",
    "build_module_map",
    "ensure_module_map",
    "files_for_module",
    "validate_module_map",
]
