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
    TIME_EXCEEDED = "time_exceeded"
    TOKEN_EXCEEDED = "token_exceeded"
    DIFF_GROWING = "diff_growing"
    FLAKY_BASELINE = "flaky_baseline"
    RED_GATE_FAILED = "red_gate_failed"


class RunLimits(_WikiModel):
    """修复循环预算（计划 14.5.5）。0 表示该项不限制。

    预算按编排器已计量的命令耗时与后端自报词元累计，不估算未计量部分。
    """

    max_duration_s: int = Field(default=0, ge=0, le=86_400)
    max_tokens: int = Field(default=0, ge=0)
    diff_growth_rounds: int = Field(default=2, ge=0, le=10)

    @property
    def has_budget(self) -> bool:
        return self.max_duration_s > 0 or self.max_tokens > 0


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
    UNSAFE_CHANGE = "unsafe_change"
    DEPENDENCY_CHANGE = "dependency_change"
    NO_PROGRESS = "no_progress"
    ROUNDS_EXHAUSTED = "rounds_exhausted"
    BUDGET_EXCEEDED = "budget_exceeded"
    DIFF_GROWING = "diff_growing"
    FLAKY_TESTS = "flaky_tests"
    VERIFIER_REJECTED = "verifier_rejected"
    TEST_NOT_RED = "test_not_red"


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


class RedGateStatus(StrEnum):
    """测试先行门禁结论（开发计划 14.5.2）。

    red 表示测试在基线 revision 上按预期失败；green 表示测试一开始就通过，
    说明没有覆盖新行为（本轮退回）；error 表示门禁本身没能执行。
    """

    RED = "red"
    GREEN = "green"
    ERROR = "error"


class RedGateReport(_WikiModel):
    """一次红门禁的证据（runs/<run_id>/round-N/redgate/redgate.json）。

    在基线 revision 的独立工作树上只应用测试改动后运行 fast 测试；
    worktree 用完即删，报告与命令输出保留供人工复核。
    """

    repo_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    base_revision: str = Field(min_length=1)
    status: RedGateStatus
    test_files: list[str] = Field(default_factory=list)
    command: list[str] = Field(default_factory=list)
    exit_code: int = 0
    duration_ms: int = Field(default=0, ge=0)
    stdout_tail: str = ""
    stderr_tail: str = ""
    message: str = ""
    worktree: str = ""
    report_path: str = ""
    created_at: datetime

    @property
    def blocked(self) -> bool:
        return self.status is RedGateStatus.GREEN


class TestFailureKind(StrEnum):
    """JUnit 用例结局；error 是测试框架报错，failure 是断言失败。"""

    FAILURE = "failure"
    ERROR = "error"


class TestFailure(_WikiModel):
    """一条 JUnit 用例级失败（14.5.4）；id 为 classname::name。"""

    id: str = Field(min_length=1)
    kind: TestFailureKind = TestFailureKind.FAILURE
    file: str = ""
    line: int = Field(default=0, ge=0)
    message: str = ""


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
    report_files: list[str] = Field(default_factory=list)
    test_failures: list[TestFailure] = Field(default_factory=list)
    test_skipped: list[str] = Field(default_factory=list)
    flaky_tests: list[str] = Field(default_factory=list)
    attempts: int = Field(default=1, ge=1, le=10)
    report_error: str = ""

    @property
    def tests_parsed(self) -> bool:
        """报告存在且解析成功才算可比较；坏 XML 退回退出码/日志。"""
        return bool(self.report_files) and not self.report_error


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
    red_gate: RedGateReport | None = None
    message: str = ""


class CommandAuditEntry(_WikiModel):
    """一条命令执行审计（<run>/audit/commands.jsonl，开发计划 14.9）。"""

    seq: int = Field(ge=1)
    kind: str = Field(min_length=1)  # stage:<kind> / backend / verifier
    argv: list[str] = Field(min_length=1)
    cwd: str = ""
    status: str = Field(min_length=1)
    exit_code: int
    duration_ms: int = Field(default=0, ge=0)
    started_at: datetime
    error: str = ""


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


class ReplayStatus(StrEnum):
    """补丁重放结论（架构 §9 报告可复现）。"""

    REPRODUCED = "reproduced"
    DIVERGED = "diverged"
    FAILED = "failed"


class ReplayCheck(_WikiModel):
    """一个阶段的重放对比：期望（原运行）与实际（重放工作树）。"""

    kind: StageKind
    expected_status: StageStatus
    expected_exit_code: int
    actual_status: StageStatus | None = None
    actual_exit_code: int = 0
    matched: bool = False


class ReplayReport(_WikiModel):
    """一次补丁重放（replay.json）：快照 + 补丁 + 阶段结果对比。"""

    run_id: str = Field(pattern=r"^RUN-[A-Za-z0-9._-]+$")
    repo_id: str = Field(min_length=1)
    spec_id: str = Field(pattern=r"^F-\d{3}$")
    task_id: str = Field(pattern=r"^T-\d{3}$")
    base_revision: str = Field(min_length=1)
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: ReplayStatus
    checks: list[ReplayCheck] = Field(default_factory=list)
    workspace: str
    replay_dir: str
    patch_path: str
    message: str = ""
    created_at: datetime


class RunMetrics(_WikiModel):
    """一次运行的计量指标（开发计划 14.11）；只统计已计量命令。"""

    succeeded: bool
    first_pass: bool = False
    rounds: int = Field(default=0, ge=0)
    max_rounds: int = Field(default=1, ge=1)
    backend_duration_ms: int = Field(default=0, ge=0)
    stage_duration_ms: int = Field(default=0, ge=0)
    total_duration_ms: int = Field(default=0, ge=0)
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    baseline_failures: int = Field(default=0, ge=0)
    final_failures: int = Field(default=0, ge=0)
    tests_parsed: bool = False
    new_failures: int = Field(default=0, ge=0)
    pre_existing_failures: int = Field(default=0, ge=0)
    skipped_tests: int = Field(default=0, ge=0)
    flaky_tests: int = Field(default=0, ge=0)
    quarantined_tests: int = Field(default=0, ge=0)
    guard_rejected: bool = False
    verifier_rejected: bool = False
    budget_exceeded: bool = False
    diff_growing: bool = False
    red_gate_blocked: bool = False
    regression: bool = False
    escalated: bool = False


class RunReport(_WikiModel):
    """一次运行的指标报告（runs/<run_id>/report.json）。"""

    run_id: str = Field(pattern=r"^RUN-[A-Za-z0-9._-]+$")
    repo_id: str = Field(min_length=1)
    spec_id: str = Field(pattern=r"^F-\d{3}$")
    task_id: str = Field(pattern=r"^T-\d{3}$")
    status: CodingRunStatus
    stop_reason: CodingStopReason
    metrics: RunMetrics
    created_at: datetime


class CodingStats(_WikiModel):
    """跨运行汇总（coding report）：成功率、一次通过率、平均轮次与耗时。"""

    runs: int = Field(default=0, ge=0)
    succeeded: int = Field(default=0, ge=0)
    first_pass: int = Field(default=0, ge=0)
    guard_rejected: int = Field(default=0, ge=0)
    verifier_rejected: int = Field(default=0, ge=0)
    regression: int = Field(default=0, ge=0)
    escalated: int = Field(default=0, ge=0)
    total_rounds: int = Field(default=0, ge=0)
    total_duration_ms: int = Field(default=0, ge=0)
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)

    @property
    def success_rate(self) -> float:
        return self.succeeded / self.runs if self.runs else 0.0

    @property
    def first_pass_rate(self) -> float:
        return self.first_pass / self.runs if self.runs else 0.0

    @property
    def avg_rounds(self) -> float:
        return self.total_rounds / self.runs if self.runs else 0.0


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
    delivery_path: str = ""
    audit_path: str = ""
    backend: BackendResult | None = None
    guard: PatchReport | None = None
    baseline: list[StageReport] = Field(default_factory=list)
    stages: list[StageReport] = Field(default_factory=list)
    errors: list[FailureItem] = Field(default_factory=list)
    verifier: VerifierReport | None = None
    red_gate: RedGateReport | None = None
    round_reports: list[RoundReport] = Field(default_factory=list)
    limits: RunLimits = Field(default_factory=RunLimits)
    quarantined_tests: list[str] = Field(default_factory=list)
    message: str = ""
    created_at: datetime


class DeliveryBundle(_WikiModel):
    """一次成功运行的交付包（delivery.json，开发计划 14.5.7）。

    只生成材料（补丁、变更说明、测试摘要、PR 草稿），不自动提交、推送或创建合并请求。
    """

    run_id: str = Field(pattern=r"^RUN-[A-Za-z0-9._-]+$")
    repo_id: str = Field(min_length=1)
    spec_id: str = Field(pattern=r"^F-\d{3}$")
    task_id: str = Field(pattern=r"^T-\d{3}$")
    branch: str = Field(min_length=1)
    base_revision: str = Field(min_length=1)
    status: CodingRunStatus
    stop_reason: CodingStopReason
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    files: list[str] = Field(default_factory=list)
    metrics: RunMetrics
    patch_path: str = Field(min_length=1)
    created_at: datetime


__all__ = [
    "BackendConfig",
    "BackendResult",
    "BackendStatus",
    "CodedTask",
    "CodingRun",
    "CodingRunStatus",
    "CodingStats",
    "CodingStopReason",
    "CommandAuditEntry",
    "ContextFile",
    "DeliveryBundle",
    "EscalationFailure",
    "EscalationReason",
    "EscalationReport",
    "FailureItem",
    "RedGateReport",
    "RedGateStatus",
    "ReplayCheck",
    "ReplayReport",
    "ReplayStatus",
    "RoundReport",
    "RunLimits",
    "RunMetrics",
    "RunReport",
    "StageKind",
    "StageReport",
    "StageStatus",
    "TaskPacket",
    "TestFailure",
    "TestFailureKind",
    "VerifierIssue",
    "VerifierReport",
    "VerifierSource",
    "VerifierVerdict",
]
