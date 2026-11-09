"""编码编排：基线 →（任务包 → 后端 → PatchGuard → 构建/测试）* → run.json（S20）。

模型只改文件；命令、diff 检查、失败回喂全部由编排器负责。
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path

from pydantic import BaseModel

from codingwiki.coding.audit import AuditRunner, AuditSink, assert_command_allowed
from codingwiki.coding.backend import run_backend
from codingwiki.coding.commands import stage_specs
from codingwiki.coding.delivery import build_delivery
from codingwiki.coding.errors import parse_failures, timeout_failure
from codingwiki.coding.escalate import build_escalation, render_escalation
from codingwiki.coding.junit import JUnitConfig, load_config, parse_reports
from codingwiki.coding.metrics import run_report, tokens_of_result
from codingwiki.coding.packet import build_packet
from codingwiki.coding.redgate import check_red_gate
from codingwiki.coding.verify import verify_patch
from codingwiki.domain.coding import (
    BackendConfig,
    BackendResult,
    BackendStatus,
    CodedTask,
    CodingRun,
    CodingRunStatus,
    CodingStopReason,
    FailureItem,
    RedGateReport,
    RedGateStatus,
    RoundReport,
    RunLimits,
    StageKind,
    StageReport,
    StageStatus,
    VerifierReport,
    VerifierVerdict,
)
from codingwiki.domain.models import RepoConfig
from codingwiki.domain.patch import (
    FindingSeverity,
    PatchFinding,
    PatchPolicy,
    PatchReport,
    PatchVerdict,
    WorkspaceRecord,
)
from codingwiki.domain.spec import SpecDocument, SpecStatus
from codingwiki.errors import InputError
from codingwiki.exec import LocalCommandRunner
from codingwiki.exec.protocol import CommandRunner, CommandSpec
from codingwiki.patch import WorkspaceManager
from codingwiki.patch.check import check_workspace
from codingwiki.patch.protocol import WorktreeGit

_ERROR_LIMIT = 20
_TEST_STAGES = frozenset({StageKind.TEST_FAST, StageKind.TEST_FULL})


def load_approved_spec(spec_dir: str | Path) -> SpecDocument:
    directory = Path(spec_dir)
    spec_path = directory / "spec.json" if directory.is_dir() else directory
    if not spec_path.is_file():
        raise InputError(f"Spec 文件不存在：{spec_path}")
    try:
        spec = SpecDocument.model_validate_json(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(f"Spec 不可读：{spec_path}: {exc}") from exc
    if spec.status is not SpecStatus.APPROVED:
        raise InputError(
            f"Spec {spec.spec_id} 状态为 {spec.status.value}，只有 approved Spec 才能编码"
        )
    return spec


def run_task(
    provider: WorktreeGit,
    manager: WorkspaceManager,
    repo: RepoConfig,
    spec: SpecDocument,
    task: CodedTask,
    config: BackendConfig,
    *,
    max_rounds: int = 5,
    verifier: BackendConfig | None = None,
    runner: CommandRunner | None = None,
    limits: RunLimits | None = None,
    now: datetime | None = None,
) -> CodingRun:
    """基线 → 多轮编码 →（可选）独立验证；成功保留工作区，失败保留证据，不自动回滚。

    预算按已计量命令耗时与后端自报词元累计；连续 diff 增长达到阈值也停止。
    """
    limits = limits or RunLimits()
    if max_rounds < 1:
        raise InputError(f"max_rounds 必须 ≥1（实际 {max_rounds}）")
    if task.spec_id != spec.spec_id:
        raise InputError(f"任务 {task.id} 归属 {task.spec_id}，与 Spec {spec.spec_id} 不一致")

    record = manager.load(repo.repo_id, task.id)
    if not record.active:
        raise InputError(f"任务 {task.id} 的工作区已 {record.status.value}，不能编码")
    workspace = Path(record.path)
    if not workspace.is_dir():
        raise InputError(f"工作树不存在：{workspace}")
    if provider.worktree_status(workspace).strip():
        raise InputError(f"工作区不干净，拒绝开始编码：{workspace}")
    _assert_revision(spec.snapshot_id, record.base_revision)

    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d%H%M%S")
    run_id = f"RUN-{task.id}-{stamp}"
    run_dir = manager.home / "coding" / repo.repo_id / spec.spec_id / task.id / "runs" / run_id

    policy = PatchPolicy(allowed_paths=task.files_allowed)
    source_root = workspace / record.root if record.root else workspace
    tmp_dir = run_dir / "tmp"
    pairs = stage_specs(repo, workspace_root=source_root, tmp_dir=tmp_dir)
    repair_pairs = stage_specs(repo, workspace_root=source_root, tmp_dir=tmp_dir, incremental=True)
    junit = load_config(repo.commands.test, source_root=source_root)
    for kind, stage_spec in [*pairs, *repair_pairs]:
        assert_command_allowed(stage_spec.argv, source=f"阶段 {kind.value}")
    assert_command_allowed(config.command, source="编码后端")
    if verifier is not None:
        assert_command_allowed(verifier.command, source="验证后端")

    run_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    audit_path = run_dir / "audit" / "commands.jsonl"
    sink = AuditSink(audit_path)
    runner = AuditRunner(runner or LocalCommandRunner(), sink)

    baseline = run_stages(pairs, runner, junit=junit, attempts=repo.commands.test.baseline_runs)
    used_ms = sum(item.duration_ms for item in baseline)
    used_tokens = 0
    baseline_errors = _errors_of(baseline)
    quarantined = _quarantined(baseline)
    critical = _critical_flaky(quarantined, repo.commands.test.critical_tests)
    if baseline_errors or critical:
        _write_models(run_dir / "baseline" / "stages.json", baseline)
        _write_models(run_dir / "baseline" / "errors.json", baseline_errors)
        if critical:
            stop_reason = CodingStopReason.FLAKY_BASELINE
            message = _flaky_baseline_message(critical, quarantined)
        else:
            stop_reason = CodingStopReason.BASELINE_FAILED
            message = _baseline_message(baseline)
        run = _build_run(
            run_id,
            repo,
            spec,
            task,
            record,
            workspace,
            status=CodingRunStatus.FAILED,
            stop_reason=stop_reason,
            rounds=0,
            max_rounds=max_rounds,
            baseline=baseline,
            message=message,
            limits=limits,
            quarantined_tests=quarantined,
            now=now,
            audit_path=str(audit_path),
        )
        return _finish_run(run_dir, run, spec, task, now=now)

    stop: CodingStopReason | None = None
    stop_message = ""
    reports: list[RoundReport] = []
    errors: list[FailureItem] = []
    final_backend: BackendResult | None = None
    final_guard: PatchReport | None = None
    final_verifier: VerifierReport | None = None
    final_red_gate: RedGateReport | None = None
    packet_dir = ""
    previous_keys: set[tuple[str, str, int, str]] | None = None
    previous_diff: int | None = None
    growth_streak = 0

    for index in range(1, max_rounds + 1):
        round_dir = run_dir / f"round-{index}"
        packet = build_packet(
            repo_id=repo.repo_id,
            spec=spec,
            task=task,
            workspace=workspace,
            base_revision=record.base_revision,
            packet_dir=round_dir / "packet",
            policy=policy,
            root=record.root,
            errors=errors or None,
            round_index=index,
            now=now,
        )
        packet_dir = packet.packet_dir
        backend = run_backend(
            config,
            packet,
            workspace,
            round_dir / "result.json",
            round_dir / "backend.log",
            audit=sink,
        )
        guard = check_workspace(provider, record, policy, manager, now=now)
        _write_model(round_dir / "check.json", guard)
        _write_model(run_dir / "check.json", guard)
        final_backend, final_guard = backend, guard
        diff_lines = guard.stats.total_lines
        if previous_diff is not None and diff_lines > previous_diff:
            growth_streak += 1
        elif previous_diff is not None:
            growth_streak = 0
        previous_diff = diff_lines
        stages: list[StageReport] = []
        round_errors: list[FailureItem] = []
        round_verifier: VerifierReport | None = None
        round_gate: RedGateReport | None = None
        round_message = ""

        if backend.status is not BackendStatus.OK:
            stop = CodingStopReason.BACKEND_FAILED
            stop_message = f"后端未成功：{backend.status.value}"
            round_message = stop_message
        elif guard.verdict is PatchVerdict.FAIL:
            stop = CodingStopReason.GUARD_REJECTED
            stop_message = _guard_message(guard.findings)
            round_message = stop_message
        else:
            round_gate = check_red_gate(
                provider,
                repo,
                record,
                workspace,
                policy,
                round_dir / "redgate",
                runner=runner,
                now=now,
            )
            if round_gate is not None:
                final_red_gate = round_gate
            if round_gate is not None and round_gate.status is RedGateStatus.GREEN:
                stop = CodingStopReason.RED_GATE_FAILED
                stop_message = (
                    "测试先行门禁："
                    f"{', '.join(round_gate.test_files)} 在基线 revision 已通过，未覆盖新行为"
                )
                round_message = stop_message
                round_errors = [
                    FailureItem(
                        stage=StageKind.TEST_FAST,
                        code="test.first_green",
                        message=stop_message,
                    )
                ]
            else:
                stages = run_stages(
                    repair_pairs,
                    runner,
                    junit=junit,
                    attempts=repo.commands.test.retries + 1,
                    retry_on_failure=True,
                )
                _write_models(round_dir / "stages.json", stages)
                failed_stage = next(
                    (item for item in stages if item.status is not StageStatus.OK), None
                )
                skipped = sum(len(item.test_skipped) for item in stages)
                round_flaky = sorted({ident for item in stages for ident in item.flaky_tests})
                if failed_stage is None and not skipped:
                    if verifier is not None:
                        verify_dir = round_dir / "verify"
                        round_verifier = verify_patch(
                            provider,
                            verifier,
                            spec,
                            task,
                            guard,
                            stages,
                            workspace,
                            record.root,
                            verify_dir,
                            verify_dir / "result.json",
                            verify_dir / "backend.log",
                            now=now,
                            audit=sink,
                        )
                        final_verifier = round_verifier
                        _write_model(round_dir / "verification.json", round_verifier)
                        _write_model(run_dir / "verification.json", round_verifier)
                    if (
                        round_verifier is not None
                        and round_verifier.verdict is VerifierVerdict.FAIL
                    ):
                        stop = CodingStopReason.VERIFY_REJECTED
                        stop_message = f"独立验证未通过：{len(round_verifier.blockers)} 项 blocker"
                        round_message = stop_message
                    else:
                        stop = CodingStopReason.SUCCEEDED
                        stop_message = f"第 {index} 轮通过：{_stage_summary(stages)}"
                        if round_verifier is not None:
                            stop_message = f"{stop_message}，独立验证通过"
                        if round_flaky:
                            stop_message = (
                                f"{stop_message}；不稳定测试 {len(round_flaky)} 项（重试后通过）"
                            )
                        round_message = stop_message
                else:
                    round_errors = _errors_of(stages)
                    errors = round_errors
                    _write_models(round_dir / "errors.json", errors)
                    if failed_stage is None:
                        round_message = (
                            f"第 {index} 轮未通过：{skipped} 项测试被跳过（跳过不算通过）"
                        )
                    else:
                        round_message = (
                            f"第 {index} 轮未通过：{failed_stage.kind.value}"
                            f"（{len(round_errors)} 项失败）"
                        )
                    keys = {item.key for item in errors}
                    if previous_keys is not None and keys == previous_keys:
                        stop = CodingStopReason.NO_PROGRESS
                        stop_message = "连续两轮同一错误，停止并转人工"
                    elif index == max_rounds:
                        stop = CodingStopReason.ROUNDS_EXHAUSTED
                        stop_message = f"{max_rounds} 轮后仍有 {len(errors)} 项失败"
                    previous_keys = keys

        token_in, token_out = tokens_of_result(backend)
        used_ms += backend.duration_ms + sum(item.duration_ms for item in stages)
        used_tokens += token_in + token_out
        if stop is not CodingStopReason.SUCCEEDED:
            budget_reason = _budget_stop(limits, used_ms, used_tokens)
            if budget_reason is not None:
                stop = budget_reason
                stop_message = _budget_message(limits, used_ms, used_tokens)
                round_message = f"{round_message}；{stop_message}".lstrip("；")
            elif limits.diff_growth_rounds and growth_streak >= limits.diff_growth_rounds:
                stop = CodingStopReason.DIFF_GROWING
                stop_message = (
                    f"连续 {growth_streak} 轮 diff 行数增长（当前 {diff_lines} 行），"
                    "停止并要求重新规划"
                )
                round_message = f"{round_message}；{stop_message}".lstrip("；")

        reports.append(
            RoundReport(
                index=index,
                backend=backend,
                guard=guard,
                stages=stages,
                errors=round_errors,
                verifier=round_verifier,
                red_gate=round_gate,
                message=round_message,
            )
        )
        if stop is not None:
            break

    run = _build_run(
        run_id,
        repo,
        spec,
        task,
        record,
        workspace,
        status=(
            CodingRunStatus.SUCCEEDED
            if stop is CodingStopReason.SUCCEEDED
            else CodingRunStatus.FAILED
        ),
        stop_reason=stop or CodingStopReason.ROUNDS_EXHAUSTED,
        rounds=len(reports),
        max_rounds=max_rounds,
        baseline=baseline,
        packet_dir=packet_dir,
        backend=final_backend,
        guard=final_guard,
        stages=reports[-1].stages if reports else [],
        errors=reports[-1].errors if reports else [],
        verifier=final_verifier,
        red_gate=final_red_gate,
        round_reports=reports,
        limits=limits,
        quarantined_tests=quarantined,
        message=stop_message,
        now=now,
        audit_path=str(audit_path),
    )
    if run.status is CodingRunStatus.SUCCEEDED:
        build_delivery(provider, run, spec, task, record, run_dir, now=now)
        run = run.model_copy(update={"delivery_path": str(run_dir / "delivery")})
    return _finish_run(run_dir, run, spec, task, now=now)


def _build_run(
    run_id: str,
    repo: RepoConfig,
    spec: SpecDocument,
    task: CodedTask,
    record: WorkspaceRecord,
    workspace: Path,
    *,
    status: CodingRunStatus,
    stop_reason: CodingStopReason,
    rounds: int,
    max_rounds: int,
    baseline: list[StageReport],
    message: str,
    packet_dir: str = "",
    backend: BackendResult | None = None,
    guard: PatchReport | None = None,
    stages: list[StageReport] | None = None,
    errors: list[FailureItem] | None = None,
    verifier: VerifierReport | None = None,
    red_gate: RedGateReport | None = None,
    round_reports: list[RoundReport] | None = None,
    limits: RunLimits | None = None,
    quarantined_tests: list[str] | None = None,
    audit_path: str = "",
    now: datetime | None = None,
) -> CodingRun:
    return CodingRun(
        run_id=run_id,
        repo_id=repo.repo_id,
        spec_id=spec.spec_id,
        task_id=task.id,
        base_revision=record.base_revision,
        workspace=str(workspace),
        status=status,
        stop_reason=stop_reason,
        rounds=rounds,
        max_rounds=max_rounds,
        packet_dir=packet_dir,
        audit_path=audit_path,
        backend=backend,
        guard=guard,
        baseline=baseline,
        stages=stages or [],
        errors=errors or [],
        verifier=verifier,
        red_gate=red_gate,
        round_reports=round_reports or [],
        limits=limits or RunLimits(),
        quarantined_tests=quarantined_tests or [],
        message=message,
        created_at=now or datetime.now(UTC),
    )


def _finish_run(
    run_dir: Path,
    run: CodingRun,
    spec: SpecDocument,
    task: CodedTask,
    *,
    now: datetime | None,
) -> CodingRun:
    """写 run.json 与 report.json；失败时追加 escalation.json/md。"""
    shutil.rmtree(run_dir / "tmp", ignore_errors=True)
    if run.status is CodingRunStatus.FAILED:
        escalation_path = run_dir / "escalation.json"
        run = run.model_copy(update={"escalation_path": str(escalation_path)})
        _write_model(run_dir / "run.json", run)
        _write_model(run_dir / "report.json", run_report(run))
        escalation = build_escalation(run, spec, task, run_dir, now=now)
        _write_model(escalation_path, escalation)
        (run_dir / "escalation.md").write_text(
            render_escalation(escalation, spec, task), encoding="utf-8"
        )
        return run
    _write_model(run_dir / "run.json", run)
    _write_model(run_dir / "report.json", run_report(run))
    return run


def run_stages(
    pairs: list[tuple[StageKind, CommandSpec]],
    runner: CommandRunner,
    *,
    junit: JUnitConfig | None = None,
    attempts: int = 1,
    retry_on_failure: bool = False,
) -> list[StageReport]:
    """顺序执行；首个失败即停（编译失败就不跑测试）。

    测试阶段按 attempts 重复：基线用于识别不稳定用例（每次都跑）；
    修复轮次只在用例失败时重跑（retry_on_failure），重跑后仍失败的才算失败。
    每个测试阶段执行完按配置解析 JUnit 报告；只接受本轮新生成的报告文件。
    """
    reports: list[StageReport] = []
    for kind, spec in pairs:
        if kind in _TEST_STAGES and attempts > 1:
            report = _run_test_stage(
                kind,
                spec,
                runner,
                junit,
                attempts=attempts,
                retry_on_failure=retry_on_failure,
            )
        else:
            report = _run_stage_once(kind, spec, runner, junit)
        reports.append(report)
        if report.status is not StageStatus.OK:
            break
    return reports


def _run_stage_once(
    kind: StageKind, spec: CommandSpec, runner: CommandRunner, junit: JUnitConfig | None
) -> StageReport:
    started_ns = time.time_ns()
    result = runner.run(spec)
    report = StageReport(
        kind=kind,
        command=spec.argv,
        status=StageStatus(result.status.value),
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        stdout_tail=result.stdout[-4000:],
        stderr_tail=result.stderr[-4000:],
        artifacts=result.artifacts,
    )
    if junit is not None and kind in _TEST_STAGES:
        parsed = parse_reports(junit, since_ns=started_ns)
        report = report.model_copy(
            update={
                "report_files": parsed.files,
                "test_failures": parsed.failures,
                "test_skipped": parsed.skipped,
                "report_error": "; ".join(parsed.errors),
            }
        )
    return report


def _run_test_stage(
    kind: StageKind,
    spec: CommandSpec,
    runner: CommandRunner,
    junit: JUnitConfig | None,
    *,
    attempts: int,
    retry_on_failure: bool,
) -> StageReport:
    collected: list[StageReport] = []
    for _index in range(attempts):
        report = _run_stage_once(kind, spec, runner, junit)
        collected.append(report)
        if not retry_on_failure:
            continue
        if report.status is StageStatus.OK:
            break
        if not report.tests_parsed:
            # 超时/报告缺失或坏 XML 重跑不会改变结论，保留证据即可。
            break
    return _combine_attempts(collected)


def _combine_attempts(attempts: list[StageReport]) -> StageReport:
    """合并同一测试命令的多次执行：只有每次都失败的用例才算真失败。"""
    first = attempts[0]
    if len(attempts) == 1:
        return first
    last = attempts[-1]
    total_ms = sum(item.duration_ms for item in attempts)
    parsed = [item for item in attempts if item.tests_parsed]
    if len(parsed) != len(attempts):
        status = (
            StageStatus.OK
            if any(item.status is StageStatus.OK for item in attempts)
            else last.status
        )
        return last.model_copy(
            update={"attempts": len(attempts), "duration_ms": total_ms, "status": status}
        )
    failure_sets = [{item.id for item in report.test_failures} for report in parsed]
    persistent = set.intersection(*failure_sets)
    flaky = sorted(set.union(*failure_sets) - persistent)
    failures = [item for item in last.test_failures if item.id in persistent]
    skipped = list(last.test_skipped)
    rescued = not persistent and not skipped and last.status is not StageStatus.OK
    status = StageStatus.OK if last.status is StageStatus.OK or rescued else last.status
    return last.model_copy(
        update={
            "status": status,
            "duration_ms": total_ms,
            "test_failures": failures,
            "test_skipped": skipped,
            "flaky_tests": flaky,
            "attempts": len(attempts),
        }
    )


def _errors_of(stages: list[StageReport]) -> list[FailureItem]:
    items: list[FailureItem] = []
    for stage in stages:
        if stage.status is StageStatus.OK:
            continue
        if stage.status is StageStatus.TIMEOUT:
            items.append(timeout_failure(stage.kind, stage.duration_ms))
            continue
        remaining = _ERROR_LIMIT - len(items)
        if remaining <= 0:
            break
        if stage.test_failures:
            items.extend(_test_failures(stage)[:remaining])
            continue
        items.extend(
            parse_failures(stage.kind, stage.stdout_tail, stage.stderr_tail, limit=remaining)
        )
    for stage in stages:
        if len(items) >= _ERROR_LIMIT:
            break
        items.extend(_skipped_failures(stage)[: _ERROR_LIMIT - len(items)])
    return items[:_ERROR_LIMIT]


def _test_failures(stage: StageReport) -> list[FailureItem]:
    return [
        FailureItem(
            stage=stage.kind,
            code=f"test:{item.id}",
            file=item.file,
            line=item.line,
            message=item.message or item.id,
        )
        for item in stage.test_failures
    ]


def _skipped_failures(stage: StageReport) -> list[FailureItem]:
    return [
        FailureItem(stage=stage.kind, code="test.skipped", message=f"测试被跳过：{ident}")
        for ident in stage.test_skipped
    ]


def _budget_stop(limits: RunLimits, used_ms: int, used_tokens: int) -> CodingStopReason | None:
    if limits.max_duration_s > 0 and used_ms >= limits.max_duration_s * 1000:
        return CodingStopReason.TIME_EXCEEDED
    if limits.max_tokens > 0 and used_tokens >= limits.max_tokens:
        return CodingStopReason.TOKEN_EXCEEDED
    return None


def _budget_message(limits: RunLimits, used_ms: int, used_tokens: int) -> str:
    if limits.max_duration_s > 0 and used_ms >= limits.max_duration_s * 1000:
        return f"预算耗尽：已计量耗时 {used_ms / 1000:.1f}s / {limits.max_duration_s}s"
    return f"预算耗尽：后端自报 tokens {used_tokens} / {limits.max_tokens}"


def _quarantined(stages: list[StageReport]) -> list[str]:
    return sorted({ident for stage in stages for ident in stage.flaky_tests})


def _critical_flaky(quarantined: list[str], patterns: list[str]) -> list[str]:
    if not patterns:
        return []
    return [
        ident for ident in quarantined if any(fnmatchcase(ident, pattern) for pattern in patterns)
    ]


def _flaky_baseline_message(critical: list[str], quarantined: list[str]) -> str:
    names = ", ".join(critical[:5])
    return f"关键测试不稳定：{names}；先修复测试再自动编码（基线隔离 {len(quarantined)} 项）"


def _baseline_message(baseline: list[StageReport]) -> str:
    failed = next((item for item in baseline if item.status is not StageStatus.OK), None)
    if failed is None:
        skipped = sum(len(item.test_skipped) for item in baseline)
        return f"基线未通过：{skipped} 项测试被跳过（跳过不算通过），拒绝交给模型"
    message = f"基线未通过：{failed.kind.value}（exit {failed.exit_code}）"
    pre_existing = len({item.id for item in failed.test_failures})
    if pre_existing:
        message += f"；其中 {pre_existing} 项为原有失败（不算本任务引入）"
    quarantined = len(_quarantined(baseline))
    if quarantined:
        message += f"；另有 {quarantined} 项不稳定测试已隔离"
    return f"{message}，拒绝交给模型"


def _stage_summary(stages: list[StageReport]) -> str:
    return "→".join(f"{item.kind.value}:{item.status.value}" for item in stages) or "无命令"


def _guard_message(findings: list[PatchFinding]) -> str:
    codes = sorted({item.code for item in findings if item.severity is FindingSeverity.BLOCKER})
    return f"PatchGuard 拒绝：{', '.join(codes)}"


def _write_model(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _write_models(path: Path, items: Sequence[BaseModel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        [item.model_dump(mode="json") for item in items], ensure_ascii=False, indent=2
    )
    path.write_text(text + "\n", encoding="utf-8")


def _assert_revision(snapshot_id: str, base_revision: str) -> None:
    token = snapshot_id.rsplit("@", 1)[-1] if "@" in snapshot_id else snapshot_id
    if not token:
        raise InputError(f"Spec snapshot_id 缺少 revision：{snapshot_id}")
    if not (base_revision.startswith(token) or token.startswith(base_revision)):
        raise InputError(f"Spec 快照 {snapshot_id} 与工作区基线 {base_revision} 不一致")


__all__ = ["load_approved_spec", "run_stages", "run_task"]
