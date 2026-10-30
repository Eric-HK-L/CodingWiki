"""混合检索（计划文档 10.5，架构文档 v0.5 14.4）。

BM25 与向量各自取 Top N，RRF（k=60）融合；查询中的标识符精确命中加分；
同分按信任层级 T1 > T2 > T3。搜索模式全程不调用模型。
"""

from __future__ import annotations

import math

from codingwiki.domain.rag import TIER_ORDER, Chunk, SearchHit
from codingwiki.retrieval.bm25 import Bm25Index
from codingwiki.retrieval.embed import EmbeddingBackend, cosine
from codingwiki.retrieval.tokenize import identifiers

_RRF_K = 60
_EXACT_IDENTIFIER_BOOST = 0.05  # 查询标识符在块文本中原样出现（大小写敏感前已归一）


class HybridSearcher:
    """对已加载的索引做混合检索。"""

    def __init__(
        self,
        chunks: list[Chunk],
        vectors: list[list[float]],
        embedder: EmbeddingBackend,
        bm25: Bm25Index | None = None,
    ) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks 与 vectors 长度不一致")
        self._chunks = chunks
        self._vectors = vectors
        self._embedder = embedder
        # 文本 = 标题 + 正文，标题命中（符号名/页面标题）应更强
        texts = [f"{c.title}\n{c.text}" for c in chunks]
        self._bm25 = bm25 or Bm25Index(texts)
        self._texts_lower = [t.lower() for t in texts]

    def search(self, query: str, top_k: int = 10, per_channel: int = 50) -> list[SearchHit]:
        if not query.strip() or not self._chunks:
            return []

        bm25_hits = self._bm25.query(query, top_n=per_channel)
        bm25_rank = _competition_ranks(bm25_hits)

        query_vec = self._embedder.embed([query])[0]
        sims = [(i, cosine(query_vec, vec)) for i, vec in enumerate(self._vectors)]
        sims = [x for x in sims if x[1] > 0.0]
        sims.sort(key=lambda x: (-x[1], x[0]))
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
                    bm25_rank=bm25_rank.get(idx, 0),
                    vector_rank=vector_rank.get(idx, 0),
                    vector_score=vector_score.get(idx, 0.0),
                )
            )
        hits.sort(key=lambda h: (-h.score, TIER_ORDER.get(h.chunk.trust_tier, 9), h.chunk.chunk_id))
        return hits[:top_k]

    def min_vector_score(self, margin: float = 1.5) -> float:
        """向量通道单独支撑结果的最低余弦（问答拒答门槛）。

        HashEmbedding 无语义能力，无关文本的余弦是噪声：单次约 N(0, 1/√dim)，
        N 个切块取 max 后漂到 √(2·ln N)/√dim（3 万块、dim=256 时约 0.28）。
        固定阈值不可能跨规模有效，故按当前索引规模计算并留 margin。
        接入真实嵌入模型后需按模型重新标定。
        """
        n = max(len(self._chunks), 2)
        noise_max = math.sqrt(2.0 * math.log(n)) / math.sqrt(self._embedder.dim)
        return margin * noise_max


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
