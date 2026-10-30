"""snapshot 包入口。"""

from __future__ import annotations

from codingwiki.snapshot.protocol import SnapshotStore
from codingwiki.snapshot.store import FilesystemSnapshotStore

__all__ = ["FilesystemSnapshotStore", "SnapshotStore"]
