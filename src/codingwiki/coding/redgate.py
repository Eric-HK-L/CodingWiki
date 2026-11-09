"""测试先行红门禁（开发计划 14.5.2）。

补丁同时含源码与测试改动时，在基线 revision 的临时工作树上只应用测试改动，
运行 fast 测试并要求它失败（红）。若测试在基线就通过，说明测试没有覆盖新行为，
本轮退回并转人工；门禁本身无法执行时只留 ERROR 证据，不阻断正常阶段。
"""

from __future__ import annotations

import contextlib
import shutil
from datetime import UTC, datetime
from pathlib import Path

from codingwiki.coding.commands import stage_specs
from codingwiki.domain.coding import RedGateReport, RedGateStatus, StageKind
from codingwiki.domain.models import RepoConfig
from codingwiki.domain.patch import ChangeKind, PatchPolicy, WorkspaceRecord
from codingwiki.exec import LocalCommandRunner
from codingwiki.exec.protocol import CommandRunner, CommandStatus
from codingwiki.patch.diff import collect_diffs
from codingwiki.patch.policy import is_code_path, is_test_path
from codingwiki.patch.protocol import WorktreeGit

_TAIL_CHARS = 2000


def check_red_gate(
    provider: WorktreeGit,
    repo: RepoConfig,
    record: WorkspaceRecord,
    workspace: Path,
    policy: PatchPolicy,
    gate_dir: Path,
    *,
    runner: CommandRunner | None = None,
    now: datetime | None = None,
) -> RedGateReport | None:
    """补丁含源码 + 测试改动时返回门禁报告；不适用（缺任一侧/未配置 fast）返回 None。"""
    if not policy.require_red_gate or not repo.commands.test.fast.strip():
        return None
    diffs = collect_diffs(provider, workspace, record.base_revision)
    tests = [
        item for item in diffs if is_test_path(item.path) and item.kind is not ChangeKind.DELETED
    ]
    code = [
        item
        for item in diffs
        if is_code_path(item.path)
        and not is_test_path(item.path)
        and item.kind is not ChangeKind.DELETED
    ]
    if not tests or not code:
        return None

    gate_dir.mkdir(parents=True, exist_ok=True)
    worktree = gate_dir / "worktree"
    report_path = gate_dir / "redgate.json"
    _clear_worktree(provider, worktree)
    status = RedGateStatus.ERROR
    command: list[str] = []
    exit_code = 0
    duration_ms = 0
    stdout_tail = ""
    stderr_tail = ""
    message = ""
    try:
        provider.create_worktree(repo, record.base_revision, worktree)
        for item in tests:
            _copy_file(workspace / item.path, worktree / item.path)
        source_root = worktree / record.root if record.root else worktree
        fast = next(
            (
                stage
                for stage in stage_specs(repo, workspace_root=source_root)
                if stage[0] is StageKind.TEST_FAST
            ),
            None,
        )
        if fast is None:
            message = "仓库未配置 fast 测试，红门禁未执行"
        else:
            result = (runner or LocalCommandRunner()).run(fast[1])
            command = fast[1].argv
            exit_code = result.exit_code
            duration_ms = result.duration_ms
            stdout_tail = result.stdout[-_TAIL_CHARS:]
            stderr_tail = result.stderr[-_TAIL_CHARS:]
            if result.status is CommandStatus.OK:
                status = RedGateStatus.GREEN
                message = "测试在基线 revision 已通过，未覆盖新行为（tests 先绿）"
            else:
                status = RedGateStatus.RED
                message = f"测试在基线 revision 失败（exit {exit_code}），符合红门禁预期"
    except Exception as exc:  # 门禁是辅助检查，不能中断正常编码轮次
        message = f"红门禁无法执行：{exc}"
    finally:
        _clear_worktree(provider, worktree)

    report = RedGateReport(
        repo_id=record.repo_id,
        task_id=record.task_id,
        base_revision=record.base_revision,
        status=status,
        test_files=[_display_path(item.path, record.root) for item in tests],
        command=command,
        exit_code=exit_code,
        duration_ms=duration_ms,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        message=message,
        worktree=str(worktree),
        report_path=str(report_path),
        created_at=now or datetime.now(UTC),
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return report


def _display_path(path: str, root: str) -> str:
    prefix = f"{root}/" if root else ""
    if prefix and path.startswith(prefix):
        return path[len(prefix) :]
    return path


def _copy_file(source: Path, target: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"测试文件不存在：{source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _clear_worktree(provider: WorktreeGit, path: Path) -> None:
    if not path.exists():
        return
    with contextlib.suppress(Exception):
        provider.remove_worktree(path)
    shutil.rmtree(path, ignore_errors=True)


__all__ = ["check_red_gate"]
