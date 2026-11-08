"""成功运行的交付包（开发计划 14.5.7）。

成功运行后只生成材料：patch.diff、delivery.json、delivery.md、pr.md。
不自动 commit、push 或创建合并请求；pr.md 给出可直接执行的命令，
由人确认后执行（L2 人工审核合并）。

失败运行不生成交付包：失败材料由升级单（escalation）承载。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from codingwiki.coding.metrics import metrics_of, render_metrics
from codingwiki.domain.coding import (
    CodedTask,
    CodingRun,
    CodingRunStatus,
    DeliveryBundle,
    RunMetrics,
)
from codingwiki.domain.patch import FindingSeverity, WorkspaceRecord
from codingwiki.domain.spec import SpecDocument
from codingwiki.errors import InputError
from codingwiki.patch.protocol import WorktreeGit

_CHECKLIST = (
    "diff 与任务目标/需求一致，未夹带无关改动",
    "改动文件都在允许清单内",
    "验收标准逐条有对应测试",
    "无密钥、调试残留与生成文件",
    "合并前跑一次完整回归",
)


def build_delivery(
    provider: WorktreeGit,
    run: CodingRun,
    spec: SpecDocument,
    task: CodedTask,
    record: WorkspaceRecord,
    run_dir: Path,
    *,
    now: datetime | None = None,
) -> DeliveryBundle:
    """生成交付四件套；成功运行属前置条件。"""
    if run.status is not CodingRunStatus.SUCCEEDED:
        raise InputError(f"运行 {run.run_id} 未成功，不生成交付包")
    delivery_dir = run_dir / "delivery"
    delivery_dir.mkdir(parents=True, exist_ok=True)
    patch_path = delivery_dir / "patch.diff"
    patch_path.write_text(
        provider.diff_unified(run.workspace, record.base_revision), encoding="utf-8"
    )

    bundle = DeliveryBundle(
        run_id=run.run_id,
        repo_id=run.repo_id,
        spec_id=run.spec_id,
        task_id=run.task_id,
        branch=record.branch,
        base_revision=record.base_revision,
        status=run.status,
        stop_reason=run.stop_reason,
        title=f"[{task.id}] {task.title}",
        summary=run.message or f"{run.rounds} 轮通过",
        files=_files(run, task),
        metrics=metrics_of(run),
        patch_path=str(patch_path),
        created_at=now or datetime.now(UTC),
    )
    (delivery_dir / "delivery.json").write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
    (delivery_dir / "delivery.md").write_text(
        render_delivery(bundle, run, spec, task), encoding="utf-8"
    )
    (delivery_dir / "pr.md").write_text(
        render_pull_request(bundle, run, spec, task), encoding="utf-8"
    )
    return bundle


def render_delivery(
    bundle: DeliveryBundle,
    run: CodingRun,
    spec: SpecDocument,
    task: CodedTask,
) -> str:
    """渲染人可读的 delivery.md（含发布建议，但命令未执行）。"""
    pr_path = Path(bundle.patch_path).parent / "pr.md"
    lines = [
        f"# 交付包：{spec.spec_id} / {task.id}",
        "",
        f"- 运行：{bundle.run_id}",
        f"- 仓库：{bundle.repo_id}",
        f"- 分支：{bundle.branch}（基线 {bundle.base_revision[:12]}）",
        f"- 状态：{bundle.status.value}（{bundle.stop_reason.value}）",
        f"- 轮次：{run.rounds}/{run.max_rounds}",
        f"- 摘要：{bundle.summary}",
        f"- 补丁：{bundle.patch_path}",
        "",
        "## 目标",
        task.goal,
        "",
        "## 需求",
        *_requirement_lines(spec, task),
        "",
        "## 验收",
        *_bullets(task.acceptance),
        "",
        "## 改动文件",
        *_file_lines(run),
        "",
        "## 测试与指标",
        *_test_lines(bundle.metrics),
        *_flaky_lines(run),
        "",
        "## 风险",
        *_risk_lines(run),
        "",
        "## 评审清单",
        *_checklist_lines(),
        "",
        "## 发布建议（未自动执行）",
        "```bash",
        f"git push origin {bundle.branch}",
        f'gh pr create --head {bundle.branch} --title "{bundle.title}" --body-file "{pr_path}"',
        "```",
    ]
    return "\n".join(lines) + "\n"


def render_pull_request(
    bundle: DeliveryBundle,
    run: CodingRun,
    spec: SpecDocument,
    task: CodedTask,
) -> str:
    """渲染可直接作为合并请求正文的 pr.md。"""
    lines = [
        f"# {bundle.title}",
        "",
        f"- 运行：{bundle.run_id}",
        f"- 分支：{bundle.branch}（基线 {bundle.base_revision[:12]}）",
        f"- Spec：{spec.spec_id} / 任务 {task.id}",
        "",
        "## 概要",
        bundle.summary,
        "",
        "## 目标与需求",
        task.goal,
        "",
        *_requirement_lines(spec, task),
        "",
        "## 改动",
        *_file_lines(run),
        "",
        "## 验收",
        *_bullets(task.acceptance),
        "",
        "## 测试与指标",
        *_test_lines(bundle.metrics),
        *_flaky_lines(run),
        "",
        "## 风险",
        *_risk_lines(run),
        "",
        "## 评审清单",
        *_checklist_lines(),
    ]
    return "\n".join(lines) + "\n"


def _files(run: CodingRun, task: CodedTask) -> list[str]:
    if run.guard is not None and run.guard.files:
        return [item.path for item in run.guard.files]
    return list(task.files_allowed)


def _requirement_lines(spec: SpecDocument, task: CodedTask) -> list[str]:
    if not task.requirement_ids:
        return ["- （Spec 未提供追溯行）"]
    requirements = {item.id: item for item in spec.requirements}
    lines: list[str] = []
    for requirement_id in task.requirement_ids:
        requirement = requirements.get(requirement_id)
        text = requirement.text if requirement is not None else "Spec 未找到需求正文"
        lines.append(f"- {requirement_id}：{text}")
    return lines


def _file_lines(run: CodingRun) -> list[str]:
    if run.guard is None or not run.guard.files:
        return ["- （guard 未报告改动文件）"]
    return [
        f"- {item.path}（+{item.added_lines} / -{item.deleted_lines}）" for item in run.guard.files
    ]


def _test_lines(metrics: RunMetrics) -> list[str]:
    lines = [f"- 指标：{render_metrics(metrics)}"]
    if metrics.tests_parsed:
        lines.append(
            f"- 测试：新增失败={metrics.new_failures} 原有失败={metrics.pre_existing_failures} "
            f"跳过={metrics.skipped_tests} 不稳定={metrics.flaky_tests} "
            f"隔离={metrics.quarantined_tests}"
        )
    else:
        lines.append("- 测试报告：未解析（按命令退出码判定）")
    return lines


def _flaky_lines(run: CodingRun) -> list[str]:
    lines: list[str] = []
    if run.quarantined_tests:
        lines.append("- 基线隔离：" + ", ".join(run.quarantined_tests))
    flaky = sorted({ident for stage in [*run.baseline, *run.stages] for ident in stage.flaky_tests})
    if flaky:
        lines.append("- 不稳定用例：" + ", ".join(flaky))
    return lines


def _risk_lines(run: CodingRun) -> list[str]:
    lines: list[str] = []
    if run.verifier is not None:
        lines.extend(f"- {item}" for item in run.verifier.residual_risks)
    if run.guard is not None:
        lines.extend(
            f"- [guard/{item.code}] {item.message}"
            for item in run.guard.findings
            if item.severity is FindingSeverity.WARNING
        )
    lines.append("- 阶段通过只证明命令成功；测试未覆盖的边界仍需人工复核")
    return lines


def _checklist_lines() -> list[str]:
    return [f"- [ ] {item}" for item in _CHECKLIST]


def _bullets(items: list[str]) -> list[str]:
    if not items:
        return ["- （无）"]
    return [f"- {item}" for item in items]


__all__ = ["build_delivery", "render_delivery", "render_pull_request"]
