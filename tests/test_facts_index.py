"""FactIndex 单元测试（S7 §16.3-1：结构化事实查询 API）。"""

from __future__ import annotations

import pytest

from codingwiki.domain.facts import (
    BuildTarget,
    CallFact,
    FileFacts,
    RepoFacts,
    SymbolFact,
    SymbolKind,
)
from codingwiki.domain.facts import TestFact as FactsTest  # 避免被 pytest 误收集为测试类
from codingwiki.errors import InputError
from codingwiki.facts import FactIndex


def _symbol(path: str, name: str, line: int, kind: SymbolKind = SymbolKind.FUNCTION) -> SymbolFact:
    return SymbolFact(
        symbol_id=f"{path}::{name}@{line}",
        name=name,
        qualified_name=name,
        kind=kind,
        path=path,
        line=line,
        end_line=line + 10,
    )


def _facts() -> RepoFacts:
    core = FileFacts(
        path="src/core.c",
        language="c",
        blob_sha256="a" * 64,
        module="core",
        symbols=[
            _symbol("src/core.c", "core_init", 10),
            _symbol("src/core.c", "core_run", 30),
            _symbol("src/core.c", "CoreCtx", 50, kind=SymbolKind.STRUCT),
        ],
        calls=[
            CallFact(
                path="src/core.c",
                line=35,
                caller="src/core.c::core_run@30",
                callee="core_init",
                targets=["src/core.c::core_init@10"],
            ),
            CallFact(path="src/core.c", line=36, callee="log", targets=[]),
        ],
    )
    app = FileFacts(
        path="src/app/main.c",
        language="c",
        blob_sha256="b" * 64,
        module="app",
        symbols=[_symbol("src/app/main.c", "main", 1)],
        calls=[
            CallFact(
                path="src/app/main.c",
                line=5,
                caller="src/app/main.c::main@1",
                callee="core_run",
                targets=["src/core.c::core_run@30"],
            ),
            CallFact(
                path="src/app/main.c",
                line=6,
                caller="src/app/main.c::main@1",
                callee="core_init",
                targets=["src/core.c::core_init@10"],
            ),
        ],
    )
    return RepoFacts(
        snapshot_id="demo@abc123",
        repo_id="demo",
        revision="abc123",
        extractor_version="test",
        files=[core, app],
        build_targets=[
            BuildTarget(
                name="core", kind="library", path="src/CMakeLists.txt", sources=["src/core.c"]
            ),
            BuildTarget(
                name="app",
                kind="executable",
                path="src/app/CMakeLists.txt",
                sources=["src/app/main.c", "src/core.c"],
            ),
        ],
        tests=[
            FactsTest(name="test_core", framework="pytest", path="tests/test_core.py"),
            FactsTest(name="test_core_sub", framework="pytest", path="tests/core/test_more.py"),
            FactsTest(name="test_app", framework="pytest", path="tests/test_app.py"),
        ],
    )


def test_symbol_by_id_exact_match() -> None:
    idx = FactIndex(_facts())
    sym = idx.symbol_by_id("src/core.c::core_init@10")
    assert sym is not None
    assert sym.name == "core_init"
    assert idx.symbol_by_id("src/core.c::missing@1") is None


def test_symbols_in_module() -> None:
    idx = FactIndex(_facts())
    names = {s.name for s in idx.symbols_in_module("core")}
    assert names == {"core_init", "core_run", "CoreCtx"}
    assert idx.symbols_in_module("ghost") == []


def test_callers_of_reverse_call_graph() -> None:
    idx = FactIndex(_facts())
    callers = idx.callers_of("src/core.c::core_init@10")
    # core_init 被 core.c:35 与 main.c:6 两处调用
    assert len(callers) == 2
    assert {c.caller for c in callers} == {
        "src/core.c::core_run@30",
        "src/app/main.c::main@1",
    }
    # 未解析调用（targets 为空）不算任何符号的调用方
    assert idx.callers_of("nope::x@1") == []


def test_tests_for_path_prefix() -> None:
    idx = FactIndex(_facts())
    names = {t.name for t in idx.tests_for_path("tests")}
    assert names == {"test_core", "test_core_sub", "test_app"}
    assert {t.name for t in idx.tests_for_path("tests/core")} == {"test_core_sub"}
    assert idx.tests_for_path("tests/test_core.py")[0].name == "test_core"


def test_build_targets_touching() -> None:
    idx = FactIndex(_facts())
    targets = idx.build_targets_touching(["src/core.c"])
    assert [t.name for t in targets] == ["app", "core"]  # 排序稳定：app 在 core 前
    assert [t.name for t in idx.build_targets_touching(["docs/x.md"])] == []


def test_assert_symbol_exists_passes() -> None:
    idx = FactIndex(_facts())
    idx.assert_symbol_exists("src/core.c::core_init@10")
    idx.assert_symbol_exists("src/core.c::core_init@10", snapshot="demo@abc123")


def test_assert_symbol_exists_missing_symbol() -> None:
    idx = FactIndex(_facts())
    with pytest.raises(InputError, match="不存在符号"):
        idx.assert_symbol_exists("src/core.c::ghost@1")


def test_assert_symbol_exists_snapshot_mismatch() -> None:
    idx = FactIndex(_facts())
    with pytest.raises(InputError, match="不一致"):
        idx.assert_symbol_exists("src/core.c::core_init@10", snapshot="demo@other")


def test_index_bindings() -> None:
    idx = FactIndex(_facts())
    assert idx.repo_id == "demo"
    assert idx.snapshot_id == "demo@abc123"


def test_unresolved_calls_not_in_callers() -> None:
    idx = FactIndex(_facts())
    # 反查表里的调用必然已解析；未解析调用（targets 空）不进任何符号的调用方
    assert all(c.resolved for c in idx.callers_of("src/core.c::core_init@10"))
