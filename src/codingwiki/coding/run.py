"""首轮编码编排：任务包 → 后端进程 → PatchGuard → run.json（S19）。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from codingwiki.coding.backend import run_backend
from codingwiki.coding.packet import build_packet
from codingwiki.domain.coding import (
    BackendConfig,
    BackendStatus,
    CodedTask,
    CodingRun,
    CodingRunStatus,
)
from codingwiki.domain.models import RepoConfig
from codingwiki.domain.patch import FindingSeverity, PatchFinding, PatchPolicy, PatchVerdict
from codingwiki.domain.spec import SpecDocument, SpecStatus
from codingwiki.errors import InputError
from codingwiki.patch.check import check_workspace
from codingwiki.patch.protocol import WorktreeGit
from codingwiki.patch.workspace import WorkspaceManager


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
    now: datetime | None = None,
) -> CodingRun:
    """执行一轮：建包、调后端、PatchGuard、落 run.json；不自动回滚。"""
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
    packet = build_packet(
        repo_id=repo.repo_id,
        spec=spec,
        task=task,
        workspace=workspace,
        base_revision=record.base_revision,
        packet_dir=run_dir / "packet",
        policy=policy,
        root=record.root,
        now=now,
    )
    backend = run_backend(
        config,
        packet,
        workspace,
        run_dir / "result.json",
        run_dir / "backend.log",
    )
    guard = check_workspace(provider, record, policy, manager, now=now)
    (run_dir / "check.json").write_text(guard.model_dump_json(indent=2) + "\n", encoding="utf-8")

    succeeded = backend.status is BackendStatus.OK and guard.verdict is PatchVerdict.PASS
    message = _message(backend.status, guard.verdict, guard.findings)
    run = CodingRun(
        run_id=run_id,
        repo_id=repo.repo_id,
        spec_id=spec.spec_id,
        task_id=task.id,
        base_revision=record.base_revision,
        workspace=str(workspace),
        status=CodingRunStatus.SUCCEEDED if succeeded else CodingRunStatus.FAILED,
        rounds=1,
        packet_dir=packet.packet_dir,
        backend=backend,
        guard=guard,
        message=message,
        created_at=now or datetime.now(UTC),
    )
    (run_dir / "run.json").write_text(run.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return run


def _assert_revision(snapshot_id: str, base_revision: str) -> None:
    token = snapshot_id.rsplit("@", 1)[-1] if "@" in snapshot_id else snapshot_id
    if not token:
        raise InputError(f"Spec snapshot_id 缺少 revision：{snapshot_id}")
    if not (base_revision.startswith(token) or token.startswith(base_revision)):
        raise InputError(f"Spec 快照 {snapshot_id} 与工作区基线 {base_revision} 不一致")


def _message(status: BackendStatus, verdict: PatchVerdict, findings: list[PatchFinding]) -> str:
    if status is not BackendStatus.OK:
        return f"后端未成功：{status.value}"
    if verdict is PatchVerdict.FAIL:
        codes = sorted({item.code for item in findings if item.severity is FindingSeverity.BLOCKER})
        return f"PatchGuard 拒绝：{', '.join(codes)}"
    return "首轮编码通过"


__all__ = ["load_approved_spec", "run_task"]
