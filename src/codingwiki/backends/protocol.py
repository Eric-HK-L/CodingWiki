"""模型后端统一接口（计划文档 11.3）。

业务层只依赖 ModelBackend Protocol，不知道背后是 Cline、Gauss 还是外部 API。
选择哪个后端由配置决定，业务代码不写环境分支。
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class BackendStatus(StrEnum):
    """后端执行结果状态。invalid_output 走修复循环；timeout 可重试（11.7）。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    INVALID_OUTPUT = "invalid_output"


class TaskPacket(BaseModel):
    """一次模型任务的文件契约。workspace 即任务包目录（9.5）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace: Path
    task_file: Path
    output_dir: Path
    timeout_s: int = Field(default=1800, ge=1)
    metadata: dict[str, str] = Field(default_factory=dict)  # page_id / snapshot / task_id 等


class BackendResult(BaseModel):
    """一次后端调用的完整记录（观测与审计，11.9）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: BackendStatus
    exit_code: int | None = None
    output_files: list[str] = Field(default_factory=list)  # 相对 workspace 的路径
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = Field(default=0, ge=0)
    tokens_in: int | None = None
    tokens_out: int | None = None
    model: str = ""
    error: str = ""


class ModelBackend(Protocol):
    """统一接口：启动一次模型任务，读任务包，写 output/，返回结果。"""

    def run(self, packet: TaskPacket) -> BackendResult: ...


__all__ = [
    "BackendResult",
    "BackendStatus",
    "ModelBackend",
    "TaskPacket",
]
