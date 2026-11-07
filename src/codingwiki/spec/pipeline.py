"""Spec 生成流水线（架构文档 阶段2 §6）。

normalize → extract → evidence → draft → validate → persist。
模型只写 draft.json；编号、元信息、需求与证据由系统注入。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from codingwiki.backends.protocol import BackendStatus, ModelBackend, TaskPacket
from codingwiki.domain.facts import RepoFacts
from codingwiki.domain.spec import (
    CandidateOption,
    DecisionRecord,
    DesignTask,
    EvidenceBundle,
    ExtractionResult,
    QuestionStatus,
    SourceDocument,
    SpecDocument,
    SpecIssue,
    SpecSection,
    SpecStatus,
    TraceRow,
    ValidationReport,
)
from codingwiki.errors import InputError
from codingwiki.spec.evidence import WikiPageRef, collect_evidence, save_evidence
from codingwiki.spec.extract import extract_requirements
from codingwiki.spec.ingest import read_document, specs_root
from codingwiki.spec.template import render_decisions_markdown, render_spec_markdown
from codingwiki.spec.validator import validate_spec

PROMPT_VERSION = "spec-draft-v1"


class _RawSection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: int
    title: str
    body: str


class _RawCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    summary: str
    pros: str = ""
    cons: str = ""


class _RawDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    context: str
    candidates: list[_RawCandidate] = Field(default_factory=list)
    choice: str
    rationale: str
    unique_justification: str = ""


class _RawTask(BaseModel):
    model_config = ConfigDict(extra="ignore")

    goal: str
    files: list[str]
    estimated_lines: int
    acceptance: list[str]
    depends_on_indexes: list[int] = Field(default_factory=list)


class _RawTrace(BaseModel):
    model_config = ConfigDict(extra="ignore")

    requirement_id: str
    design_section: str
    code_location: str
    test: str
    task_index: int


class _RawDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sections: list[_RawSection] = Field(default_factory=list)
    tasks: list[_RawTask] = Field(default_factory=list)
    trace: list[_RawTrace] = Field(default_factory=list)
    decisions: list[_RawDecision] = Field(default_factory=list)


@dataclass(frozen=True)
class SpecOutcome:
    spec: SpecDocument
    directory: Path
    attempts: int


_TASK_INSTRUCTIONS = """# 任务：为需求文档草拟开发 Spec

输入：
- `context/extract.json`：系统抽取并编号的需求/开放问题（不得新增或改写需求）
- `context/evidence.json`：每条需求的代码/Wiki/配置/构建/测试证据与影响面
- `context/document.md`：规范化需求原文

必须输出 11 章（编号与标题固定）：
1 背景与目标 / 2 术语 / 3 现状 / 4 需求 / 5 方案设计 / 6 影响分析 /
7 测试计划 / 8 编码任务拆分 / 9 风险与回滚 / 10 开放问题与假设 / 11 追溯矩阵

硬性约束：
1. 任务最多 3 个文件、最多 300 行，单一目标；文件来自 evidence 的 impact.files。
2. 追溯矩阵每条需求恰好一行；`code_location` 必须是证据中的 path:line、symbol_id 或 wiki:page_id。
3. 方案设计至少 2 个候选；确属唯一方案时填写 `unique_justification`。
   `choice` 必须逐字复制某个候选的 `name`（含编号前缀与说明文字），不得简写。
4. 没有内容写「不适用」并给出原因，不得删章。
5. 只写 `output/draft.json`，不要输出系统编号（T-/D- 由系统生成）。

# 输出 JSON

{
  "sections": [{"number": 1, "title": "背景与目标", "body": "..."}],
  "tasks": [{"goal": "...", "files": ["a.cpp"], "estimated_lines": 120,
             "acceptance": ["..."], "depends_on_indexes": []}],
  "trace": [{"requirement_id": "R-001", "design_section": "5 方案设计",
             "code_location": "src/a.cpp:12", "test": "test_a", "task_index": 1}],
  "decisions": [{"title": "...", "context": "...",
                 "candidates": [{"name": "方案A", "summary": "...", "pros": "...", "cons": "..."},
                                {"name": "方案B", "summary": "...", "pros": "...", "cons": "..."}],
                 "choice": "方案A", "rationale": "...", "unique_justification": ""}]
}
"""


def _spec_dir(repo_id: str, spec_id: str, base_dir: str | Path | None) -> Path:
    return specs_root(base_dir) / repo_id / spec_id


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, BaseModel):
        text = payload.model_dump_json(indent=2)
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(text + "\n", encoding="utf-8")


def _prepare_workspace(
    *,
    directory: Path,
    repo_id: str,
    spec_id: str,
    extraction: ExtractionResult,
    evidence: EvidenceBundle,
    normalized_text: str,
) -> TaskPacket:
    workspace = directory / "draft-task"
    context = workspace / "context"
    output = workspace / "output"
    context.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    (workspace / "task.md").write_text(_TASK_INSTRUCTIONS, encoding="utf-8")
    (context / "document.md").write_text(normalized_text, encoding="utf-8")
    _write_json(context / "extract.json", extraction)
    _write_json(context / "evidence.json", evidence)
    return TaskPacket(
        workspace=workspace,
        task_file=workspace / "task.md",
        output_dir=output,
        metadata={"task_type": "spec_draft", "spec_id": spec_id, "repo_id": repo_id},
    )


def _write_attempt_meta(
    packet: TaskPacket,
    *,
    repo_id: str,
    spec_id: str,
    doc_id: str,
    snapshot_id: str,
    attempt: int,
) -> None:
    meta = {
        "task_type": "spec_draft",
        "repo_id": repo_id,
        "spec_id": spec_id,
        "doc_id": doc_id,
        "snapshot_id": snapshot_id,
        "attempt": attempt,
        "prompt_version": PROMPT_VERSION,
    }
    _write_json(packet.workspace / "meta.json", meta)


def _write_errors(packet: TaskPacket, issues: list[SpecIssue]) -> None:
    _write_json(
        packet.workspace / "errors.json",
        [issue.model_dump(mode="json") for issue in issues],
    )


def _read_draft(packet: TaskPacket) -> _RawDraft:
    path = packet.output_dir / "draft.json"
    if not path.is_file():
        raise ValueError(f"后端没有产出 draft.json: {path}")
    try:
        return _RawDraft.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"draft.json 不符合契约: {exc}") from exc


def _assemble(
    *,
    spec_id: str,
    snapshot_id: str,
    extraction: ExtractionResult,
    payload: _RawDraft,
) -> SpecDocument:
    sections = [
        SpecSection(number=raw.number, title=raw.title, body=raw.body)
        for raw in sorted(payload.sections, key=lambda item: item.number)
    ]
    task_ids = [f"T-{index:03d}" for index in range(1, len(payload.tasks) + 1)]
    tasks: list[DesignTask] = []
    for index, raw in enumerate(payload.tasks):
        depends_on = [
            task_ids[position - 1]
            for position in raw.depends_on_indexes
            if 1 <= position <= len(task_ids)
        ]
        tasks.append(
            DesignTask(
                id=task_ids[index],
                goal=raw.goal,
                files=raw.files,
                estimated_lines=raw.estimated_lines,
                acceptance=raw.acceptance,
                depends_on=depends_on,
            )
        )
    trace = [
        TraceRow(
            requirement_id=raw.requirement_id,
            design_section=raw.design_section,
            code_location=raw.code_location,
            test=raw.test,
            task=f"T-{raw.task_index:03d}",
        )
        for raw in payload.trace
    ]
    decisions = [
        DecisionRecord(
            id=f"D-{index:03d}",
            title=raw.title,
            context=raw.context,
            candidates=[
                CandidateOption(
                    name=candidate.name,
                    summary=candidate.summary,
                    pros=candidate.pros,
                    cons=candidate.cons,
                )
                for candidate in raw.candidates
            ],
            choice=raw.choice,
            rationale=raw.rationale,
            unique_justification=raw.unique_justification,
        )
        for index, raw in enumerate(payload.decisions, start=1)
    ]
    open_questions = [
        question for question in extraction.questions if question.status is QuestionStatus.OPEN
    ]
    return SpecDocument(
        spec_id=spec_id,
        title=extraction.title,
        snapshot_id=snapshot_id,
        source_doc=SourceDocument(
            path=extraction.source_path,
            doc_sha256=extraction.doc_sha256,
            doc_id=extraction.doc_id,
        ),
        status=SpecStatus.NEEDS_INFO if open_questions else SpecStatus.DRAFT,
        sections=sections,
        requirements=extraction.requirements,
        questions=extraction.questions,
        tasks=tasks,
        trace=trace,
        decisions=decisions,
    )


def _persist(
    spec: SpecDocument,
    *,
    directory: Path,
    evidence: EvidenceBundle,
    report: ValidationReport,
    attempts: int,
) -> SpecOutcome:
    final = spec.model_copy(update={"validation": report})
    _write_json(directory / "spec.json", final)
    (directory / "spec.md").write_text(render_spec_markdown(final), encoding="utf-8")
    (directory / "decisions.md").write_text(render_decisions_markdown(final), encoding="utf-8")
    _write_json(directory / "validation.json", report)
    save_evidence(evidence, directory)
    return SpecOutcome(spec=final, directory=directory, attempts=attempts)


def generate_spec(
    *,
    repo_id: str,
    doc_path: str | Path,
    spec_id: str,
    backend: ModelBackend,
    facts: RepoFacts,
    pages: list[WikiPageRef] | None = None,
    base_dir: str | Path | None = None,
    max_rounds: int = 3,
    backend_name: str = "",
    model_name: str = "",
) -> SpecOutcome:
    """需求文档 → 完整 Spec；修复循环耗尽时报错并保留草稿。"""
    rounds = max(1, max_rounds)
    extraction = extract_requirements(
        repo_id=repo_id,
        doc_path=doc_path,
        backend=backend,
        snapshot_id=facts.snapshot_id,
        base_dir=base_dir,
        backend_name=backend_name,
        model_name=model_name,
    )
    evidence = collect_evidence(
        repo_id=repo_id,
        doc_id=extraction.doc_id,
        doc_title=extraction.title,
        requirements=extraction.requirements,
        facts=facts,
        pages=pages or [],
        spec_id=spec_id,
        snapshot_id=facts.snapshot_id,
    )
    normalized = read_document(doc_path)
    directory = _spec_dir(repo_id, spec_id, base_dir)
    packet = _prepare_workspace(
        directory=directory,
        repo_id=repo_id,
        spec_id=spec_id,
        extraction=extraction,
        evidence=evidence,
        normalized_text=normalized,
    )

    previous_errors: list[SpecIssue] = []
    for attempt in range(1, rounds + 1):
        _write_attempt_meta(
            packet,
            repo_id=repo_id,
            spec_id=spec_id,
            doc_id=extraction.doc_id,
            snapshot_id=facts.snapshot_id,
            attempt=attempt,
        )
        if previous_errors:
            _write_errors(packet, previous_errors)
        result = backend.run(packet)
        if result.status is not BackendStatus.SUCCEEDED:
            previous_errors = [
                SpecIssue(
                    rule="backend_failed",
                    message=f"后端状态 {result.status.value}: {result.error or '无详情'}",
                )
            ]
            continue
        try:
            payload = _read_draft(packet)
            spec = _assemble(
                spec_id=spec_id,
                snapshot_id=facts.snapshot_id,
                extraction=extraction,
                payload=payload,
            )
            report = validate_spec(spec, extraction, evidence)
        except ValueError as exc:
            previous_errors = [SpecIssue(rule="draft_contract", message=str(exc))]
            continue
        if report.blockers:
            previous_errors = report.blockers
            continue
        return _persist(
            spec,
            directory=directory,
            evidence=evidence,
            report=report,
            attempts=attempt,
        )

    _write_json(
        directory / "validation-errors.json",
        {
            "attempts": rounds,
            "errors": [issue.model_dump(mode="json") for issue in previous_errors],
        },
    )
    detail = "；".join(issue.message for issue in previous_errors[:3]) or "无详情"
    raise InputError(
        f"Spec 草拟 {rounds} 轮仍未通过自动校验：{detail}；已保留草稿与错误清单: {directory}"
    )


__all__ = ["PROMPT_VERSION", "SpecOutcome", "generate_spec"]
