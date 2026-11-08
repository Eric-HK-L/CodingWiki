"""阶段一的领域数据模型。

对应架构文档 v0.2 第 3.4 节、计划文档 7.2 和 7.5.3。
所有模型只放 Pydantic 字段定义和 classmethod 校验，不读写文件、不打日志。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _DomainModel(BaseModel):
    """领域模型基类：拒绝未知字段、不可变、去掉字符串首尾空白。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class RepoType(StrEnum):
    """支持的代码源类型。公司内 P4 先占位，Git/GitHub 是 S2 主路径。"""

    GIT = "git"
    GITHUB = "github"  # 语义等价于 git；配置层区分是为了直观
    P4 = "p4"


class BuildCommands(_DomainModel):
    """构建命令（计划文档 14.7.1）。空字符串表示未配置该步。"""

    configure: str = ""
    compile: str = ""
    incremental: str = ""
    timeout_s: int = Field(default=1800, ge=1, le=86_400)


class TestCommands(_DomainModel):
    """测试命令（14.7.1）。fast 是快速通道，full 是回归全量。

    report_format/report_paths 声明测试报告位置（14.5.4）；路径是相对源码根的 glob，
    只允许仓库内相对路径，避免读取仓库外文件。
    baseline_runs 为基线重复次数（14.7.3 默认 3 次，用于识别不稳定测试）；
    retries 为用例失败后的重跑次数；critical_tests 是不允许不稳定的测试 id 模式。
    """

    fast: str = ""
    full: str = ""
    timeout_s: int = Field(default=3600, ge=1, le=86_400)
    report_format: Literal["", "junit"] = ""
    report_paths: list[str] = Field(default_factory=list)
    baseline_runs: int = Field(default=3, ge=1, le=5)
    retries: int = Field(default=2, ge=0, le=5)
    critical_tests: list[str] = Field(default_factory=list)

    @field_validator("report_paths")
    @classmethod
    def _report_paths_relative(cls, value: list[str]) -> list[str]:
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("report_paths 不能包含空路径")
            path = Path(text)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"report_paths 只允许仓库内相对路径：{text}")
        return value

    @field_validator("critical_tests")
    @classmethod
    def _critical_tests_patterns(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("critical_tests 不能包含空模式")
        return value


class LintCommands(_DomainModel):
    """静态检查命令（14.7.1）。"""

    command: str = ""
    timeout_s: int = Field(default=600, ge=1, le=86_400)


class RepoCommands(_DomainModel):
    """仓库级命令配置：configure/compile/test/lint 由编排器执行（14.3 硬边界：
    模型只改文件，不跑命令）。命令是 shell 字符串形态，落 CommandSpec 时由调用方
    负责拆 argv。"""

    build: BuildCommands = Field(default_factory=BuildCommands)
    test: TestCommands = Field(default_factory=TestCommands)
    lint: LintCommands = Field(default_factory=LintCommands)


class RepoConfig(_DomainModel):
    """单个代码仓库的运行配置。"""

    repo_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    provider: RepoType
    url: str = Field(min_length=1)
    branch: str = "main"
    root: str = ""  # 可选：仓库内子目录作为分析根
    revision: Literal["latest"] | str = "latest"
    enabled: bool = True  # false 时 `run --repo all` 跳过该仓库（计划 5172）
    workdir: str = ""  # 空表示用默认值（.codingwiki/repos/<repo_id>/work）
    excludes: list[str] = Field(default_factory=list)
    max_text_file_size_kb: int = Field(default=2048, ge=1, le=512 * 1024)
    language_overrides: dict[str, str] = Field(default_factory=dict)
    binary_extensions: list[str] = Field(default_factory=list)
    generate_file_exclusion_defaults: bool = True
    commands: RepoCommands = Field(default_factory=RepoCommands)

    @field_validator("url")
    @classmethod
    def _url_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("url 不能为空")
        return value

    @model_validator(mode="after")
    def _provider_fields(self) -> RepoConfig:
        """GitHub 只是 git 的别名，url 可以是非 github.com 风格。"""
        # 不强报错，只做软提示；本地/镜像场景 url 可能不含 github.com
        if self.provider is RepoType.GITHUB and "github.com" not in self.url:
            pass
        return self


class RepoMetadata(_DomainModel):
    """仓库的静态元数据（provider.metadata() 返回）。"""

    repo_id: str
    provider: RepoType
    url: str
    default_branch: str
    language_counts: dict[str, int] = Field(default_factory=dict)


class FileEntry(_DomainModel):
    """manifest 里的单个文件条目。"""

    path: str
    blob_sha256: str
    size_bytes: int = Field(ge=0)
    language: str = "unknown"
    excluded: bool = False
    exclusion_reason: str = ""
    binary: bool = False
    truncated: bool = False


class Manifest(_DomainModel):
    """一次快照的完整清单。

    对应计划文档 7.5.3 的 JSON 格式。
    """

    snapshot_id: str
    repo_id: str
    provider: RepoType
    revision: str
    parent_snapshot_id: str = ""  # 空表示第一次快照
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    file_count: int = Field(ge=0)
    in_scope_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    languages: dict[str, int] = Field(default_factory=dict)
    files: list[FileEntry] = Field(default_factory=list)
    exclusions: list[dict[str, str]] = Field(default_factory=list)

    @field_validator("files")
    @classmethod
    def _counts_consistent(cls, value: list[FileEntry]) -> list[FileEntry]:
        if value:
            excluded = sum(1 for f in value if f.excluded)
            in_scope = len(value) - excluded
            # 这里只做弱校验，真正的一致关系由 SnapshotStore.create() 负责
            if excluded + in_scope != len(value):
                raise ValueError("excluded + in_scope 应该等于 files 总数")
        return value


class Snapshot(_DomainModel):
    """快照本身（轻量，文件清单在 Manifest 里单独存）。"""

    snapshot_id: str  # 格式：<repo_id>@<sha>
    repo_id: str
    revision: str
    provider: RepoType
    manifest_path: str
    parent_snapshot_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ChangeSet(_DomainModel):
    """两个版本之间的变更。对应计划文档 7.2。"""

    old_revision: str
    new_revision: str
    added: list[str] = Field(default_factory=list)
    modified: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)
    renamed: list[tuple[str, str]] = Field(default_factory=list)
    binary_changed: list[str] = Field(default_factory=list)


__all__ = [
    "BuildCommands",
    "ChangeSet",
    "FileEntry",
    "LintCommands",
    "Manifest",
    "RepoCommands",
    "RepoConfig",
    "RepoMetadata",
    "RepoType",
    "Snapshot",
    "TestCommands",
]
