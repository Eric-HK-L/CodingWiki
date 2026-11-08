"""Spec → CodedTask 任务分解（开发计划 14.4）。"""

from __future__ import annotations

from codingwiki.domain.coding import CodedTask
from codingwiki.domain.spec import SpecDocument, TraceRow

_LIMIT_CONSTRAINTS = (
    "改动上限：3 个文件、300 行；不得把多个目标塞进同一任务",
    "不得修改构建脚本、CI 配置、密钥/证书、自动生成文件、第三方目录、依赖清单或二进制",
    "不得删除测试、跳过测试或修改既有断言",
    "不得引入新依赖、网络调用或执行构建/测试命令（命令由编排器执行）",
)


def decompose(spec: SpecDocument) -> list[CodedTask]:
    """把 approved Spec 的 DesignTask 全部转成 CodedTask；顺序保持 Spec 顺序。"""
    rows = {row.task: row for row in spec.trace}
    return [_coded_task(spec, item.id, rows) for item in spec.tasks]


def _coded_task(spec: SpecDocument, task_id: str, rows: dict[str, TraceRow]) -> CodedTask:
    source = next(item for item in spec.tasks if item.id == task_id)
    requirement_ids = sorted({row.requirement_id for row in spec.trace if row.task == task_id})
    row: TraceRow | None = rows.get(task_id)
    constraints = [
        f"只允许修改：{', '.join(source.files)}",
        "不得新增允许清单外的文件；同模块测试文件仅可新增测试",
        *_LIMIT_CONSTRAINTS,
    ]
    if requirement_ids:
        constraints.insert(0, f"目标需求：{', '.join(requirement_ids)}")
    else:
        constraints.insert(0, "Spec 未提供追溯行，需求归属需人工确认")
    if row is not None:
        constraints.append(f"Spec 追溯：设计 {row.design_section}；测试 {row.test}")
    return CodedTask(
        id=source.id,
        spec_id=spec.spec_id,
        title=_short_title(source.goal),
        goal=source.goal,
        requirement_ids=requirement_ids,
        files_allowed=list(source.files),
        acceptance=list(source.acceptance),
        depends_on=list(source.depends_on),
        constraints=constraints,
        estimated_lines=source.estimated_lines,
    )


def _short_title(goal: str) -> str:
    first = goal.strip().splitlines()[0].strip()
    return first if len(first) <= 80 else first[:77] + "..."


__all__ = ["decompose"]
