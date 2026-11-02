"""S9 评测基线领域模型（计划文档 10.9，架构文档 §16.7）。

金标准题、单题结果、评测报告。全部 Pydantic v2，extra="forbid"，frozen。
指标口径见任务卡 S9-eval-baseline.md。
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import Field, model_validator

from codingwiki.domain.wiki import _WikiModel


class GoldenQuestion(_WikiModel):
    """一道金标准题。检索题必须给 expect_any；拒答题必须不给。"""

    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    expect_refused: bool = False
    expect_any: tuple[str, ...] = ()  # page_id 精确 / 文件路径（以 / 结尾按前缀）
    difficulty: str = "medium"  # easy / medium / hard
    note: str = ""

    @model_validator(mode="after")
    def _expectations_consistent(self) -> GoldenQuestion:
        if self.expect_refused and self.expect_any:
            raise ValueError(f"{self.id}: 拒答题不能标注 expect_any")
        if not self.expect_refused and not self.expect_any:
            raise ValueError(f"{self.id}: 检索题必须标注 expect_any")
        return self


class GoldenSet(_WikiModel):
    """一份版本化金标准集（只增不改，改则升 set_id）。"""

    set_id: str = Field(min_length=1)  # 如 cuphy-v1
    repo_id: str = Field(min_length=1)
    description: str = ""
    questions: list[GoldenQuestion] = Field(min_length=1)

    @model_validator(mode="after")
    def _ids_unique(self) -> GoldenSet:
        ids = [q.id for q in self.questions]
        if len(ids) != len(set(ids)):
            dup = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"题目 id 重复: {', '.join(dup)}")
        return self


class QuestionResult(_WikiModel):
    """单题结果。"""

    question_id: str
    question: str
    expect_refused: bool
    refused: bool  # AnswerPipeline 的实际判定
    first_hit_rank: int = Field(default=0, ge=0)  # 0 = 检索题未命中；拒答题恒 0
    ndcg: float = Field(default=0.0, ge=0.0, le=1.0)
    citation_total: int = Field(default=0, ge=0)
    citation_valid: int = Field(default=0, ge=0)


class EvalReport(_WikiModel):
    """一次评测的完整报告（.codingwiki/evals/<repo>/<ts>-<set_id>.json）。"""

    set_id: str
    repo_id: str
    index_model: str = ""  # 嵌入模型全名，如 bge-m3-d1024
    snapshot_id: str = ""
    top_k: int = Field(ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    question_count: int = Field(ge=1)
    # 检索指标（只统计检索题）
    recall_at_k: float | None = None
    mrr: float | None = None
    ndcg_at_k: float | None = None
    # 问答指标
    refusal_correct: int = Field(default=0, ge=0)  # 拒答题正确拒答数
    refusal_total: int = Field(default=0, ge=0)
    false_refusal: int = Field(default=0, ge=0)  # 可答题误拒数
    answerable_total: int = Field(default=0, ge=0)
    refusal_accuracy: float | None = None  # (refusal_correct + 无误拒可答题) / 总题数
    citation_valid_rate: float | None = None
    per_question: list[QuestionResult] = Field(default_factory=list)

    @property
    def misses(self) -> list[QuestionResult]:
        """检索未命中的题（错误分析入口，计划 10.9.3）。"""
        return [q for q in self.per_question if not q.expect_refused and q.first_hit_rank == 0]

    @property
    def wrong_refusals(self) -> list[QuestionResult]:
        """拒答判定错误的题。"""
        return [q for q in self.per_question if q.refused != q.expect_refused]


__all__ = ["EvalReport", "GoldenQuestion", "GoldenSet", "QuestionResult"]
