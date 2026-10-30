"""SourceProvider Protocol 与工厂。

架构文档 v0.2 3.1 节定义的统一代码源接口。所有代码源实现同一组方法：
- latest_revision：返回最新稳定版本号；
- materialize：把指定版本同步到固定本地目录（git worktree / p4 workspace）；
- changes：两个版本之间的变更清单；
- read_file：按版本读取单个文件内容，用于校验和补取；
- metadata：仓库的静态元数据。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from codingwiki.domain.models import (
    ChangeSet,
    RepoConfig,
    RepoMetadata,
    RepoType,
)


@runtime_checkable
class SourceProvider(Protocol):
    name: str  # provider 名字：git / p4

    def latest_revision(self, repo: RepoConfig) -> str: ...
    def materialize(self, repo: RepoConfig, revision: str) -> Path: ...
    def changes(self, repo: RepoConfig, old: str, new: str) -> ChangeSet: ...
    def read_file(self, repo: RepoConfig, revision: str, path: str) -> bytes: ...
    def metadata(self, repo: RepoConfig) -> RepoMetadata: ...


def create_provider(provider_type: RepoType) -> SourceProvider:
    """按 RepoType 创建对应 Provider。

    GIT 与 GITHUB 都走 git 协议，但 GitHub 多一层 URL 规范化和令牌鉴权；
    P4 在 S3 实现，这里只放占位——抛 EnvironmentIssue，提示配置到位后再启用。
    """
    if provider_type is RepoType.GITHUB:
        from codingwiki.providers.github import GitHubProvider

        return GitHubProvider()
    if provider_type is RepoType.GIT:
        from codingwiki.providers.git import GitProvider

        return GitProvider()
    if provider_type is RepoType.P4:
        from codingwiki.errors import EnvironmentIssue

        raise EnvironmentIssue("P4 Provider 未实现；请在 S3 启用，或改用 git/github")
    # 不可达：RepoType 是 StrEnum，只有上述三种
    raise ValueError(f"未知 provider 类型: {provider_type}")  # pragma: no cover


__all__ = ["SourceProvider", "create_provider"]
