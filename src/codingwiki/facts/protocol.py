"""事实层 Protocol（架构文档 v0.3 3.5 / 12 节）。"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from codingwiki.domain.facts import FileFacts, RepoFacts
from codingwiki.domain.models import Snapshot


@runtime_checkable
class FactExtractor(Protocol):
    """从一个快照 + 工作树产出完整事实批次。"""

    def extract(self, snapshot: Snapshot, workdir: Path) -> RepoFacts: ...


@runtime_checkable
class FactStore(Protocol):
    """事实落盘与读取；单文件缓存按内容哈希复用。"""

    def save(self, facts: RepoFacts) -> Path: ...

    def load(self, repo_id: str, revision: str) -> RepoFacts | None: ...

    def latest(self, repo_id: str) -> RepoFacts | None: ...

    def cached_file(self, repo_id: str, blob_sha256: str, version: str) -> FileFacts | None: ...

    def cache_file(
        self, repo_id: str, blob_sha256: str, version: str, facts: FileFacts
    ) -> None: ...


__all__ = ["FactExtractor", "FactStore"]
