"""隔离工作区生命周期（计划 14.6.1）。

每个任务一个独立 worktree，从固定 revision 建分支；记录落
`.codingwiki/patches/<repo>/<task>/workspace.json`。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from codingwiki.domain.models import RepoConfig
from codingwiki.domain.patch import WorkspaceRecord, WorkspaceStatus
from codingwiki.errors import InputError, InternalError
from codingwiki.patch.protocol import WorktreeGit

WORKSPACE_FILE = "workspace.json"
CHECK_FILE = "check.json"


def default_home() -> Path:
    return Path(os.environ.get("CODINGWIKI_HOME", ".codingwiki"))


class WorkspaceManager:
    """工作区创建、加载、回滚与关闭。只读操作（list/load）不需要 provider。"""

    def __init__(self, provider: WorktreeGit | None = None, home: str | Path | None = None) -> None:
        self._provider = provider
        self._home = Path(home).resolve() if home is not None else default_home().resolve()

    def _git(self) -> WorktreeGit:
        if self._provider is None:
            raise InternalError("未提供 git provider，无法执行工作区写操作")
        return self._provider

    @property
    def home(self) -> Path:
        return self._home

    # ------------------------------------------------------------------ 路径

    def worktree_path(self, repo_id: str, task_id: str) -> Path:
        return self._home / "worktrees" / repo_id / task_id

    def record_dir(self, repo_id: str, task_id: str) -> Path:
        return self._home / "patches" / repo_id / task_id

    def record_path(self, repo_id: str, task_id: str) -> Path:
        return self.record_dir(repo_id, task_id) / WORKSPACE_FILE

    def report_path(self, repo_id: str, task_id: str) -> Path:
        return self.record_dir(repo_id, task_id) / CHECK_FILE

    # ------------------------------------------------------------------ 生命周期

    def open(
        self,
        repo: RepoConfig,
        task_id: str,
        revision: str,
        *,
        branch: str = "",
        now: datetime | None = None,
    ) -> WorkspaceRecord:
        existing = self._load_optional(repo.repo_id, task_id)
        if existing is not None and existing.active:
            raise InputError(f"任务 {task_id} 的隔离工作区已存在：{existing.path}")

        worktree = self.worktree_path(repo.repo_id, task_id)
        if worktree.exists() and any(worktree.iterdir()):
            raise InputError(f"工作树路径已存在且非空：{worktree}")

        branch_name = branch or f"cw/{repo.repo_id}/{task_id}"
        provider = self._git()
        if branch and provider.branch_exists(repo, branch):
            raise InputError(f"分支已存在，拒绝覆盖：{branch}")
        if not branch and provider.branch_exists(repo, branch_name):
            branch_name = f"{branch_name}-{datetime.now(UTC):%Y%m%d%H%M%S}"

        provider.create_worktree(repo, revision, worktree, branch_name)
        record = WorkspaceRecord(
            repo_id=repo.repo_id,
            task_id=task_id,
            base_revision=revision,
            branch=branch_name,
            path=str(worktree.resolve()),
            root=repo.root,
            status=WorkspaceStatus.ACTIVE,
            created_at=now or datetime.now(UTC),
        )
        self._save(record)
        return record

    def load(self, repo_id: str, task_id: str) -> WorkspaceRecord:
        record = self._load_optional(repo_id, task_id)
        if record is None:
            raise InputError(
                f"任务 {task_id} 没有隔离工作区记录：{self.record_path(repo_id, task_id)}"
            )
        return record

    def list(self, repo_id: str) -> list[WorkspaceRecord]:
        base = self._home / "patches" / repo_id
        if not base.is_dir():
            return []
        records = []
        for directory in sorted(base.iterdir()):
            record = self._load_optional(repo_id, directory.name)
            if record is not None:
                records.append(record)
        return records

    def rollback(
        self, repo_id: str, task_id: str, *, now: datetime | None = None
    ) -> WorkspaceRecord:
        record = self.load(repo_id, task_id)
        if not record.active:
            raise InputError(f"任务 {task_id} 的工作区已 {record.status.value}，不能回滚")
        worktree = Path(record.path)
        if not worktree.is_dir():
            raise InputError(f"工作树不存在：{worktree}")

        self._git().reset_worktree(worktree, record.base_revision)
        self._git().clean_worktree(worktree)
        status = self._git().worktree_status(worktree).strip()
        if status:
            raise InternalError(f"回滚后工作区仍不干净：{status}")

        updated = record.model_copy(update={"last_rollback_at": now or datetime.now(UTC)})
        self._save(updated)
        return updated

    def close(
        self,
        repo: RepoConfig,
        task_id: str,
        *,
        keep_branch: bool = False,
        now: datetime | None = None,
    ) -> WorkspaceRecord:
        record = self.load(repo.repo_id, task_id)
        if not record.active:
            raise InputError(f"任务 {task_id} 的工作区已 {record.status.value}，不能重复关闭")

        worktree = Path(record.path)
        if worktree.is_dir():
            self._git().remove_worktree(worktree)
        if not keep_branch and record.branch:
            self._git().delete_branch(repo, record.branch)

        updated = record.model_copy(
            update={
                "status": WorkspaceStatus.KEPT if keep_branch else WorkspaceStatus.DISCARDED,
            }
        )
        self._save(updated)
        return updated

    # ------------------------------------------------------------------ 落盘

    def save_report(self, report_json: str, repo_id: str, task_id: str) -> Path:
        path = self.report_path(repo_id, task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report_json + "\n", encoding="utf-8")
        return path

    def _save(self, record: WorkspaceRecord) -> None:
        path = self.record_path(record.repo_id, record.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")

    def _load_optional(self, repo_id: str, task_id: str) -> WorkspaceRecord | None:
        path = self.record_path(repo_id, task_id)
        if not path.is_file():
            return None
        try:
            return WorkspaceRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InputError(f"工作区记录不可读：{path}: {exc}") from exc


__all__ = ["CHECK_FILE", "WORKSPACE_FILE", "WorkspaceManager", "default_home"]
