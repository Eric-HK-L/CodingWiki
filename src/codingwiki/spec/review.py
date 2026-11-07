"""评审状态机（架构 阶段2 §8）。

`review_spec` 为纯函数：输入 Spec、现场校验报告、既有评审记录与评审参数，
输出迁移后的 Spec 与追加后的评审记录；不读写文件。
`load_review_log` / `save_review_log` / `apply_review` 负责 review.json 与 spec 落盘。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from codingwiki.domain.spec import (
    QuestionStatus,
    ReviewDecision,
    ReviewLog,
    ReviewRecord,
    ReviewRole,
    SpecDocument,
    SpecStatus,
    ValidationReport,
)
from codingwiki.errors import CodingWikiError, ExitCode, InputError
from codingwiki.spec.template import render_spec_markdown

REVIEW_FILE = "review.json"


class ReviewGateError(CodingWikiError):
    """评审门禁失败：blockers、未关问题、未确认 warning、非法迁移。"""

    exit_code = ExitCode.ENVIRONMENT


def load_review_log(directory: Path) -> ReviewLog | None:
    path = directory / REVIEW_FILE
    if not path.is_file():
        return None
    try:
        return ReviewLog.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(f"评审记录不可读: {path}: {exc}") from exc


def save_review_log(log: ReviewLog, directory: Path) -> Path:
    path = directory / REVIEW_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(log.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def _write_model(path: Path, payload: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _close_questions(spec: SpecDocument, question_ids: Sequence[str]) -> SpecDocument:
    """显式关闭开放问题；未知编号按输入错误拒绝，避免静默忽略。"""
    known = {question.id: question for question in spec.questions}
    for question_id in question_ids:
        if question_id not in known:
            raise InputError(f"Spec {spec.spec_id} 没有开放问题 {question_id}")
    questions = [
        question.model_copy(update={"status": QuestionStatus.CLOSED})
        if question.id in set(question_ids)
        else question
        for question in spec.questions
    ]
    return spec.model_copy(update={"questions": questions})


def review_spec(
    spec: SpecDocument,
    report: ValidationReport,
    log: ReviewLog | None,
    *,
    role: ReviewRole,
    decision: ReviewDecision,
    comment: str = "",
    close_questions: Sequence[str] = (),
    ack_warnings: bool = False,
    snapshot_id: str = "",
    now: datetime | None = None,
) -> tuple[SpecDocument, ReviewLog]:
    """执行一次评审并返回 (迁移后的 Spec, 追加后的评审记录)。"""
    if snapshot_id and snapshot_id != spec.snapshot_id:
        raise ReviewGateError(
            f"评审快照与 Spec 不一致：{snapshot_id} != {spec.snapshot_id}；"
            "请先重新校验/生成，不评审过期 Spec"
        )
    if log is not None and (log.spec_id != spec.spec_id or log.snapshot_id != spec.snapshot_id):
        raise ReviewGateError(
            f"既有评审记录与 Spec 不一致：{log.spec_id}/{log.snapshot_id} != "
            f"{spec.spec_id}/{spec.snapshot_id}"
        )

    if decision is ReviewDecision.REJECT:
        if not comment.strip():
            raise ReviewGateError("reject 必须填写 --comment 说明退回原因")
        if close_questions:
            raise ReviewGateError("reject 不接受 --close-question；先关闭问题再决定")
        if spec.status is SpecStatus.REJECTED:
            raise ReviewGateError(f"Spec {spec.spec_id} 已是 rejected，不能重复退回")
        target = SpecStatus.REJECTED
        updated = spec
    else:
        if spec.status is SpecStatus.REJECTED:
            raise ReviewGateError(f"Spec {spec.spec_id} 已 rejected，不能直接批准")
        if spec.status is SpecStatus.APPROVED:
            raise ReviewGateError(f"Spec {spec.spec_id} 已 approved，不能重复批准")
        updated = _close_questions(spec, close_questions) if close_questions else spec
        open_questions = [
            question.id for question in updated.questions if question.status is QuestionStatus.OPEN
        ]
        if open_questions:
            raise ReviewGateError(
                f"仍有未关闭的开放问题，不能 approved: {', '.join(open_questions)}；"
                "确认后用 --close-question 关闭"
            )
        if report.blockers:
            rules = ", ".join(
                f"{issue.rule}[{issue.ref}]" if issue.ref else issue.rule
                for issue in report.blockers
            )
            raise ReviewGateError(
                f"校验仍有 {len(report.blockers)} 个 blocker，不能 approved: {rules}"
            )
        if updated.validation is None or updated.validation.blockers:
            raise ReviewGateError("Spec 缺少 0 blocker 的校验报告，请先执行 spec validate")
        if report.warnings and not ack_warnings:
            raise ReviewGateError(
                f"有 {len(report.warnings)} 个 warning 待确认；"
                "逐条确认后加 --ack-warnings（确认会写入评审记录）"
            )
        target = SpecStatus.APPROVED

    record = ReviewRecord(
        spec_id=spec.spec_id,
        role=role,
        decision=decision,
        comment=comment,
        created_at=now or datetime.now(UTC),
        snapshot_id=spec.snapshot_id,
        blockers=len(report.blockers),
        warnings=len(report.warnings),
        warnings_acknowledged=ack_warnings,
    )
    updated_log = ReviewLog(
        spec_id=spec.spec_id,
        snapshot_id=spec.snapshot_id,
        reviews=[*(log.reviews if log else []), record],
    )
    return updated.model_copy(update={"status": target}), updated_log


def apply_review(
    spec: SpecDocument,
    report: ValidationReport,
    directory: Path,
    *,
    role: ReviewRole,
    decision: ReviewDecision,
    comment: str = "",
    close_questions: Sequence[str] = (),
    ack_warnings: bool = False,
    snapshot_id: str = "",
    now: datetime | None = None,
) -> tuple[SpecDocument, ReviewLog]:
    """评审并同源写回 spec.json / spec.md / review.json；validation.json 不动。"""
    log = load_review_log(directory)
    updated_spec, updated_log = review_spec(
        spec,
        report,
        log,
        role=role,
        decision=decision,
        comment=comment,
        close_questions=close_questions,
        ack_warnings=ack_warnings,
        snapshot_id=snapshot_id,
        now=now,
    )
    _write_model(directory / "spec.json", updated_spec)
    (directory / "spec.md").write_text(render_spec_markdown(updated_spec), encoding="utf-8")
    save_review_log(updated_log, directory)
    return updated_spec, updated_log


__all__ = [
    "REVIEW_FILE",
    "ReviewGateError",
    "apply_review",
    "load_review_log",
    "review_spec",
    "save_review_log",
]
