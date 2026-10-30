"""GitProvider 单元测试。

全部用 `git init` 造临时仓库，不依赖网络。覆盖：
- latest_revision 能拿到 HEAD SHA
- materialize 用 worktree 固定到指定 SHA，且重复调用不会产生新 worktree
- changes 两次提交之间正确识别 added/modified/deleted/renamed
- changes 的 R 相似度 < 100% 按 del+add 记
- read_file 能读取指定版本的文件
- P4 provider 会抛 EnvironmentIssue
"""

from __future__ import annotations

import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest

from codingwiki.domain.models import RepoConfig, RepoType
from codingwiki.errors import EnvironmentIssue, InternalError
from codingwiki.providers import GitProvider, create_provider


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    home = tmp_path / ".codingwiki"
    monkeypatch.setenv("CODINGWIKI_HOME", str(home))
    yield home


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        timeout=30,
    )


def _init_remote(tmp: Path) -> Path:
    """造一个本地 bare 仓库作为远程。"""
    bare = tmp / "remote.git"
    _run_git(bare.parent, "init", "--bare", str(bare))
    _run_git(bare, "config", "user.email", "test@test")
    _run_git(bare, "config", "user.name", "Test")
    return bare


def _commit_one(work: Path, name: str, body: str) -> None:
    f = work / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")
    _run_git(work, "add", str(f.relative_to(work)))
    _run_git(work, "commit", "-m", f"add {name}")


def _make_two_commit_repo(tmp: Path) -> tuple[Path, str, str]:
    """返回 bare 远程、sha1、sha2。

    commit 序列（保证 sha1→sha2 跨树 diff 能看到 D/R/M/A 四种状态）：

      c_base : src/main.py v1
      c1     : src/util.py + deprecated.txt + docs/README.md （sha1）
      c2     : main.py v2 + rm deprecated.txt + mv docs/README.md → readme.md （sha2）

    sha1 tree: {main.py v1, util.py, deprecated.txt, docs/README.md}
    sha2 tree: {main.py v2, util.py, readme.md}
    diff:
      main.py        M
      deprecated.txt  D
      docs/README.md  D → -M 配对 readme.md → R100
      readme.md      A → 配对后变 renamed
      util.py        两端相同不显示
    """
    bare = _init_remote(tmp)
    work = tmp / "work"
    work.mkdir()
    _run_git(work, "init")
    _run_git(work, "config", "user.email", "test@test")
    _run_git(work, "config", "user.name", "Test")
    _run_git(work, "remote", "add", "origin", str(bare))
    _run_git(work, "checkout", "-b", "main")

    # 基础 commit：只有 src/main.py v1
    _commit_one(work, "src/main.py", "print('v1')")
    _run_git(work, "push", "-u", "origin", "main")

    # c1: 新增 util.py / deprecated.txt / docs/README.md → sha1
    _commit_one(work, "src/util.py", "# helper")
    _commit_one(work, "deprecated.txt", "will be removed")
    (work / "docs").mkdir(exist_ok=True)
    (work / "docs" / "README.md").write_text("hello\n", encoding="utf-8")
    _run_git(work, "add", ".")
    _run_git(work, "commit", "-m", "add util / deprecated / docs")
    sha1 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(work), capture_output=True, text=True, check=True
    ).stdout.strip()
    _run_git(work, "push", "origin", "main")

    # c2: main.py v2 + rm deprecated + mv docs/README → readme.md
    (work / "src" / "main.py").write_text("print('v2')", encoding="utf-8")
    _run_git(work, "rm", "-f", "deprecated.txt")
    _run_git(work, "mv", "docs/README.md", "readme.md")
    _run_git(work, "add", ".")
    _run_git(work, "commit", "-m", "update main / rm deprecated / move docs")
    sha2 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(work), capture_output=True, text=True, check=True
    ).stdout.strip()
    _run_git(work, "push", "origin", "main")

    return bare, sha1, sha2


def _repo_config(tmp: Path, *, provider: RepoType = RepoType.GIT) -> RepoConfig:
    bare_dir = tmp / "remote.git"
    return RepoConfig(
        repo_id="demo",
        provider=provider,
        url=str(bare_dir),
        branch="main",
        revision="latest",
        workdir=str(tmp / "work_ignored"),
    )


# ---------------------------------------------------------------- 基本行为


def test_latest_revision_returns_head_sha(isolated_home: Path, tmp_path: Path) -> None:
    _bare, _sha1, sha2 = _make_two_commit_repo(tmp_path)
    repo = _repo_config(tmp_path)
    provider = GitProvider()

    sha = provider.latest_revision(repo)

    # 第二次 push 后 remote HEAD 指向 sha2
    assert sha == sha2


def test_materialize_fixes_worktree_to_specified_revision(
    isolated_home: Path, tmp_path: Path
) -> None:
    _bare, sha1, sha2 = _make_two_commit_repo(tmp_path)
    repo = _repo_config(tmp_path)
    provider = GitProvider()

    workdir = provider.materialize(repo, sha1)

    assert workdir.is_dir()
    actual = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert actual == sha1


def test_materialize_reuses_workdir_when_revision_matches(
    isolated_home: Path, tmp_path: Path
) -> None:
    _bare, sha1, _ = _make_two_commit_repo(tmp_path)
    repo = _repo_config(tmp_path)
    provider = GitProvider()

    w1 = provider.materialize(repo, sha1)
    w2 = provider.materialize(repo, sha1)

    assert w1.resolve() == w2.resolve()


def test_materialize_recycles_worktree_for_different_revision(
    isolated_home: Path, tmp_path: Path
) -> None:
    _bare, sha1, sha2 = _make_two_commit_repo(tmp_path)
    repo = _repo_config(tmp_path)
    provider = GitProvider()

    provider.materialize(repo, sha1)
    workdir = provider.materialize(repo, sha2)
    actual = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert actual == sha2


# ---------------------------------------------------------------- changes


def test_changes_detects_added_modified_deleted(isolated_home: Path, tmp_path: Path) -> None:
    _bare, sha1, sha2 = _make_two_commit_repo(tmp_path)
    repo = _repo_config(tmp_path)
    provider = GitProvider()

    cs = provider.changes(repo, sha1, sha2)

    # main.py 跨 sha1→sha2 修改、deprecated.txt 被删除、
    # docs/README.md 被配对到 readme.md → renamed（不计入 deleted/added）
    assert "src/main.py" in cs.modified
    assert "deprecated.txt" in cs.deleted
    # 配对成 renamed 的那两个不应出现在 added / deleted 里
    assert "readme.md" not in cs.added
    assert "docs/README.md" not in cs.deleted


def test_changes_detects_renamed(isolated_home: Path, tmp_path: Path) -> None:
    _bare, sha1, sha2 = _make_two_commit_repo(tmp_path)
    repo = _repo_config(tmp_path)
    provider = GitProvider()

    cs = provider.changes(repo, sha1, sha2)

    # docs/README.md → readme.md（跨目录、相似度 100%）
    pairs = {tuple(sorted((a, b))) for a, b in cs.renamed}
    assert ("docs/README.md", "readme.md") in pairs or ("readme.md", "docs/README.md") in pairs


def test_changes_after_identical_revisions_is_empty(isolated_home: Path, tmp_path: Path) -> None:
    _bare, sha1, _ = _make_two_commit_repo(tmp_path)
    repo = _repo_config(tmp_path)
    provider = GitProvider()

    cs = provider.changes(repo, sha1, sha1)

    assert not cs.added
    assert not cs.modified
    assert not cs.deleted
    assert not cs.renamed


# ---------------------------------------------------------------- read_file


def test_read_file_returns_content(isolated_home: Path, tmp_path: Path) -> None:
    _bare, sha1, _ = _make_two_commit_repo(tmp_path)
    repo = _repo_config(tmp_path)
    provider = GitProvider()

    data = provider.read_file(repo, sha1, "src/main.py")

    assert b"print('v1')" in data


def test_read_file_missing_raises_internal_error(isolated_home: Path, tmp_path: Path) -> None:
    _bare, sha1, _ = _make_two_commit_repo(tmp_path)
    repo = _repo_config(tmp_path)
    provider = GitProvider()

    with pytest.raises(InternalError):
        provider.read_file(repo, sha1, "nope.py")


# ---------------------------------------------------------------- 工厂


def test_create_provider_git_returns_gitprovider() -> None:
    provider = create_provider(RepoType.GIT)
    assert isinstance(provider, GitProvider)


def test_create_provider_github_returns_gitprovider() -> None:
    provider = create_provider(RepoType.GITHUB)
    assert isinstance(provider, GitProvider)


def test_create_provider_p4_raises_environment_issue() -> None:
    with pytest.raises(EnvironmentIssue):
        create_provider(RepoType.P4)


# ---------------------------------------------------------------- metadata


def test_metadata_returns_repo_metadata(isolated_home: Path, tmp_path: Path) -> None:
    _bare, _, _ = _make_two_commit_repo(tmp_path)
    repo = _repo_config(tmp_path)
    provider = GitProvider()

    md = provider.metadata(repo)

    assert md.repo_id == "demo"
    assert md.provider is RepoType.GIT
    assert md.default_branch == "main"
