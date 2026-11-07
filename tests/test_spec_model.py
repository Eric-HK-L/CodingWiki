"""Spec 领域模型不变量测试（S12）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from codingwiki.domain.spec import (
    SPEC_SECTIONS,
    AcceptanceCriterion,
    DesignTask,
    EvidenceItem,
    EvidenceKind,
    OpenQuestion,
    QuestionStatus,
    Requirement,
    RequirementKind,
    SourceDocument,
    SpecDocument,
    SpecSection,
    SpecStatus,
    TraceRow,
)

_SHA = "a" * 64


def _sections() -> list[SpecSection]:
    return [
        SpecSection(number=number, title=title, body=f"第 {number} 章内容")
        for number, title in SPEC_SECTIONS.items()
    ]


def _requirement(**overrides: object) -> Requirement:
    data: dict[str, object] = {
        "id": "R-001",
        "kind": RequirementKind.FUNCTIONAL,
        "text": "日志级别可通过配置调整",
        "source_section": "2 功能需求 / 2.1 配置项",
        "source_quote": "希望日志级别可以通过配置调整",
        "acceptance": [AcceptanceCriterion(id="AC-001", text="未配置时行为与 info 一致")],
    }
    data.update(overrides)
    return Requirement.model_validate(data)


def _spec(**overrides: object) -> SpecDocument:
    data: dict[str, object] = {
        "spec_id": "F-001",
        "title": "日志级别配置",
        "snapshot_id": "sample@rev",
        "source_doc": SourceDocument(
            path="evals/specs/demo.md", doc_sha256=_SHA, doc_id=f"sample@{_SHA}"
        ),
        "status": SpecStatus.DRAFT,
        "sections": _sections(),
        "requirements": [_requirement()],
        "questions": [],
        "tasks": [],
        "trace": [],
    }
    data.update(overrides)
    return SpecDocument.model_validate(data)


def test_requirement_requires_source_quote() -> None:
    with pytest.raises(ValidationError, match="source_quote"):
        _requirement(source_quote="")


def test_requirement_prefix_must_match_kind() -> None:
    with pytest.raises(ValidationError, match="前缀"):
        _requirement(kind=RequirementKind.NON_FUNCTIONAL)


def test_requirement_acceptance_ids_unique() -> None:
    dup = [
        AcceptanceCriterion(id="AC-001", text="第一条"),
        AcceptanceCriterion(id="AC-001", text="第二条"),
    ]
    with pytest.raises(ValidationError, match="验收标准编号重复"):
        _requirement(acceptance=dup)


def test_design_task_rejects_more_than_three_files() -> None:
    with pytest.raises(ValidationError):
        DesignTask(
            id="T-001",
            goal="新增配置项",
            files=["a.c", "b.c", "c.c", "d.c"],
            estimated_lines=40,
            acceptance=["构建通过"],
        )


def test_design_task_rejects_over_300_lines() -> None:
    with pytest.raises(ValidationError):
        DesignTask(
            id="T-001",
            goal="新增配置项",
            files=["a.c"],
            estimated_lines=301,
            acceptance=["构建通过"],
        )


def test_design_task_accepts_boundary() -> None:
    task = DesignTask(
        id="T-001",
        goal="新增配置项",
        files=["a.c", "b.c", "c.c"],
        estimated_lines=300,
        acceptance=["构建通过"],
    )
    assert task.estimated_lines == 300


def test_spec_document_requires_eleven_sections() -> None:
    with pytest.raises(ValidationError, match="11 章"):
        _spec(sections=_sections()[:-1])


def test_spec_document_rejects_wrong_chapter_title() -> None:
    sections = _sections()
    sections[2] = SpecSection(number=3, title="现状分析", body="内容")
    with pytest.raises(ValidationError, match="标题"):
        _spec(sections=sections)


def test_spec_document_rejects_out_of_order_numbers() -> None:
    sections = _sections()
    sections[0], sections[1] = sections[1], sections[0]
    with pytest.raises(ValidationError, match="编号"):
        _spec(sections=sections)


def test_spec_document_rejects_duplicate_ids() -> None:
    questions = [
        OpenQuestion(id="Q-001", question="阈值未定", status=QuestionStatus.OPEN),
        OpenQuestion(id="Q-001", question="环境变量名未定", status=QuestionStatus.OPEN),
    ]
    with pytest.raises(ValidationError, match="重复"):
        _spec(questions=questions)


def test_approved_spec_cannot_have_open_questions() -> None:
    question = OpenQuestion(id="Q-001", question="阈值未定", status=QuestionStatus.OPEN)
    with pytest.raises(ValidationError, match="开放问题"):
        _spec(status=SpecStatus.APPROVED, questions=[question])


def test_valid_spec_document() -> None:
    spec = _spec()
    assert [section.number for section in spec.sections] == list(range(1, 12))
    assert spec.requirements[0].id == "R-001"


def test_evidence_item_requires_locator_and_snapshot() -> None:
    with pytest.raises(ValidationError, match="path"):
        EvidenceItem(kind=EvidenceKind.SYMBOL, snapshot_id="s@r")
    with pytest.raises(ValidationError):
        EvidenceItem(kind=EvidenceKind.SYMBOL, symbol_id="src/a.c::foo@10", snapshot_id="")
    with pytest.raises(ValidationError, match="page_id"):
        EvidenceItem(kind=EvidenceKind.WIKI, snapshot_id="s@r")


def test_trace_row_requires_all_columns() -> None:
    with pytest.raises(ValidationError):
        TraceRow(
            requirement_id="R-001",
            design_section="",
            code_location="src/a.c:10",
            test="test_a",
            task="T-001",
        )
