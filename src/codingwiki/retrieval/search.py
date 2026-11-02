"""混合检索（计划文档 10.5，架构文档 v0.6 14.4 / 15.4）。

BM25 与向量各自取 Top N，RRF（k=60）融合；查询中的标识符精确命中加分；
同分按信任层级 T1 > T2 > T3。启用重排时，对融合后的候选池做 cross-encoder 重排。
搜索模式全程不调用生成模型。
"""

from __future__ import annotations

import math

from codingwiki.domain.rag import TIER_ORDER, Chunk, SearchFilters, SearchHit
from codingwiki.retrieval.bm25 import Bm25Index
from codingwiki.retrieval.embed import EmbeddingBackend, cosine
from codingwiki.retrieval.rerank import RerankerBackend
from codingwiki.retrieval.tokenize import identifiers

_RRF_K = 60
_EXACT_IDENTIFIER_BOOST = 0.05  # 查询标识符在块文本中原样出现（大小写敏感前已归一）
# 无语义模型（哈希）时的噪声统计上限系数，见 min_vector_score()
_HASH_NOISE_MARGIN = 1.5
# 真实语义模型的标定拒答门槛（cosine 低于此值视为不相关）。
# S9-b 按 cuphy-v2 金标准集（56 问，bge-m3，27,640 块）实测分布标定：
# 拒答题 Top1 向量分上界 0.5907（R01，另有 1 道 hard 例 R05=0.665 不可分），
# 可答题下界 0.5740（Q23）——能同时拒掉 R01 且放过 Q06 的分隔区间为
# (0.5907, 0.5999)，取中点 0.595：拒答正确 15/16、可答题误拒 2/40（Q23/Q29）。
_DEFAULT_SEMANTIC_MIN_SCORE = 0.595


class HybridSearcher:
    """对已加载的索引做混合检索。"""

    def __init__(
        self,
        chunks: list[Chunk],
        vectors: list[list[float]],
        embedder: EmbeddingBackend,
        bm25: Bm25Index | None = None,
        *,
        reranker: RerankerBackend | None = None,
        rerank_pool: int = 20,
        min_vector_score: float | None = None,
    ) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks 与 vectors 长度不一致")
        self._chunks = chunks
        self._vectors = vectors
        self._embedder = embedder
        self._reranker = reranker
        self._rerank_pool = max(1, rerank_pool)
        self._min_score_override = min_vector_score
        # 文本 = 标题 + 正文，标题命中（符号名/页面标题）应更强
        texts = [f"{c.title}\n{c.text}" for c in chunks]
        self._bm25 = bm25 or Bm25Index(texts)
        self._texts_lower = [t.lower() for t in texts]

    @property
    def embedder_name(self) -> str:
        """嵌入模型全名（评测报告与索引元数据对账用）。"""
        return self._embedder.name

    def search(
        self,
        query: str,
        top_k: int = 10,
        per_channel: int = 50,
        filters: SearchFilters | None = None,
    ) -> list[SearchHit]:
        if not query.strip() or not self._chunks:
            return []

        # 范围过滤（§16.3-2）：在候选阶段就生效——BM25 与向量两路都只在
        # 允许的块里取 Top N，否则过滤会退化成「事后丢弃」。
        allowed: frozenset[int] | None = None
        if filters is not None and not filters.is_empty():
            allowed = frozenset(self._filter_indexes(filters))
            if not allowed:
                return []

        bm25_hits = self._bm25.query(query, top_n=per_channel)
        if allowed is not None:
            bm25_hits = [(i, s) for i, s in bm25_hits if i in allowed]
        bm25_rank = _competition_ranks(bm25_hits)

        query_vec = self._embedder.embed([query])[0]
        sims = [(i, cosine(query_vec, vec)) for i, vec in enumerate(self._vectors)]
        sims = [x for x in sims if x[1] > 0.0]
        sims.sort(key=lambda x: (-x[1], x[0]))
        if allowed is not None:
            sims = [x for x in sims if x[0] in allowed]
        top_sims = sims[:per_channel]
        vector_rank = _competition_ranks(top_sims)
        vector_score = {idx: s for idx, s in top_sims}

        query_idents = {w.lower() for w in identifiers(query)}
        candidate_ids = set(bm25_rank) | set(vector_rank)
        hits: list[SearchHit] = []
        for idx in candidate_ids:
            score = 0.0
            if idx in bm25_rank:
                score += 1.0 / (_RRF_K + bm25_rank[idx])
            if idx in vector_rank:
                score += 1.0 / (_RRF_K + vector_rank[idx])
            if query_idents and any(w in self._texts_lower[idx] for w in query_idents):
                score += _EXACT_IDENTIFIER_BOOST * sum(
                    1 for w in query_idents if w in self._texts_lower[idx]
                )
            hits.append(
                SearchHit(
                    chunk=self._chunks[idx],
                    score=score,
                    rrf_score=score,
                    bm25_rank=bm25_rank.get(idx, 0),
                    vector_rank=vector_rank.get(idx, 0),
                    vector_score=vector_score.get(idx, 0.0),
                )
            )
        hits.sort(key=lambda h: (-h.score, TIER_ORDER.get(h.chunk.trust_tier, 9), h.chunk.chunk_id))
        if self._reranker is None or not hits:
            return hits[:top_k]
        return self._rerank(query, hits, top_k)

    def _filter_indexes(self, filters: SearchFilters) -> list[int]:
        """按过滤条件算出允许参与检索的块下标；六个条件取交集。"""
        source_types = set(filters.source_types)
        trust_tiers = set(filters.trust_tiers)
        page_ids = set(filters.page_ids)
        matched: list[int] = []
        for idx, chunk in enumerate(self._chunks):
            if filters.module and chunk.module != filters.module:
                continue
            if filters.file_prefix and not chunk.file.startswith(filters.file_prefix):
                continue
            if source_types and chunk.source_type not in source_types:
                continue
            if trust_tiers and chunk.trust_tier not in trust_tiers:
                continue
            if filters.snapshot_id and chunk.snapshot_id != filters.snapshot_id:
                continue
            if page_ids and chunk.page_id not in page_ids:
                continue
            matched.append(idx)
        return matched

    def _rerank(self, query: str, hits: list[SearchHit], top_k: int) -> list[SearchHit]:
        """对融合后的候选池做 cross-encoder 重排；池外的结果不参与最终排序。"""
        reranker = self._reranker
        if reranker is None:  # pragma: no cover - 由 search() 保证
            return hits[:top_k]
        pool = hits[: max(self._rerank_pool, top_k)]
        scores = reranker.score(query, [hit.chunk.text for hit in pool]) if pool else []
        reranked = [
            hit.model_copy(update={"score": score, "rerank_score": score})
            for hit, score in zip(pool, scores, strict=True)
        ]
        reranked.sort(
            key=lambda h: (-h.score, TIER_ORDER.get(h.chunk.trust_tier, 9), h.chunk.chunk_id)
        )
        return reranked[:top_k]

    def min_vector_score(self, margin: float = _HASH_NOISE_MARGIN) -> float:
        """向量通道单独支撑结果的最低余弦（问答拒答门槛）。

        哈希嵌入无语义能力，无关文本的余弦是噪声：单次约 N(0, 1/√dim)，
        N 个切块取 max 后漂到 √(2·ln N)/√dim（3 万块、dim=256 时约 0.28）。
        固定阈值不可能跨规模有效，故按当前索引规模计算并留 margin。
        真实语义模型下相似度由语义决定，改用标定值（架构文档 v0.6 15.5）。
        """
        if self._min_score_override is not None:
            return self._min_score_override
        if not self._embedder.name.startswith("hash-embed"):
            return _DEFAULT_SEMANTIC_MIN_SCORE
        n = max(len(self._chunks), 2)
        noise_max = math.sqrt(2.0 * math.log(n)) / math.sqrt(self._embedder.dim)
        return margin * noise_max

    def is_refusal(self, top: SearchHit | None) -> bool:
        """Top1 命中是否不足以支撑回答（拒答判定，架构 §15.5 v0.8 两档规则）。

        哈希后端（无语义）：双条件——BM25 也无命中**且**向量分低于噪声门槛才拒答。
        哈希向量分本身是噪声，可答题的向量分同样过不了门槛，只看向量会全部误拒。
        语义后端（bge-m3 等）：单条件——Top1 向量分低于标定阈值即拒答。
        bge-m3 下领域外问题仍常因词面重叠产生 BM25 命中（S9 实测拒答题 BM25 rank
        0–25 与可答题不可分），双条件的第一条恒不成立导致全部漏拒。
        """
        if top is None:
            return True
        if self._embedder.name.startswith("hash-embed"):
            return top.bm25_rank == 0 and top.vector_score < self.min_vector_score()
        return top.vector_score < self.min_vector_score()


def _competition_ranks(hits: list[tuple[int, float]]) -> dict[int, int]:
    """竞赛排名：同分同 rank。否则相同内容的块会因列表顺序拿不同 rank，层级破平失效。"""
    ranks: dict[int, int] = {}
    rank = 0
    prev_score: float | None = None
    for position, (idx, score) in enumerate(hits, start=1):
        if prev_score is None or score < prev_score:
            rank = position
            prev_score = score
        ranks[idx] = rank
    return ranks


__all__ = ["HybridSearcher"]
