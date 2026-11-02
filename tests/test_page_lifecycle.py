"""页面生命周期测试（S7 §16.3-5）：stale 标记与回滚。

单元部分直接驱动 FilesystemPageStore；端到端部分走真实 CLI：
sync → generate(mock) → publish → 改远端 → sync → stale → rollback。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from codingwiki.domain.models import ChangeSet
from codingwiki.domain.wiki import PageDeps, PageResult, PageStatus, PageType, TaskMeta
from codingwiki.errors import InputError
from codingwiki.publish import FilesystemPageStore

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "facts" / "sample"

PAGE_A = "---\nid: module/a\ntype: module\ntitle: a\nsnapshot: r@1\n---\n正文。[E1]\n"
PAGE_B = "---\nid: module/b\ntype: module\ntitle: b\nsnapshot: r@1\n---\n正文。[E1]\n"


def _meta(page_id: str) -> TaskMeta:
    return TaskMeta(
        task_id="t",
        page_id=page_id,
        page_type=PageType.MODULE,
        title_hint=page_id,
        repo_id="r",
        snapshot_id="r@1",
        prompt_version="p@1",
    )


def _result_text(page_id: str) -> str:
    return PageResult(page_id=page_id, snapshot="r@1").model_dump_json(indent=2)


def _report(page_id: str, page_text: str, result_text: str):
    import hashlib

    from codingwiki.domain.wiki import VerifyReport

    return VerifyReport(
        page_id=page_id,
        status="passed",
        verifier_version="v",
        page_sha256=hashlib.sha256(page_text.encode()).hexdigest(),
        result_sha256=hashlib.sha256(result_text.encode()).hexdigest(),
    )


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FilesystemPageStore:
    monkeypatch.setenv("CODINGWIKI_HOME", str(tmp_path / ".codingwiki"))
    return FilesystemPageStore("r")


def _stage(store: FilesystemPageStore, page_id: str, page_text: str, files: dict[str, str]) -> None:
    result_text = _result_text(page_id)
    store.stage(
        page_text,
        result_text,
        _report(page_id, page_text, result_text),
        _meta(page_id),
        PageDeps(page_id=page_id, snapshot_id="r@1", files=files),
    )


def test_publish_writes_deps_into_version(store: FilesystemPageStore) -> None:
    _stage(store, "module/a", PAGE_A, {"src/a.c": "sha"})
    index = store.publish("r@1")

    deps = store.load_version_deps(index.version_id)
    assert deps["module/a"].files == {"src/a.c": "sha"}


def test_mark_stale_marks_affected_pages_only(store: FilesystemPageStore) -> None:
    _stage(store, "module/a", PAGE_A, {"src/a.c": "sha"})
    _stage(store, "module/b", PAGE_B, {"docs/b.md": "sha"})
    version = store.publish("r@1").version_id

    changes = ChangeSet(old_revision="1", new_revision="2", modified=["src/a.c"])
    marked = store.mark_stale(changes)

    assert marked == ["module/a"]
    index = store.version_index(version)
    statuses = {p.page_id: p.status for p in index.pages}
    assert statuses["module/a"] is PageStatus.STALE
    assert statuses["module/b"] is PageStatus.VERIFIED


def test_mark_stale_counts_deleted_renamed_binary(store: FilesystemPageStore) -> None:
    _stage(store, "module/a", PAGE_A, {"src/a.c": "sha", "old.c": "s", "bin.dat": "s"})
    store.publish("r@1")

    changes = ChangeSet(
        old_revision="1",
        new_revision="2",
        deleted=["gone.c"],
        renamed=[("old.c", "new.c")],
        binary_changed=["bin.dat"],
    )
    assert store.mark_stale(changes) == ["module/a"]


def test_mark_stale_no_changes_is_noop(store: FilesystemPageStore) -> None:
    _stage(store, "module/a", PAGE_A, {"src/a.c": "sha"})
    store.publish("r@1")
    assert store.mark_stale(ChangeSet(old_revision="1", new_revision="1")) == []


def test_mark_stale_without_version_raises(store: FilesystemPageStore) -> None:
    with pytest.raises(InputError, match="还没有发布版本"):
        store.mark_stale(ChangeSet(old_revision="1", new_revision="2"))


def test_rollback_switches_current_pointer(store: FilesystemPageStore) -> None:
    _stage(store, "module/a", PAGE_A, {"src/a.c": "sha"})
    v1 = store.publish("r@1").version_id

    store.rollback(v1)
    assert store.current_version() == v1


def test_rollback_unknown_version_raises(store: FilesystemPageStore) -> None:
    _stage(store, "module/a", PAGE_A, {"src/a.c": "sha"})
    store.publish("r@1")
    with pytest.raises(InputError, match="不存在"):
        store.rollback("v20990101000000-deadbeef")


# ---------------------------------------------------------------- CLI 端到端


def _run(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "codingwiki", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, timeout=30)


def _prepare(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    origin = tmp_path / "origin"
    shutil.copytree(FIXTURES, origin)
    _git(origin, "init", "-b", "main")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "fixture")

    home = tmp_path / ".cw"
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    (config_dir / "sample.yaml").write_text(
        f"repo_id: sample\nprovider: git\nurl: {origin}\nbranch: main\n",
        encoding="utf-8",
    )
    env = {**os.environ, "CODINGWIKI_HOME": str(home), "PYTHONPATH": str(REPO_ROOT / "src")}

    sync = _run(
        ["repo", "sync", "--repo", "sample", "--config-dir", str(config_dir)], cwd=tmp_path, env=env
    )
    assert sync.returncode == 0, sync.stderr
    extract = _run(
        ["facts", "extract", "--repo", "sample", "--config-dir", str(config_dir)],
        cwd=tmp_path,
        env=env,
    )
    assert extract.returncode == 0, extract.stderr
    return config_dir, origin, env


def test_stale_and_rollback_end_to_end(tmp_path: Path) -> None:
    config_dir, origin, env = _prepare(tmp_path)

    generate = _run(
        ["wiki", "generate", "--repo", "sample", "--config-dir", str(config_dir)],
        cwd=tmp_path,
        env=env,
    )
    assert generate.returncode == 0, generate.stderr
    publish = _run(
        ["wiki", "publish", "--repo", "sample", "--config-dir", str(config_dir)],
        cwd=tmp_path,
        env=env,
    )
    assert publish.returncode == 0, publish.stderr

    page_store = FilesystemPageStore("sample", tmp_path / ".cw")
    v1 = page_store.current_version()
    assert v1 is not None

    # 同一快照重复执行 stale：不标任何页
    stale_same = _run(["wiki", "stale", "--repo", "sample"], cwd=tmp_path, env=env)
    assert stale_same.returncode == 0, stale_same.stderr
    assert "快照未变化" in stale_same.stdout

    # 远端新增修改 → 新快照 → stale 标记受影响页面
    helper = origin / "src" / "helper.c"
    helper.write_text(helper.read_text(encoding="utf-8") + "\n// touched\n", encoding="utf-8")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "touch helper.c")
    sync2 = _run(
        ["repo", "sync", "--repo", "sample", "--config-dir", str(config_dir)], cwd=tmp_path, env=env
    )
    assert sync2.returncode == 0, sync2.stderr

    stale = _run(["wiki", "stale", "--repo", "sample"], cwd=tmp_path, env=env)
    assert stale.returncode == 0, stale.stderr
    assert "stale 标记:" in stale.stdout

    index = page_store.version_index(v1)
    assert index is not None
    assert any(p.status is PageStatus.STALE for p in index.pages)

    # 回滚到 v1 并确认指针切换
    rollback = _run(["wiki", "rollback", "--repo", "sample", "--to", v1], cwd=tmp_path, env=env)
    assert rollback.returncode == 0, rollback.stderr
    assert f"current -> {v1}" in rollback.stdout
    assert page_store.current_version() == v1

    # 回滚到不存在的版本：退出码 2
    bad = _run(
        ["wiki", "rollback", "--repo", "sample", "--to", "v20990101000000-deadbeef"],
        cwd=tmp_path,
        env=env,
    )
    assert bad.returncode == 2
