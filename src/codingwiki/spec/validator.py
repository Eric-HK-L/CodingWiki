"""Spec 结构校验（S14 基线，S15 在同一入口扩充十项规则）。

纯函数：输入 Spec / 抽取产物 / 证据包，输出 ValidationReport；
不读写文件、不调用模型。
"""

from __future__ import annotations

from codingwiki.domain.spec import (
    SPEC_SECTIONS,
    EvidenceBundle,
    EvidenceItem,
    ExtractionResult,
    RequirementKind,
    SpecDocument,
    SpecIssue,
    ValidationReport,
)

MAX_TASK_FILES = 3
MAX_TASK_LINES = 300


def evidence_locators(items: list[EvidenceItem]) -> set[str]:
    """证据可被追溯矩阵引用的稳定位置写法。"""
    locators: set[str] = set()
    for item in items:
        if item.path:
            locators.add(f"{item.path}:{item.line}" if item.line else item.path)
        if item.symbol_id:
            locators.add(item.symbol_id)
        if item.page_id:
            locators.add(f"wiki:{item.page_id}")
    return locators


def validate_spec(
    spec: SpecDocument,
    extraction: ExtractionResult,
    evidence: EvidenceBundle,
) -> ValidationReport:
    """S14 结构规则；返回 blockers/warnings/info，供修复循环回喂。"""
    blockers: list[SpecIssue] = []
    warnings: list[SpecIssue] = []
    info: list[SpecIssue] = []

    # 1. 11 章结构
    if len(spec.sections) != len(SPEC_SECTIONS):
        blockers.append(
            SpecIssue(rule="section_structure", message=f"章节数应为 11，实际 {len(spec.sections)}")
        )
    for section in spec.sections:
        expected = SPEC_SECTIONS.get(section.number)
        if expected is None or section.title != expected:
            blockers.append(
                SpecIssue(
                    rule="section_structure",
                    message=f"第 {section.number} 章标题应为 {expected!r}，实际 {section.title!r}",
                    ref=f"S{section.number}",
                )
            )

    # 2. 需求覆盖：模型不得新增/遗漏
    spec_ids = {requirement.id for requirement in spec.requirements}
    extracted_ids = {requirement.id for requirement in extraction.requirements}
    for missing in sorted(extracted_ids - spec_ids):
        blockers.append(
            SpecIssue(rule="requirement_coverage", message=f"Spec 遗漏需求 {missing}", ref=missing)
        )
    for extra in sorted(spec_ids - extracted_ids):
        blockers.append(
            SpecIssue(
                rule="requirement_coverage", message=f"Spec 出现抽取产物外的需求 {extra}", ref=extra
            )
        )

    # 3. 功能需求至少 1 条验收标准
    for requirement in spec.requirements:
        if requirement.kind is RequirementKind.FUNCTIONAL and not requirement.acceptance:
            blockers.append(
                SpecIssue(
                    rule="acceptance_criteria",
                    message=f"功能需求 {requirement.id} 没有验收标准",
                    ref=requirement.id,
                )
            )

    # 4. 任务大小与编号
    task_ids = {task.id for task in spec.tasks}
    if len(task_ids) != len(spec.tasks):
        blockers.append(SpecIssue(rule="task_size", message="任务编号重复"))
    for task in spec.tasks:
        if len(task.files) > MAX_TASK_FILES:
            blockers.append(
                SpecIssue(
                    rule="task_size",
                    message=f"任务 {task.id} 涉及 {len(task.files)} 个文件，超过 {MAX_TASK_FILES}",
                    ref=task.id,
                )
            )
        if task.estimated_lines > MAX_TASK_LINES:
            blockers.append(
                SpecIssue(
                    rule="task_size",
                    message=f"任务 {task.id} 预估 {task.estimated_lines} 行，超过 {MAX_TASK_LINES}",
                    ref=task.id,
                )
            )
        for dependency in task.depends_on:
            if dependency not in task_ids:
                blockers.append(
                    SpecIssue(
                        rule="task_size",
                        message=f"任务 {task.id} 依赖不存在的 {dependency}",
                        ref=task.id,
                    )
                )

    # 5. 追溯矩阵
    evidence_by_requirement = {item.requirement_id: item for item in evidence.requirements}
    if evidence.snapshot_id != spec.snapshot_id:
        blockers.append(
            SpecIssue(
                rule="evidence_coverage",
                message=f"证据快照 {evidence.snapshot_id} 与 Spec {spec.snapshot_id} 不一致",
            )
        )
    for requirement_id in sorted(extracted_ids):
        if requirement_id not in evidence_by_requirement:
            blockers.append(
                SpecIssue(
                    rule="evidence_coverage",
                    message=f"需求 {requirement_id} 没有证据包",
                    ref=requirement_id,
                )
            )
    trace_by_requirement: dict[str, int] = {}
    for row in spec.trace:
        trace_by_requirement[row.requirement_id] = (
            trace_by_requirement.get(row.requirement_id, 0) + 1
        )
        if row.requirement_id not in extracted_ids:
            blockers.append(
                SpecIssue(
                    rule="trace_completeness",
                    message=f"追溯矩阵引用了不存在的需求 {row.requirement_id}",
                    ref=row.requirement_id,
                )
            )
        if row.task not in task_ids:
            blockers.append(
                SpecIssue(
                    rule="trace_completeness",
                    message=f"追溯矩阵引用了不存在的任务 {row.task}",
                    ref=row.requirement_id,
                )
            )
    for requirement_id in sorted(extracted_ids):
        count = trace_by_requirement.get(requirement_id, 0)
        if count != 1:
            blockers.append(
                SpecIssue(
                    rule="trace_completeness",
                    message=f"需求 {requirement_id} 在追溯矩阵出现 {count} 次（应为 1）",
                    ref=requirement_id,
                )
            )
        located = evidence_by_requirement.get(requirement_id)
        if located is not None:
            trace_row = next(
                (item for item in spec.trace if item.requirement_id == requirement_id), None
            )
            if trace_row is not None and trace_row.code_location not in evidence_locators(
                located.items
            ):
                blockers.append(
                    SpecIssue(
                        rule="trace_location",
                        message=(
                            f"需求 {requirement_id} 的代码位置 {trace_row.code_location!r} "
                            "无法回指证据"
                        ),
                        ref=requirement_id,
                    )
                )

    # 6. 方案决策
    if not spec.decisions:
        blockers.append(SpecIssue(rule="decision_present", message="缺少方案决策（decisions）"))

    # 7. 状态与开放问题
    open_questions = [
        question.id for question in spec.questions if question.status.value != "closed"
    ]
    if open_questions and spec.status.value == "approved":
        blockers.append(
            SpecIssue(
                rule="status_questions",
                message=f"仍有未关闭问题却标记 approved: {', '.join(open_questions)}",
            )
        )
    if open_questions:
        info.append(
            SpecIssue(
                rule="status_questions",
                message=f"存在 {len(open_questions)} 个未关闭问题，状态保持 needs-info",
            )
        )

    return ValidationReport(
        spec_id=spec.spec_id,
        blockers=blockers,
        warnings=warnings,
        info=info,
    )


__all__ = ["evidence_locators", "validate_spec"]
