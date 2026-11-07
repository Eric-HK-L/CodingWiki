"""S15 Spec 自动校验器测试：十项规则正反例、故障注入矩阵、CLI 退出码。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codingwiki.backends.mock import (
    FAIL_SPEC_BAD_TASK,
    FAIL_SPEC_DANGLING_LOCATION,
    FAIL_SPEC_MISSING_SECTION,
    FAIL_SPEC_NO_ROLLBACK,
    FAIL_SPEC_ORPHAN_REQUIREMENT,
    FAIL_SPEC_ORPHAN_TASK,
    FAIL_SPEC_TERM_DRIFT,
    FAIL_SPEC_UNSOURCED_CLAIM,
    MockBackend,
)
from codingwiki.domain.facts import (
    BuildTarget,
    ConfigFact,
    FileFacts,
    RepoFacts,
    SymbolFact,
    SymbolKind,
)
from codingwiki.domain.facts import TestFact as _TestFact
from codingwiki.domain.spec import (
    SPEC_SECTIONS,
    AcceptanceCriterion,
    CandidateOption,
    DecisionRecord,
    DesignTask,
    EvidenceBundle,
    EvidenceItem,
    EvidenceKind,
    ExtractionResult,
    ImpactAnalysis,
    OpenQuestion,
    Requirement,
    RequirementEvidence,
    RequirementKind,
    RequirementPriority,
    SourceDocument,
    SpecDocument,
    SpecIssue,
    SpecSection,
    SpecStatus,
    TraceRow,
)
from codingwiki.spec import SpecOutcome, generate_spec
from codingwiki.spec.validator import validate_spec

SNAP = "demo@rev"
SYMBOL = "src/app/log.cpp::set_log_level@12"
DOC_ID = "demo@abc"


def _requirement(acceptance: bool = True) -> Requirement:
    return Requirement(
        id="R-001",
        kind=RequirementKind.FUNCTIONAL,
        text="在配置中新增日志级别配置项 log_level，默认 info",
        priority=RequirementPriority.MUST,
        acceptance=(
            [AcceptanceCriterion(id="AC-001", text="未配置时按 info 输出")] if acceptance else []
        ),
        source_section="1 功能需求",
        source_quote="在配置中新增日志级别配置项 log_level，默认 info",
    )


def _evidence(*, config: list[str] | None = None, snapshot: str = SNAP) -> EvidenceBundle:
    return EvidenceBundle(
        repo_id="demo",
        snapshot_id=SNAP,
        doc_id=DOC_ID,
        requirements=[
            RequirementEvidence(
                requirement_id="R-001",
                items=[
                    EvidenceItem(
                        kind=EvidenceKind.SYMBOL,
                        path="src/app/log.cpp",
                        line=12,
                        symbol_id=SYMBOL,
                        snapshot_id=snapshot,
                    )
                ],
                impact=ImpactAnalysis(
                    requirement_id="R-001",
                    files=["src/app/log.cpp"],
                    interfaces=[SYMBOL],
                    tests=["test_log_level"],
                    config=config or [],
                ),
            )
        ],
    )


def _extraction(requirements: list[Requirement] | None = None) -> ExtractionResult:
    return ExtractionResult(
        repo_id="demo",
        doc_id=DOC_ID,
        doc_sha256="a" * 64,
        source_path="requirements.md",
        title="日志级别需求",
        sections=[],
        requirements=requirements if requirements is not None else [_requirement()],
    )


def _sections(
    *,
    term: str = "术语：set_log_level。",
    current: str = "- R-001：`src/app/log.cpp:12`",
    impact: str = "- R-001：文件 src/app/log.cpp（依据 `src/app/log.cpp:12`）",
    tests: str = "- R-001：test_log_level",
    risk: str = (
        "风险：改动限定在影响面列出的文件；安全：沿用既有输入校验；回滚方式：按任务反向撤销提交。"
    ),
) -> list[SpecSection]:
    bodies = [
        "背景：为日志级别增加运行期配置。",
        term,
        current,
        "- R-001（must, functional）：新增 log_level 配置。",
        "候选方案：方案A / 方案B；选择方案A。",
        impact,
        tests,
        "- T-001：新增 log_level 配置读取（1 文件，120 行）。",
        risk,
        "无。",
        "| R-001 | 5 方案设计 | `src/app/log.cpp:12` | test_log_level | T-001 |",
    ]
    return [
        SpecSection(number=number, title=SPEC_SECTIONS[number], body=body)
        for number, body in enumerate(bodies, start=1)
    ]


def _spec(
    *,
    requirements: list[Requirement] | None = None,
    sections: list[SpecSection] | None = None,
    tasks: list[DesignTask] | None = None,
    trace: list[TraceRow] | None = None,
    decisions: list[DecisionRecord] | None = None,
    questions: list[OpenQuestion] | None = None,
    status: SpecStatus = SpecStatus.DRAFT,
    snapshot: str = SNAP,
) -> SpecDocument:
    return SpecDocument(
        spec_id="F-001",
        title="日志级别需求",
        snapshot_id=snapshot,
        source_doc=SourceDocument(path="requirements.md", doc_sha256="a" * 64, doc_id=DOC_ID),
        status=status,
        sections=sections or _sections(),
        requirements=requirements if requirements is not None else [_requirement()],
        questions=questions or [],
        tasks=tasks
        if tasks is not None
        else [
            DesignTask(
                id="T-001",
                goal="新增 log_level 配置读取",
                files=["src/app/log.cpp"],
                estimated_lines=120,
                acceptance=["未配置时按 info 输出"],
            )
        ],
        trace=trace
        if trace is not None
        else [
            TraceRow(
                requirement_id="R-001",
                design_section="5 方案设计",
                code_location="src/app/log.cpp:12",
                test="test_log_level",
                task="T-001",
            )
        ],
        decisions=decisions
        if decisions is not None
        else [
            DecisionRecord(
                id="D-001",
                title="实现路径选择",
                context="证据集中在现有模块。",
                candidates=[
                    CandidateOption(name="方案A", summary="扩展现有模块"),
                    CandidateOption(name="方案B", summary="新增独立模块"),
                ],
                choice="方案A",
                rationale="回归范围更小。",
            )
        ],
    )


def _rules(report_blockers: list[SpecIssue]) -> set[str]:
    return {issue.rule for issue in report_blockers}


def _valid_report():
    return validate_spec(_spec(), _extraction(), _evidence())


def test_valid_spec_has_no_findings() -> None:
    report = _valid_report()
    assert report.blockers == []
    assert report.warnings == []
    assert report.info == []


def test_requirement_coverage_blocks_missing_requirement() -> None:
    report = validate_spec(_spec(requirements=[]), _extraction(), _evidence())
    assert "requirement_coverage" in _rules(report.blockers)


def test_acceptance_criteria_blocks_functional_without_ac() -> None:
    report = validate_spec(
        _spec(requirements=[_requirement(acceptance=False)]),
        _extraction(requirements=[_requirement(acceptance=False)]),
        _evidence(),
    )
    assert "acceptance_criteria" in _rules(report.blockers)


def test_code_source_blocks_unsourced_claim() -> None:
    sections = _sections(current="- R-001：`src/nowhere/dangling.cpp:999` 是入口")
    report = validate_spec(_spec(sections=sections), _extraction(), _evidence())
    assert "code_source" in _rules(report.blockers)


def test_trace_completeness_blocks_orphan_requirement_and_task() -> None:
    report = validate_spec(_spec(trace=[]), _extraction(), _evidence())
    rules = _rules(report.blockers)
    assert "trace_completeness" in rules
    assert any("孤立任务" in issue.message for issue in report.blockers)


def test_trace_location_blocks_dangling_location() -> None:
    trace = [
        TraceRow(
            requirement_id="R-001",
            design_section="5 方案设计",
            code_location="src/nowhere/dangling.cpp:999",
            test="test_log_level",
            task="T-001",
        )
    ]
    report = validate_spec(_spec(trace=trace), _extraction(), _evidence())
    assert "trace_location" in _rules(report.blockers)


def test_task_size_blocks_missing_dependency() -> None:
    tasks = [
        DesignTask(
            id="T-001",
            goal="新增 log_level 配置读取",
            files=["src/app/log.cpp"],
            estimated_lines=120,
            acceptance=["未配置时按 info 输出"],
            depends_on=["T-999"],
        )
    ]
    report = validate_spec(_spec(tasks=tasks), _extraction(), _evidence())
    assert "task_size" in _rules(report.blockers)


def test_task_scope_blocks_files_outside_evidence() -> None:
    tasks = [
        DesignTask(
            id="T-001",
            goal="新增 log_level 配置读取",
            files=["src/other/module.cpp"],
            estimated_lines=120,
            acceptance=["未配置时按 info 输出"],
        )
    ]
    report = validate_spec(_spec(tasks=tasks), _extraction(), _evidence())
    assert "task_scope" in _rules(report.blockers)


def test_test_plan_blocks_missing_requirement_and_warns_placeholder() -> None:
    missing = _sections(tests="- 不适用：尚未规划测试")
    report = validate_spec(_spec(sections=missing), _extraction(), _evidence())
    assert "test_plan" in _rules(report.blockers)

    placeholder = [
        TraceRow(
            requirement_id="R-001",
            design_section="5 方案设计",
            code_location="src/app/log.cpp:12",
            test="待补充回归测试（现有快照未找到对应测试）",
            task="T-001",
        )
    ]
    report = validate_spec(_spec(trace=placeholder), _extraction(), _evidence())
    assert "test_plan" not in _rules(report.blockers)
    assert "test_plan" in {issue.rule for issue in report.warnings}


def test_status_questions_blocks_draft_with_open_question() -> None:
    questions = [OpenQuestion(id="Q-001", question="默认级别是否需要可配置？")]
    report = validate_spec(_spec(questions=questions), _extraction(), _evidence())
    assert "status_questions" in _rules(report.blockers)
    assert "status_questions" in {issue.rule for issue in report.info}


def test_term_consistency_blocks_case_drift() -> None:
    sections = _sections(term="术语：SET_LOG_LEVEL。")
    report = validate_spec(_spec(sections=sections), _extraction(), _evidence())
    assert "term_consistency" in _rules(report.blockers)


def test_security_review_warns_when_config_requirement_lacks_section() -> None:
    sections = _sections(risk="风险：改动限定在影响面列出的文件；回滚方式：按任务反向撤销提交。")
    report = validate_spec(
        _spec(sections=sections),
        _extraction(),
        _evidence(config=["log_level"]),
    )
    assert report.blockers == []
    assert "security_review" in {issue.rule for issue in report.warnings}


def test_rollback_blocks_missing_step() -> None:
    sections = _sections(risk="风险：改动限定在影响面列出的文件。")
    report = validate_spec(_spec(sections=sections), _extraction(), _evidence())
    assert "rollback" in _rules(report.blockers)


def test_decision_present_blocks_empty_decisions() -> None:
    report = validate_spec(_spec(decisions=[]), _extraction(), _evidence())
    assert "decision_present" in _rules(report.blockers)


def test_evidence_coverage_blocks_snapshot_mismatch() -> None:
    report = validate_spec(_spec(), _extraction(), _evidence(snapshot="other@rev"))
    assert "evidence_coverage" in _rules(report.blockers)


# ---------------------------------------------------------------- 故障注入矩阵


_REQ_DOC = """# 日志级别需求

## 1. 功能需求

### 1.1 配置项

在仓库配置中新增日志级别配置项 log_level，默认 info。

验收标准：

- 未配置该项时按 info 输出；
- 配置为 debug 时 debug 级日志可见。

### 1.2 运行期覆盖

允许通过环境变量覆盖配置文件的日志级别。

验收标准：

- 环境变量合法时生效。

## 2. 非功能需求

- 日志级别切换不需要重新编译。
"""


def _facts() -> RepoFacts:
    symbol = "src/app/log.cpp::set_log_level@12"
    files = [
        FileFacts(
            path="src/app/log.cpp",
            language="cpp",
            blob_sha256="a" * 64,
            module="src/app",
            symbols=[
                SymbolFact(
                    symbol_id=symbol,
                    name="set_log_level",
                    qualified_name="set_log_level",
                    kind=SymbolKind.FUNCTION,
                    path="src/app/log.cpp",
                    line=12,
                    end_line=20,
                )
            ],
        )
    ]
    paths = ["src/app/log.cpp", "configs/app.yaml", "CMakeLists.txt", "test/log_test.cpp"]
    return RepoFacts(
        snapshot_id=SNAP,
        repo_id="demo",
        revision="rev",
        extractor_version="test-v1",
        files=files,
        build_targets=[
            BuildTarget(
                name="app",
                kind="executable",
                path="CMakeLists.txt",
                line=10,
                sources=["src/app/log.cpp"],
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


def _doc(tmp_path: Path) -> Path:
    path = tmp_path / "requirements.md"
    path.write_text(_REQ_DOC, encoding="utf-8")
    return path


def _generate(tmp_path: Path, failure: str, *, max_rounds: int = 3) -> SpecOutcome:
    backend = MockBackend(failures=[{"attempt": 1, "kind": failure}])
    return generate_spec(
        repo_id="demo",
        doc_path=_doc(tmp_path),
        spec_id="F-001",
        backend=backend,
        facts=_facts(),
        base_dir=tmp_path / "home",
        max_rounds=max_rounds,
        backend_name="mock",
        model_name="mock-v1",
    )


@pytest.mark.parametrize(
    "failure",
    [
        FAIL_SPEC_MISSING_SECTION,
        FAIL_SPEC_BAD_TASK,
        FAIL_SPEC_ORPHAN_REQUIREMENT,
        FAIL_SPEC_ORPHAN_TASK,
        FAIL_SPEC_DANGLING_LOCATION,
        FAIL_SPEC_UNSOURCED_CLAIM,
        FAIL_SPEC_TERM_DRIFT,
        FAIL_SPEC_NO_ROLLBACK,
    ],
)
def test_repair_loop_recovers_from_eight_fault_classes(tmp_path: Path, failure: str) -> None:
    outcome = _generate(tmp_path, failure)
    assert outcome.attempts == 2
    assert outcome.spec.validation is not None
    assert outcome.spec.validation.blockers == []


def test_repair_loop_exhausted_keeps_validation_errors(tmp_path: Path) -> None:
    from codingwiki.errors import InputError
    from codingwiki.spec.ingest import specs_root

    with pytest.raises(InputError, match="仍未通过自动校验"):
        _generate(tmp_path, FAIL_SPEC_NO_ROLLBACK, max_rounds=1)
    directory = specs_root(tmp_path / "home") / "demo" / "F-001"
    payload = json.loads((directory / "validation-errors.json").read_text(encoding="utf-8"))
    assert payload["attempts"] == 1
    assert any(item["rule"] == "rollback" for item in payload["errors"])
    assert not (directory / "spec.json").exists()


# ---------------------------------------------------------------- CLI


def _write_valid_spec_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "F-001"
    directory.mkdir()
    (directory / "spec.json").write_text(_spec().model_dump_json(indent=2) + "\n", encoding="utf-8")
    (directory / "evidence.json").write_text(
        _evidence().model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return directory


def _run_cli(args: list[str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "CODINGWIKI_HOME": str(tmp_path / ".codingwiki"),
        "PYTHONPATH": str(repo_root / "src"),
    }
    return subprocess.run(
        [sys.executable, "-m", "codingwiki", *args],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_cli_spec_validate_returns_zero_and_json_report(tmp_path: Path) -> None:
    directory = _write_valid_spec_dir(tmp_path)
    result = _run_cli(["spec", "validate", str(directory), "--json"], tmp_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["blockers"] == []
    assert "spec validate" not in result.stdout


def test_cli_spec_validate_returns_three_on_blocker(tmp_path: Path) -> None:
    directory = _write_valid_spec_dir(tmp_path)
    payload = json.loads((directory / "spec.json").read_text(encoding="utf-8"))
    payload["sections"][2]["body"] += "\n- 结论：`src/nowhere/dangling.cpp:999` 是入口。"
    (directory / "spec.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    result = _run_cli(["spec", "validate", str(directory)], tmp_path)
    assert result.returncode == 3, result.stderr
    assert "blockers=1" in result.stdout
    assert "code_source" in result.stdout


def test_cli_spec_validate_missing_evidence_returns_two(tmp_path: Path) -> None:
    directory = _write_valid_spec_dir(tmp_path)
    (directory / "evidence.json").unlink()
    result = _run_cli(["spec", "validate", str(directory)], tmp_path)
    assert result.returncode == 2
    assert "缺少证据包" in result.stderr
