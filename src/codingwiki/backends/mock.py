"""MockBackend：确定性模型后端（计划文档 11.3，架构文档 v0.4 13.4）。

读任务包（meta.json + context/facts.json），纯模板化组装页面与 result.json：
结论全部来自事实字段，每条结论挂对应 E 编号。代表"生成质量地板"，
开发与 CI 不依赖真实模型。

故障注入用于测试修复循环：failures 中某项命中当前 attempt 时输出坏结果。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import yaml

from codingwiki.backends.protocol import BackendResult, BackendStatus, TaskPacket
from codingwiki.domain.facts import Confidence
from codingwiki.domain.front_matter import dump_front_matter
from codingwiki.domain.wiki import (
    Claim,
    PageMeta,
    PageResult,
    PageStatus,
    PageType,
    TaskFacts,
    TaskMeta,
)

BACKEND_NAME = "mock"
MODEL_NAME = "mock-v1"

# 故障注入种类
FAIL_BAD_EVIDENCE_REF = "bad_evidence_ref"  # claim 引用不存在的 E 编号
FAIL_MISSING_TITLE = "missing_title"  # front matter 缺 title
FAIL_FUZZY_WORD = "fuzzy_word"  # 正文混入无依据模糊词
FAIL_BAD_JSON = "bad_json"  # result.json 写成非法 JSON


class MockBackend:
    """确定性后端。failures: [{"attempt": 1, "kind": FAIL_*}]，命中即注入。"""

    def __init__(self, failures: list[dict[str, Any]] | None = None) -> None:
        self._failures = {int(f["attempt"]): str(f["kind"]) for f in (failures or [])}

    def run(self, packet: TaskPacket) -> BackendResult:
        started = time.monotonic()
        try:
            meta = TaskMeta.model_validate_json(
                (packet.workspace / "meta.json").read_text(encoding="utf-8")
            )
            facts_path = packet.workspace / "context" / "facts.json"
            facts = (
                TaskFacts.model_validate_json(facts_path.read_text(encoding="utf-8"))
                if facts_path.is_file()
                else TaskFacts()
            )
        except (OSError, ValueError) as exc:
            return BackendResult(
                status=BackendStatus.FAILED,
                duration_ms=_elapsed_ms(started),
                model=MODEL_NAME,
                error=f"任务包不可读: {exc}",
            )

        packet.output_dir.mkdir(parents=True, exist_ok=True)
        failure = self._failures.get(meta.attempt)
        try:
            if failure == FAIL_BAD_JSON:
                page_text = self._render_page(meta, facts, failure=None)
                (packet.output_dir / "page.md").write_text(page_text, encoding="utf-8")
                (packet.output_dir / "result.json").write_text("{ 不是合法 JSON", encoding="utf-8")
            else:
                page_text = self._render_page(meta, facts, failure=failure)
                result = self._build_result(meta, failure=failure)
                (packet.output_dir / "page.md").write_text(page_text, encoding="utf-8")
                (packet.output_dir / "result.json").write_text(
                    result.model_dump_json(indent=2), encoding="utf-8"
                )
        except ValueError as exc:
            return BackendResult(
                status=BackendStatus.FAILED,
                duration_ms=_elapsed_ms(started),
                model=MODEL_NAME,
                error=f"生成失败: {exc}",
            )

        outputs = sorted(
            str(p.relative_to(packet.workspace)) for p in packet.output_dir.iterdir() if p.is_file()
        )
        return BackendResult(
            status=BackendStatus.SUCCEEDED,
            exit_code=0,
            output_files=outputs,
            duration_ms=_elapsed_ms(started),
            tokens_in=_rough_tokens(packet.workspace),
            tokens_out=_rough_tokens(packet.output_dir),
            model=MODEL_NAME,
        )

    # ---------------------------------------------------------------- 页面组装

    def _render_page(self, meta: TaskMeta, facts: TaskFacts, failure: str | None) -> str:
        body = _BODY_RENDERERS[meta.page_type](meta, facts)
        if failure == FAIL_FUZZY_WORD:
            body += "\n\n这个模块大概还负责其他相关处理。\n"
        evidence_block = _evidence_block(meta)
        page_meta = PageMeta(
            id=meta.page_id,
            type=meta.page_type,
            title=(
                "占位标题" if failure == FAIL_MISSING_TITLE else (meta.title_hint or meta.page_id)
            ),
            snapshot=meta.snapshot_id,
            module=meta.module,
            status=PageStatus.DRAFT,
            coverage_level=meta.coverage_level,
            evidence_count=len(meta.evidence),
            generated_by=f"{BACKEND_NAME}/{MODEL_NAME}",
            prompt_version=meta.prompt_version,
            last_verified_at=None,
            tags=[meta.page_type.value],
        )
        if failure == FAIL_MISSING_TITLE:
            # 缺 title：绕开模型校验，手工拼 front matter
            data = page_meta.model_dump(mode="json", exclude_none=True)
            data.pop("title", None)
            header = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
            return f"---\n{header}---\n\n{body}\n{evidence_block}"
        return f"{dump_front_matter(page_meta)}\n{body}\n{evidence_block}"

    def _build_result(self, meta: TaskMeta, failure: str | None) -> PageResult:
        claims: list[Claim] = []
        if meta.evidence:
            for idx, ev in enumerate(meta.evidence, start=1):
                claims.append(
                    Claim(
                        id=f"C{idx}",
                        text=_claim_text_for(meta, idx, evidence_id=ev.id),
                        evidence=[ev.id],
                        confidence=Confidence.HIGH,
                    )
                )
            if failure == FAIL_BAD_EVIDENCE_REF:
                claims.append(
                    Claim(
                        id=f"C{len(meta.evidence) + 1}",
                        text="这条结论引用了不存在的证据。",
                        evidence=["E999"],
                        confidence=Confidence.LOW,
                    )
                )
        return PageResult(
            page_id=meta.page_id,
            snapshot=meta.snapshot_id,
            claims=claims,
            evidence=list(meta.evidence),
        )


# ---------------------------------------------------------------- 正文模板


def _claim_text_for(meta: TaskMeta, idx: int, evidence_id: str) -> str:
    ev = meta.evidence[idx - 1]
    if ev.symbol:
        return f"{meta.page_id} 涉及符号 `{ev.symbol}`（见 {ev.file}）。[{evidence_id}]"
    return f"{meta.page_id} 的事实来源：{ev.file}。[{evidence_id}]"


def _evidence_block(meta: TaskMeta) -> str:
    if not meta.evidence:
        return ""
    lines = ["## 出处", ""]
    for ev in meta.evidence:
        symbol = f" · `{ev.symbol}`" if ev.symbol else ""
        lines.append(f"[{ev.id}]: {ev.file}{symbol} · 快照 {meta.snapshot_id}")
    return "\n".join(lines) + "\n"


def _render_module(meta: TaskMeta, facts: TaskFacts) -> str:
    lines = [f"# {meta.title_hint or meta.module}", ""]
    lines += ["## 一句话职责", ""]
    lines.append(
        f"模块 `{meta.module}` 包含 {len(facts.symbols)} 个符号、"
        f"{len(facts.configs)} 个配置项、{len(facts.tests)} 个测试用例；"
        "详细职责语义需人工评审确认。"
    )
    lines.append("")
    if facts.symbols:
        lines += ["## 对外接口", ""]
        for sym in facts.symbols:
            ref = _ev_ref(meta, symbol=sym.name)
            sig = f"：`{sym.signature}`" if sym.signature else ""
            lines.append(
                f"- `{sym.qualified_name}`（{sym.kind.value}）{sig}，"
                f"位于 {sym.path}:{sym.line}。{ref}"
            )
        lines.append("")
    if facts.configs:
        lines += ["## 配置项", "", "| 配置 | 读取位置 |", "|---|---|"]
        for cfg in facts.configs:
            ref = _ev_ref(meta, file=cfg.path, line=cfg.line)
            lines.append(f"| `{cfg.key}` | {cfg.path}:{cfg.line} {ref} |")
        lines.append("")
    if facts.tests:
        lines += ["## 测试", ""]
        for test in facts.tests:
            ref = _ev_ref(meta, file=test.path, line=test.line)
            lines.append(f"- `{test.name}`（{test.framework}），{test.path}:{test.line}。{ref}")
        lines.append("")
    lines += ["## 已知限制与未确认事项", "", "- 模块职责的详细语义需人工评审确认。"]
    return "\n".join(lines)


def _render_interface(meta: TaskMeta, facts: TaskFacts) -> str:
    sym = facts.symbols[0] if facts.symbols else None
    lines = [f"# {meta.title_hint or meta.subject}", ""]
    if sym is not None:
        ref = _ev_ref(meta, symbol=sym.name)
        sig = sym.signature or sym.qualified_name
        lines += [
            "## 签名",
            "",
            f"`{sig}`（{sym.kind.value}，{sym.path}:{sym.line}）。{ref}",
            "",
            "## 参数与返回",
            "",
            f"参数与返回值的语义以签名为准，详细约束见出处代码。{ref}",
        ]
    else:
        lines += ["## 签名", "", "未确认：缺少符号事实。"]
    lines += ["", "## 已知限制与未确认事项", "", "- 错误码与调用约束需人工评审确认。"]
    return "\n".join(lines)


def _render_config(meta: TaskMeta, facts: TaskFacts) -> str:
    lines = [
        f"# {meta.title_hint or meta.subject}",
        "",
        "## 配置项",
        "",
        "| 配置 | 位置 |",
        "|---|---|",
    ]
    for cfg in facts.configs:
        ref = _ev_ref(meta, file=cfg.path, line=cfg.line)
        lines.append(f"| `{cfg.key}` | {cfg.path}:{cfg.line} {ref} |")
    lines += ["", "## 已知限制与未确认事项", "", "- 默认值与取值范围需人工确认。"]
    return "\n".join(lines)


def _render_build(meta: TaskMeta, facts: TaskFacts) -> str:
    lines = [f"# {meta.title_hint or meta.subject}", "", "## 构建目标", ""]
    for target in facts.build_targets:
        ref = _ev_ref(meta, file=target.path, line=target.line)
        lines.append(
            f"- `{target.name}`（{target.kind}，{target.build_system}），"
            f"定义于 {target.path}:{target.line}。{ref}"
        )
    lines += [
        "",
        "## 已知限制与未确认事项",
        "",
        "- 编译命令需在 CI 环境实测验证（当前标注未验证）。",
    ]
    return "\n".join(lines)


def _render_test(meta: TaskMeta, facts: TaskFacts) -> str:
    lines = [f"# {meta.title_hint or meta.page_id}", "", "## 测试用例", ""]
    for test in facts.tests:
        ref = _ev_ref(meta, file=test.path, line=test.line)
        lines.append(f"- `{test.name}`（{test.framework}），{test.path}:{test.line}。{ref}")
    lines += ["", "## 已知限制与未确认事项", "", "- 测试覆盖范围需人工评审确认。"]
    return "\n".join(lines)


def _render_flow(meta: TaskMeta, facts: TaskFacts) -> str:
    lines = [f"# {meta.title_hint or meta.subject}", "", "## 关键步骤", ""]
    for idx, call in enumerate(facts.calls, start=1):
        ref = _ev_ref(meta, file=call.path, line=call.line)
        lines.append(
            f"{idx}. `{call.caller or '(顶层)'}` 调用 `{call.callee}`"
            f"（{call.path}:{call.line}）。{ref}"
        )
    if not facts.calls:
        lines.append("未确认：缺少调用事实。")
    lines += ["", "## 已知限制与未确认事项", "", "- 失败路径需人工评审确认。"]
    return "\n".join(lines)


def _render_overview(meta: TaskMeta, facts: TaskFacts) -> str:
    lines = [f"# {meta.title_hint or '系统概览'}", "", "## 规模统计", ""]
    for key in sorted(facts.stats):
        lines.append(f"- {key}: {facts.stats[key]}")
    lines += ["", "## 已知限制与未确认事项", "", "- 模块协作关系需结合流程页阅读。"]
    return "\n".join(lines)


_BODY_RENDERERS = {
    PageType.HOME: _render_overview,
    PageType.OVERVIEW: _render_overview,
    PageType.MODULE: _render_module,
    PageType.INTERFACE: _render_interface,
    PageType.CONFIG: _render_config,
    PageType.BUILD: _render_build,
    PageType.TEST: _render_test,
    PageType.FLOW: _render_flow,
}


def _ev_ref(meta: TaskMeta, symbol: str | None = None, file: str = "", line: int = 0) -> str:
    """找与事实对应的证据编号；找不到返回空串（该事实不进页面）。"""
    for ev in meta.evidence:
        if symbol is not None and ev.symbol == symbol:
            return f"[{ev.id}]"
        if file and ev.file == file and (line == 0 or ev.line_hint == line) and not ev.symbol:
            return f"[{ev.id}]"
    return ""


def _elapsed_ms(started: float) -> int:
    return max(int((time.monotonic() - started) * 1000), 0)


def _rough_tokens(path: Path) -> int:
    """粗估词元数：字符数 × 0.6（11.6）。"""
    total = 0
    if path.is_dir():
        for item in path.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
    return int(total * 0.6)


__all__ = [
    "BACKEND_NAME",
    "FAIL_BAD_EVIDENCE_REF",
    "FAIL_BAD_JSON",
    "FAIL_FUZZY_WORD",
    "FAIL_MISSING_TITLE",
    "MODEL_NAME",
    "MockBackend",
]
