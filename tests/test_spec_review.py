"""S16 评审状态机测试：门禁、迁移、review.json 落盘与 CLI 退出码。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
    QuestionStatus,
    Requirement,
    RequirementEvidence,
    RequirementKind,
    RequirementPriority,
    ReviewDecision,
    ReviewLog,
    ReviewRecord,
    ReviewRole,
    SourceDocument,
    SpecDocument,
    SpecSection,
    SpecStatus,
    TraceRow,
)
from codingwiki.spec.review import ReviewGateError, apply_review, load_review_log, review_spec
from codingwiki.spec.validator import validate_spec

SNAP = "demo@rev"
OTHER_SNAP = "demo@other"
SYMBOL = "src/app/log.cpp::set_log_level@12"
DOC_ID = "demo@abc"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def _requirement() -> Requirement:
    return Requirement(
        id="R-001",
        kind=RequirementKind.FUNCTIONAL,
        text="在配置中新增日志级别配置项 log_level，默认 info",
        priority=RequirementPriority.MUST,
        acceptance=[AcceptanceCriterion(id="AC-001", text="未配置时按 info 输出")],
        source_section="1 功能需求",
        source_quote="在配置中新增日志级别配置项 log_level，默认 info",
    )


def _evidence(snapshot: str = SNAP) -> EvidenceBundle:
    return EvidenceBundle(
        repo_id="demo",
        snapshot_id=snapshot,
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
                ),
            )
        ],
    )


def _extraction() -> ExtractionResult:
    return ExtractionResult(
        repo_id="demo",
        doc_id=DOC_ID,
        doc_sha256="a" * 64,
        source_path="requirements.md",
        title="日志级别需求",
        sections=[],
        requirements=[_requirement()],
    )


def _sections(*, risk: str = "") -> list[SpecSection]:
    risk_body = risk or (
        "风险：改动限定在影响面列出的文件；安全：沿用既有输入校验；回滚方式：按任务反向撤销提交。"
    )
    bodies = [
        "背景：为日志级别增加运行期配置。",
        "术语：set_log_level。",
        "- R-001：`src/app/log.cpp:12`",
        "- R-001（must, functional）：新增 log_level 配置。",
        "候选方案：方案A / 方案B；选择方案A。",
        "- R-001：文件 src/app/log.cpp（依据 `src/app/log.cpp:12`）",
        "- R-001：test_log_level",
        "- T-001：新增 log_level 配置读取（1 文件，120 行）。",
        risk_body,
        "无。",
        "| R-001 | 5 方案设计 | `src/app/log.cpp:12` | test_log_level | T-001 |",
    ]
    return [
        SpecSection(number=number, title=SPEC_SECTIONS[number], body=body)
        for number, body in enumerate(bodies, start=1)
    ]


def _spec(
    *,
    status: SpecStatus = SpecStatus.DRAFT,
    questions: list[OpenQuestion] | None = None,
    risk: str = "",
) -> SpecDocument:
    return SpecDocument(
        spec_id="F-001",
        title="日志级别需求",
        snapshot_id=SNAP,
        source_doc=SourceDocument(path="requirements.md", doc_sha256="a" * 64, doc_id=DOC_ID),
        status=status,
        sections=_sections(risk=risk),
        requirements=[_requirement()],
        questions=questions or [],
        tasks=[
            DesignTask(
                id="T-001",
                goal="新增 log_level 配置读取",
                files=["src/app/log.cpp"],
                estimated_lines=120,
                acceptance=["未配置时按 info 输出"],
            )
        ],
        trace=[
            TraceRow(
                requirement_id="R-001",
                design_section="5 方案设计",
                code_location="src/app/log.cpp:12",
                test="test_log_level",
                task="T-001",
            )
        ],
        decisions=[
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


def _ready(
    status: SpecStatus = SpecStatus.DRAFT,
    questions: list[OpenQuestion] | None = None,
    risk: str = "",
) -> SpecDocument:
    spec = _spec(status=status, questions=questions, risk=risk)
    report = validate_spec(spec, _extraction(), _evidence())
    return spec.model_copy(update={"validation": report})


def _review(
    spec: SpecDocument,
    *,
    role: ReviewRole = ReviewRole.MODULE_OWNER,
    decision: ReviewDecision = ReviewDecision.APPROVE,
    comment: str = "",
    close_questions: tuple[str, ...] = (),
    ack_warnings: bool = False,
    snapshot_id: str = "",
) -> tuple[SpecDocument, ReviewLog]:
    report = validate_spec(spec, _extraction(), _evidence())
    return review_spec(
        spec,
        report,
        None,
        role=role,
        decision=decision,
        comment=comment,
        close_questions=close_questions,
        ack_warnings=ack_warnings,
        snapshot_id=snapshot_id,
        now=NOW,
    )


def test_approve_draft_transitions_and_records() -> None:
    spec, log = _review(_ready())
    assert spec.status is SpecStatus.APPROVED
    assert log.latest.role is ReviewRole.MODULE_OWNER
    assert log.latest.created_at == NOW
    assert log.latest.blockers == 0


def test_approve_with_blockers_is_rejected() -> None:
    spec = _ready()
    broken = spec.model_copy(update={"trace": []})
    with pytest.raises(ReviewGateError, match="blocker"):
        _review(broken)


def test_approve_needs_info_requires_closing_questions() -> None:
    question = OpenQuestion(
        id="Q-001", question="默认级别是否可配置为 warn？", status=QuestionStatus.OPEN
    )
    spec = _ready(status=SpecStatus.NEEDS_INFO, questions=[question])
    with pytest.raises(ReviewGateError, match="未关闭"):
        _review(spec)
    approved, log = _review(spec, close_questions=("Q-001",), comment="默认 warn 不可配置")
    assert approved.status is SpecStatus.APPROVED
    assert approved.questions[0].status is QuestionStatus.CLOSED
    assert log.latest.decision is ReviewDecision.APPROVE


def test_approve_requires_warning_acknowledgement() -> None:
    spec = _ready(risk="风险：改动限定在影响面列出的文件；回滚方式：按任务反向撤销提交。")
    with pytest.raises(ReviewGateError, match="warning"):
        _review(spec)
    approved, log = _review(spec, ack_warnings=True)
    assert approved.status is SpecStatus.APPROVED
    assert log.latest.warnings == 1
    assert log.latest.warnings_acknowledged is True


def test_reject_requires_comment_and_transitions() -> None:
    with pytest.raises(ReviewGateError, match="comment"):
        _review(_ready(), decision=ReviewDecision.REJECT)
    rejected, log = _review(
        _ready(),
        role=ReviewRole.TEST_OWNER,
        decision=ReviewDecision.REJECT,
        comment="测试计划不可执行",
    )
    assert rejected.status is SpecStatus.REJECTED
    assert log.latest.comment == "测试计划不可执行"
    with pytest.raises(ReviewGateError, match="rejected"):
        _review(rejected, decision=ReviewDecision.REJECT, comment="重复")


def test_reject_rejects_close_question_flag() -> None:
    with pytest.raises(ReviewGateError, match="close-question"):
        _review(
            _ready(),
            decision=ReviewDecision.REJECT,
            comment="先退回",
            close_questions=("Q-001",),
        )


def test_snapshot_mismatch_is_gate_failure() -> None:
    with pytest.raises(ReviewGateError, match="快照"):
        _review(_ready(), snapshot_id=OTHER_SNAP)


def test_review_record_invariants() -> None:
    with pytest.raises(ValueError, match="blocker"):
        ReviewRecord(
            spec_id="F-001",
            role=ReviewRole.ARCHITECT,
            decision=ReviewDecision.APPROVE,
            created_at=NOW,
            snapshot_id=SNAP,
            blockers=1,
        )
    with pytest.raises(ValueError, match="意见"):
        ReviewRecord(
            spec_id="F-001",
            role=ReviewRole.ARCHITECT,
            decision=ReviewDecision.REJECT,
            created_at=NOW,
            snapshot_id=SNAP,
        )
    with pytest.raises(ValueError, match="确认"):
        ReviewRecord(
            spec_id="F-001",
            role=ReviewRole.ARCHITECT,
            decision=ReviewDecision.APPROVE,
            created_at=NOW,
            snapshot_id=SNAP,
            warnings=2,
        )


def test_apply_review_persists_review_log_and_keeps_validation(tmp_path: Path) -> None:
    spec = _ready()
    assert spec.validation is not None
    directory = tmp_path / "F-001"
    directory.mkdir()
    (directory / "spec.json").write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (directory / "validation.json").write_text(
        spec.validation.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    report = validate_spec(spec, _extraction(), _evidence())
    apply_review(
        spec,
        report,
        directory,
        role=ReviewRole.ARCHITECT,
        decision=ReviewDecision.APPROVE,
        now=NOW,
    )
    log = load_review_log(directory)
    assert log is not None and len(log.reviews) == 1
    assert log.latest.snapshot_id == SNAP
    assert (directory / "spec.md").is_file()
    reloaded = SpecDocument.model_validate_json(
        (directory / "spec.json").read_text(encoding="utf-8")
    )
    assert reloaded.status is SpecStatus.APPROVED
    assert (directory / "validation.json").read_text(encoding="utf-8").strip().startswith("{")


def test_validator_status_integrity_blocks_manual_approval() -> None:
    spec = _ready()
    tampered = spec.model_copy(update={"status": SpecStatus.APPROVED, "trace": []})
    report = validate_spec(tampered, _extraction(), _evidence())
    assert any(issue.rule == "status_integrity" for issue in report.blockers)


def test_review_record_rejects_cross_spec_log(tmp_path: Path) -> None:
    spec = _ready()
    directory = tmp_path / "F-001"
    directory.mkdir()
    (directory / "review.json").write_text(
        json.dumps(
            {
                "spec_id": "F-002",
                "snapshot_id": SNAP,
                "reviews": [
                    {
                        "spec_id": "F-002",
                        "role": "architect",
                        "decision": "reject",
                        "comment": "错位",
                        "created_at": NOW.isoformat(),
                        "snapshot_id": SNAP,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    report = validate_spec(spec, _extraction(), _evidence())
    with pytest.raises(ReviewGateError, match="评审记录"):
        apply_review(
            spec,
            report,
            directory,
            role=ReviewRole.ARCHITECT,
            decision=ReviewDecision.APPROVE,
            now=NOW,
        )


# ---------------------------------------------------------------- CLI


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "CODINGWIKI_HOME": str(cwd / ".codingwiki"),
        "PYTHONPATH": str(repo_root / "src"),
    }
    return subprocess.run(
        [sys.executable, "-m", "codingwiki", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _write_spec_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "F-001"
    directory.mkdir()
    (directory / "spec.json").write_text(
        _ready().model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (directory / "evidence.json").write_text(
        _evidence().model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return directory


def test_cli_review_approve_returns_zero(tmp_path: Path) -> None:
    directory = _write_spec_dir(tmp_path)
    result = _run_cli(
        ["spec", "review", str(directory), "--approve", "--role", "architect"],
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "status=approved" in result.stdout
    assert (directory / "review.json").is_file()
    payload = json.loads((directory / "spec.json").read_text(encoding="utf-8"))
    assert payload["status"] == "approved"


def test_cli_review_gate_failure_returns_three(tmp_path: Path) -> None:
    directory = _write_spec_dir(tmp_path)
    result = _run_cli(
        ["spec", "review", str(directory), "--reject", "--role", "test_owner"],
        tmp_path,
    )
    assert result.returncode == 3, result.stderr
    assert "comment" in result.stderr
    payload = json.loads((directory / "spec.json").read_text(encoding="utf-8"))
    assert payload["status"] == "draft"


def test_cli_review_missing_spec_returns_two(tmp_path: Path) -> None:
    result = _run_cli(
        ["spec", "review", str(tmp_path / "nope"), "--approve", "--role", "architect"],
        tmp_path,
    )
    assert result.returncode == 2, result.stderr
