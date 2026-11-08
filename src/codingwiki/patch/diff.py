"""工作树 diff 解析（S18）。

把 `git diff --unified=0 --no-renames` 与未跟踪文件统一成 FileDiff，
供策略层做路径、规模与内容检查。解析器不认识业务规则。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from codingwiki.domain.patch import ChangeKind, FileChange
from codingwiki.patch.protocol import WorktreeGit


@dataclass(frozen=True)
class FileDiff:
    """单个文件的变更；保留 added/removed 文本用于内容扫描。"""

    path: str
    kind: ChangeKind
    added_lines: int = 0
    deleted_lines: int = 0
    binary: bool = False
    whitespace_only: bool = False
    line_endings_only: bool = False
    added_text: tuple[str, ...] = ()
    removed_text: tuple[str, ...] = ()

    def to_change(self) -> FileChange:
        return FileChange(
            path=self.path,
            kind=self.kind,
            added_lines=self.added_lines,
            deleted_lines=self.deleted_lines,
            binary=self.binary,
        )


@dataclass
class _Builder:
    path: str = ""
    kind: ChangeKind = ChangeKind.MODIFIED
    added_lines: int = 0
    deleted_lines: int = 0
    binary: bool = False
    added_text: list[str] = field(default_factory=list)
    removed_text: list[str] = field(default_factory=list)

    def build(self) -> FileDiff:
        return FileDiff(
            path=self.path,
            kind=self.kind,
            added_lines=self.added_lines,
            deleted_lines=self.deleted_lines,
            binary=self.binary,
            added_text=tuple(self.added_text),
            removed_text=tuple(self.removed_text),
        )


def collect_diffs(provider: WorktreeGit, worktree: Path, base_revision: str) -> list[FileDiff]:
    """收集工作树相对基线的全部变更（含未跟踪文件），按路径排序。"""
    tracked = _parse_unified(provider.diff_unified(worktree, base_revision))
    untracked = _untracked_diffs(provider, worktree, {item.path for item in tracked})
    diffs = [*tracked, *untracked]
    diffs = _apply_whitespace_flags(provider, worktree, base_revision, diffs)
    return sorted(diffs, key=lambda item: item.path)


def _parse_unified(raw: str) -> list[FileDiff]:
    results: list[FileDiff] = []
    current: _Builder | None = None
    in_hunk = False

    for line in raw.splitlines():
        if line.startswith("diff --git "):
            if current is not None and current.path:
                results.append(current.build())
            current = _Builder(path=_path_from_header(line[len("diff --git ") :]))
            in_hunk = False
            continue
        if current is None:
            continue
        if line.startswith("new file mode"):
            current.kind = ChangeKind.ADDED
        elif line.startswith("deleted file mode"):
            current.kind = ChangeKind.DELETED
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current.binary = True
        elif line.startswith("--- "):
            path = _strip_prefix(line[4:].strip(), "a/")
            if path == "/dev/null":
                current.kind = ChangeKind.ADDED
            elif path:
                current.path = path
        elif line.startswith("+++ "):
            path = _strip_prefix(line[4:].strip(), "b/")
            if path == "/dev/null":
                current.kind = ChangeKind.DELETED
            elif path:
                current.path = path
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            current.added_lines += 1
            current.added_text.append(line[1:])
        elif in_hunk and line.startswith("-"):
            current.deleted_lines += 1
            current.removed_text.append(line[1:])

    if current is not None and current.path:
        results.append(current.build())
    return [item for item in results if item.path and not _ignored_placeholder(item.path)]


def _path_from_header(header: str) -> str:
    body = header[2:] if header.startswith("a/") else header
    if " b/" in body:
        return body.split(" b/", 1)[0]
    return body


def _strip_prefix(value: str, prefix: str) -> str:
    return value[len(prefix) :] if value.startswith(prefix) else value


def _ignored_placeholder(path: str) -> bool:
    return path in {"/dev/null"}


def _untracked_diffs(provider: WorktreeGit, worktree: Path, known: set[str]) -> list[FileDiff]:
    results: list[FileDiff] = []
    for path in provider.untracked_paths(worktree):
        if path in known:
            continue
        data = provider.read_worktree_file(worktree, path)
        if _is_binary(data):
            results.append(FileDiff(path=path, kind=ChangeKind.UNTRACKED, binary=True))
            continue
        text = data.decode("utf-8", errors="replace")
        lines = tuple(text.splitlines())
        results.append(
            FileDiff(
                path=path,
                kind=ChangeKind.UNTRACKED,
                added_lines=len(lines),
                added_text=lines,
            )
        )
    return results


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data


def _apply_whitespace_flags(
    provider: WorktreeGit,
    worktree: Path,
    base_revision: str,
    diffs: list[FileDiff],
) -> list[FileDiff]:
    """用 numstat 对比判定“纯空白 / 仅换行符”改动。

    正常 diff 有行数、忽略空白与忽略 CR 后为 0，说明改动只在这些维度上。
    """
    normal = _numstat_map(provider.diff_numstat(worktree, base_revision), keep_binary=False)
    ignore_all = _numstat_map(
        provider.diff_numstat(worktree, base_revision, ignore_all_space=True),
        keep_binary=False,
    )
    ignore_cr = _numstat_map(
        provider.diff_numstat(worktree, base_revision, ignore_cr_at_eol=True),
        keep_binary=False,
    )

    updated: list[FileDiff] = []
    for item in diffs:
        if item.kind is ChangeKind.UNTRACKED or item.binary:
            updated.append(item)
            continue
        counted = normal.get(item.path)
        if counted is None or counted == 0:
            updated.append(item)
            continue
        if ignore_all.get(item.path, 0) == 0:
            only_cr = ignore_cr.get(item.path, 0) == 0
            updated.append(replace(item, line_endings_only=only_cr, whitespace_only=not only_cr))
            continue
        updated.append(item)
    return updated


def _numstat_map(
    records: list[tuple[int | None, int | None, str]], *, keep_binary: bool
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for added, deleted, path in records:
        if added is None or deleted is None:
            if keep_binary:
                counts[path] = 1
            continue
        counts[path] = added + deleted
    return counts


__all__ = ["FileDiff", "collect_diffs"]
