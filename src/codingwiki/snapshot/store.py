"""FilesystemSnapshotStore：manifest 生成、diff、内容寻址 blob 存储。

架构文档 v0.2 3.2 + 11.1。manifest 是权威变更来源：
- GitProvider.changes() 用来做跨版本触发；
- SnapshotStore.diff_manifests() 才是最终变更清单（两个 manifest 逐文件比对）。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from codingwiki.domain.models import (
    ChangeSet,
    FileEntry,
    Manifest,
    RepoConfig,
    RepoType,
    Snapshot,
)
from codingwiki.errors import InternalError


class FilesystemSnapshotStore:
    """快照全部落在 CODINGWIKI_HOME（默认 .codingwiki）下。"""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        base = Path(base_dir or os.environ.get("CODINGWIKI_HOME", ".codingwiki")).resolve()
        self._base = base
        self._snapshots_dir = base / "snapshots"
        self._blobs_dir = base / "blobs"
        self._indices_dir = base / "indices"
        # 惰性创建，首次用到才 mkdir

    # ---------------------------------------------------------------- 公共方法

    def create(
        self,
        repo_id: str,
        revision: str,
        workdir: Path,
        provider: RepoType,
        config: RepoConfig | None = None,
    ) -> Snapshot:

        # 仓库级配置可选：传了就按 config 的排除/语言覆盖/大小上限走，
        # 不传则保持 S2 的默认行为（便于旧调用方和测试）。
        from codingwiki.snapshot._exclude import (
            detect_language,
            is_binary_by_extension,
            looks_text_by_content,
            should_exclude,
        )

        workdir = Path(workdir).resolve()
        if not workdir.is_dir():
            raise InternalError(f"workdir 不存在或不是目录: {workdir}")

        extra_excludes = config.excludes if config else []
        language_overrides = config.language_overrides if config else {}
        extra_binary_ext = _extra_binary_extensions(config)
        max_bytes = config.max_text_file_size_kb * 1024 if config else _env_max_bytes()

        snapshot_id = f"{repo_id}@{revision}"

        # 找 parent snapshot
        parent = self._latest_snapshot(repo_id)
        parent_id = parent.snapshot_id if parent else ""

        # 遍历 + 统计
        files: list[FileEntry] = []
        exclusions: list[dict[str, str]] = []
        languages: dict[str, int] = {}

        for file_path in _iter_files(workdir):
            rel = str(file_path.relative_to(workdir)).replace(os.sep, "/")
            # 软链：不跟随，记录目标路径
            if file_path.is_symlink():
                target = _safe_readlink(file_path)
                files.append(
                    FileEntry(
                        path=rel,
                        blob_sha256="",
                        size_bytes=0,
                        language="symlink",
                        excluded=True,
                        exclusion_reason=f"symlink->{target}",
                        binary=False,
                        truncated=False,
                    )
                )
                exclusions.append({"path": rel, "reason": "symlink"})
                continue

            # 先算 binary 和 language（即使文件被排除，标记也要准确）
            binary = is_binary_by_extension(rel, extra_binary_ext)
            if not binary:
                binary = not looks_text_by_content(file_path)
            language = detect_language(rel, language_overrides)
            size = file_path.stat().st_size

            # 排除规则
            excluded, reason = should_exclude(rel, extra_rules=extra_excludes)
            if excluded:
                files.append(
                    FileEntry(
                        path=rel,
                        blob_sha256="",
                        size_bytes=size,
                        language=language,
                        excluded=True,
                        exclusion_reason=reason,
                        binary=binary,
                        truncated=False,
                    )
                )
                exclusions.append({"path": rel, "reason": reason})
                continue

            # 计算内容哈希 / 截断
            truncated = False
            blob_sha = ""
            if binary:
                # 二进制：只记录元数据，不算内容哈希（算了也没用，不做后续分析）
                # 用文件路径 + 大小 + mtime 凑一个稳定签名
                blob_sha = hashlib.sha256(
                    f"binary:{rel}:{size}:{file_path.stat().st_mtime_ns}".encode()
                ).hexdigest()
            else:
                blob_sha = _sha256_file(file_path)
                if size > max_bytes:
                    truncated = True
                    # 截断后只存前 1 MB 内容进 BlobStore
                    self._blob_store(blob_sha, _read_head(file_path, 1024 * 1024))
                else:
                    self._blob_store(blob_sha, file_path.read_bytes())

            files.append(
                FileEntry(
                    path=rel,
                    blob_sha256=blob_sha,
                    size_bytes=size,
                    language=language,
                    excluded=False,
                    exclusion_reason="",
                    binary=binary,
                    truncated=truncated,
                )
            )
            languages[language] = languages.get(language, 0) + 1

        total = len(files)
        excluded_count = sum(1 for f in files if f.excluded)
        manifest = Manifest(
            snapshot_id=snapshot_id,
            repo_id=repo_id,
            provider=provider,
            revision=revision,
            parent_snapshot_id=parent_id,
            file_count=total,
            in_scope_count=total - excluded_count,
            excluded_count=excluded_count,
            languages=languages,
            files=files,
            exclusions=exclusions,
        )

        # 落盘
        self._snapshots_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = self._manifest_path(repo_id, snapshot_id)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        # model_dump_json() 直接序列化成 JSON 字符串，内部正确处理 datetime、Enum 等类型
        manifest_path.write_text(
            manifest.model_dump_json(indent=2),
            encoding="utf-8",
        )

        # 写 latest 指针
        self._indices_dir.mkdir(parents=True, exist_ok=True)
        latest_file = self._indices_dir / f"{repo_id}.latest"
        latest_file.write_text(snapshot_id, encoding="utf-8")

        return Snapshot(
            snapshot_id=snapshot_id,
            repo_id=repo_id,
            revision=revision,
            provider=provider,
            manifest_path=str(manifest_path),
            parent_snapshot_id=parent_id,
        )

    def manifest(self, snapshot_id: str) -> Manifest:
        repo_id, _, _ = snapshot_id.partition("@")
        path = self._manifest_path(repo_id, snapshot_id)
        if not path.is_file():
            raise InternalError(f"manifest 不存在: {path}")
        # model_validate_json 直接吃字符串，免去 json.loads 中间步骤
        return Manifest.model_validate_json(path.read_text(encoding="utf-8"))

    def latest(self, repo_id: str) -> Snapshot | None:
        return self._latest_snapshot(repo_id)

    def manifest_path(self, snapshot_id: str) -> Path:
        repo_id, _, _ = snapshot_id.partition("@")
        return self._manifest_path(repo_id, snapshot_id)

    def snapshot(self, snapshot_id: str) -> Snapshot:
        """按 manifest 重建 Snapshot（manifest 是唯一权威来源）。"""
        manifest = self.manifest(snapshot_id)
        return Snapshot(
            snapshot_id=manifest.snapshot_id,
            repo_id=manifest.repo_id,
            revision=manifest.revision,
            provider=manifest.provider,
            manifest_path=str(self.manifest_path(snapshot_id)),
            parent_snapshot_id=manifest.parent_snapshot_id,
        )

    def diff_manifests(self, old_id: str, new_id: str) -> ChangeSet:
        old = self.manifest(old_id)
        new = self.manifest(new_id)
        return _diff_two(old, new)

    def read_file(self, snapshot_id: str, path: str) -> bytes:
        """从 BlobStore 读；二进制或截断过的文件可能不存在，会抛 InternalError。"""
        manifest = self.manifest(snapshot_id)
        for entry in manifest.files:
            if entry.path == path:
                if entry.binary or entry.excluded or entry.truncated:
                    reason = (
                        f"无法从 snapshot 内容寻址读取 (binary={entry.binary}, "
                        f"excluded={entry.excluded}, truncated={entry.truncated})"
                    )
                    raise InternalError(f"文件 {path} {reason}")
                return self._blob_load(entry.blob_sha256)
        raise InternalError(f"snapshot {snapshot_id} 里没有 {path}")

    # ---------------------------------------------------------------- 内部辅助

    def _latest_snapshot(self, repo_id: str) -> Snapshot | None:
        latest_file = self._indices_dir / f"{repo_id}.latest"
        if not latest_file.is_file():
            return None
        sid = latest_file.read_text(encoding="utf-8").strip()
        if not sid:
            return None
        # 验证 manifest 真存在
        mpath = self._manifest_path(repo_id, sid)
        if not mpath.is_file():
            return None
        manifest = self.manifest(sid)
        return Snapshot(
            snapshot_id=sid,
            repo_id=repo_id,
            revision=manifest.revision,
            provider=manifest.provider,
            manifest_path=str(mpath),
            parent_snapshot_id=manifest.parent_snapshot_id,
        )

    def _manifest_path(self, repo_id: str, snapshot_id: str) -> Path:
        safe = snapshot_id.replace("/", "_").replace("@", "_")
        return self._snapshots_dir / repo_id / f"{safe}.json"

    def _blob_store(self, sha: str, data: bytes) -> None:
        if not sha:
            return
        prefix = sha[:2]
        dir_path = self._blobs_dir / prefix
        dir_path.mkdir(parents=True, exist_ok=True)
        blob_path = dir_path / sha
        if not blob_path.exists():
            blob_path.write_bytes(data)

    def _blob_load(self, sha: str) -> bytes:
        if not sha:
            raise InternalError("空 blob_sha256 无法读取")
        blob_path = self._blobs_dir / sha[:2] / sha
        if not blob_path.is_file():
            raise InternalError(f"blob 不存在: {sha}")
        return blob_path.read_bytes()


# ---------------------------------------------------------------- 模块级辅助函数


def _iter_files(root: Path) -> list[Path]:
    """遍历目录下所有常规文件和软链；跳过 workdir 根的 .git。

    followlinks=False：不跟随目录软链递归进去，否则目录软链指向的文件会以
    `link_to_src/main.py` 这样的路径出现在 manifest，而不是显式记录为一个
    被排除的软链条目。目录软链和文件软链都需要显式收集进来。
    """
    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # 原地剔除 .git，避免遍历版本控制元数据
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            results.append(Path(dirpath) / name)
        # 目录软链：os.walk 默认把目录软链也塞进来（但不跟随，因 followlinks=False）
        # 我们需要显式把它当作一个"软链条目"记录下来
        for name in list(dirnames):
            p = Path(dirpath) / name
            if p.is_symlink():
                results.append(p)
                dirnames.remove(name)
    return results


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _read_head(path: Path, n: int) -> bytes:
    try:
        return path.read_bytes()[:n]
    except OSError:
        return b""


def _safe_readlink(path: Path) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return ""


def _default_max_bytes() -> int:
    return int(os.environ.get("CODINGWIKI_MAX_TEXT_KB", "2048")) * 1024


def _env_max_bytes() -> int:
    try:
        return _default_max_bytes()
    except ValueError:
        return 2048 * 1024


def _extra_binary_extensions(config: RepoConfig | None) -> frozenset[str]:
    """把配置里的扩展名统一成带点的小写形式；空配置返回空集合。"""
    if config is None:
        return frozenset()
    return frozenset(
        ext.lower() if ext.startswith(".") else f".{ext.lower()}"
        for ext in config.binary_extensions
    )


def _diff_two(old: Manifest, new: Manifest) -> ChangeSet:
    """逐文件比对两个 manifest。

    重命名识别逻辑（架构文档 11.1 / 计划文档 7.3.3）：
    1. old 里有、new 里没有、且两个条目哈希相同 → renames；
    2. 哈希不同的 deleted + added 按 change 记录（不记 renamed）；
    3. excluded 的文件不参与 diff（它在下一版可能又被包含回来）。
    """
    old_map = {e.path: e for e in old.files if not e.excluded}
    new_map = {e.path: e for e in new.files if not e.excluded}
    old_paths = set(old_map.keys())
    new_paths = set(new_map.keys())

    added_paths = new_paths - old_paths
    deleted_paths = old_paths - new_paths
    common_paths = old_paths & new_paths

    added: list[str] = []
    deleted: list[str] = []
    modified: list[str] = []
    renamed: list[tuple[str, str]] = []

    # 重命名识别：deleted 里找，added 里也找，且哈希相同
    deleted_by_hash: dict[str, list[str]] = {}
    for p in deleted_paths:
        h = old_map[p].blob_sha256
        deleted_by_hash.setdefault(h, []).append(p)
    used_deleted: set[str] = set()

    for p in added_paths:
        h = new_map[p].blob_sha256
        candidates = deleted_by_hash.get(h, [])
        matched = None
        for c in candidates:
            if c not in used_deleted:
                matched = c
                break
        if matched:
            renamed.append((matched, p))
            used_deleted.add(matched)
        else:
            added.append(p)

    for p in deleted_paths:
        if p not in used_deleted:
            deleted.append(p)

    for p in common_paths:
        if old_map[p].blob_sha256 != new_map[p].blob_sha256:
            modified.append(p)

    # binary_changed 列表：modified 里 binary=True 的
    binary_changed = [p for p in modified if new_map[p].binary or old_map[p].binary]

    return ChangeSet(
        old_revision=old.revision,
        new_revision=new.revision,
        added=sorted(added),
        modified=sorted(modified),
        deleted=sorted(deleted),
        renamed=sorted(renamed),
        binary_changed=sorted(binary_changed),
    )


__all__ = ["FilesystemSnapshotStore"]
