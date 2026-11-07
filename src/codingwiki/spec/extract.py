"""需求抽取（架构文档 阶段2 §4、计划 13.4.1）。

流水线：规范化（ingest）→ 章节骨架 → 模型原子化/草拟 AC → 原文定位校验
→ 确定性编号 → 落盘 extract.json。

模型只负责把章节正文拆成原子需求并草拟验收标准；编号、章节归属、
原文引用定位全部由系统确定性完成，避免模型改写原文或伪造需求。
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from codingwiki.backends.protocol import BackendResult, BackendStatus, ModelBackend, TaskPacket
from codingwiki.domain.spec import (
    AcceptanceCriterion,
    DocSection,
    ExtractionResult,
    OpenQuestion,
    QuestionStatus,
    Requirement,
    RequirementKind,
    RequirementPriority,
)
from codingwiki.errors import InputError
from codingwiki.spec.ingest import (
    build_doc_chunks,
    doc_bundle_dir,
    doc_sha256,
    locate_quote,
    make_doc_id,
    parse_sections,
    read_document,
    save_extraction,
    write_doc_bundle,
)

PROMPT_VERSION = "spec-extract-v1"


class _RawModel(BaseModel):
    """模型输出解析用：忽略多余字段，缺字段给空值后统一校验。"""

    model_config = ConfigDict(extra="ignore", frozen=True, str_strip_whitespace=True)


class _RawRequirement(_RawModel):
    kind: RequirementKind = RequirementKind.FUNCTIONAL
    text: str = ""
    priority: RequirementPriority = RequirementPriority.SHOULD
    source_section: str = ""  # 模型提示，最终以原文定位为准
    source_quote: str = ""
    acceptance: list[str] = Field(default_factory=list)


class _RawQuestion(_RawModel):
    question: str = ""
    impact: str = ""
    owner: str = ""


class _RawPayload(_RawModel):
    requirements: list[_RawRequirement] = Field(default_factory=list)
    questions: list[_RawQuestion] = Field(default_factory=list)


def extract_requirements(
    *,
    repo_id: str,
    doc_path: str | Path,
    backend: ModelBackend,
    snapshot_id: str,
    base_dir: str | Path | None = None,
    backend_name: str = "",
    model_name: str = "",
) -> ExtractionResult:
    """抽取需求并落盘到 .codingwiki/specs/<repo>/docs/<doc_id>/。"""
    if not snapshot_id.strip():
        raise InputError("抽取需要绑定固定快照：请先 facts extract 或传 --snapshot")

    normalized = read_document(doc_path)
    sections = parse_sections(normalized)
    if not sections:
        raise InputError(
            f"需求文档没有 Markdown 标题，无法建立章节骨架: {doc_path}；"
            "请补标题层级（# / ## / ###）后再抽取"
        )

    sha = doc_sha256(normalized)
    doc_id = make_doc_id(repo_id, sha)
    source_path = str(doc_path)
    chunks = build_doc_chunks(
        repo_id=repo_id,
        snapshot_id=snapshot_id,
        doc_id=doc_id,
        source_path=source_path,
        normalized_text=normalized,
        sections=sections,
    )
    write_doc_bundle(
        repo_id=repo_id,
        doc_id=doc_id,
        normalized_text=normalized,
        chunks=chunks,
        base_dir=base_dir,
    )

    packet = _prepare_packet(
        repo_id=repo_id,
        doc_id=doc_id,
        doc_sha=sha,
        source_path=source_path,
        normalized_text=normalized,
        sections=sections,
        snapshot_id=snapshot_id,
        base_dir=base_dir,
    )
    result = backend.run(packet)
    payload = _read_payload(result, packet)

    requirements = _build_requirements(payload, normalized, sections)
    questions = _build_questions(payload)
    if not requirements and not questions:
        raise InputError(
            "模型没有抽取到任何需求或开放问题；请检查文档是否包含可验证的行为描述，"
            "或换用真实模型后端"
        )

    extraction = ExtractionResult(
        repo_id=repo_id,
        doc_id=doc_id,
        doc_sha256=sha,
        source_path=source_path,
        title=sections[0].title,
        sections=sections,
        requirements=requirements,
        questions=questions,
        backend=backend_name or result.model,
        model=model_name or result.model,
    )
    save_extraction(extraction, base_dir)
    return extraction


# ---------------------------------------------------------------- 任务包


def _prepare_packet(
    *,
    repo_id: str,
    doc_id: str,
    doc_sha: str,
    source_path: str,
    normalized_text: str,
    sections: list[DocSection],
    snapshot_id: str,
    base_dir: str | Path | None,
) -> TaskPacket:
    workspace = doc_bundle_dir(repo_id, doc_id, base_dir) / "extract-task"
    context = workspace / "context"
    output = workspace / "output"
    context.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    (workspace / "task.md").write_text(_TASK_INSTRUCTIONS, encoding="utf-8")
    meta = {
        "task_type": "spec_extract",
        "repo_id": repo_id,
        "doc_id": doc_id,
        "doc_sha256": doc_sha,
        "source_path": source_path,
        "snapshot_id": snapshot_id,
        "prompt_version": PROMPT_VERSION,
    }
    (workspace / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (context / "document.md").write_text(normalized_text, encoding="utf-8")
    (context / "sections.json").write_text(
        json.dumps(
            [section.model_dump(mode="json") for section in sections],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return TaskPacket(
        workspace=workspace,
        task_file=workspace / "task.md",
        output_dir=output,
        metadata={"task_type": "spec_extract", "doc_id": doc_id, "snapshot_id": snapshot_id},
    )


_TASK_INSTRUCTIONS = """# 任务：需求抽取

把 `context/document.md` 中的需求拆成原子需求：

1. 一条需求只描述一个可验证行为；功能需求与非功能需求分开。
2. 每条需求必须给出 `source_quote`：从原文逐字复制的一段话（允许空白差异），
   不得改写、不得拼接、不得编造。
3. `acceptance` 只写原文明确给出的验收标准；原文没有就留空。
4. 原文不明确的地方写入 `questions`，不要猜答案。
5. 优先级：must / should / may。

# 输出

只输出 JSON，写入 `output/extract.json`：

{
  "requirements": [
    {
      "kind": "functional",
      "text": "……",
      "priority": "must",
      "source_section": "2 功能需求 / 2.1 配置项",
      "source_quote": "……",
      "acceptance": ["……"]
    }
  ],
  "questions": [{"question": "……", "impact": "……", "owner": "……"}]
}
"""


# ---------------------------------------------------------------- 解析与编号


def _read_payload(result: BackendResult, packet: TaskPacket) -> _RawPayload:
    if result.status is not BackendStatus.SUCCEEDED:
        detail = result.error or result.status.value
        raise InputError(
            f"需求抽取失败（{result.status.value}）: {detail}；请检查模型后端配置与文档内容后重试"
        )
    output = packet.output_dir / "extract.json"
    if not output.is_file():
        raise InputError(f"后端没有产出 extract.json: {output}")
    try:
        return _RawPayload.model_validate_json(output.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(f"extract.json 不符合契约: {output}: {exc}") from exc


def _build_requirements(
    payload: _RawPayload, normalized_text: str, sections: list[DocSection]
) -> list[Requirement]:
    located: list[tuple[int, int, _RawRequirement, DocSection]] = []
    problems: list[str] = []
    for index, raw in enumerate(payload.requirements):
        if not raw.text:
            problems.append(f"第 {index + 1} 条需求缺少 text")
            continue
        if not raw.source_quote:
            problems.append(f"第 {index + 1} 条需求缺少 source_quote")
            continue
        location = locate_quote(raw.source_quote, normalized_text, sections)
        if location is None:
            problems.append(f"第 {index + 1} 条需求引用无法在原文定位：{raw.source_quote[:60]}")
            continue
        located.append((location.line, index, raw, location.section))

    if problems:
        raise InputError("模型输出存在不可追溯的需求（禁止编造）:\n- " + "\n- ".join(problems))

    requirements: list[Requirement] = []
    functional = 0
    non_functional = 0
    acceptance_seq = 1
    for _line, _index, raw, section in sorted(located, key=lambda item: (item[0], item[1])):
        kind = _effective_kind(raw.kind, section)
        if kind is RequirementKind.FUNCTIONAL:
            functional += 1
            req_id = f"R-{functional:03d}"
        else:
            non_functional += 1
            req_id = f"N-{non_functional:03d}"
        acceptance: list[AcceptanceCriterion] = []
        for text in raw.acceptance:
            if not text:
                continue
            acceptance.append(AcceptanceCriterion(id=f"AC-{acceptance_seq:03d}", text=text))
            acceptance_seq += 1
        requirements.append(
            Requirement(
                id=req_id,
                kind=kind,
                text=raw.text,
                priority=raw.priority,
                acceptance=acceptance,
                source_section=section.path,
                source_quote=raw.source_quote,
            )
        )
    return requirements


def _effective_kind(kind: RequirementKind, section: DocSection) -> RequirementKind:
    """章节语义与模型判断冲突时以章节为准（结构性事实优先）。"""
    if "非功能需求" in section.path:
        return RequirementKind.NON_FUNCTIONAL
    if "功能需求" in section.path:
        return RequirementKind.FUNCTIONAL
    return kind


def _build_questions(payload: _RawPayload) -> list[OpenQuestion]:
    questions: list[OpenQuestion] = []
    for index, raw in enumerate(payload.questions, start=1):
        if not raw.question:
            continue
        questions.append(
            OpenQuestion(
                id=f"Q-{index:03d}",
                question=raw.question,
                impact=raw.impact,
                owner=raw.owner,
                status=QuestionStatus.OPEN,
            )
        )
    return questions


__all__ = ["PROMPT_VERSION", "extract_requirements"]
