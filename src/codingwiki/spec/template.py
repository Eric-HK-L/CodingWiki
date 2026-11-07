"""Spec 人读稿渲染（架构文档 阶段2 §6.3）。

spec.md 与 spec.json 同源：先有 SpecDocument，再由这里确定性渲染；
不调用模型、不读时间。
"""

from __future__ import annotations

from codingwiki.domain.spec import SpecDocument


def render_spec_markdown(spec: SpecDocument) -> str:
    lines = [
        f"# {spec.spec_id} {spec.title}",
        "",
        f"> 快照：`{spec.snapshot_id}`  ",
        f"> 状态：`{spec.status.value}`  ",
        f"> 来源：`{spec.source_doc.path}`（doc_id `{spec.source_doc.doc_id}`）",
        "",
    ]
    for section in spec.sections:
        lines.extend([f"## {section.number} {section.title}", "", section.body.strip(), ""])
    if spec.decisions:
        lines.extend(["## 附录：方案决策", ""])
        for decision in spec.decisions:
            lines.append(f"- **{decision.id} {decision.title}**：选择「{decision.choice}」")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_decisions_markdown(spec: SpecDocument) -> str:
    lines = [f"# 方案决策记录（{spec.spec_id}）", ""]
    if not spec.decisions:
        lines.extend(["不适用：本次 Spec 没有需要取舍的方案。", ""])
        return "\n".join(lines)
    for decision in spec.decisions:
        lines.extend(
            [
                f"## {decision.id} {decision.title}",
                "",
                f"- 状态：已选择 {decision.choice}",
                f"- 背景：{decision.context}",
                "",
                "### 候选方案",
                "",
            ]
        )
        for index, candidate in enumerate(decision.candidates, start=1):
            lines.append(f"{index}. **{candidate.name}**：{candidate.summary}")
            if candidate.pros:
                lines.append(f"   - 优点：{candidate.pros}")
            if candidate.cons:
                lines.append(f"   - 缺点：{candidate.cons}")
        lines.extend(["", "### 决策", "", f"选择 **{decision.choice}**。{decision.rationale}"])
        if decision.unique_justification:
            lines.extend(["", f"唯一方案依据：{decision.unique_justification}"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = ["render_decisions_markdown", "render_spec_markdown"]
