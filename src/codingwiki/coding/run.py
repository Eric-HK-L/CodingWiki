"""编码编排：基线 →（任务包 → 后端 → PatchGuard → 构建/测试）* → run.json（S20）。

模型只改文件；命令、diff 检查、失败回喂全部由编排器负责。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from codingwiki.coding.backend import run_backend
from codingwiki.coding.commands import stage_specs
from codingwiki.coding.errors import parse_failures, timeout_failure
from codingwiki.coding.packet import build_packet
from codingwiki.domain.coding import (
    BackendConfig,
    BackendResult,
    BackendStatus,
    CodedTask,
    CodingRun,
    CodingRunStatus,
    CodingStopReason,
    FailureItem,
    RoundReport,
    StageKind,
    StageReport,
    StageStatus,
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
    runner: CommandRunner | None = None,
    now: datetime | None = None,
) -> CodingRun:
    """基线 → 多轮编码；成功保留工作区，失败保留证据，不自动回滚。"""
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
    run_dir.mkdir(parents=True, exist_ok=True)

    policy = PatchPolicy(allowed_paths=task.files_allowed)
    source_root = workspace / record.root if record.root else workspace
    pairs = stage_specs(repo, workspace_root=source_root)
    runner = runner or LocalCommandRunner()

    baseline = _run_stages(pairs, runner)
    if any(item.status is not StageStatus.OK for item in baseline):
        failed = next(item for item in baseline if item.status is not StageStatus.OK)
        baseline_errors = _errors_of(baseline)
        _write_models(run_dir / "baseline" / "stages.json", baseline)
        _write_models(run_dir / "baseline" / "errors.json", baseline_errors)
        run = _build_run(
            run_id,
            repo,
            spec,
            task,
            record,
            workspace,
            status=CodingRunStatus.FAILED,
            stop_reason=CodingStopReason.BASELINE_FAILED,
            rounds=0,
            max_rounds=max_rounds,
            baseline=baseline,
            message=f"基线未通过：{failed.kind.value}（exit {failed.exit_code}），拒绝交给模型",
            now=now,
        )
        _write_model(run_dir / "run.json", run)
        return run

    stop: CodingStopReason | None = None
    stop_message = ""
    reports: list[RoundReport] = []
    errors: list[FailureItem] = []
    final_backend: BackendResult | None = None
    final_guard: PatchReport | None = None
    packet_dir = ""
    previous_keys: set[tuple[str, str, int, str]] | None = None

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
        )
        guard = check_workspace(provider, record, policy, manager, now=now)
        _write_model(round_dir / "check.json", guard)
        _write_model(run_dir / "check.json", guard)
        final_backend, final_guard = backend, guard
        stages: list[StageReport] = []
        round_errors: list[FailureItem] = []
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
            stages = _run_stages(pairs, runner)
            _write_models(round_dir / "stages.json", stages)
            failed_stage = next(
                (item for item in stages if item.status is not StageStatus.OK), None
            )
            if failed_stage is None:
                stop = CodingStopReason.SUCCEEDED
                stop_message = f"第 {index} 轮通过：{_stage_summary(stages)}"
                round_message = stop_message
            else:
                round_errors = _errors_of(stages)
                errors = round_errors
                _write_models(round_dir / "errors.json", errors)
                round_message = (
                    f"第 {index} 轮未通过：{failed_stage.kind.value}（{len(round_errors)} 项失败）"
                )
                keys = {item.key for item in errors}
                if previous_keys is not None and keys == previous_keys:
                    stop = CodingStopReason.NO_PROGRESS
                    stop_message = "连续两轮同一错误，停止并转人工"
                elif index == max_rounds:
                    stop = CodingStopReason.ROUNDS_EXHAUSTED
                    stop_message = f"{max_rounds} 轮后仍有 {len(errors)} 项失败"
                previous_keys = keys

        reports.append(
            RoundReport(
                index=index,
                backend=backend,
                guard=guard,
                stages=stages,
                errors=round_errors,
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
        round_reports=reports,
        message=stop_message,
        now=now,
    )
    _write_model(run_dir / "run.json", run)
    return run


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
    round_reports: list[RoundReport] | None = None,
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
        backend=backend,
        guard=guard,
        baseline=baseline,
        stages=stages or [],
        errors=errors or [],
        round_reports=round_reports or [],
        message=message,
        created_at=now or datetime.now(UTC),
    )


def _run_stages(
    pairs: list[tuple[StageKind, CommandSpec]], runner: CommandRunner
) -> list[StageReport]:
    """顺序执行；首个失败即停（编译失败就不跑测试）。"""
    reports: list[StageReport] = []
    for kind, spec in pairs:
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
        reports.append(report)
        if report.status is not StageStatus.OK:
            break
    return reports


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
        items.extend(
            parse_failures(stage.kind, stage.stdout_tail, stage.stderr_tail, limit=remaining)
        )
    return items[:_ERROR_LIMIT]


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


__all__ = ["load_approved_spec", "run_task"]
