"""Spec 自动校验（计划 13.8 十项检查）。

纯函数：输入 Spec / 抽取产物 / 证据包，输出 ValidationReport；
不读写文件、不调用模型。规则按固定顺序追加，报告可逐字节复算。
"""

from __future__ import annotations

import re

from codingwiki.domain.spec import (
    SPEC_SECTIONS,
    EvidenceBundle,
    EvidenceItem,
    ExtractionResult,
    RequirementKind,
    SpecDocument,
    SpecIssue,
    SpecStatus,
    ValidationReport,
)

MAX_TASK_FILES = 3
MAX_TASK_LINES = 300

_CODE_REF = re.compile(r"`([^`\n]+)`")
_PLACEHOLDER_TESTS = ("待补充", "不适用", "TODO", "TBD")
_ROLLBACK_WORDS = ("回滚", "rollback")
_SECURITY_WORDS = ("安全", "security")
_SECURITY_TRIGGERS = (
    "输入",
    "权限",
    "配置",
    "认证",
    "授权",
    "密钥",
    "环境变量",
    "config",
    "input",
    "permission",
    "auth",
)
_ID_PREFIXES = ("R-", "N-", "Q-", "T-", "D-", "F-", "AC-")


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


def _section_body(spec: SpecDocument, number: int) -> str:
    for section in spec.sections:
        if section.number == number:
            return section.body
    return ""


def _looks_like_locator(value: str) -> bool:
    """正文反引号中的代码/Wiki 位置候选；排除需求/任务编号与 URL。"""
    if not value or " " in value or "\t" in value:
        return False
    if value.startswith(("http://", "https://")):
        return False
    if value.startswith(_ID_PREFIXES):
        return False
    if ":" in value or "@" in value:
        return True
    tail = value.rsplit("/", 1)[-1]
    return "/" in value and "." in tail


def _code_refs(body: str) -> list[str]:
    return [
        value.strip() for value in _CODE_REF.findall(body) if _looks_like_locator(value.strip())
    ]


def _is_identifier(term: str) -> bool:
    """术语检查只针对强标识符，避免自然语言被误判。"""
    if len(term) < 5 or "/" in term or "." in term:
        return False
    return "_" in term or any(char.isupper() for char in term[1:])


def _symbol_name(symbol_id: str) -> str:
    body = symbol_id.split("::", 1)[1] if "::" in symbol_id else symbol_id
    body = body.rsplit("@", 1)[0]
    return body.split("::")[-1]


def _glossary(evidence: EvidenceBundle) -> set[str]:
    terms: set[str] = set()
    for requirement in evidence.requirements:
        terms.update(requirement.impact.config)
        terms.update(requirement.impact.build)
        terms.update(requirement.impact.tests)
        for item in requirement.items:
            if item.symbol_id:
                terms.add(_symbol_name(item.symbol_id))
    return {term for term in terms if _is_identifier(term)}


def _placeholder_test(value: str) -> bool:
    return any(marker in value for marker in _PLACEHOLDER_TESTS)


def _evidence_items(evidence: EvidenceBundle) -> list[EvidenceItem]:
    return [item for requirement in evidence.requirements for item in requirement.items]


def _open_questions(spec: SpecDocument) -> list[str]:
    return [question.id for question in spec.questions if question.status.value != "closed"]


def validate_spec(
    spec: SpecDocument,
    extraction: ExtractionResult,
    evidence: EvidenceBundle,
) -> ValidationReport:
    """计划 13.8 十项检查 + 结构闭环；返回 blockers/warnings/info 供修复循环回喂。"""
    blockers: list[SpecIssue] = []
    warnings: list[SpecIssue] = []
    info: list[SpecIssue] = []

    spec_ids = {requirement.id for requirement in spec.requirements}
    extracted_ids = {requirement.id for requirement in extraction.requirements}
    task_ids = {task.id for task in spec.tasks}
    evidence_by_requirement = {item.requirement_id: item for item in evidence.requirements}

    # 0. 11 章结构与编号
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

    # 1. 需求覆盖：模型不得新增/遗漏
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

    # 2. 功能需求至少 1 条验收标准
    for requirement in spec.requirements:
        if requirement.kind is RequirementKind.FUNCTIONAL and not requirement.acceptance:
            blockers.append(
                SpecIssue(
                    rule="acceptance_criteria",
                    message=f"功能需求 {requirement.id} 没有验收标准",
                    ref=requirement.id,
                )
            )

    # 3. 代码出处：现状/影响章节的显式位置必须命中证据
    locators: set[str] = set()
    for evidence_requirement in evidence.requirements:
        locators.update(evidence_locators(evidence_requirement.items))
    for section_number in (3, 6):
        body = _section_body(spec, section_number)
        for ref in _code_refs(body):
            if ref not in locators:
                blockers.append(
                    SpecIssue(
                        rule="code_source",
                        message=(f"第 {section_number} 章的代码结论 `{ref}` 在证据包中没有出处"),
                        ref=f"S{section_number}",
                    )
                )

    # 4. 追溯完整：需求恰好一行、任务至少一行、引用存在、位置回指证据
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
    traced_tasks = {row.task for row in spec.trace}
    for task in spec.tasks:
        if task.id not in traced_tasks:
            blockers.append(
                SpecIssue(
                    rule="trace_completeness",
                    message=f"任务 {task.id} 没有被任何需求引用（孤立任务）",
                    ref=task.id,
                )
            )

    # 5. 任务大小：模型层已限制文件数与行数，这里复核编号与依赖
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

    # 6. 方案与影响面一致：任务文件必须来自证据影响面或证据路径
    impact_paths: set[str] = set()
    for evidence_requirement in evidence.requirements:
        impact_paths.update(evidence_requirement.impact.files)
        impact_paths.update(item.path for item in evidence_requirement.items if item.path)
    for task in spec.tasks:
        for path in task.files:
            if path not in impact_paths:
                blockers.append(
                    SpecIssue(
                        rule="task_scope",
                        message=f"任务 {task.id} 的文件 {path} 不在证据影响面内",
                        ref=task.id,
                    )
                )

    # 7. 测试计划：功能需求在第 7 章出现，且追溯行给出测试
    test_body = _section_body(spec, 7)
    for requirement in spec.requirements:
        if requirement.kind is not RequirementKind.FUNCTIONAL or not requirement.acceptance:
            continue
        if requirement.id not in test_body:
            blockers.append(
                SpecIssue(
                    rule="test_plan",
                    message=f"第 7 章测试计划没有覆盖需求 {requirement.id}",
                    ref=requirement.id,
                )
            )
        tests = [
            row.test for row in spec.trace if row.requirement_id == requirement.id and row.test
        ]
        if not tests or all(_placeholder_test(value) for value in tests):
            warnings.append(
                SpecIssue(
                    rule="test_plan",
                    message=f"需求 {requirement.id} 没有已定位的测试证据，测试计划待评审确认",
                    ref=requirement.id,
                )
            )

    # 8. 术语一致：强标识符的大小写/拼写漂移
    for term in sorted(_glossary(evidence)):
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        drift: str | None = None
        for section in spec.sections:
            for match in pattern.finditer(section.body):
                if match.group(0) != term:
                    drift = match.group(0)
                    break
            if drift is not None:
                break
        if drift is not None:
            blockers.append(
                SpecIssue(
                    rule="term_consistency",
                    message=f"术语 {term!r} 在正文写成 {drift!r}，与证据不一致",
                    ref=term,
                )
            )

    # 9. 安全：涉及输入/权限/配置的需求须有安全考虑
    security_relevant: list[str] = []
    for requirement in spec.requirements:
        pack = evidence_by_requirement.get(requirement.id)
        triggered = any(trigger in requirement.text.lower() for trigger in _SECURITY_TRIGGERS)
        if triggered or (pack is not None and bool(pack.impact.config)):
            security_relevant.append(requirement.id)
    risk_body = _section_body(spec, 9)
    if security_relevant and not any(word in risk_body.lower() for word in _SECURITY_WORDS):
        warnings.append(
            SpecIssue(
                rule="security_review",
                message=(
                    "需求涉及输入/权限/配置，但第 9 章没有安全考虑：" + ", ".join(security_relevant)
                ),
                ref="S9",
            )
        )

    # 10. 回滚：第 9 章必须有回滚步骤
    if not any(word in risk_body.lower() for word in _ROLLBACK_WORDS):
        blockers.append(SpecIssue(rule="rollback", message="第 9 章缺少回滚步骤", ref="S9"))

    # 11. 决策：至少一条方案决策
    if not spec.decisions:
        blockers.append(SpecIssue(rule="decision_present", message="缺少方案决策（decisions）"))

    # 12. 证据覆盖：快照一致、每条需求有证据、证据条目绑定同一快照
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
    for item in _evidence_items(evidence):
        if item.snapshot_id != spec.snapshot_id:
            blockers.append(
                SpecIssue(
                    rule="evidence_coverage",
                    message=f"证据 {item.path or item.symbol_id or item.page_id} 快照不一致",
                )
            )
            break

    # 13. 状态与开放问题：未关闭问题只能停留在 needs-info
    open_questions = _open_questions(spec)
    if open_questions and spec.status in (SpecStatus.DRAFT, SpecStatus.APPROVED):
        blockers.append(
            SpecIssue(
                rule="status_questions",
                message=(f"仍有未关闭问题却标记 {spec.status.value}: {', '.join(open_questions)}"),
            )
        )
    if open_questions:
        info.append(
            SpecIssue(
                rule="status_questions",
                message=f"存在 {len(open_questions)} 个未关闭问题，状态应保持 needs-info",
            )
        )

    return ValidationReport(
        spec_id=spec.spec_id,
        blockers=blockers,
        warnings=warnings,
        info=info,
    )


__all__ = ["evidence_locators", "validate_spec"]
