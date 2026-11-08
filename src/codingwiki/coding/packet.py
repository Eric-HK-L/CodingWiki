"""聚焦上下文任务包（开发计划 14.5.1 / 11.3）。

packet/ 内产物：task.json、prompt.md、spec-excerpt.md、schema.json。
模型只读工作树里的文件；包内只放路径与指纹，不复制源码。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from codingwiki.domain.coding import CodedTask, ContextFile, TaskPacket
from codingwiki.domain.patch import PatchPolicy
from codingwiki.domain.spec import SpecDocument
from codingwiki.errors import InputError

_EXCERPT_SECTIONS = (4, 5, 6, 7, 8)

_RESULT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ok", "failed"]},
        "summary": {"type": "string"},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status"],
}


def build_packet(
    *,
    repo_id: str,
    spec: SpecDocument,
    task: CodedTask,
    workspace: Path,
    base_revision: str,
    packet_dir: Path,
    policy: PatchPolicy,
    root: str = "",
    now: datetime | None = None,
) -> TaskPacket:
    """生成任务包并写盘；允许文件缺失即拒绝。

    root 为 RepoConfig.root：允许文件路径相对代码根，而工作树是仓库根。
    """
    packet_dir.mkdir(parents=True, exist_ok=True)
    source = workspace / root if root else workspace
    context = [_context_file(source, path) for path in task.files_allowed]

    excerpt = packet_dir / "spec-excerpt.md"
    excerpt.write_text(_spec_excerpt(spec), encoding="utf-8")
    context.append(_fingerprint(excerpt, packet_dir))

    prompt = packet_dir / "prompt.md"
    prompt.write_text(_prompt(task, spec, workspace, source, packet_dir), encoding="utf-8")
    schema = packet_dir / "schema.json"
    schema.write_text(
        json.dumps(_RESULT_SCHEMA, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d%H%M%S")
    packet = TaskPacket(
        packet_id=f"PKT-{task.id}-{stamp}",
        repo_id=repo_id,
        spec_id=spec.spec_id,
        task_id=task.id,
        base_revision=base_revision,
        task=task,
        context_files=context,
        prompt_path=str(prompt.resolve()),
        schema_path=str(schema.resolve()),
        packet_dir=str(packet_dir.resolve()),
        policy=policy,
        created_at=now or datetime.now(UTC),
    )
    (packet_dir / "task.json").write_text(packet.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return packet


def _context_file(workspace: Path, relative: str) -> ContextFile:
    path = workspace / relative
    if not path.is_file():
        raise InputError(f"允许文件不在工作区：{relative}（{path}）")
    data = path.read_bytes()
    return ContextFile(
        path=relative,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
    )


def _fingerprint(path: Path, relative_to: Path) -> ContextFile:
    data = path.read_bytes()
    return ContextFile(
        path=str(path.relative_to(relative_to)),
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
    )


def _spec_excerpt(spec: SpecDocument) -> str:
    lines = [f"# Spec {spec.spec_id}：{spec.title}", ""]
    for section in spec.sections:
        if section.number in _EXCERPT_SECTIONS:
            lines += [f"## {section.number} {section.title}", "", section.body, ""]
    return "\n".join(lines).rstrip() + "\n"


def _prompt(
    task: CodedTask, spec: SpecDocument, workspace: Path, source: Path, packet_dir: Path
) -> str:
    parts = [
        f"# 编码任务 {task.id}：{task.title}",
        "",
        f"- Spec：{spec.spec_id}",
        f"- 工作树：{workspace.resolve()}",
        f"- 代码根：{source.resolve()}（允许文件路径相对该目录）",
        f"- 任务包：{packet_dir.resolve()}",
        "",
        "## 目标",
        "",
        task.goal,
        "",
        "## 允许修改的文件",
        "",
        *[f"- `{path}`" for path in task.files_allowed],
        "",
        "## 验收",
        "",
        *[f"- {item}" for item in task.acceptance],
        "",
        "## 约束",
        "",
        *[f"- {item}" for item in task.constraints],
        "",
        "## Spec 摘录",
        "",
        "需求、方案、影响分析、测试计划、任务拆分见同目录 `spec-excerpt.md`。",
        "",
        "## 输出约定",
        "",
        "- 直接修改工作树中的文件；不要新建允许清单外的文件。",
        "- 不要执行构建、测试或网络命令；这些由编排器执行。",
        "- 可写 `result.json`（结构见 `schema.json`）：`status` 为 ok/failed，附简短 summary；"
        "不写也不影响退出码与 diff 判定。",
        "",
    ]
    return "\n".join(parts)


__all__ = ["build_packet"]
