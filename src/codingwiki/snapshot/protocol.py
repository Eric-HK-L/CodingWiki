"""SnapshotStore Protocol。

架构文档 v0.2 3.2 节。快照 = 不可变版本号 + manifest；manifest 是权威变更来源。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from codingwiki.domain.models import ChangeSet, Manifest, RepoConfig, RepoType, Snapshot


@runtime_checkable
class SnapshotStore(Protocol):
    """快照存储：创建、读取、diff、回滚。"""

    def create(
        self,
        repo_id: str,
        revision: str,
        workdir: Path,
        provider: RepoType,
        config: RepoConfig | None = None,
    ) -> Snapshot: ...

    def manifest(self, snapshot_id: str) -> Manifest: ...
    def latest(self, repo_id: str) -> Snapshot | None: ...
    def diff_manifests(self, old_id: str, new_id: str) -> ChangeSet: ...
    def read_file(self, snapshot_id: str, path: str) -> bytes: ...


__all__ = ["SnapshotStore"]
