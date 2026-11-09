"""失败处理与人工升级（开发计划 14.10）。

失败运行不自动重试、不降级；升级单把“任务/需求、已尝试、失败信息、
需要人做的决定、建议下一步、产物”整理成可审计的 JSON 与 Markdown。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from codingwiki.domain.coding import (
    CodedTask,
    CodingRun,
    CodingRunStatus,
    CodingStopReason,
    EscalationFailure,
    EscalationReason,
    EscalationReport,
    StageReport,
    StageStatus,
    VerifierIssue,
)
from codingwiki.domain.patch import FindingSeverity, PatchFinding
from codingwiki.domain.spec import QuestionStatus, SpecDocument
from codingwiki.errors import InputError

_SECURITY_HINTS = ("secret", "credential", "security")
_TAIL_CHARS = 300

_STOP_REASONS: dict[CodingStopReason, EscalationReason] = {
    CodingStopReason.BASELINE_FAILED: EscalationReason.ENVIRONMENT_BROKEN,
    CodingStopReason.BACKEND_FAILED: EscalationReason.BACKEND_FAILED,
    CodingStopReason.GUARD_REJECTED: EscalationReason.FORBIDDEN_FILE,
    CodingStopReason.VERIFY_REJECTED: EscalationReason.VERIFIER_REJECTED,
    CodingStopReason.NO_PROGRESS: EscalationReason.NO_PROGRESS,
    CodingStopReason.ROUNDS_EXHAUSTED: EscalationReason.ROUNDS_EXHAUSTED,
    CodingStopReason.TIME_EXCEEDED: EscalationReason.BUDGET_EXCEEDED,
    CodingStopReason.TOKEN_EXCEEDED: EscalationReason.BUDGET_EXCEEDED,
    CodingStopReason.DIFF_GROWING: EscalationReason.DIFF_GROWING,
    CodingStopReason.FLAKY_BASELINE: EscalationReason.FLAKY_TESTS,
    CodingStopReason.RED_GATE_FAILED: EscalationReason.TEST_NOT_RED,
}

_DECISIONS: dict[EscalationReason, tuple[str, ...]] = {
    EscalationReason.REQUIREMENT_DECISION: (
        "确认 Spec 开放问题的归属与结论",
        "判断当前失败是否源于需求歧义",
        "决定是否暂停该任务直到需求澄清",
    ),
    EscalationReason.ENVIRONMENT_BROKEN: (
        "确认构建环境（工具链/依赖/磁盘/网络）是否可用",
        "决定修复环境还是调整命令配置",
    ),
    EscalationReason.BACKEND_FAILED: (
        "检查模型后端进程、凭据与超时配置",
        "决定更换后端、稍后重试或人工实现",
    ),
    EscalationReason.FORBIDDEN_FILE: (
        "确认越界文件是否应纳入允许清单",
        "决定扩大 Spec files 还是要求模型改回",
    ),
    EscalationReason.SECURITY_ALERT: (
        "人工审计命中的密钥/安全规则",
        "确认是否需要吊销凭据或清理工作区",
    ),
    EscalationReason.UNSAFE_CHANGE: (
        "人工审计命中的危险调用",
        "确认该调用是否为任务必需",
        "决定要求模型改写、调整策略还是人工实现",
    ),
    EscalationReason.DEPENDENCY_CHANGE: (
        "确认新增依赖是否在 Spec 中明确允许",
        "评估依赖来源、许可证与离线安装可行性",
        "决定批准依赖变更、要求模型改用现有依赖还是人工实现",
    ),
    EscalationReason.NO_PROGRESS: (
        "判断重复错误是任务拆分问题还是模型能力问题",
        "决定人工修复、重新拆分或更换后端",
    ),
    EscalationReason.ROUNDS_EXHAUSTED: (
        "评估残余失败是否可人工快速修复",
        "决定提高轮次上限还是回滚重做",
    ),
    EscalationReason.VERIFIER_REJECTED: (
        "判断验证问题是否成立",
        "决定人工修改、调整验证器规则或重新拆任务",
    ),
    EscalationReason.TEST_NOT_RED: (
        "确认新增/修改的测试是否覆盖任务验收标准",
        "决定补充断言、调整测试范围还是人工实现",
        "若必须“先实现接口再补测试”，在 Spec/任务说明中登记例外后重跑",
    ),
    EscalationReason.BUDGET_EXCEEDED: (
        "确认耗时/词元预算是否合理",
        "决定缩小任务、拆分还是提高预算",
        "决定人工完成剩余部分",
    ),
    EscalationReason.DIFF_GROWING: (
        "判断模型是否在反复重写而非收敛",
        "决定重新规划任务还是人工接管",
    ),
    EscalationReason.FLAKY_TESTS: (
        "确认关键测试不稳定的根因（时序/资源/数据）",
        "决定先修测试、扩大隔离范围还是暂停自动编码",
    ),
}

_STEPS: dict[EscalationReason, tuple[str, ...]] = {
    EscalationReason.REQUIREMENT_DECISION: (
        "关闭 Spec 开放问题并重新评审",
        "更新任务验收标准后重跑编码",
        "若需求不确定，先拆分或缩小任务范围",
    ),
    EscalationReason.ENVIRONMENT_BROKEN: (
        "人工复跑基线命令并修复环境",
        "修复后重新运行同一任务",
        "若工具链不可用，改用可编译的验证环境",
    ),
    EscalationReason.BACKEND_FAILED: (
        "查看 backend.log 与 result.json 定位失败点",
        "修复后端配置后重跑",
        "必要时先 rollback 工作区再重试",
    ),
    EscalationReason.FORBIDDEN_FILE: (
        "人工查看 check.json 中的越界 diff",
        "修改 Spec 文件清单并重新评审后重跑",
        "或要求模型只改允许文件",
    ),
    EscalationReason.SECURITY_ALERT: (
        "立即停止自动流程并查看命中 diff",
        "按安全流程处理凭据",
        "确认无风险后再决定回滚或继续",
    ),
    EscalationReason.UNSAFE_CHANGE: (
        "查看 check.json 中 diff.dangerous_call 的具体行",
        "要求模型移除危险调用或改用受控 API",
        "若任务确需该能力，调整任务/策略后重新评审",
    ),
    EscalationReason.DEPENDENCY_CHANGE: (
        "查看 check.json 中 dep.new_dependency 的依赖清单与新增行",
        "确认依赖是否已在 Spec/任务说明中登记并获人批准",
        "批准则在 Spec 明确范围后用 --allow-new-dependencies 重跑，否则要求模型改用现有依赖",
    ),
    EscalationReason.NO_PROGRESS: (
        "对照 errors.json 分析重复根因",
        "补充任务包上下文或拆分任务",
        "必要时人工直接修改并复验",
    ),
    EscalationReason.ROUNDS_EXHAUSTED: (
        "查看最后一轮 errors.json 与 stages.json",
        "人工修复残余失败并跑回归",
        "若任务过大，拆分后重新编码",
    ),
    EscalationReason.VERIFIER_REJECTED: (
        "逐条核对 verification.json 的 blocker 与证据",
        "问题成立则人工修复后复验",
        "问题不成立则调整验证规则并重跑",
    ),
    EscalationReason.TEST_NOT_RED: (
        "查看 redgate.json 的测试命令、退出码与输出",
        "补充能复现新行为的测试断言后重跑",
        "确属接口先行场景时，缩小任务或调整 Spec 后重新评审",
    ),
    EscalationReason.BUDGET_EXCEEDED: (
        "查看 report.json 的耗时与词元统计",
        "缩小任务或提高预算后重跑",
        "保留工作区以便人工接续",
    ),
    EscalationReason.DIFF_GROWING: (
        "对比相邻轮次 check.json 的 diff 规模",
        "回滚工作区并按更小任务重试",
        "若属正常增量，调高 diff-growth-rounds",
    ),
    EscalationReason.FLAKY_TESTS: (
        "查看基线 stages.json 中标记的 flaky 用例",
        "修复测试或调整 critical_tests 配置后重跑",
        "不要通过跳过测试绕过不稳定用例",
    ),
}


def escalation_reason(run: CodingRun, spec: SpecDocument) -> EscalationReason:
    """按开放问题、安全命中、stop_reason 的顺序判定升级原因。"""
    if run.status is CodingRunStatus.SUCCEEDED or run.stop_reason is CodingStopReason.SUCCEEDED:
        raise InputError(f"运行 {run.run_id} 已成功，不需要升级")
    if any(question.status is QuestionStatus.OPEN for question in spec.questions):
        return EscalationReason.REQUIREMENT_DECISION
    if run.stop_reason in {
        CodingStopReason.GUARD_REJECTED,
        CodingStopReason.VERIFY_REJECTED,
    }:
        codes = {item.code for item in _guard_blockers(run)}
        codes |= {item.code for item in _verifier_blockers(run)}
        if any(hint in code for code in codes for hint in _SECURITY_HINTS):
            return EscalationReason.SECURITY_ALERT
        if any(code == "diff.dangerous_call" for code in codes):
            return EscalationReason.UNSAFE_CHANGE
        if any(code == "dep.new_dependency" for code in codes):
            return EscalationReason.DEPENDENCY_CHANGE
    return _STOP_REASONS[run.stop_reason]


def build_escalation(
    run: CodingRun,
    spec: SpecDocument,
    task: CodedTask,
    run_dir: Path | None = None,
    *,
    now: datetime | None = None,
) -> EscalationReport:
    """生成升级单；成功运行属调用错误。"""
    reason = escalation_reason(run, spec)
    return EscalationReport(
        run_id=run.run_id,
        repo_id=run.repo_id,
        spec_id=run.spec_id,
        task_id=run.task_id,
        status=run.status,
        stop_reason=run.stop_reason,
        reason=reason,
        summary=_summary(run),
        attempted_rounds=run.rounds,
        max_rounds=run.max_rounds,
        attempts=_attempts(run),
        failures=_failures(run),
        decisions=_decisions(reason, run, spec),
        suggested_next_steps=list(_STEPS[reason]),
        artifacts=_artifacts(run, run_dir),
        workspace=run.workspace,
        created_at=now or datetime.now(UTC),
    )


def render_escalation(report: EscalationReport, spec: SpecDocument, task: CodedTask) -> str:
    """渲染人可读的 escalation.md。"""
    lines = [
        f"# 升级单：{spec.spec_id} / {task.id}",
        "",
        f"- 运行：{report.run_id}",
        f"- 仓库：{report.repo_id}",
        f"- 状态：{report.status.value}（{report.stop_reason.value}）",
        f"- 轮次：{report.attempted_rounds}/{report.max_rounds}",
        f"- 原因：{report.reason.value}",
        f"- 摘要：{report.summary}",
        "",
        "## 任务与需求",
        f"- 标题：{task.title}",
        f"- 目标：{task.goal}",
        f"- 需求：{', '.join(task.requirement_ids) or 'Spec 未提供追溯行'}",
        f"- 允许修改：{', '.join(task.files_allowed)}",
        f"- 验收：{'; '.join(task.acceptance)}",
        f"- 工作区：{report.workspace or '（无）'}",
        "",
        "## 已尝试",
        *_numbered(report.attempts),
        "",
        "## 失败信息",
        *_failure_lines(report.failures),
        "",
        "## 需要人做的决定",
        *_numbered(report.decisions),
        "",
        "## 建议下一步",
        *_numbered(report.suggested_next_steps),
        "",
        "## 产物",
        *_bullets(report.artifacts),
    ]
    return "\n".join(lines) + "\n"


def _summary(run: CodingRun) -> str:
    if run.message:
        return run.message
    return f"{run.stop_reason.value}（{run.rounds}/{run.max_rounds} 轮）"


def _attempts(run: CodingRun) -> list[str]:
    if not run.round_reports:
        return ["基线：未进入编码轮次"]
    return [f"第 {report.index} 轮：{report.message or '无说明'}" for report in run.round_reports]


def _failures(run: CodingRun) -> list[EscalationFailure]:
    items: list[EscalationFailure] = []
    source = f"round-{run.rounds}" if run.rounds else "baseline"
    items.extend(
        EscalationFailure(
            source=source,
            code=item.code,
            message=item.message,
            file=item.file,
            line=item.line,
        )
        for item in run.errors
    )
    if run.stop_reason is CodingStopReason.BASELINE_FAILED:
        items.extend(
            EscalationFailure(
                source="baseline",
                code=f"{stage.kind.value}.{stage.status.value}",
                message=_stage_tail(stage),
            )
            for stage in run.baseline
            if stage.status is not StageStatus.OK
        )
    if run.stop_reason in {CodingStopReason.BASELINE_FAILED, CodingStopReason.FLAKY_BASELINE}:
        items.extend(
            EscalationFailure(
                source="baseline",
                code="test.flaky",
                message=f"不稳定测试：{ident}",
            )
            for ident in run.quarantined_tests
        )
    items.extend(
        EscalationFailure(source="guard", code=item.code, message=item.message, file=item.path)
        for item in _guard_blockers(run)
    )
    items.extend(
        EscalationFailure(
            source="verifier",
            code=item.code,
            message=item.message,
            file=item.file,
            line=item.line,
        )
        for item in _verifier_blockers(run)
    )
    if not items:
        items.append(
            EscalationFailure(
                source="run",
                code=run.stop_reason.value,
                message=run.message or run.stop_reason.value,
            )
        )
    return items


def _decisions(reason: EscalationReason, run: CodingRun, spec: SpecDocument) -> list[str]:
    items = list(_DECISIONS[reason])
    if reason is EscalationReason.FORBIDDEN_FILE:
        paths = sorted({item.path for item in _guard_blockers(run) if item.path})
        if paths:
            items.append("确认是否允许修改：" + ", ".join(paths[:5]))
    elif reason is EscalationReason.SECURITY_ALERT:
        codes = sorted({item.code for item in _guard_blockers(run)})
        codes += sorted({item.code for item in _verifier_blockers(run)})
        if codes:
            items.append("人工审计命中的规则：" + ", ".join(codes))
    elif reason is EscalationReason.UNSAFE_CHANGE:
        codes = sorted(
            {item.code for item in _guard_blockers(run) if item.code == "diff.dangerous_call"}
        )
        if codes:
            items.append("危险调用规则：" + ", ".join(codes))
    elif reason is EscalationReason.REQUIREMENT_DECISION:
        open_ids = [item.id for item in spec.questions if item.status is QuestionStatus.OPEN]
        if open_ids:
            items.append("关闭或转交 Spec 开放问题：" + ", ".join(open_ids))
    return items


def _artifacts(run: CodingRun, run_dir: Path | None) -> list[str]:
    paths: list[str] = []
    if run_dir is not None:
        for name in (
            "run.json",
            "check.json",
            "verification.json",
            "audit/commands.jsonl",
            "baseline/stages.json",
        ):
            path = run_dir / name
            if path.is_file():
                paths.append(str(path))
        for report in run.round_reports:
            path = run_dir / f"round-{report.index}"
            if path.is_dir():
                paths.append(str(path))
    if run.workspace:
        paths.append(run.workspace)
    return paths


def _guard_blockers(run: CodingRun) -> list[PatchFinding]:
    if run.guard is None:
        return []
    return [item for item in run.guard.findings if item.severity is FindingSeverity.BLOCKER]


def _verifier_blockers(run: CodingRun) -> list[VerifierIssue]:
    if run.verifier is None:
        return []
    return run.verifier.blockers


def _stage_tail(stage: StageReport) -> str:
    text = stage.stderr_tail.strip() or stage.stdout_tail.strip()
    if text:
        return text[-_TAIL_CHARS:]
    return f"{stage.kind.value} 阶段失败"


def _failure_lines(failures: list[EscalationFailure]) -> list[str]:
    if not failures:
        return ["- （无）"]
    lines: list[str] = []
    for item in failures:
        location = f"{item.file}:{item.line}" if item.file else "-"
        lines.append(f"- [{item.source}/{item.code}] {location} {item.message}")
    return lines


def _numbered(items: list[str]) -> list[str]:
    if not items:
        return ["1. （无）"]
    return [f"{index}. {item}" for index, item in enumerate(items, start=1)]


def _bullets(items: list[str]) -> list[str]:
    if not items:
        return ["- （无）"]
    return [f"- {item}" for item in items]


__all__ = ["build_escalation", "escalation_reason", "render_escalation"]
