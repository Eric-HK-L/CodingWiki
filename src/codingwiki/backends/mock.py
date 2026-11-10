"""MockBackend：确定性模型后端（计划文档 11.3，架构文档 v0.4 13.4）。

读任务包（meta.json + context/facts.json），纯模板化组装页面与 result.json：
结论全部来自事实字段，每条结论挂对应 E 编号。代表"生成质量地板"，
开发与 CI 不依赖真实模型。

故障注入用于测试修复循环：failures 中某项命中当前 attempt 时输出坏结果。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import yaml

from codingwiki.backends.protocol import BackendResult, BackendStatus, TaskPacket
from codingwiki.domain.facts import Confidence, SymbolKind
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
FAIL_BAD_CITATION = "bad_citation"  # ask：引用不存在的 S 编号
FAIL_SPEC_MISSING_SECTION = "spec_missing_section"  # spec_draft：缺第 11 章
FAIL_SPEC_BAD_TASK = "spec_bad_task"  # spec_draft：任务超过 3 个文件
FAIL_SPEC_ORPHAN_REQUIREMENT = "spec_orphan_requirement"  # spec_draft：删掉一条追溯行
FAIL_SPEC_ORPHAN_TASK = "spec_orphan_task"  # spec_draft：新增未被追溯的任务
FAIL_SPEC_DANGLING_LOCATION = "spec_dangling_location"  # spec_draft：追溯位置不在证据
FAIL_SPEC_UNSOURCED_CLAIM = "spec_unsourced_claim"  # spec_draft：现状章出现无出处位置
FAIL_SPEC_TERM_DRIFT = "spec_term_drift"  # spec_draft：术语大小写漂移
FAIL_SPEC_NO_ROLLBACK = "spec_no_rollback"  # spec_draft：风险章缺回滚


class MockBackend:
    """确定性后端。failures: [{"attempt": 1, "kind": FAIL_*}]，命中即注入。"""

    def __init__(self, failures: list[dict[str, Any]] | None = None) -> None:
        self._failures = {int(f["attempt"]): str(f["kind"]) for f in (failures or [])}

    def run(self, packet: TaskPacket) -> BackendResult:
        started = time.monotonic()
        if _is_spec_extract(packet.workspace):
            return self._run_spec_extract(packet, started)
        if _is_spec_draft(packet.workspace):
            return self._run_spec_draft(packet, started)
        if (packet.workspace / "context" / "question.txt").is_file():
            return self._run_ask(packet, started)
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

    # ---------------------------------------------------------------- 需求抽取

    def _run_spec_extract(self, packet: TaskPacket, started: float) -> BackendResult:
        """确定性需求抽取：只复制文档里已有的句子/条目，不补写内容。"""
        try:
            document = (packet.workspace / "context" / "document.md").read_text(encoding="utf-8")
            raw_sections = json.loads(
                (packet.workspace / "context" / "sections.json").read_text(encoding="utf-8")
            )
            if not isinstance(raw_sections, list):
                raise ValueError("sections.json 顶层必须是数组")
            sections = [item for item in raw_sections if isinstance(item, dict)]
            requirements, questions = _spec_extract(document, sections)
        except (OSError, ValueError) as exc:
            return BackendResult(
                status=BackendStatus.FAILED,
                duration_ms=_elapsed_ms(started),
                model=MODEL_NAME,
                error=f"需求抽取任务包不可读: {exc}",
            )

        packet.output_dir.mkdir(parents=True, exist_ok=True)
        (packet.output_dir / "extract.json").write_text(
            json.dumps(
                {"requirements": requirements, "questions": questions},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return BackendResult(
            status=BackendStatus.SUCCEEDED,
            exit_code=0,
            output_files=["output/extract.json"],
            duration_ms=_elapsed_ms(started),
            tokens_in=_rough_tokens(packet.workspace),
            tokens_out=_rough_tokens(packet.output_dir),
            model=MODEL_NAME,
        )

    # ---------------------------------------------------------------- Spec 草拟

    def _run_spec_draft(self, packet: TaskPacket, started: float) -> BackendResult:
        """模板化草拟 11 章：章节/任务/追溯全部由证据与抽取产物组装。"""
        try:
            extract = json.loads(
                (packet.workspace / "context" / "extract.json").read_text(encoding="utf-8")
            )
            evidence = json.loads(
                (packet.workspace / "context" / "evidence.json").read_text(encoding="utf-8")
            )
            meta = json.loads((packet.workspace / "meta.json").read_text(encoding="utf-8"))
            attempt = int(meta.get("attempt") or 1)
            payload = _spec_draft(extract, evidence, failure=self._failures.get(attempt))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return BackendResult(
                status=BackendStatus.FAILED,
                duration_ms=_elapsed_ms(started),
                model=MODEL_NAME,
                error=f"Spec 草拟任务包不可读: {exc}",
            )

        packet.output_dir.mkdir(parents=True, exist_ok=True)
        (packet.output_dir / "draft.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return BackendResult(
            status=BackendStatus.SUCCEEDED,
            exit_code=0,
            output_files=["output/draft.json"],
            duration_ms=_elapsed_ms(started),
            tokens_in=_rough_tokens(packet.workspace),
            tokens_out=_rough_tokens(packet.output_dir),
            model=MODEL_NAME,
        )

    # ---------------------------------------------------------------- 问答组装

    def _run_ask(self, packet: TaskPacket, started: float) -> BackendResult:
        """问答模式：模板化组装"直接答案 + 逐条引用"，引用全部来自 passages。"""
        try:
            question = (
                (packet.workspace / "context" / "question.txt").read_text(encoding="utf-8").strip()
            )
            passages = json.loads(
                (packet.workspace / "context" / "passages.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            return BackendResult(
                status=BackendStatus.FAILED,
                duration_ms=_elapsed_ms(started),
                model=MODEL_NAME,
                error=f"问答任务包不可读: {exc}",
            )

        attempt = int(packet.metadata.get("attempt", "1"))
        failure = self._failures.get(attempt)
        sids = [str(p["sid"]) for p in passages]
        citations = list(sids)
        if failure == FAIL_BAD_CITATION:
            citations.append("S999")

        lines = [
            f"关于「{question}」：",
            "",
        ]
        if passages:
            first = passages[0]
            lines.append(
                f"最相关的资料是 `{first.get('title') or first['reference']}`"
                f"（{first['reference']}）。[S1]"
            )
            lines.append("")
            lines.append("依据：")
            for p in passages:
                lines.append(f"- {p['reference']}（信任层级 {p['trust_tier']}）。[{p['sid']}]")
            if failure == FAIL_BAD_CITATION:
                lines.append("- 另见不存在来源的资料。[S999]")
        else:
            lines.append("根据现有资料无法确认。")
        answer = "\n".join(lines) + "\n"

        result = {
            "question": question,
            "answer_type": "answered" if passages else "refused",
            "citations": citations,
            "confidence": "high" if len(sids) >= 2 else "medium",
            "unsupported": [],
            "followups": [],
        }

        packet.output_dir.mkdir(parents=True, exist_ok=True)
        (packet.output_dir / "answer.md").write_text(answer, encoding="utf-8")
        if failure == FAIL_BAD_JSON:
            (packet.output_dir / "result.json").write_text("{ 不是合法 JSON", encoding="utf-8")
        else:
            (packet.output_dir / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
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
    names = [symbol.name for symbol in facts.symbols[:3]]
    lead = "、".join(f"`{name}`" for name in names) or "本目录的公开符号"
    files = "、".join(f"`{item.path}`" for item in facts.files[:4]) or "本模块文件"
    ref = _first_ref(meta)
    lines = [
        f"# {meta.title_hint or meta.module}",
        "",
        f"> 一句话职责：本模块负责组织 {lead} 的实现与调用，"
        "为上层的信号处理流程提供可复用的计算能力。",
        "",
        "## 职责边界",
        "",
        f"- 负责：维护本目录内的实现文件（{files}），"
        f"并把 {facts.stats.get('public_symbols', len(facts.symbols))} 个公开符号"
        f"暴露给调用方。[{ref}]",
        "- 不负责：进程启动、命令行解析与外部 I/O；这些由 examples 或上层驱动完成。",
        "",
        "## 关键数据结构",
        "",
        "| 类型 | 位置 | 作用 |",
        "|---|---|---|",
    ]
    for symbol in facts.symbols[:12]:
        row_ref = _cite(meta, symbol=symbol.name, file=symbol.path)
        lines.append(
            f"| `{symbol.name}` | {symbol.path}:{symbol.line} | "
            f"{symbol.kind.value}，签名 `{symbol.signature or symbol.qualified_name}` {row_ref} |"
        )
    lines += ["", "## 对外入口", "", "| 入口 | 签名 | 使用场景 |", "|---|---|---|"]
    entries = [s for s in facts.symbols if s.kind in _PUBLIC_KINDS][:12]
    for symbol in entries:
        row_ref = _cite(meta, symbol=symbol.name, file=symbol.path)
        lines.append(
            f"| `{symbol.qualified_name}` | `{symbol.signature or symbol.name}` | "
            f"由同模块或其他模块调用 {row_ref} |"
        )
    lines += [
        "",
        "## 内部结构",
        "",
        f"实现按文件组织：{files}。公开符号与内部符号共享同一套数据布局，"
        f"调用关系由任务包中的 {len(facts.calls)} 条调用事实给出。[{ref}]",
        "入口函数先校验输入，再按配置选择计算路径，最后把结果写回调用方提供的缓冲区。",
        "",
        "## 文件清单",
        "",
        "| 文件 | 所在模块 | 说明 |",
        "|---|---|---|",
    ]
    for item in facts.files[:16]:
        item_ref = _cite(meta, file=item.path)
        lines.append(
            f"| `{item.path}` | `{item.module or meta.module or '-'}` | "
            f"本模块实现文件 {item_ref} |"
        )
    lines += [
        "",
        "## 依赖与影响",
        "",
        f"- 依赖：本模块引用的公开头文件与同目录实现文件。[{ref}]",
        "- 被谁使用：调用方摘要见 context/callers.json；"
        "改动公开入口时，所有调用方与对应测试都需要同步检查。",
        "",
        "## 常见改动指引",
        "",
        f"- 修改公开入口签名：先查调用方摘要，再同步测试与示例，最后重新生成本页。[{ref}]",
        "- 修改配置读取：同步 config 页列出的配置项，避免默认值漂移。",
        "",
        "## 已知限制与未确认事项",
        "",
        "- 运行时性能特征需要基准测试数据；当前页面只陈述代码结构。",
    ]
    return "\n".join(lines)


def _render_interface(meta: TaskMeta, facts: TaskFacts) -> str:
    ref = _first_ref(meta)
    path = facts.files[0].path if facts.files else meta.subject
    lines = [
        f"# {meta.title_hint or meta.subject}",
        "",
        f"> 一句话职责：把 {path} 中的公共符号聚合为一组稳定入口，供调用方按签名使用。",
        "",
        "## 公共入口",
        "",
        "| 入口 | 签名 | 使用场景 |",
        "|---|---|---|",
    ]
    for symbol in facts.symbols[:12]:
        row_ref = _ev_ref(meta, symbol=symbol.name)
        lines.append(
            f"| `{symbol.qualified_name}` | `{symbol.signature or symbol.name}` | "
            f"{symbol.kind.value} 入口 {row_ref} |"
        )
    lines += [
        "",
        "## 参数与返回约定",
        "",
        f"参数与返回值由 {path} 中的声明决定；调用方必须按签名传入有效句柄与缓冲区。[{ref}]",
        "实现侧在入口处完成边界检查，错误通过返回码或状态对象向上传递。",
        "",
        "## 调用方",
        "",
    ]
    if facts.calls:
        for call in facts.calls[:8]:
            lines.append(
                f"- `{call.caller or '(顶层)'}` 在 {call.path}:{call.line} 调用 "
                f"`{call.callee}`。{_ev_ref(meta, file=call.path, line=call.line)}"
            )
    else:
        lines.append(f"- 任务包未提供调用方摘要；以头文件可见入口为准。[{ref}]")
    lines += [
        "",
        "## 文件清单",
        "",
        "| 文件 | 所在模块 | 说明 |",
        "|---|---|---|",
    ]
    for item in facts.files[:16]:
        item_ref = _cite(meta, file=item.path)
        lines.append(
            f"| `{item.path}` | `{item.module or meta.module or '-'}` | "
            f"本模块实现文件 {item_ref} |"
        )
    lines += [
        "",
        "## 依赖与影响",
        "",
        f"- 依赖：{path} 的声明与同模块实现文件。",
        "- 影响：改动签名会破坏调用方编译；改动语义需要同步测试与示例。",
        "",
        "## 出处",
    ]
    return "\n".join(lines)


def _render_config(meta: TaskMeta, facts: TaskFacts) -> str:
    ref = _first_ref(meta)
    lines = [
        f"# {meta.title_hint or meta.subject}",
        "",
        "> 一句话职责：集中列出仓库的配置键、读取位置与影响范围。",
        "",
        "## 配置项",
        "",
        "| 配置 | 位置 | 作用 |",
        "|---|---|---|",
    ]
    for cfg in facts.configs[:20]:
        row_ref = _ev_ref(meta, file=cfg.path, line=cfg.line)
        lines.append(
            f"| `{cfg.key}` | {cfg.path}:{cfg.line} | {cfg.format or cfg.kind} 配置 {row_ref} |"
        )
    lines += [
        "",
        "## 默认值与影响范围",
        "",
        f"配置项共 {facts.stats.get('config_facts', len(facts.configs))} 条；"
        f"页面只列出任务包提供的 {len(facts.configs)} 条。[{ref}]",
        "修改配置前先确认读取位置，避免同一键在多处解析出不同默认值。",
        "",
        "## 修改指引",
        "",
        "改配置键名时同步更新读取代码、示例与测试；新增键时补默认值与校验。[" + ref + "]",
        "",
        "## 出处",
    ]
    return "\n".join(lines)


def _render_build(meta: TaskMeta, facts: TaskFacts) -> str:
    ref = _first_ref(meta)
    lines = [
        f"# {meta.title_hint or meta.subject}",
        "",
        "> 一句话职责：说明构建系统、主要目标与运行入口。",
        "",
        "## 构建系统",
        "",
        "仓库使用 CMake 组织构建，任务包提供 "
        f"{facts.stats.get('build_targets', len(facts.build_targets))} 个目标。[{ref}]",
        "",
        "## 主要目标",
        "",
        "| 目标 | 类型 | 来源 |",
        "|---|---|---|",
    ]
    for target in facts.build_targets[:20]:
        row_ref = _ev_ref(meta, file=target.path, line=target.line)
        lines.append(f"| `{target.name}` | {target.kind} | {target.path}:{target.line} {row_ref} |")
    lines += [
        "",
        "## 运行入口",
        "",
        f"构建完成后按目标类型运行可执行文件或测试；具体命令以 CMake 目标定义为准。[{ref}]",
        "",
        "## 常见问题",
        "",
        "缺少依赖或编译定义不一致时，先检查目标声明的 include 目录与宏定义。",
        "",
        "## 出处",
    ]
    return "\n".join(lines)


def _render_test(meta: TaskMeta, facts: TaskFacts) -> str:
    ref = _first_ref(meta)
    lines = [
        f"# {meta.title_hint or meta.page_id}",
        "",
        "> 一句话职责：说明测试框架、主要用例与运行方式。",
        "",
        "## 测试框架",
        "",
        f"任务包列出 {facts.stats.get('tests', len(facts.tests))} 个测试事实，"
        f"本页覆盖其中 {len(facts.tests)} 个。[{ref}]",
        "",
        "## 主要测试",
        "",
        "| 测试 | 框架 | 位置 |",
        "|---|---|---|",
    ]
    for test in facts.tests[:20]:
        row_ref = _ev_ref(meta, file=test.path, line=test.line)
        lines.append(f"| `{test.name}` | {test.framework} | {test.path}:{test.line} {row_ref} |")
    lines += [
        "",
        "## 运行方式",
        "",
        "按测试框架执行对应二进制或 pytest 入口；失败时先看用例断言再查构建目标。[" + ref + "]",
        "",
        "## 覆盖与缺口",
        "",
        "未在任务包出现的路径不做覆盖承诺；新增功能时同步补测试事实。",
        "",
        "## 出处",
    ]
    return "\n".join(lines)


def _render_flow(meta: TaskMeta, facts: TaskFacts) -> str:
    ref = _first_ref(meta)
    lines = [
        f"# {meta.title_hint or meta.subject}",
        "",
        f"> 一句话职责：描述入口 {meta.subject} 从触发到结果返回的端到端链路。",
        "",
        "## 触发入口",
        "",
        f"入口位于 {meta.subject}，由 main 或测试驱动调用。[{ref}]",
        "",
        "## 步骤",
        "",
    ]
    for idx, call in enumerate(facts.calls[:16], start=1):
        call_ref = _ev_ref(meta, file=call.path, line=call.line)
        lines.append(
            f"{idx}. `{call.caller or '(顶层)'}` 调用 `{call.callee}`"
            f"（{call.path}:{call.line}）。{call_ref}"
        )
    if not facts.calls:
        lines.append(f"1. 任务包未提供调用事实，链路以入口文件为准。[{ref}]")
    lines += [
        "",
        "## 关键数据结构",
        "",
        "跨步骤传递的对象来自任务包列出的符号；每一步只读写自己的缓冲区。",
        "",
        "## 失败与边界",
        "",
        "参数非法或资源不足时，入口返回错误码并停止后续步骤；失败路径需结合测试覆盖。",
        "",
        "## 出处",
    ]
    return "\n".join(lines)


def _render_overview(meta: TaskMeta, facts: TaskFacts) -> str:
    ref = _first_ref(meta)
    title = meta.title_hint or "项目总览"
    lines = [
        f"# {title}",
        "",
        "> 一句话职责：用一页说明仓库规模、架构分层、核心模块与阅读路径。",
        "",
        "## 项目画像",
        "",
    ]
    for key in sorted(facts.stats):
        lines.append(f"- {key}: {facts.stats[key]}")
    lines += [
        "",
        f"以上数字来自当前快照的事实层。[{ref}]",
        "",
        "## 架构分层",
        "",
        "| 层 | 目录/模块 | 职责 |",
        "|---|---|---|",
        "| 系统页 | home / overview / architecture | 给出全局导航与约束 |",
        f"| 模块层 | {len(facts.modules)} 个模块 | 各自维护实现与公开入口 [{ref}] |",
        "| 流程层 | flow 页 | 串起入口到结果的调用链 |",
        "",
        "## 核心模块",
        "",
        "| 模块 | 文件数 | 符号数 | 流入调用 |",
        "|---|---|---|---|",
    ]
    for module in facts.modules[:20]:
        lines.append(f"| `{module.name}` | {module.files} | {module.symbols} | {module.incoming} |")
    lines += [
        "",
        f"模块表按符号规模排序，共列出 {min(len(facts.modules), 20)} 个。[{ref}]",
        "模块之间通过公开入口协作；调用方向与流入量见架构页的模块关系表。",
        "每个模块页都会给出职责边界、关键数据结构、对外入口与依赖影响四类信息，"
        "读者可以按模块名直接下钻，不需要先记住符号表。",
        "如果只想回答「负责什么、入口在哪、依赖谁」，读一个模块页即可；"
        "如果要看跨模块顺序，再读对应的流程页。",
        "",
        "## 构建与运行",
        "",
        f"构建目标、配置项与测试入口分别见 build / config / test 页；本页只给总览。[{ref}]",
        "构建与测试页列出目标、读取位置与运行方式，便于从总览继续下钻。",
        "",
        "## 阅读路径",
        "",
        f"1. 首页 → 本页（项目画像）。[{ref}]",
        f"2. 本页 → 核心模块表，选择与当前任务最相关的模块。[{ref}]",
        "3. 模块页 → flow 页，沿调用链核对入口与结果。",
        f"以上路径中的页面都由同一快照生成，出处编号可回查。[{ref}]",
        "",
        "## 出处",
    ]
    return "\n".join(lines)


def _render_architecture(meta: TaskMeta, facts: TaskFacts) -> str:
    ref = _first_ref(meta)
    lines = [
        f"# {meta.title_hint or '架构'}",
        "",
        "> 一句话概述：按层划分目录，模块之间通过公开入口与调用边协作。",
        "",
        "## 分层",
        "",
        "| 层 | 代表模块 | 说明 |",
        "|---|---|---|",
    ]
    for module in facts.modules[:12]:
        lines.append(f"| 模块 | `{module.name}` | {module.symbols} 个符号 [{ref}] |")
    lines += [
        "",
        "分层的目的是把变化限制在局部：系统页只依赖模块页的标题与入口，"
        "模块页只依赖本目录的事实，流程页只依赖调用边。[" + ref + "]",
        "这样修改一个模块时，只需要重新生成该模块页及其相关流程页；"
        "系统页与架构页只在模块集合或依赖方向变化时更新。",
        "架构页不重复模块页的细节，只保留分层、依赖方向与关键约束，"
        "避免同一事实在多页出现不一致的表述。",
        "读者如果只关心某个子系统，可以跳过本页，直接从首页进入对应模块页，"
        "再按依赖方向回看上游与下游模块。",
        "",
        "## 模块关系",
        "",
        "| 模块 | 流入 | 流出 |",
        "|---|---|---|",
    ]
    for module in facts.modules[:16]:
        lines.append(f"| `{module.name}` | {module.incoming} | {module.outgoing} |")
    lines += [
        "",
        f"流入/流出按解析到的调用边统计。[{ref}]",
        "流入高的模块通常是被多个上层流程复用的基础设施；流出高的模块通常负责编排多个子模块。",
        "",
        "## 关键约束",
        "",
        "- 公开入口只在头文件声明，实现文件不暴露内部符号。",
        "- 模块不得反向依赖上层示例；示例只调用模块公开入口。",
        f"- 构建目标与源文件的一致性由事实层质量报告给出。[{ref}]",
        "跨层调用必须经过公开入口，避免把内部数据结构泄漏到上层。",
        "同一目录内的实现文件可以互相调用，但不得跨目录直接访问内部符号。",
        "",
        "## 典型调用链",
        "",
        "入口 → 公开函数 → 模块内部实现 → 结果缓冲区；流程页给出具体步骤。",
        f"调用链中的每一步都能在流程页找到对应的 file:line 出处。[{ref}]",
        "当链路跨越多个模块时，先看模块关系表的流入/流出，再下钻到具体流程。",
        "读流程页时建议同时打开对应模块页，核对入口签名与依赖方向是否一致。",
        "",
        "## 出处",
    ]
    return "\n".join(lines)


def _render_home(meta: TaskMeta, facts: TaskFacts) -> str:
    ref = _first_ref(meta)
    lines = [
        f"# {meta.title_hint or '首页'}",
        "",
        "> 一句话职责：给出可逐层下钻的阅读入口与仓库画像。",
        "",
        "## 导航",
        "",
        f"- 项目总览：`overview`（规模与核心模块）[{ref}]",
        f"- 架构：`architecture`（分层与依赖方向）[{ref}]",
        f"- 模块：`module/*`（职责、入口、依赖）[{ref}]",
        "- 流程：`flow/*`（端到端调用链）",
        "- 接口：`interface/*`（公共 API 聚合）",
        "- 配置、构建、测试、术语：`config/overview`、`build/overview`、"
        "`test/overview`、`glossary`",
        "",
        "## 仓库画像",
        "",
    ]
    for key in sorted(facts.stats):
        lines.append(f"- {key}: {facts.stats[key]}")
    lines += [
        "",
        f"数字来自当前快照的事实层。[{ref}]",
        "",
        "## 推荐阅读顺序",
        "",
        "1. 先读项目总览，建立规模与目录认知。",
        "2. 再读架构页，理解模块依赖方向。",
        "3. 最后按模块页与流程页下钻。",
        "",
        "## 出处",
    ]
    return "\n".join(lines)


def _render_glossary(meta: TaskMeta, facts: TaskFacts) -> str:
    ref = _first_ref(meta)
    lines = [
        f"# {meta.title_hint or '术语表'}",
        "",
        "> 一句话职责：统一领域词与代码位置的对照。",
        "",
        "## 术语",
        "",
        "| 术语 | 代码位置 | 含义 |",
        "|---|---|---|",
    ]
    for term in facts.terms[:20]:
        row_ref = _ev_ref(meta, symbol=term.term)
        lines.append(
            f"| `{term.term}` | {term.path}:{term.line} | {term.kind} 类型/符号 {row_ref} |"
        )
    lines += [
        "",
        "## 易混淆词",
        "",
        f"术语表共 {facts.stats.get('terms', len(facts.terms))} 条；"
        "相近缩写的区分以代码位置为准。[" + ref + "]",
        "",
        "## 出处",
    ]
    return "\n".join(lines)


def _render_flow_detail(meta: TaskMeta, facts: TaskFacts) -> str:
    """S37：关键流程结构化页（优化架构 §14.3）；步骤来自任务包调用事实。"""
    ref = _first_ref(meta)
    entry_path = meta.subject.partition("::")[0]
    lines = [
        f"# {meta.title_hint or meta.subject}",
        "",
        f"> 一句话职责：描述入口 {meta.subject} 从触发到结果返回的关键业务流程，"
        "覆盖阶段划分、分支与跨模块协作。",
        "",
        "## 流程概览",
        "",
        f"流程由 {entry_path} 中的入口触发，沿调用事实逐阶段推进，"
        f"任务包提供 {len(facts.calls)} 条直接调用事实作为步骤骨架。[{ref}]",
        "输入为入口参数与全局配置，输出写回调用方缓冲区；参与模块以入口所在目录为主。",
        "",
        "## 步骤",
        "",
    ]
    for idx, call in enumerate(facts.calls[:16], start=1):
        call_ref = _cite(meta, file=call.path, line=call.line)
        lines.append(
            f"{idx}. 阶段 {idx}：`{call.caller or '(顶层)'}` 调用 `{call.callee}`"
            f"（{call.path}:{call.line}），完成本阶段的数据变换。{call_ref}"
        )
    if not facts.calls:
        lines.append(f"1. 阶段 1：任务包未提供调用事实，步骤以入口文件为准。[{ref}]")
    lines += [
        "",
        "## 分支与边界",
        "",
        f"参数非法或资源不足时流程在入口校验阶段终止并返回错误码；"
        f"各阶段只读写自己的缓冲区，跨阶段共享对象以任务包符号表为准。[{ref}]",
        "循环与条件分支由入口参数驱动，具体判定条件以源码中的分支语句为准。",
        "",
        "## 跨模块关系",
        "",
        f"流程跨目录的调用方向与协作模块见架构页与相关模块页；"
        f"本页步骤只引用真实存在的符号与调用事实。[{ref}]",
        "改动入口签名前应先检查调用方摘要与对应流程页，避免阶段顺序被破坏。",
        "",
        "| 被调方文件 | 被调符号 | 出处 |",
        "|---|---|---|",
    ]
    seen_pairs: set[tuple[str, str]] = set()
    for call in facts.calls:
        key = (call.path, call.callee)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        if len(seen_pairs) > 12:
            break
        lines.append(
            f"| `{call.path}` | `{call.callee}` | "
            f"{_cite(meta, file=call.path, line=call.line)} |"
        )
    lines += [
        "",
        "## 出处",
    ]
    return "\n".join(lines)


def _render_concept(meta: TaskMeta, facts: TaskFacts) -> str:
    """S37：概念页（优化架构 §14.3）；术语与角色来自任务包上下文。"""
    ref = _first_ref(meta)
    token = meta.subject
    modules_using: list[str] = sorted({f.module for f in facts.files if f.module})
    lines = [
        f"# 概念：{token}",
        "",
        f"> 一句话定义：`{token}` 是本仓库中一组相关类型与函数共同表达的概念。",
        "",
        "## 定义",
        "",
        f"概念 `{token}` 由任务包中 {facts.stats.get('terms', len(facts.terms))} 个"
        f"名称包含该词的符号共同刻画；这些符号的声明位置见下表。[{ref}]",
        "概念的确切语义以符号声明与调用上下文为准，本页只做归纳性描述。",
        "",
        "## 相关术语",
        "",
        "| 术语 | 位置 | 职责 |",
        "|---|---|---|",
    ]
    for term in facts.terms[:16]:
        # 术语来自符号，优先按符号名匹配证据（file+line 匹配会被 symbol 证据挡住）
        row_ref = _cite(meta, symbol=term.term, file=term.path)
        lines.append(
            f"| `{term.term}` | {term.path}:{term.line} | "
            f"{term.kind}，参与 `{token}` 概念 {row_ref} |"
        )
    if not facts.terms:
        # 小仓库可能没有匹配术语：退化为符号表，保证术语段落有可校验出处
        for symbol in facts.symbols[:16]:
            row_ref = _cite(meta, symbol=symbol.name, file=symbol.path)
            lines.append(
                f"| `{symbol.name}` | {symbol.path}:{symbol.line} | "
                f"{symbol.kind.value}，参与 `{token}` 概念 {row_ref} |"
            )
    if not facts.terms and not facts.symbols:
        lines.append(
            f"| `{token}` | 见出处 | 任务包未提供匹配术语，以符号表为准 [{ref}] |"
        )
    modules_text = "、".join(f"`{m}`" for m in modules_using[:8]) or "见模块页"
    lines += [
        "",
        "## 在仓库中的角色",
        "",
        f"使用该概念符号的模块：{modules_text}。[{ref}]",
        "修改该概念对应的数据结构时，相关术语所在文件与上述模块都需要同步检查。",
        "",
        "## 出处",
    ]
    return "\n".join(lines)


_BODY_RENDERERS = {
    PageType.HOME: _render_home,
    PageType.OVERVIEW: _render_overview,
    PageType.ARCHITECTURE: _render_architecture,
    PageType.MODULE: _render_module,
    PageType.INTERFACE: _render_interface,
    PageType.CONFIG: _render_config,
    PageType.BUILD: _render_build,
    PageType.TEST: _render_test,
    PageType.FLOW: _render_flow,
    PageType.FLOW_DETAIL: _render_flow_detail,
    PageType.CONCEPT: _render_concept,
    PageType.GLOSSARY: _render_glossary,
}

_PUBLIC_KINDS = {SymbolKind.FUNCTION, SymbolKind.CLASS, SymbolKind.STRUCT, SymbolKind.KERNEL}


def _first_ref(meta: TaskMeta) -> str:
    return meta.evidence[0].id if meta.evidence else ""


def _ev_ref(meta: TaskMeta, symbol: str | None = None, file: str = "", line: int = 0) -> str:
    """找与事实对应的证据编号；找不到返回空串（该事实不进页面）。"""
    for ev in meta.evidence:
        if symbol is not None and ev.symbol == symbol:
            return f"[{ev.id}]"
        if file and ev.file == file and (line == 0 or ev.line_hint == line) and not ev.symbol:
            return f"[{ev.id}]"
    return ""


def _cite(
    meta: TaskMeta,
    symbol: str | None = None,
    file: str = "",
    line: int = 0,
) -> str:
    """页面引用兜底：精确匹配 → 同文件任一证据 → 首条证据。

    mock 后端是质量地板：表格/步骤行必须有引用，否则证据密度校验无法通过；
    退化为文件级引用仍然指向真实证据，不虚构位置。
    """
    ref = _ev_ref(meta, symbol=symbol, file=file, line=line)
    if ref:
        return ref
    if file:
        for ev in meta.evidence:
            if ev.file == file:
                return f"[{ev.id}]"
    first = _first_ref(meta)
    return f"[{first}]" if first else ""


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


# ---------------------------------------------------------------- 需求抽取（mock）


SPEC_EXTRACT_TASK = "spec_extract"


def _is_spec_extract(workspace: Path) -> bool:
    meta = workspace / "meta.json"
    if not meta.is_file():
        return False
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("task_type") == SPEC_EXTRACT_TASK


SPEC_DRAFT_TASK = "spec_draft"
_DRAFT_SECTIONS = [
    "背景与目标",
    "术语",
    "现状",
    "需求",
    "方案设计",
    "影响分析",
    "测试计划",
    "编码任务拆分",
    "风险与回滚",
    "开放问题与假设",
    "追溯矩阵",
]


def _is_spec_draft(workspace: Path) -> bool:
    meta = workspace / "meta.json"
    if not meta.is_file():
        return False
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("task_type") == SPEC_DRAFT_TASK


def _draft_locator(item: dict[str, Any]) -> str:
    path = str(item.get("path") or "")
    if path:
        line = int(item.get("line") or 0)
        return f"{path}:{line}" if line else path
    symbol = str(item.get("symbol_id") or "")
    if symbol:
        return symbol
    page = str(item.get("page_id") or "")
    return f"wiki:{page}" if page else ""


def _draft_term(symbols: list[str], evidence_rows: dict[str, dict[str, Any]]) -> str:
    """取一个强标识符用于术语漂移注入。"""
    if symbols:
        raw = symbols[0].split("::", 1)[1] if "::" in symbols[0] else symbols[0]
        return raw.rsplit("@", 1)[0].split("::")[-1]
    configs = sorted(
        str(key)
        for row in evidence_rows.values()
        for key in (row.get("impact") or {}).get("config", [])
        if key and "_" in str(key)
    )
    return configs[0] if configs else ""


def _spec_draft(
    extract: dict[str, Any], evidence: dict[str, Any], failure: str | None
) -> dict[str, Any]:
    """确定性草稿：章节与任务全部来自 extract/evidence，不新增内容。"""
    requirements = [item for item in extract.get("requirements", []) if isinstance(item, dict)]
    questions = [item for item in extract.get("questions", []) if isinstance(item, dict)]
    evidence_rows = {
        str(row.get("requirement_id")): row
        for row in evidence.get("requirements", [])
        if isinstance(row, dict)
    }
    title = str(extract.get("title") or "需求文档")
    snapshot = str(evidence.get("snapshot_id") or "")

    tasks: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    for index, requirement in enumerate(requirements, start=1):
        requirement_id = str(requirement.get("id") or f"R-{index:03d}")
        row = evidence_rows.get(requirement_id, {})
        raw_impact = row.get("impact")
        impact: dict[str, Any] = raw_impact if isinstance(raw_impact, dict) else {}
        items = [item for item in row.get("items", []) if isinstance(item, dict)]
        files = [str(path) for path in impact.get("files", []) if path][:3]
        if not files:
            files = sorted({str(item.get("path")) for item in items if item.get("path")})[:3]
        acceptance = [
            str(ac.get("text"))
            for ac in requirement.get("acceptance", [])
            if isinstance(ac, dict) and ac.get("text")
        ] or [f"满足 {requirement_id} 的可观察结果"]
        tasks.append(
            {
                "goal": f"实现 {requirement_id}：{requirement.get('text', '')}",
                "files": files,
                "estimated_lines": 120,
                "acceptance": acceptance,
                "depends_on_indexes": [],
            }
        )
        locator = next((_draft_locator(item) for item in items if _draft_locator(item)), "")
        tests = [str(name) for name in impact.get("tests", []) if name]
        trace.append(
            {
                "requirement_id": requirement_id,
                "design_section": "5 方案设计",
                "code_location": locator,
                "test": tests[0] if tests else "待补充回归测试（现有快照未找到对应测试）",
                "task_index": index,
            }
        )

    decision: dict[str, Any] = {
        "title": "实现路径选择",
        "context": f"{title} 共 {len(requirements)} 条需求，证据绑定快照 {snapshot}。",
        "candidates": [
            {
                "name": "方案A",
                "summary": "在现有模块内扩展，复用已有接口与配置读取。",
                "pros": "改动面最小，与现有调用方兼容。",
                "cons": "模块职责继续变重。",
            },
            {
                "name": "方案B",
                "summary": "新增独立模块，通过窄接口接入现有流程。",
                "pros": "职责清晰，便于独立测试。",
                "cons": "新增构建目标与集成成本。",
            },
        ],
        "choice": "方案A",
        "rationale": "证据集中在现有模块，扩展方案的回归范围更小。",
        "unique_justification": "",
    }

    status_line = f"{title}。共 {len(requirements)} 条需求，绑定快照 `{snapshot}`。"
    symbols = sorted(
        {
            str(item.get("symbol_id"))
            for row in evidence_rows.values()
            for item in row.get("items", [])
            if isinstance(item, dict) and item.get("symbol_id")
        }
    )
    current_lines: list[str] = []
    for requirement in requirements:
        row = evidence_rows.get(str(requirement.get("id")), {})
        for item in [i for i in row.get("items", []) if isinstance(i, dict)][:3]:
            locator = _draft_locator(item)
            if locator:
                current_lines.append(f"- {requirement.get('id')}：`{locator}`")
    requirement_lines = []
    for requirement in requirements:
        acceptance = [
            str(ac.get("text"))
            for ac in requirement.get("acceptance", [])
            if isinstance(ac, dict) and ac.get("text")
        ]
        line = (
            f"- {requirement.get('id')}（{requirement.get('priority')}，"
            f"{requirement.get('kind')}）：{requirement.get('text')}"
        )
        if acceptance:
            line += "；验收：" + "；".join(acceptance)
        requirement_lines.append(line)
    impact_lines = []
    for requirement in requirements:
        impact = evidence_rows.get(str(requirement.get("id")), {}).get("impact") or {}
        interfaces = len(impact.get("interfaces", []))
        callers = len(impact.get("callers", []))
        impact_lines.append(
            f"- {requirement.get('id')}：文件 {', '.join(impact.get('files', [])[:3]) or '无'}；"
            f"接口 {interfaces} 个；调用方 {callers} 个"
        )
    test_lines = []
    no_test = "不适用：现有快照未找到对应测试，需新增回归测试"
    for requirement in requirements:
        impact = evidence_rows.get(str(requirement.get("id")), {}).get("impact") or {}
        tests = [str(name) for name in impact.get("tests", []) if name]
        test_lines.append(f"- {requirement.get('id')}：{', '.join(tests[:5]) or no_test}")
    task_lines = [
        f"- T-{index:03d}：{task['goal']}（{len(task['files'])} 文件，"
        f"{task['estimated_lines']} 行）"
        for index, task in enumerate(tasks, start=1)
    ]
    trace_lines = ["| 需求 | 设计章节 | 代码位置 | 测试 | 任务 |", "|---|---|---|---|---|"]
    trace_lines += [
        f"| {row['requirement_id']} | {row['design_section']} | `{row['code_location']}` | "
        f"{row['test']} | T-{row['task_index']:03d} |"
        for row in trace
    ]
    question_lines = [
        f"- Q：{question.get('question')}（影响：{question.get('impact') or '待评估'}）"
        for question in questions
        if question.get("question")
    ] or ["无。"]
    bodies = [
        status_line,
        "本文涉及符号：" + ("、".join(symbols[:8]) if symbols else "不适用：证据中没有符号。"),
        "\n".join(current_lines) or "不适用：证据中没有可定位的代码位置。",
        "\n".join(requirement_lines) or "不适用：抽取产物没有需求。",
        "候选方案：\n"
        + "\n".join(
            f"- {candidate['name']}：{candidate['summary']}" for candidate in decision["candidates"]
        )
        + f"\n\n选择 **{decision['choice']}**：{decision['rationale']}",
        "\n".join(impact_lines) or "不适用：没有影响面数据。",
        "\n".join(test_lines) or "不适用：没有测试证据。",
        "\n".join(task_lines) or "不适用：没有可拆分的编码任务。",
        "风险：改动限定在影响面列出的文件；回滚方式为按任务反向撤销提交。"
        "若涉及配置默认值，需在发布说明中标注。",
        "\n".join(question_lines),
        "\n".join(trace_lines),
    ]
    if failure == FAIL_SPEC_UNSOURCED_CLAIM:
        bodies[2] += "\n- 结论：`src/nowhere/dangling.cpp:999` 是入口。"
    if failure == FAIL_SPEC_TERM_DRIFT:
        drift = _draft_term(symbols, evidence_rows)
        if drift:
            bodies[1] += f"\n术语漂移示例：`{drift.upper()}`。"
    if failure == FAIL_SPEC_NO_ROLLBACK:
        bodies[8] = "风险：改动限定在影响面列出的文件。"
    sections = [
        {"number": number, "title": title_text, "body": body}
        for number, (title_text, body) in enumerate(
            zip(_DRAFT_SECTIONS, bodies, strict=True), start=1
        )
    ]
    if failure == FAIL_SPEC_MISSING_SECTION:
        sections = sections[:-1]
    if failure == FAIL_SPEC_BAD_TASK and tasks:
        tasks[0]["files"] = ["a.cpp", "b.cpp", "c.cpp", "d.cpp"]
    if failure == FAIL_SPEC_ORPHAN_REQUIREMENT and trace:
        trace = trace[:-1]
    if failure == FAIL_SPEC_ORPHAN_TASK:
        extra_files = list(tasks[0]["files"]) if tasks else ["CMakeLists.txt"]
        tasks.append(
            {
                "goal": "孤立任务：没有被任何需求引用",
                "files": extra_files,
                "estimated_lines": 10,
                "acceptance": ["占位"],
                "depends_on_indexes": [],
            }
        )
    if failure == FAIL_SPEC_DANGLING_LOCATION and trace:
        trace[0]["code_location"] = "src/nowhere/dangling.cpp:999"
    return {
        "sections": sections,
        "tasks": tasks,
        "trace": trace,
        "decisions": [decision],
    }


def _spec_extract(
    document: str, sections: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """只搬运原文：需求取条目标题下的首句或条目，验收标准取「验收标准」后的条目。"""
    lines = document.splitlines()
    requirements: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    for section in sections:
        path = str(section.get("path", ""))
        if "开放问题" in path:
            for text in _section_bullets(lines, section):
                questions.append({"question": text, "impact": "", "owner": ""})
            continue
        if "功能需求" not in path:
            continue
        kind = "non_functional" if "非功能" in path else "functional"
        current: dict[str, Any] | None = None
        acceptance_mode = False
        for raw in _section_body(lines, section):
            stripped = raw.strip()
            if not stripped or stripped.startswith(("```", "~~~")):
                continue
            if stripped.startswith("验收标准"):
                acceptance_mode = True
                continue
            bullet = _bullet_text(stripped)
            if bullet is not None:
                if acceptance_mode and current is not None:
                    current["acceptance"].append(bullet)
                else:
                    current = _new_requirement(bullet, path, kind)
                    requirements.append(current)
                    acceptance_mode = False
                continue
            if not acceptance_mode and current is None:
                current = _new_requirement(stripped, path, kind)
                requirements.append(current)
    return requirements, questions


def _section_body(lines: list[str], section: dict[str, Any]) -> list[str]:
    start = int(section.get("start_line", 0) or 0)
    end = int(section.get("end_line", 0) or 0)
    if start < 1:
        return []
    return lines[start : max(end, start)]


def _section_bullets(lines: list[str], section: dict[str, Any]) -> list[str]:
    bullets: list[str] = []
    for raw in _section_body(lines, section):
        stripped = raw.strip()
        if not stripped or stripped.startswith(("```", "~~~")):
            continue
        bullet = _bullet_text(stripped)
        if bullet is not None:
            bullets.append(bullet)
    return bullets


def _bullet_text(line: str) -> str | None:
    for marker in ("- ", "* ", "+ "):
        if line.startswith(marker):
            return line[len(marker) :].strip()
    return None


def _new_requirement(text: str, path: str, kind: str) -> dict[str, Any]:
    priority = "must" if any(word in text for word in ("必须", "不得", "禁止")) else "should"
    return {
        "kind": kind,
        "text": text,
        "priority": priority,
        "source_section": path,
        "source_quote": text,
        "acceptance": [],
    }


__all__ = [
    "BACKEND_NAME",
    "FAIL_BAD_CITATION",
    "FAIL_BAD_EVIDENCE_REF",
    "FAIL_BAD_JSON",
    "FAIL_FUZZY_WORD",
    "FAIL_MISSING_TITLE",
    "FAIL_SPEC_BAD_TASK",
    "FAIL_SPEC_DANGLING_LOCATION",
    "FAIL_SPEC_MISSING_SECTION",
    "FAIL_SPEC_NO_ROLLBACK",
    "FAIL_SPEC_ORPHAN_REQUIREMENT",
    "FAIL_SPEC_ORPHAN_TASK",
    "FAIL_SPEC_TERM_DRIFT",
    "FAIL_SPEC_UNSOURCED_CLAIM",
    "MODEL_NAME",
    "MockBackend",
    "SPEC_DRAFT_TASK",
    "SPEC_EXTRACT_TASK",
]
