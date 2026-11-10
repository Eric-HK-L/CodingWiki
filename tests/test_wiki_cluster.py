"""S37 单元测试：模块三信号聚类（cluster.py）与页面体系 v2 规划（优化架构 §14）。

覆盖：划分不变量、确定性、构建拆分、协作阈值、分层（链/环）、
ensure_module_map 复用与失效、concept_groups/name_tokens、
规划器新增 flow-detail/concept 任务、scope_files 新类型分支。
"""

from __future__ import annotations

import pytest

from codingwiki.domain.facts import (
    BuildTarget,
    CallFact,
    EntryFact,
    FileFacts,
    RepoFacts,
    SymbolFact,
    SymbolKind,
)
from codingwiki.domain.wiki import PageTask, PageType
from codingwiki.errors import InternalError
from codingwiki.generate.cluster import (
    build_module_map,
    ensure_module_map,
    validate_module_map,
)
from codingwiki.generate.plan import CoveragePlanner, concept_groups, name_tokens, scope_files


def _symbol(path: str, name: str, line: int = 1) -> SymbolFact:
    return SymbolFact(
        symbol_id=f"{path}::{name}@{line}",
        name=name,
        qualified_name=name,
        kind=SymbolKind.FUNCTION,
        path=path,
        line=line,
        end_line=line + 2,
    )


def _facts(
    files: list[FileFacts],
    targets: list[BuildTarget] | None = None,
    revision: str = "rev1",
) -> RepoFacts:
    return RepoFacts(
        repo_id="r",
        snapshot_id=f"r@{revision}",
        revision=revision,
        extractor_version="facts-v1",
        files=files,
        build_targets=targets or [],
    )


def _file(path: str, **kwargs: object) -> FileFacts:
    return FileFacts(path=path, language="c", blob_sha256=f"sha-{path}", **kwargs)  # type: ignore[arg-type]


def _chain_facts() -> RepoFacts:
    """两个目录模块，src/a → src/b 单向调用 6 次（超协作阈值）。"""
    a_calls = [
        CallFact(
            path="src/a/a1.c",
            line=i + 1,
            caller=f"src/a/a1.c::fa@{i + 1}",
            callee="fb",
            targets=["src/b/b1.c::fb@1"],
        )
        for i in range(6)
    ]
    return _facts(
        [
            _file(
                "src/a/a1.c",
                symbols=[_symbol("src/a/a1.c", f"fa{i}", i + 1) for i in range(6)],
                calls=a_calls,
            ),
            _file("src/b/b1.c", symbols=[_symbol("src/b/b1.c", "fb", 1)]),
        ]
    )


# ---------------------------------------------------------------- 聚类


def test_partition_and_determinism() -> None:
    facts = _chain_facts()
    first = build_module_map(facts)
    second = build_module_map(facts)
    assert first.model_dump_json() == second.model_dump_json()
    keys = first.module_keys
    assert set(keys) == {"src/a", "src/b"}
    all_files = sorted(p for e in first.entries for p in e.files)
    assert all_files == ["src/a/a1.c", "src/b/b1.c"]


def test_collaboration_over_threshold() -> None:
    mm = build_module_map(_chain_facts())
    assert len(mm.collaborations) == 1
    pair = mm.collaborations[0]
    assert {pair.a, pair.b} == {"src/a", "src/b"}
    assert pair.calls == 6
    assert pair.call_density == pytest.approx(6.0)


def test_collaboration_under_threshold_excluded() -> None:
    facts = _facts(
        [
            _file(
                "src/a/a1.c",
                symbols=[_symbol("src/a/a1.c", "fa", 1)],
                calls=[
                    CallFact(
                        path="src/a/a1.c",
                        line=1,
                        caller="src/a/a1.c::fa@1",
                        callee="fb",
                        targets=["src/b/b1.c::fb@1"],
                    )
                ],
            ),
            _file("src/b/b1.c", symbols=[_symbol("src/b/b1.c", "fb", 1)]),
        ]
    )
    mm = build_module_map(facts)
    assert mm.collaborations == []


def test_build_target_split() -> None:
    files = [_file(f"src/m/{i}.c") for i in range(10)]
    targets = [
        BuildTarget(name="t1", sources=[f"src/m/{i}.c" for i in range(4)]),
        BuildTarget(name="t2", sources=[f"src/m/{i}.c" for i in range(4, 8)]),
    ]
    mm = build_module_map(_facts(files, targets))
    keys = set(mm.module_keys)
    assert "src/m--t1" in keys
    assert "src/m--t2" in keys
    base = mm.entry("src/m")
    assert base is not None and base.files == ["src/m/8.c", "src/m/9.c"]
    sub = mm.entry("src/m--t1")
    assert sub is not None and sub.source_signals == ["dir", "build"]
    # 不变量仍成立：全部文件恰好归属一次
    all_files = sorted(p for e in mm.entries for p in e.files)
    assert all_files == sorted(f.path for f in files)


def test_build_target_split_not_triggered_with_single_big_group() -> None:
    files = [_file(f"src/m/{i}.c") for i in range(6)]
    targets = [BuildTarget(name="t1", sources=[f"src/m/{i}.c" for i in range(4)])]
    mm = build_module_map(_facts(files, targets))
    assert mm.module_keys == ["src/m"]


def test_layers_chain_and_cycle() -> None:
    # a 调 b、b 调 c：a 在最上层（依赖链最长）
    calls_ab = [
        CallFact(
            path="src/a/x.c",
            line=1,
            caller="src/a/x.c::fa@1",
            callee="fb",
            targets=["src/b/y.c::fb@1"],
        )
    ]
    calls_bc = [
        CallFact(
            path="src/b/y.c",
            line=1,
            caller="src/b/y.c::fb@1",
            callee="fc",
            targets=["src/c/z.c::fc@1"],
        )
    ]
    facts = _facts(
        [
            _file("src/a/x.c", symbols=[_symbol("src/a/x.c", "fa")], calls=calls_ab),
            _file("src/b/y.c", symbols=[_symbol("src/b/y.c", "fb")], calls=calls_bc),
            _file("src/c/z.c", symbols=[_symbol("src/c/z.c", "fc")]),
        ]
    )
    mm = build_module_map(facts)
    assert mm.entry("src/c").layer == "L0"
    assert mm.entry("src/b").layer == "L1"
    assert mm.entry("src/a").layer == "L2"


def test_layers_cycle_same_layer() -> None:
    ab = CallFact(
        path="src/a/x.c", line=1, caller="src/a/x.c::fa@1", callee="fb",
        targets=["src/b/y.c::fb@1"],
    )
    ba = CallFact(
        path="src/b/y.c", line=1, caller="src/b/y.c::fb@1", callee="fa",
        targets=["src/a/x.c::fa@1"],
    )
    facts = _facts(
        [
            _file("src/a/x.c", symbols=[_symbol("src/a/x.c", "fa")], calls=[ab]),
            _file("src/b/y.c", symbols=[_symbol("src/b/y.c", "fb")], calls=[ba]),
        ]
    )
    mm = build_module_map(facts)
    assert mm.entry("src/a").layer == mm.entry("src/b").layer


def test_validate_rejects_broken_partition() -> None:
    facts = _chain_facts()
    mm = build_module_map(facts)
    broken = mm.model_copy(
        update={"entries": [*mm.entries, mm.entries[0]]}  # 重复归属
    )
    with pytest.raises(InternalError, match="重复归属"):
        validate_module_map(broken, facts)


def test_ensure_module_map_reuse_and_invalidation(tmp_path) -> None:
    facts = _chain_facts()
    built = ensure_module_map(facts, tmp_path)
    assert (tmp_path / "module_map.json").is_file()
    again = ensure_module_map(facts, tmp_path)
    assert again.model_dump_json() == built.model_dump_json()
    changed = _chain_facts().model_copy(update={"snapshot_id": "r@rev2", "revision": "rev2"})
    rebuilt = ensure_module_map(changed, tmp_path)
    assert rebuilt.snapshot_id == "r@rev2"


# ---------------------------------------------------------------- 概念聚类


def test_name_tokens() -> None:
    assert name_tokens("PuschConfig") == {"pusch", "config"}
    assert name_tokens("LDPC_decoder") == {"ldpc", "decoder"}
    assert name_tokens("XMLParser2") == {"xml", "parser2"}


def test_concept_groups_min_terms() -> None:
    files = [
        _file(
            "src/a/x.c",
            symbols=[
                SymbolFact(
                    symbol_id=f"src/a/x.c::{name}@{i}",
                    name=name,
                    qualified_name=name,
                    kind=SymbolKind.STRUCT,
                    path="src/a/x.c",
                    line=i,
                    end_line=i + 1,
                )
                for i, name in enumerate(["PuschConfig", "PuschState", "PuschResult"], start=1)
            ],
        )
    ]
    groups = concept_groups(_facts(files))
    assert groups.get("pusch") == ["src/a/x.c"]
    assert "config" not in groups  # 只出现一次，低于阈值


# ---------------------------------------------------------------- 规划器


def _planner_facts() -> RepoFacts:
    entries = [EntryFact(path="src/app/main.c", line=1, name="main", kind="main")]
    call = CallFact(
        path="src/app/main.c",
        line=6,
        caller="",
        callee="run_pusch",
        targets=["src/phy/run.c::run_pusch@1"],
    )
    symbols = [
        SymbolFact(
            symbol_id=f"src/app/main.c::{name}@{i}",
            name=name,
            qualified_name=name,
            kind=SymbolKind.STRUCT,
            path="src/app/main.c",
            line=i,
            end_line=i + 1,
        )
        for i, name in enumerate(["PuschConfig", "PuschState", "PuschResult"], start=2)
    ]
    return _facts(
        [
            _file("src/app/main.c", symbols=symbols, entries=entries, calls=[call]),
            _file("src/phy/run.c", symbols=[_symbol("src/phy/run.c", "run_pusch", 1)]),
        ]
    )


def test_plan_includes_flow_detail_and_concept() -> None:
    plan = CoveragePlanner().plan(_planner_facts())
    by_type: dict[PageType, list[str]] = {}
    for task in plan.tasks:
        by_type.setdefault(task.page_type, []).append(task.page_id)
    assert by_type.get(PageType.FLOW_DETAIL) == ["flow-detail/src/app/main"]
    assert "concept/pusch" in by_type.get(PageType.CONCEPT, [])


def test_scope_files_concept_and_flow_detail() -> None:
    facts = _planner_facts()
    plan = CoveragePlanner().plan(facts)
    concept = next(t for t in plan.tasks if t.page_type is PageType.CONCEPT)
    flow_detail = next(t for t in plan.tasks if t.page_type is PageType.FLOW_DETAIL)
    assert scope_files(concept, facts) == ["src/app/main.c"]
    assert scope_files(flow_detail, facts) == ["src/app/main.c"]


def test_scope_files_module_uses_module_map() -> None:
    files = [_file(f"src/m/{i}.c") for i in range(10)]
    targets = [
        BuildTarget(name="t1", sources=[f"src/m/{i}.c" for i in range(4)]),
        BuildTarget(name="t2", sources=[f"src/m/{i}.c" for i in range(4, 8)]),
    ]
    facts = _facts(files, targets)
    mm = build_module_map(facts)
    task = PageTask(
        task_id="t",
        page_id="module/src/m--t1",
        page_type=PageType.MODULE,
        title_hint="t",
        subject="t",
        scope="src/m--t1",
    )
    assert scope_files(task, facts, mm) == [f"src/m/{i}.c" for i in range(4)]
