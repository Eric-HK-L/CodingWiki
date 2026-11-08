"""第三阶段补丁协议与隔离工作区领域模型（计划 14.6/14.9）。

只放字段定义与不变量：不读写文件、不执行 git、不打日志。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator

from codingwiki.domain.wiki import _WikiModel


class WorkspaceStatus(StrEnum):
    """工作区生命周期：active 可检查，discarded 已移除，kept 成功保留分支。"""

    ACTIVE = "active"
    DISCARDED = "discarded"
    KEPT = "kept"


class FindingSeverity(StrEnum):
    """检查结论级别。blocker 一律拒绝补丁，warning 交人工复核。"""

    BLOCKER = "blocker"
    WARNING = "warning"
    INFO = "info"


class ChangeKind(StrEnum):
    """单文件变更类型。未跟踪文件单独记 untracked，便于审计新建文件。"""

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    UNTRACKED = "untracked"


class PatchVerdict(StrEnum):
    """补丁总判定。"""

    PASS = "pass"
    FAIL = "fail"


class PatchFinding(_WikiModel):
    """一条检查发现；code 稳定可用于脚本过滤（如 path.forbidden）。"""

    code: str = Field(min_length=1)
    severity: FindingSeverity
    message: str = Field(min_length=1)
    path: str = ""


class FileChange(_WikiModel):
    """一个被改动的文件及其行数统计。"""

    path: str = Field(min_length=1)
    kind: ChangeKind
    added_lines: int = Field(default=0, ge=0)
    deleted_lines: int = Field(default=0, ge=0)
    binary: bool = False


class PatchStats(_WikiModel):
    """补丁规模统计。"""

    files: int = Field(default=0, ge=0)
    added_lines: int = Field(default=0, ge=0)
    deleted_lines: int = Field(default=0, ge=0)

    @property
    def total_lines(self) -> int:
        return self.added_lines + self.deleted_lines


class PatchPolicy(_WikiModel):
    """补丁策略。默认值即计划 14.6.2/14.6.3 的默认禁止集合。

    allowed_paths 为空表示源码改动一律拒绝（只允许同模块测试文件），
    避免“忘了传 Spec”时静默放行。
    """

    max_files: int = Field(default=3, ge=1, le=1000)
    max_lines: int = Field(default=300, ge=1, le=100000)
    allowed_paths: list[str] = Field(default_factory=list)
    allow_module_tests: bool = True
    allow_formatting: bool = False
    allow_generated: bool = False
    max_commented_lines: int = Field(default=15, ge=1, le=1000)
    todo_blocker: bool = False
    require_test_first: bool = True

    @field_validator("allowed_paths")
    @classmethod
    def _paths_nonempty(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("allowed_paths 不能包含空路径")
        return value


class WorkspaceRecord(_WikiModel):
    """一个任务的隔离工作区记录（workspace.json）。"""

    repo_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    task_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    base_revision: str = Field(min_length=1)
    branch: str = ""
    path: str = Field(min_length=1)
    root: str = ""  # 仓库内分析根；check 时把 diff 路径归一化到该根下
    status: WorkspaceStatus = WorkspaceStatus.ACTIVE
    created_at: datetime
    last_rollback_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.status is WorkspaceStatus.ACTIVE


class PatchReport(_WikiModel):
    """一次补丁检查的结果（check.json）。policy 一并落盘，便于复算判定。"""

    repo_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    task_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    base_revision: str = Field(min_length=1)
    worktree: str = Field(min_length=1)
    verdict: PatchVerdict
    stats: PatchStats
    files: list[FileChange] = Field(default_factory=list)
    findings: list[PatchFinding] = Field(default_factory=list)
    policy: PatchPolicy
    created_at: datetime

    @property
    def blockers(self) -> list[PatchFinding]:
        return [item for item in self.findings if item.severity is FindingSeverity.BLOCKER]

    @property
    def warnings(self) -> list[PatchFinding]:
        return [item for item in self.findings if item.severity is FindingSeverity.WARNING]

    @property
    def has_blockers(self) -> bool:
        return bool(self.blockers)


__all__ = [
    "ChangeKind",
    "FileChange",
    "FindingSeverity",
    "PatchFinding",
    "PatchPolicy",
    "PatchReport",
    "PatchStats",
    "PatchVerdict",
    "WorkspaceRecord",
    "WorkspaceStatus",
]
