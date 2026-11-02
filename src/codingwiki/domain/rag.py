"""RAG 领域模型（计划文档第 10 章，架构文档 v0.5 第 14 节）。

切块、索引元数据、检索命中、问答结果。全部 Pydantic v2，extra="forbid"，frozen。
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import Field

from codingwiki.domain.wiki import _WikiModel

# 信任层级（10.2）：T1 Wiki 页 > T2 事实 > T3 代码块
TIER_ORDER = {"wiki": 0, "fact": 1, "code": 2}


class Chunk(_WikiModel):
    """一个可检索的文本块。chunk_id 由来源确定性生成，同一快照重建一致。"""

    chunk_id: str = Field(min_length=1)
    source_type: str  # code / wiki / build / config / test
    repo_id: str
    snapshot_id: str
    file: str = ""  # 代码/事实块为仓库相对路径；wiki 块为空
    symbol: str = ""
    module: str = ""
    title: str = ""  # wiki 块为标题路径；代码块为限定名
    text: str = Field(min_length=1)
    start_line: int = Field(default=0, ge=0)
    end_line: int = Field(default=0, ge=0)
    trust_tier: str  # wiki / fact / code
    page_id: str = ""  # wiki 块所属页面

    @property
    def reference(self) -> str:
        """人类可读的出处。"""
        if self.trust_tier == "wiki":
            return f"wiki:{self.page_id}"
        if self.start_line > 0:
            return f"{self.file}:{self.start_line}-{self.end_line}"
        return self.file


class SearchFilters(_WikiModel):
    """检索范围过滤（架构文档 v0.7 §16.3-2）。

    全部字段缺省 = 不过滤（行为与 S5 一致）。二三阶段用它们做
    「只找某模块的影响面」「只看允许修改的文件」与 ACL 的 allowed_modules。
    """

    module: str = ""  # Chunk.module 精确匹配
    file_prefix: str = ""  # Chunk.file 前缀匹配（wiki 块 file 为空，会被排除）
    source_types: tuple[str, ...] = ()  # code / wiki / build / config / test
    trust_tiers: tuple[str, ...] = ()  # wiki / fact / code
    snapshot_id: str = ""  # Chunk.snapshot_id 精确匹配
    page_ids: tuple[str, ...] = ()  # wiki 块所属页面

    def is_empty(self) -> bool:
        """没有任何过滤条件时为 True，检索层据此走原路径。"""
        return not (
            self.module
            or self.file_prefix
            or self.source_types
            or self.trust_tiers
            or self.snapshot_id
            or self.page_ids
        )


class IndexMeta(_WikiModel):
    """index.json：索引版本元数据，绑定快照与嵌入模型。"""

    index_id: str
    repo_id: str
    snapshot_id: str
    embedding_model: str
    dim: int = Field(ge=1)
    chunk_count: int = Field(ge=0)
    vocab_size: int = Field(ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SearchHit(_WikiModel):
    """一条检索结果。"""

    chunk: Chunk
    score: float
    bm25_rank: int = 0  # 0 表示未进入 BM25 榜单
    vector_rank: int = 0  # 0 表示未进入向量榜单
    vector_score: float = 0.0  # 原始余弦相似度（未入榜为 0）
    rrf_score: float = 0.0  # RRF 融合分（重排前的排序依据）
    rerank_score: float = 0.0  # cross-encoder 相关分（未重排为 0）

    @property
    def reference(self) -> str:
        return self.chunk.reference


class SourcePassage(_WikiModel):
    """问答上下文里的一段资料，编号 S1..Sn。"""

    sid: str = Field(pattern=r"^S\d+$")
    reference: str
    trust_tier: str
    text: str


class AnswerResult(_WikiModel):
    """output/result.json 的问答契约（10.6.2 裁剪版）。"""

    question: str
    answer_type: str = "answered"  # answered / partial / refused
    citations: list[str] = Field(default_factory=list)  # S 编号
    confidence: str = "low"  # high / medium / low
    unsupported: list[str] = Field(default_factory=list)
    followups: list[str] = Field(default_factory=list)


class AskOutcome(_WikiModel):
    """一次问答的最终结果（校验通过后返回给调用方）。"""

    question: str
    answer: str  # answer.md 正文
    result: AnswerResult
    passages: list[SourcePassage]
    attempts: int = Field(ge=0)
    refused: bool = False


__all__ = [
    "AnswerResult",
    "AskOutcome",
    "Chunk",
    "IndexMeta",
    "SearchFilters",
    "SearchHit",
    "SourcePassage",
    "TIER_ORDER",
]
