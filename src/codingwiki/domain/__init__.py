"""领域层：纯数据模型，无 I/O、无日志。

所有模块都可以依赖领域层；领域层不能反向 import 其他层。
"""

from __future__ import annotations

from codingwiki.domain.models import (
    ChangeSet,
    FileEntry,
    Manifest,
    RepoConfig,
    RepoMetadata,
    RepoType,
    Snapshot,
)

__all__ = [
    "ChangeSet",
    "FileEntry",
    "Manifest",
    "RepoConfig",
    "RepoMetadata",
    "RepoType",
    "Snapshot",
]
