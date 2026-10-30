"""FilesystemFactStore：事实落盘 + 单文件解析缓存。

目录布局（架构文档 v0.3 第 5 节）：
- `.codingwiki/facts/<repo_id>/<revision>.json` 事实批次；
- `.codingwiki/facts/<repo_id>/latest` 指向最近一次 revision；
- `.codingwiki/facts/<repo_id>/cache/<version>/<sha[:2]>/<sha>.json` 单文件缓存。
"""

from __future__ import annotations

import os
from pathlib import Path

from codingwiki.domain.facts import FileFacts, RepoFacts


class FilesystemFactStore:
    def __init__(self, base_dir: str | Path | None = None) -> None:
        base = Path(base_dir or os.environ.get("CODINGWIKI_HOME", ".codingwiki")).resolve()
        self._base = base
        self._facts_dir = base / "facts"

    # ---------------------------------------------------------------- 事实批次

    def save(self, facts: RepoFacts) -> Path:
        repo_dir = self._facts_dir / facts.repo_id
        repo_dir.mkdir(parents=True, exist_ok=True)
        path = repo_dir / f"{_safe(facts.revision)}.json"
        path.write_text(facts.model_dump_json(indent=2), encoding="utf-8")
        (repo_dir / "latest").write_text(facts.revision, encoding="utf-8")
        return path

    def load(self, repo_id: str, revision: str) -> RepoFacts | None:
        path = self._facts_dir / repo_id / f"{_safe(revision)}.json"
        if not path.is_file():
            return None
        return RepoFacts.model_validate_json(path.read_text(encoding="utf-8"))

    def latest(self, repo_id: str) -> RepoFacts | None:
        pointer = self._facts_dir / repo_id / "latest"
        if not pointer.is_file():
            return None
        revision = pointer.read_text(encoding="utf-8").strip()
        if not revision:
            return None
        return self.load(repo_id, revision)

    # ---------------------------------------------------------------- 单文件缓存

    def cached_file(self, repo_id: str, blob_sha256: str, version: str) -> FileFacts | None:
        path = self._cache_path(repo_id, blob_sha256, version)
        if not path.is_file():
            return None
        try:
            return FileFacts.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            # 缓存损坏按未命中处理，下次会重写
            return None

    def cache_file(self, repo_id: str, blob_sha256: str, version: str, facts: FileFacts) -> None:
        path = self._cache_path(repo_id, blob_sha256, version)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(facts.model_dump_json(), encoding="utf-8")

    # ---------------------------------------------------------------- 内部

    def _cache_path(self, repo_id: str, blob_sha256: str, version: str) -> Path:
        sha = blob_sha256 or "unknown"
        return self._facts_dir / repo_id / "cache" / _safe(version) / sha[:2] / f"{sha}.json"


def _safe(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_").replace("@", "_")


__all__ = ["FilesystemFactStore"]
