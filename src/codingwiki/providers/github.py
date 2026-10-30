"""GitHubProvider：GitProvider 之上的 GitHub 专属适配。

只补三件 GitHub 特有的事，取代码/快照逻辑全部复用 GitProvider：
1. URL 规范化：`owner/repo`、`github.com/owner/repo` 自动补成 https 地址；
2. 私有仓库鉴权：有令牌时用 `git -c http.extraheader` 注入 Basic 头，
   令牌不会写进 bare 仓库的 remote url，避免落盘泄漏；
3. 跳过 LFS smudge：`GIT_LFS_SKIP_SMUDGE=1`，否则大文件会被拉成实体，
   快照体积失控，且无 LFS 环境时 clone 会直接失败。

子模块不递归初始化（`git worktree add` 默认行为），快照里只记录为空目录，
不进入 manifest，行为确定可复现。
"""

from __future__ import annotations

import base64
import os
import re
from urllib.parse import urlsplit

from codingwiki.domain.models import RepoConfig, RepoMetadata, RepoType
from codingwiki.providers.git import GitProvider

_SHORTHAND = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_TOKEN_ENV_VARS = ("CODINGWIKI_GITHUB_TOKEN", "GITHUB_TOKEN")


class GitHubProvider(GitProvider):
    name = "github"

    def __init__(self, git_bin: str | None = None, token: str | None = None) -> None:
        super().__init__(git_bin)
        self._token = token or _token_from_env()

    # ------------------------------------------------------------------ 公共方法

    def metadata(self, repo: RepoConfig) -> RepoMetadata:
        return RepoMetadata(
            repo_id=repo.repo_id,
            provider=RepoType.GITHUB,
            url=self._effective_url(repo),
            default_branch=repo.branch,
        )

    # ------------------------------------------------------------------ 钩子实现

    def _effective_url(self, repo: RepoConfig) -> str:
        return normalize_github_url(repo.url)

    def _auth_args(self, repo: RepoConfig) -> list[str]:
        url = self._effective_url(repo)
        if not self._token or not url.startswith(("http://", "https://")):
            return []
        # Basic 认证：用户名 x-access-token，密码为 PAT；GitHub 认这种组合
        pair = f"x-access-token:{self._token}".encode()
        header = f"AUTHORIZATION: basic {base64.b64encode(pair).decode()}"
        return ["-c", f"http.extraheader={header}"]

    def _command_env(self) -> dict[str, str] | None:
        return {"GIT_LFS_SKIP_SMUDGE": "1"}


def normalize_github_url(url: str) -> str:
    """把常见 GitHub 写法补全成可 clone 的地址；无法识别的原样返回。

    - `owner/repo`                       → `https://github.com/owner/repo.git`
    - `github.com/owner/repo`            → `https://github.com/owner/repo.git`
    - `https://github.com/owner/repo`    → 补 `.git` 后缀
    - `git@github.com:owner/repo`        → 原样保留（SSH 走本机密钥）
    - 本地路径 / 其他远端                → 原样保留（便于测试与镜像场景）
    """
    raw = url.strip()
    if not raw:
        return raw
    if raw.startswith("git@"):
        return raw

    if _SCHEME.match(raw):
        normalized = raw.rstrip("/")
        parts = urlsplit(normalized)
        if parts.netloc.endswith("github.com"):
            path = parts.path.strip("/")
            if path and not path.endswith(".git"):
                return f"{parts.scheme}://{parts.netloc}/{path}.git"
        return normalized

    cleaned = raw.rstrip("/")
    if cleaned.startswith("github.com/"):
        path = cleaned[len("github.com/") :]
        return f"https://github.com/{path}" + ("" if path.endswith(".git") else ".git")

    if _SHORTHAND.match(cleaned):
        return f"https://github.com/{cleaned}" + ("" if cleaned.endswith(".git") else ".git")

    return raw


def _token_from_env() -> str | None:
    for name in _TOKEN_ENV_VARS:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def parse_github_owner_repo(url: str) -> tuple[str, str] | None:
    """从 GitHub 地址里解析 (owner, repo)；解析不出来返回 None。"""
    normalized = normalize_github_url(url)
    if normalized.startswith("git@"):
        _, _, path = normalized.partition(":")
        path = path.strip("/")
    elif _SCHEME.match(normalized):
        parts = urlsplit(normalized)
        if "github.com" not in parts.netloc:
            return None
        path = parts.path.strip("/")
    else:
        return None

    path = path.removesuffix(".git")
    owner, _, repo = path.partition("/")
    if not owner or not repo:
        return None
    return owner, repo


__all__ = ["GitHubProvider", "normalize_github_url", "parse_github_owner_repo"]
