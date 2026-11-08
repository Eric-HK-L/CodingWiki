"""命令执行器协议（架构文档 v0.7 §16.3-3，计划文档 14.3 / 14.7.1）。

硬边界：Cline 只改文件，不执行命令；编译/测试/lint 由编排器执行，
输出统一成 CommandResult 可审计。三阶段直接复用本协议，不必改接口。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CommandStatus(StrEnum):
    """命令执行结果状态。"""

    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"


class _ExecModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class CommandSpec(_ExecModel):
    """一次命令执行的输入。argv 列表直接 exec，不经 shell，杜绝注入。"""

    argv: list[str] = Field(min_length=1)
    cwd: str = ""  # 空表示当前工作目录
    timeout_s: int = Field(default=600, ge=1, le=86_400)
    env: dict[str, str] = Field(default_factory=dict)  # 叠加在进程环境之上
    artifacts: list[str] = Field(default_factory=list)  # 期望产物（相对 cwd）
    label: str = ""  # 审计用标签；不影响执行
    cpu_seconds: int = Field(default=0, ge=0, le=86_400)  # 0 表示不限制（RLIMIT_CPU）

    @field_validator("argv")
    @classmethod
    def _argv_not_blank(cls, value: list[str]) -> list[str]:
        if not value[0].strip():
            raise ValueError("argv 第一项不能为空")
        return value


class CommandResult(_ExecModel):
    """一次命令执行的输出（§16.3-3 契约字段）。"""

    status: CommandStatus
    exit_code: int  # 超时记 -1
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = Field(default=0, ge=0)
    artifacts: list[str] = Field(default_factory=list)  # 实际存在的产物路径


@runtime_checkable
class CommandRunner(Protocol):
    """统一命令执行接口。实现只负责跑命令，不理解业务语义。"""

    def run(self, spec: CommandSpec) -> CommandResult: ...


__all__ = ["CommandResult", "CommandRunner", "CommandSpec", "CommandStatus"]
