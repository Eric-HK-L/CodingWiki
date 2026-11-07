"""S13 证据与影响分析测试：六类证据、快照纪律、零证据、确定性、CLI 端到端。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codingwiki.domain.facts import (
    BuildTarget,
    CallFact,
    ConfigFact,
    FileFacts,
    RepoFacts,
    SymbolFact,
    SymbolKind,
)
from codingwiki.domain.facts import (
    TestFact as _TestFact,
)
from codingwiki.domain.spec import (
    DocSection,
    EvidenceBundle,
    EvidenceItem,
    EvidenceKind,
    ExtractionResult,
    Requirement,
    RequirementEvidence,
    RequirementKind,
)
from codingwiki.errors import InputError
from codingwiki.facts import FilesystemFactStore
from codingwiki.spec.evidence import (
    WikiPageRef,
    collect_evidence,
    save_evidence,
    validate_bundle,
)
from codingwiki.spec.impact import build_impact
from codingwiki.spec.ingest import save_extraction

SNAP = "demo@rev"
_MAIN = "src/app/main.cpp::main@8"
_SET_LEVEL = "src/app/log.cpp::set_log_level@12"
_PARSE_LEVEL = "src/app/log.cpp::parse_level@30"
_SIMILAR = "src/other/log.cpp::set_log_level@10"


def _facts() -> RepoFacts:
    log_file = FileFacts(
        path="src/app/log.cpp",
        language="cpp",
        blob_sha256="a" * 64,
        module="src/app",
        symbols=[
            SymbolFact(
                symbol_id=_SET_LEVEL,
                name="set_log_level",
                qualified_name="set_log_level",
                kind=SymbolKind.FUNCTION,
                path="src/app/log.cpp",
                line=12,
                end_line=20,
                signature="void set_log_level(int level)",
            ),
            SymbolFact(
                symbol_id=_PARSE_LEVEL,
                name="parse_level",
                qualified_name="parse_level",
                kind=SymbolKind.FUNCTION,
                path="src/app/log.cpp",
                line=30,
                end_line=35,
            ),
        ],
    )
    main_file = FileFacts(
        path="src/app/main.cpp",
        language="cpp",
        blob_sha256="b" * 64,
        module="src/app",
        symbols=[
            SymbolFact(
                symbol_id=_MAIN,
                name="main",
                qualified_name="main",
                kind=SymbolKind.FUNCTION,
                path="src/app/main.cpp",
                line=8,
                end_line=40,
            )
        ],
        calls=[
            CallFact(
                path="src/app/main.cpp",
                line=20,
                caller=_MAIN,
                callee="set_log_level",
                targets=[_SET_LEVEL],
            )
        ],
    )
    other_file = FileFacts(
        path="src/other/log.cpp",
        language="cpp",
        blob_sha256="c" * 64,
        module="src/other",
        symbols=[
            SymbolFact(
                symbol_id=_SIMILAR,
                name="set_log_level",
                qualified_name="set_log_level",
                kind=SymbolKind.FUNCTION,
                path="src/other/log.cpp",
                line=10,
                end_line=18,
            )
        ],
    )
    paths = [
        "src/app/log.cpp",
        "src/app/main.cpp",
        "src/other/log.cpp",
        "configs/app.yaml",
        "CMakeLists.txt",
        "test/log_test.cpp",
    ]
    return RepoFacts(
        snapshot_id=SNAP,
        repo_id="demo",
        revision="rev",
        extractor_version="test-v1",
        files=[log_file, main_file, other_file],
        build_targets=[
            BuildTarget(
                name="app",
                kind="executable",
                path="CMakeLists.txt",
                line=10,
                sources=["src/app/log.cpp", "src/app/main.cpp"],
            )
        ],
        config_facts=[
            ConfigFact(key="log_level", kind="file", format="yaml", path="configs/app.yaml")
        ],
        tests=[
            _TestFact(
                name="test_log_level",
                framework="gtest",
                path="test/log_test.cpp",
                line=10,
                target="app",
            )
        ],
        blobs={path: "d" * 64 for path in paths},
    )


def _requirement(requirement_id: str = "R-001") -> Requirement:
    return Requirement(
        id=requirement_id,
        kind=RequirementKind.FUNCTIONAL,
        text="新增配置文件中的日志级别配置项 log_level，默认 info",
        priority="must",
        source_section="1 需求",
        source_quote="新增配置文件中的日志级别配置项",
    )


def _pages() -> list[WikiPageRef]:
    return [
        WikiPageRef(
            page_id="module/log",
            title="日志级别",
            text="set_log_level 负责日志级别；本文介绍日志级别配置。",
            snapshot_id=SNAP,
        )
    ]


def test_collects_all_evidence_kinds_and_impact() -> None:
    bundle = collect_evidence(
        repo_id="demo",
        doc_id="demo@sha",
        doc_title="日志级别需求",
        requirements=[_requirement()],
        facts=_facts(),
        pages=_pages(),
    )
    evidence = bundle.requirements[0]
    kinds = {item.kind for item in evidence.items}
    assert kinds == {
        EvidenceKind.SYMBOL,
        EvidenceKind.CALLER,
        EvidenceKind.CONFIG,
        EvidenceKind.BUILD,
        EvidenceKind.TEST,
        EvidenceKind.SIMILAR,
        EvidenceKind.WIKI,
    }
    assert evidence.impact.files == ["configs/app.yaml", "src/app/log.cpp"]
    assert evidence.impact.callers == [_MAIN]
    assert evidence.impact.config == ["log_level"]
    assert evidence.impact.build == ["app"]
    assert evidence.impact.tests == ["test_log_level"]
    assert _SET_LEVEL in evidence.impact.interfaces
    assert all(item.snapshot_id == SNAP for item in evidence.items)


def test_same_input_is_byte_stable() -> None:
    first = collect_evidence(
        repo_id="demo",
        doc_id="demo@sha",
        doc_title="日志级别需求",
        requirements=[_requirement()],
        facts=_facts(),
        pages=_pages(),
    )
    second = collect_evidence(
        repo_id="demo",
        doc_id="demo@sha",
        doc_title="日志级别需求",
        requirements=[_requirement()],
        facts=_facts(),
        pages=_pages(),
    )
    assert first.model_dump_json() == second.model_dump_json()


def _bundle(item: EvidenceItem) -> EvidenceBundle:
    return EvidenceBundle(
        repo_id="demo",
        snapshot_id=SNAP,
        doc_id="demo@sha",
        requirements=[
            RequirementEvidence(
                requirement_id="R-001",
                items=[item],
                impact=build_impact(requirement_id="R-001"),
            )
        ],
    )


def test_missing_symbol_is_rejected() -> None:
    item = EvidenceItem(
        kind=EvidenceKind.SYMBOL,
        path="src/app/log.cpp",
        line=1,
        symbol_id="src/app/log.cpp::ghost@99",
        snapshot_id=SNAP,
    )
    with pytest.raises(InputError, match="不存在符号"):
        validate_bundle(_bundle(item), _facts())


def test_path_outside_snapshot_is_rejected() -> None:
    item = EvidenceItem(
        kind=EvidenceKind.CONFIG,
        path="ghost.yaml",
        line=1,
        snapshot_id=SNAP,
    )
    with pytest.raises(InputError, match="悬空路径"):
        validate_bundle(_bundle(item), _facts())


def test_stale_wiki_page_is_rejected() -> None:
    stale = WikiPageRef(
        page_id="module/log", title="日志级别", text="日志级别", snapshot_id="demo@old"
    )
    item = EvidenceItem(kind=EvidenceKind.WIKI, page_id="module/log", snapshot_id="demo@old")
    with pytest.raises(InputError, match="不一致"):
        validate_bundle(_bundle(item), _facts(), [stale])


def test_requirement_without_evidence_is_rejected() -> None:
    empty = Requirement(
        id="R-001",
        kind=RequirementKind.FUNCTIONAL,
        text="zzz qqq",
        priority="must",
        source_section="1 需求",
        source_quote="zzz",
    )
    with pytest.raises(InputError, match="没有任何可定位证据"):
        collect_evidence(
            repo_id="demo",
            doc_id="demo@sha",
            doc_title="zzz",
            requirements=[empty],
            facts=_facts(),
        )


def test_snapshot_mismatch_is_rejected() -> None:
    with pytest.raises(InputError, match="不一致"):
        collect_evidence(
            repo_id="demo",
            doc_id="demo@sha",
            doc_title="日志级别需求",
            requirements=[_requirement()],
            facts=_facts(),
            snapshot_id="demo@other",
        )


def _env(home: Path) -> dict[str, str]:
    repo_root = Path(__file__).resolve().parents[1]
    return {
        **os.environ,
        "CODINGWIKI_HOME": str(home),
        "PYTHONPATH": str(repo_root / "src"),
    }


def test_cli_evidence_end_to_end(tmp_path: Path) -> None:
    home = tmp_path / ".codingwiki"
    FilesystemFactStore(base_dir=home).save(_facts())
    extraction = ExtractionResult(
        repo_id="demo",
        doc_id="demo@sha",
        doc_sha256="e" * 64,
        source_path="req.md",
        title="日志级别需求",
        sections=[DocSection(path="1 需求", title="1 需求", level=1, start_line=1, end_line=5)],
        requirements=[_requirement()],
    )
    extract_path = save_extraction(extraction, home)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "codingwiki",
            "spec",
            "evidence",
            "--repo",
            "demo",
            "--spec",
            extraction.doc_id,
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        env=_env(home),
    )
    assert result.returncode == 0, result.stderr
    assert "证据包" in result.stdout
    assert "悬空引用 0" in result.stdout
    output = extract_path.parent / "evidence.json"
    assert output.is_file()
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["snapshot_id"] == SNAP
    assert payload["requirements"][0]["requirement_id"] == "R-001"

    mismatch = subprocess.run(
        [
            sys.executable,
            "-m",
            "codingwiki",
            "spec",
            "evidence",
            "--repo",
            "demo",
            "--spec",
            extraction.doc_id,
            "--snapshot",
            "demo@other",
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        env=_env(home),
    )
    assert mismatch.returncode == 2
    assert "不一致" in mismatch.stderr


def test_cli_evidence_accepts_explicit_spec_path(tmp_path: Path) -> None:
    home = tmp_path / ".codingwiki"
    FilesystemFactStore(base_dir=home).save(_facts())
    extraction = ExtractionResult(
        repo_id="demo",
        doc_id="demo@sha",
        doc_sha256="f" * 64,
        source_path="req.md",
        title="日志级别需求",
        sections=[DocSection(path="1 需求", title="1 需求", level=1, start_line=1, end_line=5)],
        requirements=[_requirement()],
    )
    spec_dir = tmp_path / "handmade-spec"
    extraction_path = spec_dir / "extract.json"
    spec_dir.mkdir()
    extraction_path.write_text(extraction.model_dump_json(indent=2), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "codingwiki",
            "spec",
            "evidence",
            "--repo",
            "demo",
            "--spec",
            str(extraction_path),
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        env=_env(home),
    )
    assert result.returncode == 0, result.stderr
    assert (spec_dir / "evidence.json").is_file()


def test_save_evidence_is_reproducible(tmp_path: Path) -> None:
    bundle = collect_evidence(
        repo_id="demo",
        doc_id="demo@sha",
        doc_title="日志级别需求",
        requirements=[_requirement()],
        facts=_facts(),
        pages=_pages(),
    )
    first = save_evidence(bundle, tmp_path)
    second = save_evidence(bundle, tmp_path)
    assert first.read_bytes() == second.read_bytes()
