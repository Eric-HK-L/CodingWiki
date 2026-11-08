"""独立验证器（开发计划 14.8，架构 §9）。

两层检查：
1. 机械层：编排器自己可判定的事实（guard 通过、阶段全绿、验收测试文件存在、
   测试未禁用、diff 无调试残留）；
2. 外部层：可选的独立评审后端，按 argv 契约读 `verification-request.json`，
   写 `result.json`（verdict/issues/evidence/confidence/residual_risks）。

不采信“看起来不对”：外部问题必须落到文件/行或给出证据，否则整份输出判失败。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from codingwiki.coding.backend import run_command_backend
from codingwiki.domain.coding import (
    BackendConfig,
    BackendResult,
    BackendStatus,
    CodedTask,
    StageReport,
    StageStatus,
    VerifierIssue,
    VerifierReport,
    VerifierSource,
    VerifierVerdict,
)
from codingwiki.domain.patch import FindingSeverity, PatchReport, PatchVerdict
from codingwiki.domain.spec import SpecDocument
from codingwiki.patch.protocol import WorktreeGit

_TEST_MARKER = re.compile(
    r"^\s*(?:void\s+test_|TEST(?:_F|_P)?\s*\(|def test_|it\s*\(|describe\s*\()", re.MULTILINE
)
_DISABLED_MARKER = re.compile(r"(?i)\bDISABLED_|\.skip\b|\bxfail\b")
_DEBUG_MARKER = re.compile(
    r"\b(std::cout|std::cerr|printf\s*\(|fprintf\s*\(|console\.log|print\s*\()"
)
_TEST_PATH_SUFFIXES = (
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hpp",
    ".py",
    ".sh",
    ".cmake",
    ".txt",
    ".md",
)


def mechanical_report(
    spec: SpecDocument,
    task: CodedTask,
    patch: PatchReport,
    stages: list[StageReport],
    worktree: Path,
    root: str,
    diff: str,
) -> VerifierReport:
    """只基于可机读事实给出结论；无问题也返回残余风险。"""
    issues: list[VerifierIssue] = []
    evidence: list[str] = []

    if patch.verdict is not PatchVerdict.PASS:
        issues.append(
            _issue(
                code="verify.guard",
                severity=FindingSeverity.BLOCKER,
                message=f"PatchGuard 判定 {patch.verdict.value}，补丁不可交付",
                evidence=f"guard findings={len(patch.findings)}",
            )
        )
    evidence.append(
        "patch: files="
        f"{patch.stats.files}, lines=+{patch.stats.added_lines}/-{patch.stats.deleted_lines}"
    )

    for stage in stages:
        if stage.status is not StageStatus.OK:
            issues.append(
                _issue(
                    code="verify.stage",
                    severity=FindingSeverity.BLOCKER,
                    message=f"{stage.kind.value} 未通过（exit {stage.exit_code}）",
                    evidence=f"{stage.kind.value}:{stage.status.value}",
                )
            )
    evidence.append(f"stages: {len(stages)} 项，全部 ok")

    base = worktree / root if root else worktree
    rows = [row for row in spec.trace if row.task == task.id]
    if not task.requirement_ids:
        issues.append(
            _issue(
                code="verify.no-requirements",
                severity=FindingSeverity.WARNING,
                message="任务没有绑定需求，无法判断验收覆盖",
                evidence="Spec 追溯矩阵未提供该任务的需求行",
            )
        )
    elif not rows:
        issues.append(
            _issue(
                code="verify.no-trace",
                severity=FindingSeverity.WARNING,
                message="任务绑定需求但追溯矩阵没有对应行",
                evidence=f"requirements={','.join(task.requirement_ids)}",
            )
        )
    else:
        seen: set[str] = set()
        for row in rows:
            if row.test in seen:
                continue
            seen.add(row.test)
            if not _looks_like_path(row.test):
                evidence.append(f"test target: {row.test}")
                continue
            test_path = base / row.test
            if not test_path.is_file():
                issues.append(
                    _issue(
                        code="verify.missing-test",
                        severity=FindingSeverity.BLOCKER,
                        message=f"验收测试文件不存在：{row.test}",
                        file=row.test,
                        evidence=f"requirements={row.requirement_id}",
                    )
                )
                continue
            text = test_path.read_text(encoding="utf-8", errors="replace")
            if not _TEST_MARKER.search(text):
                issues.append(
                    _issue(
                        code="verify.no-test-case",
                        severity=FindingSeverity.WARNING,
                        message=f"测试文件中未发现用例标记：{row.test}",
                        file=row.test,
                        evidence=f"requirements={row.requirement_id}",
                    )
                )
        evidence.append(f"trace: {len(rows)} 行，测试项 {len(seen)} 个")

    for path, line_text in _added_lines(diff):
        if _DISABLED_MARKER.search(line_text):
            issues.append(
                _issue(
                    code="verify.disabled-test",
                    severity=FindingSeverity.BLOCKER,
                    message=f"补丁禁用/跳过测试：{line_text.strip()[:120]}",
                    file=path,
                    evidence=line_text.strip()[:200],
                )
            )
        if _DEBUG_MARKER.search(line_text):
            issues.append(
                _issue(
                    code="verify.debug-output",
                    severity=FindingSeverity.WARNING,
                    message=f"补丁含调试输出：{line_text.strip()[:120]}",
                    file=path,
                    evidence=line_text.strip()[:200],
                )
            )

    residual = [
        "机械检查只验证可机读事实（guard/阶段/测试存在性与禁用标记），不判断需求语义是否真正实现",
        "追溯矩阵的测试目标名无法机械映射到具体测试用例/文件，需外部评审确认",
        "未重放补丁：复现性依赖 stages.json 与工作区现场",
    ]
    blockers = [item for item in issues if item.severity is FindingSeverity.BLOCKER]
    return VerifierReport(
        verdict=VerifierVerdict.FAIL if blockers else VerifierVerdict.PASS,
        issues=issues,
        evidence=evidence,
        residual_risks=residual,
        confidence=1.0,
        external=False,
        message=f"机械检查：{len(blockers)} 项 blocker / {len(issues)} 项问题",
    )


def verify_patch(
    provider: WorktreeGit,
    config: BackendConfig | None,
    spec: SpecDocument,
    task: CodedTask,
    patch: PatchReport,
    stages: list[StageReport],
    workspace: Path,
    root: str,
    packet_dir: Path,
    result_path: Path,
    log_path: Path,
    *,
    now: datetime | None = None,
) -> VerifierReport:
    """机械检查 +（可选）外部评审；产物写 packet_dir/verification-request.json。"""
    diff = provider.diff_unified(workspace, patch.base_revision)
    mechanical = mechanical_report(spec, task, patch, stages, workspace, root, diff)
    packet_dir.mkdir(parents=True, exist_ok=True)
    request = {
        "spec_id": spec.spec_id,
        "task": task.model_dump(mode="json"),
        "diff": diff,
        "patch": patch.model_dump(mode="json"),
        "stages": [item.model_dump(mode="json") for item in stages],
        "mechanical_issues": [item.model_dump(mode="json") for item in mechanical.issues],
        "requirements": [row.model_dump(mode="json") for row in spec.trace if row.task == task.id],
        "instructions": (
            "你是独立评审者，不是编码者。判断补丁是否真正实现需求，而不是只让测试通过。"
            "每条问题必须给出 file+line 或 evidence；verdict=fail 必须至少列一条 issue；"
            "verdict=pass 必须列出 residual_risks。"
            "输出 JSON：verdict(issues/evidence/confidence/residual_risks)。"
        ),
        "created_at": (now or datetime.now().astimezone()).isoformat(),
    }
    _write_json(packet_dir / "verification-request.json", request)

    if config is None:
        return mechanical

    result = run_command_backend(config, str(packet_dir), workspace, result_path, log_path, now=now)
    external = _external_report(result)
    return _combine(mechanical, external)


def _external_report(result: BackendResult) -> VerifierReport:
    """校验外部输出；任何不满足证据/理由要求的情况都判失败。"""
    if result.status is not BackendStatus.OK:
        return _invalid(
            "verify.backend",
            f"验证器后端未成功：{result.status.value}",
            evidence=result.error or result.status.value,
        )
    payload = result.result_json
    if not payload:
        return _invalid("verify.invalid-output", "验证器未写 result.json")

    try:
        verdict = VerifierVerdict(str(payload["verdict"]))
        raw_issues = payload.get("issues", [])
        issues = [
            VerifierIssue(
                source=VerifierSource.EXTERNAL,
                code=str(item.get("code", "")).strip() or "verify.issue",
                severity=FindingSeverity(str(item.get("severity", "blocker"))),
                message=str(item.get("message", "")).strip(),
                file=str(item.get("file", "")),
                line=int(item.get("line", 0) or 0),
                evidence=str(item.get("evidence", "")),
            )
            for item in raw_issues
        ]
        confidence = float(payload["confidence"])
        evidence = [str(item) for item in payload.get("evidence", [])]
        risks = [str(item) for item in payload.get("residual_risks", [])]
    except (KeyError, TypeError, ValueError) as exc:
        return _invalid("verify.invalid-output", f"验证器输出不合法：{exc}")

    unsupported = [item for item in issues if not item.has_evidence]
    if unsupported:
        return _invalid(
            "verify.invalid-evidence",
            f"{len(unsupported)} 项问题没有 file/line 或 evidence",
            evidence="；".join(item.message[:80] for item in unsupported),
        )
    if verdict is VerifierVerdict.FAIL and not issues:
        return _invalid("verify.no-reason", "verdict=fail 但未列出任何问题")
    if verdict is VerifierVerdict.PASS and not risks:
        return _invalid("verify.no-residual-risk", "verdict=pass 但未列出残余风险")
    if verdict is VerifierVerdict.PASS and any(
        item.severity is FindingSeverity.BLOCKER for item in issues
    ):
        return _invalid("verify.verdict-conflict", "verdict=pass 但存在 blocker 问题")

    return VerifierReport(
        verdict=verdict,
        issues=issues,
        evidence=evidence,
        residual_risks=risks,
        confidence=confidence,
        external=True,
        message=f"外部评审：{verdict.value}，问题 {len(issues)} 项",
    )


def _combine(mechanical: VerifierReport, external: VerifierReport) -> VerifierReport:
    issues = [*mechanical.issues, *external.issues]
    failed = mechanical.verdict is VerifierVerdict.FAIL or external.verdict is VerifierVerdict.FAIL
    return VerifierReport(
        verdict=VerifierVerdict.FAIL if failed else VerifierVerdict.PASS,
        issues=issues,
        evidence=[*mechanical.evidence, *external.evidence],
        residual_risks=[*mechanical.residual_risks, *external.residual_risks],
        confidence=external.confidence,
        external=True,
        message=f"机械 {len(mechanical.issues)} 项 + 外部 {len(external.issues)} 项",
    )


def _invalid(code: str, message: str, *, evidence: str = "") -> VerifierReport:
    issue = VerifierIssue(
        source=VerifierSource.EXTERNAL,
        code=code,
        severity=FindingSeverity.BLOCKER,
        message=message,
        evidence=evidence or message,
    )
    return VerifierReport(
        verdict=VerifierVerdict.FAIL,
        issues=[issue],
        evidence=[evidence or message],
        residual_risks=["验证器输出不可信，按失败处理"],
        confidence=0.0,
        external=True,
        message=message,
    )


def _issue(
    *,
    code: str,
    severity: FindingSeverity,
    message: str,
    file: str = "",
    line: int = 0,
    evidence: str = "",
) -> VerifierIssue:
    return VerifierIssue(
        source=VerifierSource.MECHANICAL,
        code=code,
        severity=severity,
        message=message,
        file=file,
        line=line,
        evidence=evidence,
    )


def _looks_like_path(value: str) -> bool:
    """追溯矩阵的 test 列可能是文件路径，也可能是 CTest 目标名。"""
    return "/" in value or value.endswith(_TEST_PATH_SUFFIXES)


def _added_lines(diff: str) -> Iterator[tuple[str, str]]:
    path = ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            yield path, line[1:]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


__all__ = ["mechanical_report", "verify_patch"]
