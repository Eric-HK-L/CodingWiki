"""补丁检查编排（计划 14.6.3）：收集 diff → 应用策略 → 落 check.json。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from codingwiki.domain.patch import (
    FindingSeverity,
    PatchFinding,
    PatchPolicy,
    PatchReport,
    PatchStats,
    PatchVerdict,
    WorkspaceRecord,
)
from codingwiki.domain.spec import DesignTask, SpecDocument, SpecStatus
from codingwiki.errors import InputError
from codingwiki.patch.diff import FileDiff, collect_diffs
from codingwiki.patch.policy import (
    content_findings,
    limit_findings,
    path_findings,
    stats_of,
)
from codingwiki.patch.protocol import WorktreeGit
from codingwiki.patch.workspace import WorkspaceManager


def load_approved_task(spec_dir: str | Path, task_id: str) -> DesignTask:
    """从已 approved 的 Spec 读取任务；未批准或任务不存在都拒绝。"""
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
            f"Spec {spec.spec_id} 状态为 {spec.status.value}，只有 approved Spec 才能进入编码"
        )
    for task in spec.tasks:
        if task.id == task_id:
            return task
    raise InputError(f"Spec {spec.spec_id} 没有任务 {task_id}")


def check_workspace(
    provider: WorktreeGit,
    workspace: WorkspaceRecord,
    policy: PatchPolicy,
    manager: WorkspaceManager,
    *,
    now: datetime | None = None,
) -> PatchReport:
    """检查工作区相对基线的改动并写 check.json；不自动修改或回滚。"""
    if not workspace.active:
        raise InputError(f"任务 {workspace.task_id} 的工作区已 {workspace.status.value}，不能检查")
    worktree = Path(workspace.path)
    if not worktree.is_dir():
        raise InputError(f"工作树不存在：{worktree}")

    diffs = collect_diffs(provider, worktree, workspace.base_revision)
    diffs = _normalize_root(diffs, workspace.root)
    stats = stats_of(diffs)
    findings: list[PatchFinding] = [
        *limit_findings(stats, policy),
        *path_findings(diffs, policy),
        *content_findings(diffs, policy),
    ]
    verdict = (
        PatchVerdict.FAIL
        if any(item.severity is FindingSeverity.BLOCKER for item in findings)
        else PatchVerdict.PASS
    )
    report = PatchReport(
        repo_id=workspace.repo_id,
        task_id=workspace.task_id,
        base_revision=workspace.base_revision,
        worktree=str(worktree),
        verdict=verdict,
        stats=PatchStats(
            files=stats.files,
            added_lines=stats.added_lines,
            deleted_lines=stats.deleted_lines,
        ),
        files=[item.to_change() for item in diffs],
        findings=findings,
        policy=policy,
        created_at=now or datetime.now(UTC),
    )
    manager.save_report(report.model_dump_json(indent=2), workspace.repo_id, workspace.task_id)
    return report


def _normalize_root(diffs: list[FileDiff], root: str) -> list[FileDiff]:
    """把仓库内子目录（RepoConfig.root）下的路径归一化成 Spec 的写法。

    根目录之外的改动保留原路径（例如仓库级 CI 配置），仍会被禁止规则拦下。
    """
    if not root:
        return diffs
    prefix = root.strip("/") + "/"
    return [
        replace(item, path=item.path[len(prefix) :]) if item.path.startswith(prefix) else item
        for item in diffs
    ]


__all__ = ["check_workspace", "load_approved_task"]
