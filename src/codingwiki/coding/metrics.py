"""运行指标与统计（开发计划 14.11）。

只统计编排器已经计量的部分：命令耗时、轮次、失败项数量、后端自报词元。
不推断成本，也不把未计量阶段估算成耗时。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from codingwiki.domain.coding import (
    CodingRun,
    CodingRunStatus,
    CodingStats,
    CodingStopReason,
    RunMetrics,
    RunReport,
    StageStatus,
)


def metrics_of(run: CodingRun) -> RunMetrics:
    backend_ms = sum(item.backend.duration_ms for item in run.round_reports)
    stage_ms = sum(item.duration_ms for item in run.baseline)
    stage_ms += sum(stage.duration_ms for item in run.round_reports for stage in item.stages)
    tokens_in, tokens_out = _tokens(run)
    baseline_ok = all(item.status is StageStatus.OK for item in run.baseline)
    return RunMetrics(
        succeeded=run.status is CodingRunStatus.SUCCEEDED,
        first_pass=run.status is CodingRunStatus.SUCCEEDED and run.rounds == 1,
        rounds=run.rounds,
        max_rounds=run.max_rounds,
        backend_duration_ms=backend_ms,
        stage_duration_ms=stage_ms,
        total_duration_ms=backend_ms + stage_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        baseline_failures=sum(1 for item in run.baseline if item.status is not StageStatus.OK),
        final_failures=len(run.errors),
        guard_rejected=run.stop_reason is CodingStopReason.GUARD_REJECTED,
        verifier_rejected=run.stop_reason is CodingStopReason.VERIFY_REJECTED,
        regression=baseline_ok
        and run.stop_reason in {CodingStopReason.ROUNDS_EXHAUSTED, CodingStopReason.NO_PROGRESS},
        escalated=run.status is CodingRunStatus.FAILED,
    )


def run_report(run: CodingRun) -> RunReport:
    return RunReport(
        run_id=run.run_id,
        repo_id=run.repo_id,
        spec_id=run.spec_id,
        task_id=run.task_id,
        status=run.status,
        stop_reason=run.stop_reason,
        metrics=metrics_of(run),
        created_at=run.created_at,
    )


def aggregate(reports: list[RunReport]) -> CodingStats:
    metrics = [item.metrics for item in reports]
    return CodingStats(
        runs=len(reports),
        succeeded=sum(1 for item in metrics if item.succeeded),
        first_pass=sum(1 for item in metrics if item.first_pass),
        guard_rejected=sum(1 for item in metrics if item.guard_rejected),
        verifier_rejected=sum(1 for item in metrics if item.verifier_rejected),
        regression=sum(1 for item in metrics if item.regression),
        escalated=sum(1 for item in metrics if item.escalated),
        total_rounds=sum(item.rounds for item in metrics),
        total_duration_ms=sum(item.total_duration_ms for item in metrics),
        tokens_in=sum(item.tokens_in for item in metrics),
        tokens_out=sum(item.tokens_out for item in metrics),
    )


def load_reports(home: Path, repo_id: str, spec_id: str, task_id: str = "") -> list[RunReport]:
    """读取 runs 目录下的 report.json；旧运行没有报告时跳过。"""
    base = home / "coding" / repo_id / spec_id
    if not base.is_dir():
        return []
    tasks = [task_id] if task_id else sorted(item.name for item in base.iterdir() if item.is_dir())
    reports: list[RunReport] = []
    for task in tasks:
        runs = base / task / "runs"
        if not runs.is_dir():
            continue
        for path in sorted(runs.glob("*/report.json")):
            try:
                reports.append(RunReport.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return reports


def render_metrics(metrics: RunMetrics) -> str:
    rate = metrics.tokens_in + metrics.tokens_out
    return (
        f"耗时={metrics.total_duration_ms / 1000:.1f}s "
        f"轮次={metrics.rounds}/{metrics.max_rounds} "
        f"tokens={metrics.tokens_in}/{metrics.tokens_out}"
        f"{'' if rate else '（未上报）'} "
        f"回归={'是' if metrics.regression else '否'}"
    )


def render_stats(stats: CodingStats) -> str:
    return (
        f"runs={stats.runs} 成功={stats.succeeded}（{stats.success_rate:.0%}） "
        f"一次通过={stats.first_pass}（{stats.first_pass_rate:.0%}） "
        f"平均轮次={stats.avg_rounds:.2f} "
        f"总耗时={stats.total_duration_ms / 1000:.1f}s "
        f"tokens={stats.tokens_in}/{stats.tokens_out} "
        f"guard拒绝={stats.guard_rejected} 验证拒绝={stats.verifier_rejected} "
        f"回归={stats.regression} 升级={stats.escalated}"
    )


def _tokens(run: CodingRun) -> tuple[int, int]:
    total_in = 0
    total_out = 0
    for item in run.round_reports:
        payload = item.backend.result_json
        total_in += _as_int(payload.get("tokens_in"))
        total_out += _as_int(payload.get("tokens_out"))
        usage = payload.get("usage")
        if isinstance(usage, dict):
            total_in += _as_int(usage.get("prompt_tokens"))
            total_out += _as_int(usage.get("completion_tokens"))
    return total_in, total_out


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


__all__ = [
    "aggregate",
    "load_reports",
    "metrics_of",
    "render_metrics",
    "render_stats",
    "run_report",
]
