"""第三阶段编码任务与模型后端契约（架构 阶段3 §4–6）。

只放字段与不变量；任务分解、任务包、进程调用在 coding/ 能力层。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from codingwiki.domain.patch import FindingSeverity, PatchPolicy, PatchReport
from codingwiki.domain.wiki import _WikiModel


class BackendStatus(StrEnum):
    """一次模型后端进程的结局。timeout/error 与 failed 区分，便于编排决策。"""

    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ERROR = "error"


class CodingRunStatus(StrEnum):
    """编码运行结论。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CodingStopReason(StrEnum):
    """停止原因；比 succeeded/failed 更细，便于人工分诊。"""

    SUCCEEDED = "succeeded"
    BASELINE_FAILED = "baseline_failed"
    BACKEND_FAILED = "backend_failed"
    GUARD_REJECTED = "guard_rejected"
    VERIFY_REJECTED = "verify_rejected"
    ROUNDS_EXHAUSTED = "rounds_exhausted"
    NO_PROGRESS = "no_progress"


class EscalationReason(StrEnum):
    """升级给人处理的首要原因（开发计划 14.10）。

    比 stop_reason 更贴近人的决策：同一个 guard_rejected 可能是越界修改，
    也可能是密钥告警，人工需要看的材料不同。
    """

    REQUIREMENT_DECISION = "requirement_decision"
    ENVIRONMENT_BROKEN = "environment_broken"
    BACKEND_FAILED = "backend_failed"
    FORBIDDEN_FILE = "forbidden_file"
    SECURITY_ALERT = "security_alert"
    NO_PROGRESS = "no_progress"
    ROUNDS_EXHAUSTED = "rounds_exhausted"
    VERIFIER_REJECTED = "verifier_rejected"


class StageKind(StrEnum):
    """编排器执行的检查阶段（14.5.4 / 14.7.1）。"""

    CONFIGURE = "configure"
    LINT = "lint"
    COMPILE = "compile"
    TEST_FAST = "test-fast"
    TEST_FULL = "test-full"


class StageStatus(StrEnum):
    """阶段结局；与 exec.CommandStatus 取值一致。"""

    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ERROR = "error"


class StageReport(_WikiModel):
    """一次构建/测试/lint 命令的结果（stage 级，不含原始全量日志）。"""

    kind: StageKind
    command: list[str] = Field(default_factory=list)
    status: StageStatus
    exit_code: int
    duration_ms: int = Field(default=0, ge=0)
    stdout_tail: str = ""
    stderr_tail: str = ""
    artifacts: list[str] = Field(default_factory=list)


class FailureItem(_WikiModel):
    """结构化失败项：文件/行/错误码/消息；回喂模型的就是这个列表。"""

    stage: StageKind
    code: str = Field(min_length=1)
    file: str = ""
    line: int = Field(default=0, ge=0)
    message: str = Field(min_length=1)

    @property
    def key(self) -> tuple[str, str, int, str]:
        """去重/无进展判定用的稳定键（消息截断，避免长尾变化）。"""
        return (self.stage.value, self.file, self.line, self.message[:120])


class VerifierVerdict(StrEnum):
    """独立验证结论。"""

    PASS = "pass"
    FAIL = "fail"


class VerifierSource(StrEnum):
    """问题来源：机械检查（编排器）或外部评审后端。"""

    MECHANICAL = "mechanical"
    EXTERNAL = "external"


class VerifierIssue(_WikiModel):
    """一条验证问题；必须能落到文件/行或提供证据，否则视为无效输出。"""

    source: VerifierSource
    code: str = Field(min_length=1)
    severity: FindingSeverity
    message: str = Field(min_length=1)
    file: str = ""
    line: int = Field(default=0, ge=0)
    evidence: str = ""

    @property
    def has_evidence(self) -> bool:
        return bool(self.evidence) or (bool(self.file) and self.line > 0)


class VerifierReport(_WikiModel):
    """独立验证报告（verification.json）；通过也必须列残余风险。"""

    verdict: VerifierVerdict
    issues: list[VerifierIssue] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    residual_risks: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0, le=1)
    external: bool = False
    message: str = ""

    @property
    def blockers(self) -> list[VerifierIssue]:
        return [item for item in self.issues if item.severity is FindingSeverity.BLOCKER]


class CodedTask(_WikiModel):
    """从 Spec DesignTask 派生出的可编码任务。"""

    id: str = Field(pattern=r"^T-\d{3}$")
    spec_id: str = Field(pattern=r"^F-\d{3}$")
    title: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    requirement_ids: list[str] = Field(default_factory=list)
    files_allowed: list[str] = Field(min_length=1, max_length=3)
    acceptance: list[str] = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    estimated_lines: int = Field(ge=1, le=300)

    @field_validator("files_allowed")
    @classmethod
    def _files_nonempty(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("files_allowed 不能包含空路径")
        return value


class ContextFile(_WikiModel):
    """任务包内一个上下文文件的指纹。"""

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)


class TaskPacket(_WikiModel):
    """一个编码任务包（packet/task.json）。"""

    packet_id: str = Field(pattern=r"^PKT-[A-Za-z0-9._-]+$")
    repo_id: str = Field(min_length=1)
    spec_id: str = Field(pattern=r"^F-\d{3}$")
    task_id: str = Field(pattern=r"^T-\d{3}$")
    base_revision: str = Field(min_length=1)
    task: CodedTask
    context_files: list[ContextFile] = Field(default_factory=list)
    prompt_path: str = Field(min_length=1)
    schema_path: str = Field(min_length=1)
    packet_dir: str = Field(min_length=1)
    policy: PatchPolicy
    round: int = Field(default=1, ge=1)
    errors_path: str = ""
    created_at: datetime


class BackendConfig(_WikiModel):
    """进程级模型后端配置。

    command 为 argv 模板，支持 {packet_dir} / {workspace} / {result_path}；
    不经过 shell，避免把任务内容拼进命令串。
    """

    kind: str = Field(default="command", pattern=r"^command$")
    command: list[str] = Field(min_length=1)
    timeout_seconds: int = Field(default=1800, ge=1, le=86400)

    @field_validator("command")
    @classmethod
    def _command_tokens(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("command 不能包含空参数")
        return value


class BackendResult(_WikiModel):
    """后端进程结果；stdout/stderr 只保留尾部。"""

    kind: str = "command"
    status: BackendStatus
    exit_code: int
    duration_ms: int = Field(ge=0)
    stdout_tail: str = ""
    stderr_tail: str = ""
    result_json: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class RoundReport(_WikiModel):
    """一轮“后端 → guard → 构建/测试”的完整记录。"""

    index: int = Field(ge=1)
    backend: BackendResult
    guard: PatchReport
    stages: list[StageReport] = Field(default_factory=list)
    errors: list[FailureItem] = Field(default_factory=list)
    verifier: VerifierReport | None = None
    message: str = ""


class EscalationFailure(_WikiModel):
    """升级单里的一条失败/拦截项；来源不限于某个构建阶段。"""

    source: str = Field(min_length=1)
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    file: str = ""
    line: int = Field(default=0, ge=0)


class EscalationReport(_WikiModel):
    """一次失败运行的升级单（escalation.json）；成功运行不生成。"""

    run_id: str = Field(pattern=r"^RUN-[A-Za-z0-9._-]+$")
    repo_id: str = Field(min_length=1)
    spec_id: str = Field(pattern=r"^F-\d{3}$")
    task_id: str = Field(pattern=r"^T-\d{3}$")
    status: CodingRunStatus
    stop_reason: CodingStopReason
    reason: EscalationReason
    summary: str = Field(min_length=1)
    attempted_rounds: int = Field(default=0, ge=0)
    max_rounds: int = Field(default=1, ge=1)
    attempts: list[str] = Field(default_factory=list)
    failures: list[EscalationFailure] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    suggested_next_steps: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    workspace: str = ""
    created_at: datetime


class CodingRun(_WikiModel):
    """一次编码运行（runs/<run_id>/run.json）。"""

    run_id: str = Field(pattern=r"^RUN-[A-Za-z0-9._-]+$")
    repo_id: str = Field(min_length=1)
    spec_id: str = Field(pattern=r"^F-\d{3}$")
    task_id: str = Field(pattern=r"^T-\d{3}$")
    base_revision: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    status: CodingRunStatus
    stop_reason: CodingStopReason = CodingStopReason.SUCCEEDED
    rounds: int = Field(default=0, ge=0)
    max_rounds: int = Field(default=1, ge=1)
    packet_dir: str = ""
    escalation_path: str = ""
    backend: BackendResult | None = None
    guard: PatchReport | None = None
    baseline: list[StageReport] = Field(default_factory=list)
    stages: list[StageReport] = Field(default_factory=list)
    errors: list[FailureItem] = Field(default_factory=list)
    verifier: VerifierReport | None = None
    round_reports: list[RoundReport] = Field(default_factory=list)
    message: str = ""
    created_at: datetime


__all__ = [
    "BackendConfig",
    "BackendResult",
    "BackendStatus",
    "CodedTask",
    "CodingRun",
    "CodingRunStatus",
    "CodingStopReason",
    "ContextFile",
    "EscalationFailure",
    "EscalationReason",
    "EscalationReport",
    "FailureItem",
    "RoundReport",
    "StageKind",
    "StageReport",
    "StageStatus",
    "TaskPacket",
    "VerifierIssue",
    "VerifierReport",
    "VerifierSource",
    "VerifierVerdict",
]
