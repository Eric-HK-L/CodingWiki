"""GitHubProvider 单元测试。

覆盖 URL 规范化、owner/repo 解析、令牌鉴权头、LFS 跳过环境变量、
以及继承自 GitProvider 的取代码行为仍然可用。
"""

from __future__ import annotations

import base64
from collections.abc import Generator
from pathlib import Path

import pytest

from codingwiki.domain.models import RepoConfig, RepoType
from codingwiki.providers import GitHubProvider, GitProvider, create_provider
from codingwiki.providers.github import normalize_github_url, parse_github_owner_repo


@pytest.fixture(autouse=True)
def _clear_token_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.delenv("CODINGWIKI_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    yield


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".codingwiki"
    monkeypatch.setenv("CODINGWIKI_HOME", str(home))
    return home


# ---------------------------------------------------------------- URL 规范化


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("owner/repo", "https://github.com/owner/repo.git"),
        ("github.com/owner/repo", "https://github.com/owner/repo.git"),
        ("https://github.com/owner/repo", "https://github.com/owner/repo.git"),
        ("https://github.com/owner/repo.git", "https://github.com/owner/repo.git"),
        ("https://github.com/owner/repo/", "https://github.com/owner/repo.git"),
        ("  owner/repo  ", "https://github.com/owner/repo.git"),
    ],
)
def test_normalize_github_url_variants(raw: str, expected: str) -> None:
    assert normalize_github_url(raw) == expected


def test_normalize_keeps_ssh_form() -> None:
    assert normalize_github_url("git@github.com:owner/repo.git") == "git@github.com:owner/repo.git"


def test_normalize_keeps_local_path() -> None:
    assert normalize_github_url("/tmp/some/repo.git") == "/tmp/some/repo.git"


def test_normalize_empty_returns_empty() -> None:
    assert normalize_github_url("   ") == ""


# ---------------------------------------------------------------- owner/repo 解析


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/NVIDIA/cuphy.git", ("NVIDIA", "cuphy")),
        ("owner/repo", ("owner", "repo")),
        ("git@github.com:owner/repo.git", ("owner", "repo")),
        ("https://gitlab.com/owner/repo.git", None),
        ("/tmp/repo.git", None),
    ],
)
def test_parse_github_owner_repo(url: str, expected: tuple[str, str] | None) -> None:
    assert parse_github_owner_repo(url) == expected


# ---------------------------------------------------------------- 工厂


def test_factory_returns_github_provider_for_github() -> None:
    provider = create_provider(RepoType.GITHUB)
    assert isinstance(provider, GitHubProvider)
    assert provider.name == "github"


def test_factory_returns_git_provider_for_git() -> None:
    provider = create_provider(RepoType.GIT)
    assert isinstance(provider, GitProvider)
    assert not isinstance(provider, GitHubProvider)


# ---------------------------------------------------------------- 鉴权与 env


def _repo(url: str, repo_id: str = "demo") -> RepoConfig:
    return RepoConfig(repo_id=repo_id, provider=RepoType.GITHUB, url=url)


def test_auth_args_empty_without_token() -> None:
    provider = GitHubProvider(token=None)
    assert provider._auth_args(_repo("owner/repo")) == []


def test_auth_args_injects_basic_header_with_token() -> None:
    provider = GitHubProvider(token="secret-token")
    args = provider._auth_args(_repo("owner/repo"))

    assert args[0] == "-c"
    header = args[1]
    assert header.startswith("http.extraheader=AUTHORIZATION: basic ")
    encoded = header.split("basic ", 1)[1]
    decoded = base64.b64decode(encoded).decode()
    assert decoded == "x-access-token:secret-token"


def test_auth_args_skipped_for_non_http_url() -> None:
    provider = GitHubProvider(token="secret-token")
    assert provider._auth_args(_repo("/tmp/local/repo.git")) == []


def test_token_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODINGWIKI_GITHUB_TOKEN", "env-token")
    provider = GitHubProvider()
    args = provider._auth_args(_repo("owner/repo"))
    assert args, "环境变量里的令牌应被使用"


def test_command_env_skips_lfs_smudge() -> None:
    provider = GitHubProvider()
    assert provider._command_env() == {"GIT_LFS_SKIP_SMUDGE": "1"}


# ---------------------------------------------------------------- metadata


def test_metadata_uses_github_provider_and_normalized_url() -> None:
    provider = GitHubProvider()
    md = provider.metadata(_repo("owner/repo"))

    assert md.provider is RepoType.GITHUB
    assert md.url == "https://github.com/owner/repo.git"
    assert md.default_branch == "main"
    assert md.repo_id == "demo"


# ---------------------------------------------------------------- 继承行为仍可用


def test_materialize_still_works_with_local_remote(isolated_home: Path, tmp_path: Path) -> None:
    """GitHubProvider 对非 GitHub 地址（如本地镜像）应原样接管。"""
    import subprocess

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    for args in (
        ["init"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "T"],
        ["checkout", "-b", "main"],
    ):
        subprocess.run(["git", *args], cwd=str(work), check=True, capture_output=True)
    (work / "a.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(work), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c1"], cwd=str(work), check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(work), capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(
        ["git", "push", "-u", str(bare), "main"], cwd=str(work), check=True, capture_output=True
    )

    repo = RepoConfig(
        repo_id="demo",
        provider=RepoType.GITHUB,
        url=str(bare),
        branch="main",
        revision="latest",
        workdir=str(tmp_path / "checkout"),
    )
    provider = GitHubProvider()

    assert provider.latest_revision(repo) == sha
    materialized = provider.materialize(repo, sha)
    assert (materialized / "a.txt").is_file()
