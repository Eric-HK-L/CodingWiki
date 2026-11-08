"""工作树操作的最小 Protocol（S18）。

只声明补丁协议用到的 git 能力，便于单测用 fake 替换；
GitProvider / GitHubProvider 结构上满足本协议。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from codingwiki.domain.models import RepoConfig


class WorktreeGit(Protocol):
    def create_worktree(
        self, repo: RepoConfig, revision: str, path: str | Path, branch: str = ""
    ) -> Path: ...
    def remove_worktree(self, path: str | Path) -> None: ...
    def branch_exists(self, repo: RepoConfig, branch: str) -> bool: ...
    def delete_branch(self, repo: RepoConfig, branch: str) -> None: ...
    def diff_unified(self, worktree: str | Path, base_revision: str) -> str: ...
    def diff_numstat(
        self,
        worktree: str | Path,
        base_revision: str,
        *,
        ignore_all_space: bool = False,
        ignore_cr_at_eol: bool = False,
    ) -> list[tuple[int | None, int | None, str]]: ...
    def untracked_paths(self, worktree: str | Path) -> list[str]: ...
    def read_worktree_file(self, worktree: str | Path, path: str) -> bytes: ...
    def reset_worktree(self, worktree: str | Path, revision: str) -> None: ...
    def clean_worktree(self, worktree: str | Path) -> None: ...
    def worktree_status(self, worktree: str | Path) -> str: ...


__all__ = ["WorktreeGit"]
