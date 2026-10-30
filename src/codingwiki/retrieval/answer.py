"""问答流水线（计划文档 10.6，架构文档 v0.5 14.5）。

检索 Top K → 组装 passages（S 编号）→ 问答任务包 → ModelBackend →
校验（引用闭合、high 置信度双来源）→ 失败带 errors.json 重试 ≤3 轮。
资料不足时不调用模型，直接拒答。
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from codingwiki.backends.protocol import BackendStatus, ModelBackend, TaskPacket
from codingwiki.domain.rag import AnswerResult, AskOutcome, SearchHit, SourcePassage
from codingwiki.domain.wiki import VerifyIssue
from codingwiki.retrieval.search import HybridSearcher

# 问答拒答的向量门槛由 searcher.min_vector_score() 按索引规模计算，见该方法文档。
_MAX_PASSAGE_CHARS = 2000

_ASK_CONSTRAINTS = """# 硬性约束

1. 只能基于 context/passages.json 中的资料回答，不得使用模型记忆。
2. 每个事实后面必须标注 [S编号]，编号必须存在于 passages.json。
3. 资料不足时在 result.json 中 answer_type=refused，不要编造。
4. confidence=high 要求至少引用两个独立来源。
5. 只能写入 output/ 目录（answer.md + result.json）。
"""

_CITATION = re.compile(r"\[S(\d+)\]")


class AnswerPipeline:
    def __init__(
        self,
        backend: ModelBackend,
        searcher: HybridSearcher,
        base_dir: str | Path | None = None,
        max_rounds: int = 3,
    ) -> None:
        self._backend = backend
        self._searcher = searcher
        self._base = Path(base_dir or os.environ.get("CODINGWIKI_HOME", ".codingwiki")).resolve()
        self._max_rounds = max(1, max_rounds)

    def ask(self, repo_id: str, question: str, top_k: int = 8) -> AskOutcome:
        hits = self._searcher.search(question, top_k=top_k)
        passages = _to_passages(hits)

        # 拒答：没有任何命中，或关键词通道完全无命中且向量分数未过噪声门槛
        top = hits[0] if hits else None
        if top is None or (
            top.bm25_rank == 0 and top.vector_score < self._searcher.min_vector_score()
        ):
            refusal = "根据现有资料无法确认。建议换用更具体的函数名/配置名，或先构建更完整的 Wiki。"
            return AskOutcome(
                question=question,
                answer=refusal,
                result=AnswerResult(question=question, answer_type="refused"),
                passages=passages,
                attempts=0,
                refused=True,
            )

        previous_errors: list[VerifyIssue] | None = None
        for attempt in range(1, self._max_rounds + 1):
            packet = _build_ask_packet(
                self._base, repo_id, question, passages, attempt, previous_errors
            )
            backend_result = self._backend.run(packet)
            if backend_result.status is not BackendStatus.SUCCEEDED:
                previous_errors = [
                    VerifyIssue(
                        rule="backend_failed",
                        message=f"后端状态 {backend_result.status.value}: "
                        f"{backend_result.error or '无详情'}",
                    )
                ]
                continue
            answer_path = packet.output_dir / "answer.md"
            result_path = packet.output_dir / "result.json"
            if not answer_path.is_file() or not result_path.is_file():
                previous_errors = [
                    VerifyIssue(
                        rule="output_missing",
                        message="output/ 中缺少 answer.md 或 result.json",
                    )
                ]
                continue
            answer_text = answer_path.read_text(encoding="utf-8")
            result_text = result_path.read_text(encoding="utf-8")
            issues = verify_answer(answer_text, result_text, passages)
            if not issues:
                return AskOutcome(
                    question=question,
                    answer=answer_text,
                    result=AnswerResult.model_validate_json(result_text),
                    passages=passages,
                    attempts=attempt,
                )
            previous_errors = issues

        # 修复循环耗尽：不输出未通过校验的答案（与 S4 页面同一原则）
        return AskOutcome(
            question=question,
            answer="生成答案未通过出处校验，已拒绝输出。请重试或缩小问题范围。",
            result=AnswerResult(question=question, answer_type="refused"),
            passages=passages,
            attempts=self._max_rounds,
            refused=True,
        )


def _to_passages(hits: list[SearchHit]) -> list[SourcePassage]:
    passages: list[SourcePassage] = []
    for idx, hit in enumerate(hits, start=1):
        text = hit.chunk.text
        if len(text) > _MAX_PASSAGE_CHARS:
            text = text[:_MAX_PASSAGE_CHARS] + "\n…（截断）"
        passages.append(
            SourcePassage(
                sid=f"S{idx}",
                reference=hit.reference,
                trust_tier=hit.chunk.trust_tier,
                text=text,
            )
        )
    return passages


def verify_answer(
    answer_text: str, result_text: str, passages: list[SourcePassage]
) -> list[VerifyIssue]:
    """回答校验（10.6.3 裁剪版）：引用闭合 + high 置信度双来源。"""
    issues: list[VerifyIssue] = []
    known = {p.sid for p in passages}

    try:
        result = AnswerResult.model_validate_json(result_text)
    except ValueError as exc:
        return [
            VerifyIssue(
                rule="result_json_invalid",
                message=f"result.json 不符合契约: {exc}",
                suggestion="按 schema 重写 result.json",
            )
        ]

    cited_in_answer = {f"S{n}" for n in _CITATION.findall(answer_text)}
    for sid in sorted(cited_in_answer - known):
        issues.append(
            VerifyIssue(
                rule="citation_not_in_passages",
                message=f"answer.md 引用了不存在的资料 [{sid}]",
                suggestion="只引用 passages.json 中列出的 S 编号",
            )
        )
    for sid in sorted(set(result.citations) - known):
        issues.append(
            VerifyIssue(
                rule="result_citation_not_in_passages",
                message=f"result.json citations 引用了不存在的资料 [{sid}]",
            )
        )
    if result.answer_type != "refused" and not cited_in_answer:
        issues.append(
            VerifyIssue(
                rule="no_citation",
                message="正文没有任何 [S] 引用，回答无出处",
                suggestion="每个事实后标注 [S编号]",
            )
        )
    if result.confidence == "high" and len(set(result.citations)) < 2:
        issues.append(
            VerifyIssue(
                rule="high_confidence_needs_two_sources",
                message="confidence=high 需要至少两个独立来源",
                suggestion="补充引用或把 confidence 降为 medium",
            )
        )
    return [i for i in issues if i.severity == "error"]


def _build_ask_packet(
    base: Path,
    repo_id: str,
    question: str,
    passages: list[SourcePassage],
    attempt: int,
    previous_errors: list[VerifyIssue] | None,
) -> TaskPacket:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    job_dir = base / "wiki" / repo_id / "jobs" / f"{stamp}-ask-a{attempt}"
    (job_dir / "context").mkdir(parents=True, exist_ok=True)
    (job_dir / "output").mkdir(parents=True, exist_ok=True)
    (job_dir / "logs").mkdir(parents=True, exist_ok=True)

    task_md = [
        "# 任务：回答用户问题",
        "",
        f"问题：{question}",
        "",
        "## 输出",
        "- output/answer.md：先直接答案，再依据；每个事实后标注 [S编号]。",
        "- output/result.json：question/answer_type/citations/confidence/unsupported/followups。",
    ]
    if previous_errors:
        task_md += ["", "## 上一次输出的问题（只修这些问题）"]
        for idx, issue in enumerate(previous_errors, start=1):
            hint = f" 建议：{issue.suggestion}" if issue.suggestion else ""
            task_md.append(f"{idx}. [{issue.rule}] {issue.message}{hint}")
    (job_dir / "task.md").write_text("\n".join(task_md) + "\n", encoding="utf-8")
    (job_dir / "constraints.md").write_text(_ASK_CONSTRAINTS, encoding="utf-8")
    (job_dir / "context" / "question.txt").write_text(question, encoding="utf-8")
    (job_dir / "context" / "passages.json").write_text(
        json.dumps([p.model_dump(mode="json") for p in passages], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if previous_errors:
        (job_dir / "errors.json").write_text(
            json.dumps(
                [i.model_dump(mode="json") for i in previous_errors],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return TaskPacket(
        workspace=job_dir,
        task_file=job_dir / "task.md",
        output_dir=job_dir / "output",
        timeout_s=1800,
        metadata={"kind": "ask", "repo_id": repo_id, "attempt": str(attempt)},
    )


__all__ = ["AnswerPipeline", "verify_answer"]
