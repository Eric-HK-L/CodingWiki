"""SnapshotStore 单元测试 + 集成测试。

不依赖 GitProvider；直接往临时目录塞文件，让 FilesystemSnapshotStore 遍历。
覆盖：manifest 内容、排除规则、语言识别、二进制、大文件截断、diff、blob 存储、latest 指针。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codingwiki.domain.models import RepoType
from codingwiki.errors import InternalError
from codingwiki.snapshot import FilesystemSnapshotStore

# ---------------------------------------------------------------- 辅助


@pytest.fixture
def codingwiki_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".cw"
    monkeypatch.setenv("CODINGWIKI_HOME", str(home))
    return home


def _write_file(root: Path, rel: str, body: str | bytes = "") -> Path:
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, bytes):
        full.write_bytes(body)
    else:
        full.write_text(body, encoding="utf-8")
    return full


def _make_workdir(tmp: Path) -> Path:
    """造一个多样化的 workdir：文本、二进制、排除项、大文件、软链。"""
    work = tmp / "work"
    (work / "src").mkdir(parents=True)
    (work / "docs").mkdir()
    (work / "build").mkdir()
    (work / ".git").mkdir()
    (work / "vendor").mkdir()

    _write_file(work, "src/main.py", "print('hi')\n")
    _write_file(work, "src/utils.rs", "fn main() {}")
    _write_file(work, "docs/README.md", "# hello")
    _write_file(work, "build/output.o", b"\x00\x01\x02\x03" * 100)  # 二进制
    _write_file(work, "vendor/lib/old.c", "old code")  # 被排除 (vendor)
    _write_file(work, "CMakeCache.txt", "stale")  # 被排除 (generated)
    _write_file(work, ".git/config", "meta")  # 被排除 (vcs)
    _write_file(work, "src/nested/deep/test.c", "int main() {}")  # 深层文本

    # 大文件（> 2MB）：截断
    _write_file(work, "src/big.log", "x" * (3 * 1024 * 1024))

    # 软链
    link = work / "link_to_src"
    link.symlink_to("src")

    return work


# ---------------------------------------------------------------- manifest 内容


def test_create_snapshot_returns_proper_id(codingwiki_home: Path, tmp_path: Path) -> None:
    store = FilesystemSnapshotStore()
    work = _make_workdir(tmp_path)

    snap = store.create("demo", "abc123", work, RepoType.GIT)

    assert snap.snapshot_id == "demo@abc123"
    assert snap.repo_id == "demo"
    assert snap.revision == "abc123"
    assert snap.provider is RepoType.GIT
    assert snap.parent_snapshot_id == ""


def test_manifest_counts_files(codingwiki_home: Path, tmp_path: Path) -> None:
    store = FilesystemSnapshotStore()
    work = _make_workdir(tmp_path)

    snap = store.create("demo", "abc123", work, RepoType.GIT)
    m = store.manifest(snap.snapshot_id)

    assert m.file_count >= 8
    # src/main.py / src/utils.rs / docs/README.md / src/nested/deep/test.c / src/big.log
    assert m.in_scope_count >= 5
    # build/output.o / vendor/lib/old.c / CMakeCache.txt / link_to_src
    assert m.excluded_count >= 3


def test_language_detection(codingwiki_home: Path, tmp_path: Path) -> None:
    store = FilesystemSnapshotStore()
    work = _make_workdir(tmp_path)

    snap = store.create("demo", "abc123", work, RepoType.GIT)
    m = store.manifest(snap.snapshot_id)
    lang_map = {e.path: e.language for e in m.files}

    assert lang_map["src/main.py"] == "python"
    assert lang_map["src/utils.rs"] == "rust"
    assert lang_map["docs/README.md"] == "markdown"
    assert lang_map["src/nested/deep/test.c"] == "c"


def test_default_excludes_apply(codingwiki_home: Path, tmp_path: Path) -> None:
    store = FilesystemSnapshotStore()
    work = _make_workdir(tmp_path)

    snap = store.create("demo", "abc123", work, RepoType.GIT)
    m = store.manifest(snap.snapshot_id)
    excluded = {e.path for e in m.files if e.excluded}

    assert "vendor/lib/old.c" in excluded
    assert "CMakeCache.txt" in excluded
    assert "build/output.o" in excluded
    # 软链也被排除
    assert "link_to_src" in excluded
    # .git/ 目录由 _iter_files 主动跳过，不在 manifest 里（这是实现选择，排除规则
    # 里虽有 .git/ 但根本遍历不到；不影响覆盖率统计和后续使用）


def test_binary_flag_set_for_build_artifacts(codingwiki_home: Path, tmp_path: Path) -> None:
    store = FilesystemSnapshotStore()
    work = _make_workdir(tmp_path)

    snap = store.create("demo", "abc123", work, RepoType.GIT)
    m = store.manifest(snap.snapshot_id)
    build_o = next(e for e in m.files if e.path == "build/output.o")

    assert build_o.binary is True
    assert build_o.excluded is True  # build/ 在默认排除


def test_big_file_is_marked_truncated(codingwiki_home: Path, tmp_path: Path) -> None:
    store = FilesystemSnapshotStore()
    work = _make_workdir(tmp_path)

    snap = store.create("demo", "abc123", work, RepoType.GIT)
    m = store.manifest(snap.snapshot_id)
    big = next(e for e in m.files if e.path == "src/big.log")

    assert big.truncated is True
    assert big.size_bytes >= 2 * 1024 * 1024


def test_read_file_from_snapshot(codingwiki_home: Path, tmp_path: Path) -> None:
    store = FilesystemSnapshotStore()
    work = _make_workdir(tmp_path)

    snap = store.create("demo", "abc123", work, RepoType.GIT)

    data = store.read_file(snap.snapshot_id, "src/main.py")

    assert data == b"print('hi')\n"


def test_read_binary_raises(codingwiki_home: Path, tmp_path: Path) -> None:
    store = FilesystemSnapshotStore()
    work = _make_workdir(tmp_path)

    snap = store.create("demo", "abc123", work, RepoType.GIT)

    with pytest.raises(InternalError):
        store.read_file(snap.snapshot_id, "build/output.o")


def test_read_missing_raises(codingwiki_home: Path, tmp_path: Path) -> None:
    store = FilesystemSnapshotStore()
    work = _make_workdir(tmp_path)

    snap = store.create("demo", "abc123", work, RepoType.GIT)

    with pytest.raises(InternalError):
        store.read_file(snap.snapshot_id, "not-exist.txt")


# ---------------------------------------------------------------- latest 指针


def test_latest_pointer(codingwiki_home: Path, tmp_path: Path) -> None:
    store = FilesystemSnapshotStore()
    work = _make_workdir(tmp_path)

    store.create("demo", "abc123", work, RepoType.GIT)
    store.create("demo", "def456", work, RepoType.GIT)

    latest = store.latest("demo")

    assert latest is not None
    assert latest.snapshot_id == "demo@def456"
    assert latest.parent_snapshot_id == "demo@abc123"


def test_latest_returns_none_when_empty(codingwiki_home: Path) -> None:
    store = FilesystemSnapshotStore()
    assert store.latest("no-such-repo") is None


# ---------------------------------------------------------------- diff_manifests


def test_diff_adds_deletes_modifies(codingwiki_home: Path, tmp_path: Path) -> None:
    store = FilesystemSnapshotStore()

    w1 = tmp_path / "w1"
    _write_file(w1, "a.txt", "A")
    _write_file(w1, "b.txt", "B")
    _write_file(w1, "c.txt", "C")
    s1 = store.create("demo", "sha1", w1, RepoType.GIT)

    w2 = tmp_path / "w2"
    _write_file(w2, "a.txt", "A-new")  # modified
    _write_file(w2, "c.txt", "C")  # same
    _write_file(w2, "d.txt", "D")  # added
    # b.txt removed
    s2 = store.create("demo", "sha2", w2, RepoType.GIT)

    cs = store.diff_manifests(s1.snapshot_id, s2.snapshot_id)

    assert cs.old_revision == "sha1"
    assert cs.new_revision == "sha2"
    assert "a.txt" in cs.modified
    assert "b.txt" in cs.deleted
    assert "d.txt" in cs.added
    assert "c.txt" not in cs.modified


def test_diff_detects_rename_by_same_hash(codingwiki_home: Path, tmp_path: Path) -> None:
    store = FilesystemSnapshotStore()

    w1 = tmp_path / "w1"
    _write_file(w1, "README.md", "same content")
    s1 = store.create("demo", "sha1", w1, RepoType.GIT)

    w2 = tmp_path / "w2"
    _write_file(w2, "readme.md", "same content")  # 改名，内容不变
    s2 = store.create("demo", "sha2", w2, RepoType.GIT)

    cs = store.diff_manifests(s1.snapshot_id, s2.snapshot_id)

    assert len(cs.renamed) == 1
    a, b = cs.renamed[0]
    assert {a, b} == {"README.md", "readme.md"}


def test_diff_low_similarity_rename_becomes_del_plus_add(
    codingwiki_home: Path, tmp_path: Path
) -> None:
    store = FilesystemSnapshotStore()

    w1 = tmp_path / "w1"
    _write_file(w1, "x.py", "old old old")
    s1 = store.create("demo", "sha1", w1, RepoType.GIT)

    w2 = tmp_path / "w2"
    _write_file(w2, "y.py", "completely new content totally different")
    s2 = store.create("demo", "sha2", w2, RepoType.GIT)

    cs = store.diff_manifests(s1.snapshot_id, s2.snapshot_id)

    # 内容不同，应该记为 del + add，不记 renamed
    assert "x.py" in cs.deleted
    assert "y.py" in cs.added
    assert not cs.renamed


def test_diff_of_same_snapshot_is_empty(codingwiki_home: Path, tmp_path: Path) -> None:
    store = FilesystemSnapshotStore()
    work = _make_workdir(tmp_path)

    s = store.create("demo", "abc", work, RepoType.GIT)
    cs = store.diff_manifests(s.snapshot_id, s.snapshot_id)

    assert not cs.added and not cs.modified and not cs.deleted and not cs.renamed
