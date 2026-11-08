"""补丁重放与报告可复现（架构 §9 / 开发计划 14.8）。

用原运行的基线快照 + 工作区 diff，在独立工作树上重放并重跑阶段命令；
逐阶段对比状态与退出码，结论落 replay.json。只覆盖已跟踪文件的改动。
"""

from __future__ import annotations

import contextlib
import shutil
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from codingwiki.coding.commands import stage_specs
from codingwiki.coding.run import run_stages
from codingwiki.domain.coding import (
    CodedTask,
    CodingRun,
    ReplayCheck,
    ReplayReport,
    ReplayStatus,
    StageReport,
)
from codingwiki.domain.models import RepoConfig
from codingwiki.domain.patch import PatchVerdict
from codingwiki.domain.spec import SpecDocument
from codingwiki.errors import InputError
from codingwiki.exec import LocalCommandRunner
from codingwiki.exec.protocol import CommandRunner, CommandSpec
from codingwiki.patch import WorkspaceManager
from codingwiki.patch.protocol import WorktreeGit


def replay_run(
    provider: WorktreeGit,
    manager: WorkspaceManager,
    repo: RepoConfig,
    spec: SpecDocument,
    task: CodedTask,
    run: CodingRun,
    *,
    runner: CommandRunner | None = None,
    now: datetime | None = None,
) -> ReplayReport:
    """重放一次运行；工作区必须仍保留原补丁（未回滚/未关闭）。"""
    if task.spec_id != spec.spec_id:
        raise InputError(f"任务 {task.id} 归属 {task.spec_id}，与 Spec {spec.spec_id} 不一致")
    if run.guard is None or run.guard.verdict is not PatchVerdict.PASS:
        raise InputError(f"运行 {run.run_id} 的 PatchGuard 未通过，拒绝重放")
    if not run.stages:
        raise InputError(f"运行 {run.run_id} 没有阶段结果，无法对比重放")
    workspace = Path(run.workspace)
    if not workspace.is_dir():
        raise InputError(f"工作区不存在：{workspace}")
    record = manager.load(run.repo_id, run.task_id)

    patch = provider.diff_unified(workspace, run.base_revision)
    if not patch.strip():
        raise InputError(f"工作区没有可重放的补丁（可能已回滚）：{workspace}")

    run_dir = manager.home / "coding" / repo.repo_id / spec.spec_id / task.id / "runs" / run.run_id
    replay_dir = run_dir / "replay"
    replay_dir.mkdir(parents=True, exist_ok=True)
    patch_path = replay_dir / "patch.diff"
    patch_path.write_text(patch, encoding="utf-8")
    digest = sha256(patch.encode("utf-8")).hexdigest()
    worktree = replay_dir / "worktree"
    _clear_worktree(provider, worktree)

    status = ReplayStatus.FAILED
    checks: list[ReplayCheck] = []
    message = ""
    try:
        provider.create_worktree(repo, run.base_revision, worktree)
        applied = LocalCommandRunner().run(
            CommandSpec(
                argv=["git", "apply", "--binary", str(patch_path)],
                cwd=str(worktree),
                timeout_s=120,
                label="replay",
            )
        )
        if applied.exit_code != 0:
            message = f"补丁应用失败（exit {applied.exit_code}）：{_tail(applied.stderr)}"
        else:
            source_root = worktree / record.root if record.root else worktree
            actual = run_stages(
                stage_specs(repo, workspace_root=source_root), runner or LocalCommandRunner()
            )
            _write_stages(replay_dir, actual)
            checks = _compare(run.stages, actual)
            matched = sum(1 for item in checks if item.matched)
            status = ReplayStatus.REPRODUCED if matched == len(checks) else ReplayStatus.DIVERGED
            message = f"{matched}/{len(checks)} 个阶段与原运行一致"
    finally:
        _clear_worktree(provider, worktree)

    report = ReplayReport(
        run_id=run.run_id,
        repo_id=run.repo_id,
        spec_id=run.spec_id,
        task_id=run.task_id,
        base_revision=run.base_revision,
        patch_sha256=digest,
        status=status,
        checks=checks,
        workspace=str(workspace),
        replay_dir=str(replay_dir),
        patch_path=str(patch_path),
        message=message,
        created_at=now or datetime.now(UTC),
    )
    (run_dir / "replay.json").write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return report


def _compare(expected: list[StageReport], actual: list[StageReport]) -> list[ReplayCheck]:
    checks: list[ReplayCheck] = []
    for index, want in enumerate(expected):
        got = actual[index] if index < len(actual) else None
        checks.append(
            ReplayCheck(
                kind=want.kind,
                expected_status=want.status,
                expected_exit_code=want.exit_code,
                actual_status=got.status if got is not None else None,
                actual_exit_code=got.exit_code if got is not None else 0,
                matched=got is not None
                and got.status is want.status
                and got.exit_code == want.exit_code,
            )
        )
    return checks


def _write_stages(replay_dir: Path, stages: list[StageReport]) -> None:
    text = "[\n" + ",\n".join(item.model_dump_json(indent=2) for item in stages) + "\n]\n"
    (replay_dir / "stages.json").write_text(text, encoding="utf-8")


def _clear_worktree(provider: WorktreeGit, path: Path) -> None:
    if not path.exists():
        return
    with contextlib.suppress(Exception):
        provider.remove_worktree(path)
    shutil.rmtree(path, ignore_errors=True)


def _tail(text: str, limit: int = 300) -> str:
    return text.strip()[-limit:]


__all__ = ["replay_run"]
