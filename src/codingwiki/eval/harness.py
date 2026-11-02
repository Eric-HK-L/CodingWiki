"""EvalHarness：跑金标准集、算指标、落报告、与上次对比（计划文档 855 行 / 10.9）。

检索指标（Recall@k / MRR / nDCG@k）基于 HybridSearcher 的原始排序；
拒答判定与引用闭合复用 AnswerPipeline / verify_answer 的现有规则，
评测不另写一套判定，避免基线失真。
"""

from __future__ import annotations

import math
import os
from datetime import UTC
from pathlib import Path

from codingwiki.domain.eval import EvalReport, GoldenSet, QuestionResult
from codingwiki.domain.rag import AskOutcome, Chunk
from codingwiki.retrieval.answer import AnswerPipeline
from codingwiki.retrieval.search import HybridSearcher


def _matches_expectation(file: str, page_id: str, expect_any: tuple[str, ...]) -> bool:
    """一条 chunk 是否命中期望资料：page_id 精确，或文件精确/前缀（以 / 结尾）。"""
    for expect in expect_any:
        if page_id and page_id == expect:
            return True
        if file and (file == expect or (expect.endswith("/") and file.startswith(expect))):
            return True
    return False


def _ndcg_binary(ranks: list[int], total_relevant: int, k: int) -> float:
    """二元相关性的 nDCG@k。ranks 是命中块在结果里的 1-based 名次（≤k）。

    理想排序是所有相关块排满前 k；相关块总数取整个索引里的真实数量。
    """
    if not ranks or total_relevant <= 0:
        return 0.0
    dcg = sum(1.0 / math.log2(r + 1) for r in ranks if r <= k)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(total_relevant, k) + 1))
    return dcg / ideal if ideal > 0 else 0.0


class EvalHarness:
    """对已加载的索引与问答流水线跑一份金标准集。"""

    def __init__(
        self,
        searcher: HybridSearcher,
        chunks: list[Chunk],
        pipeline: AnswerPipeline,
        *,
        known_pages: set[str],
        known_paths: set[str],
    ) -> None:
        self._searcher = searcher
        self._chunks = chunks
        self._pipeline = pipeline
        self._known_pages = known_pages
        self._known_paths = known_paths
        self._expect_cache: dict[tuple[str, ...], int] = {}

    def relevant_total(self, expect_any: tuple[str, ...]) -> int:
        """整个索引里与期望资料匹配的块数（nDCG 的 IDCG 用；缓存）。"""
        cached = self._expect_cache.get(expect_any)
        if cached is None:
            cached = sum(
                1 for c in self._chunks if _matches_expectation(c.file, c.page_id, expect_any)
            )
            self._expect_cache[expect_any] = cached
        return cached

    def run(self, golden: GoldenSet, *, top_k: int = 10) -> EvalReport:
        per_question: list[QuestionResult] = []
        recall_hits = 0
        answerable = 0
        rr_sum = 0.0
        ndcg_sum = 0.0
        refusal_correct = 0
        refusal_total = 0
        false_refusal = 0
        citation_total = 0
        citation_valid = 0

        for q in golden.questions:
            if q.expect_refused:
                refusal_total += 1
                outcome = self._pipeline.ask(golden.repo_id, q.question, top_k=top_k)
                if outcome.refused:
                    refusal_correct += 1
                per_question.append(
                    QuestionResult(
                        question_id=q.id,
                        question=q.question,
                        expect_refused=True,
                        refused=outcome.refused,
                    )
                )
                continue

            answerable += 1
            hits = self._searcher.search(q.question, top_k=top_k)
            hit_ranks = [
                rank
                for rank, hit in enumerate(hits, start=1)
                if _matches_expectation(hit.chunk.file, hit.chunk.page_id, q.expect_any)
            ]
            first = hit_ranks[0] if hit_ranks else 0
            if first:
                recall_hits += 1
                rr_sum += 1.0 / first
            ndcg = _ndcg_binary(hit_ranks, self.relevant_total(q.expect_any), top_k)
            ndcg_sum += ndcg

            outcome = self._pipeline.ask(golden.repo_id, q.question, top_k=top_k)
            if outcome.refused:
                false_refusal += 1
            total, valid = self._citation_stats(outcome)
            citation_total += total
            citation_valid += valid

            per_question.append(
                QuestionResult(
                    question_id=q.id,
                    question=q.question,
                    expect_refused=False,
                    refused=outcome.refused,
                    first_hit_rank=first,
                    ndcg=ndcg,
                    citation_total=total,
                    citation_valid=valid,
                )
            )

        return EvalReport(
            set_id=golden.set_id,
            repo_id=golden.repo_id,
            index_model=self._searcher.embedder_name,
            top_k=top_k,
            question_count=len(golden.questions),
            recall_at_k=(recall_hits / answerable) if answerable else None,
            mrr=(rr_sum / answerable) if answerable else None,
            ndcg_at_k=(ndcg_sum / answerable) if answerable else None,
            refusal_correct=refusal_correct,
            refusal_total=refusal_total,
            false_refusal=false_refusal,
            answerable_total=answerable,
            refusal_accuracy=(
                (refusal_correct + (answerable - false_refusal)) / len(golden.questions)
                if golden.questions
                else None
            ),
            citation_valid_rate=(citation_valid / citation_total) if citation_total else None,
            per_question=per_question,
        )

    def _citation_stats(self, outcome: AskOutcome) -> tuple[int, int]:
        """回答引用的出处有效率：S 编号闭合 + reference 指向真实页面/文件。"""
        passages = {p.sid: p.reference for p in outcome.passages}
        total = 0
        valid = 0
        for sid in sorted(set(outcome.result.citations)):
            total += 1
            reference = passages.get(sid)
            if reference is None:
                continue
            if reference.startswith("wiki:"):
                ok = reference[len("wiki:") :] in self._known_pages
            else:
                ok = reference.split(":", 1)[0] in self._known_paths
            if ok:
                valid += 1
        return total, valid


def save_report(report: EvalReport, repo_id: str) -> Path:
    """报告落盘 .codingwiki/evals/<repo>/<ts>-<set_id>.json，返回路径。"""
    base = Path(os.environ.get("CODINGWIKI_HOME", ".codingwiki")).resolve()
    report_dir = base / "evals" / repo_id
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.created_at.astimezone(UTC).strftime("%Y%m%d-%H%M%S")
    path = report_dir / f"{stamp}-{report.set_id}.json"
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_previous_report(repo_id: str, set_id: str) -> EvalReport | None:
    """读最近一次同 repo 同 set_id 的报告（按文件名时间戳排序取最新）。"""
    base = Path(os.environ.get("CODINGWIKI_HOME", ".codingwiki")).resolve()
    report_dir = base / "evals" / repo_id
    if not report_dir.is_dir():
        return None
    candidates = sorted(report_dir.glob(f"*-{set_id}.json"), key=lambda p: p.name)
    if not candidates:
        return None
    return EvalReport.model_validate_json(candidates[-1].read_text(encoding="utf-8"))


__all__ = ["EvalHarness", "load_previous_report", "save_report"]
