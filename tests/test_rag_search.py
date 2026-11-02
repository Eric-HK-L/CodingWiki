"""混合检索测试（架构文档 v0.5 14.4）。"""

from __future__ import annotations

from codingwiki.domain.rag import Chunk, SearchHit
from codingwiki.retrieval import HashEmbedding, HybridSearcher


def _chunk(chunk_id: str, title: str, text: str, tier: str = "code", file: str = "a.c") -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        source_type="code" if tier == "code" else "wiki",
        repo_id="r",
        snapshot_id="r@rev",
        file=file,
        title=title,
        text=text,
        start_line=1,
        end_line=5,
        trust_tier=tier,
        page_id=chunk_id if tier == "wiki" else "",
    )


def _searcher(chunks: list[Chunk]) -> HybridSearcher:
    embedder = HashEmbedding()
    texts = [f"{c.title}\n{c.text}" for c in chunks]
    return HybridSearcher(chunks, embedder.embed(texts), embedder)


def test_bm25_keyword_hit_ranked() -> None:
    chunks = [
        _chunk("c1", "helper", "int helper(int value);"),
        _chunk("c2", "engine", "class Engine 调度器"),
    ]
    hits = _searcher(chunks).search("helper")
    assert hits
    assert hits[0].chunk.chunk_id == "c1"
    assert hits[0].bm25_rank == 1
    assert hits[0].reference == "a.c:1-5"


def test_empty_query_returns_nothing() -> None:
    searcher = _searcher([_chunk("c1", "t", "x")])
    assert searcher.search("   ") == []


def test_empty_index_returns_nothing() -> None:
    embedder = HashEmbedding()
    searcher = HybridSearcher([], [], embedder)
    assert searcher.search("query") == []


def test_identifier_exact_hit_boosted() -> None:
    # 两个块都含 launch/kernel 词元；含完整标识符 cudaLaunchKernel 的被加权到第一
    chunks = [
        _chunk("c1", "launch", "launch kernel 的通用说明文档"),
        _chunk("c2", "cudaLaunchKernel", "cudaLaunchKernel 是核函数启动接口 launch kernel"),
    ]
    hits = _searcher(chunks).search("cudaLaunchKernel")
    assert hits[0].chunk.chunk_id == "c2"


def test_tie_break_prefers_higher_tier() -> None:
    # 完全相同的文本 → 各通道同分，按信任层级 wiki > code 破平
    text = "完全相同的正文内容"
    chunks = [
        _chunk("zz_code", "同一标题", text, tier="code"),
        _chunk("aa_wiki", "同一标题", text, tier="wiki"),
    ]
    hits = _searcher(chunks).search("同一标题")
    assert len(hits) == 2
    assert hits[0].chunk.trust_tier == "wiki"


def test_top_k_limits_output() -> None:
    chunks = [_chunk(f"c{i}", "共同标题", "shared body 共同内容") for i in range(10)]
    hits = _searcher(chunks).search("shared 共同", top_k=3)
    assert len(hits) == 3


def test_vector_channel_finds_lexically_distant_overlap() -> None:
    # 查询与块共享部分词元：BM25 与向量都应命中
    chunks = [
        _chunk("c1", "张量", "tensor descriptor 张量描述符"),
        _chunk("c2", "日志", "logging level 日志级别"),
    ]
    hits = _searcher(chunks).search("tensor 张量")
    assert hits[0].chunk.chunk_id == "c1"
    assert hits[0].vector_rank >= 1
    assert hits[0].vector_score > 0.0


def test_min_vector_score_scales_with_corpus_size() -> None:
    # 哈希噪声随 N 增长：门槛必须随规模上调，否则大库下无关查询被拒答失败
    small = _searcher([_chunk("c1", "t", "x")])
    big = _searcher([_chunk(f"c{i}", "t", "x") for i in range(30000)])
    assert big.min_vector_score() > small.min_vector_score()
    assert 0.0 < small.min_vector_score() < 1.0


# ---------------------------------------------------------------- 拒答判定两档规则


class _FakeSemanticEmbedder:
    """名字不带 hash-embed 的假语义嵌入：按内容给正交向量，分数可控。"""

    @property
    def name(self) -> str:
        return "fake-bge-d4"

    @property
    def dim(self) -> int:
        return 4

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [
            [1.0, 0.0, 0.0, 0.0] if "ldpc" in t.lower() else [0.0, 1.0, 0.0, 0.0] for t in texts
        ]


def test_is_refusal_semantic_single_condition() -> None:
    # 语义后端：单条件——向量分低于标定阈值即拒答，BM25 有命中也不例外
    # （S9 实测：领域外问题 BM25 仍常命中，双条件恒不成立）
    embedder = _FakeSemanticEmbedder()
    chunks = [_chunk("c1", "ldpc", "ldpc decoder ldpc")]
    vectors = embedder.embed([f"{c.title}\n{c.text}" for c in chunks])
    # 查询 "decoder"：BM25 命中块，但向量与块正交（cos=0）——复现 S9 漏拒场景
    searcher = HybridSearcher(chunks, vectors, embedder, min_vector_score=0.60)
    hits = searcher.search("decoder")
    assert hits and hits[0].bm25_rank >= 1  # BM25 有命中
    assert hits[0].vector_score < 0.60
    assert searcher.is_refusal(hits[0])  # 语义单条件 → 拒答

    # 查询 "ldpc"：向量 cos=1.0 过阈值 → 不拒答
    searcher2 = HybridSearcher(chunks, vectors, embedder, min_vector_score=0.60)
    assert not searcher2.is_refusal(searcher2.search("ldpc")[0])


def test_is_refusal_semantic_none_top() -> None:
    embedder = _FakeSemanticEmbedder()
    searcher = HybridSearcher([], [], embedder, min_vector_score=0.60)
    assert searcher.is_refusal(None)


def test_is_refusal_hash_keeps_dual_condition() -> None:
    # 哈希后端：双条件——BM25 有命中时即使向量分低于噪声门槛也不拒答
    # （哈希向量分是噪声，可答题同样过不了向量门槛，单看向量会全部误拒）
    chunks = [_chunk("c1", "ldpc", "ldpc decoder ldpc")]
    searcher = _searcher(chunks)
    hits = searcher.search("ldpc")
    assert hits and hits[0].bm25_rank >= 1
    assert not searcher.is_refusal(hits[0])

    # 双条件的另一半：无 BM25 命中且向量分低于门槛 → 拒答
    hit = SearchHit(chunk=chunks[0], score=0.0, bm25_rank=0, vector_rank=0, vector_score=0.0)
    assert searcher.is_refusal(hit)


class _FakeReranker:
    """把含 answer 的段落打到最高分，用于验证重排确实改变最终顺序。"""

    @property
    def name(self) -> str:
        return "fake-reranker"

    def score(self, query: str, passages: list[str]) -> list[float]:
        return [1.0 if "answer" in passage else 0.0 for passage in passages]


def test_reranker_reorders_fused_candidates() -> None:
    chunks = [
        _chunk("c1", "alpha alpha", "alpha alpha alpha 关键词密集但无答案"),
        _chunk("c2", "alpha answer", "alpha answer 命中重排"),
    ]
    embedder = HashEmbedding()
    texts = [f"{c.title}\n{c.text}" for c in chunks]
    searcher = HybridSearcher(
        chunks,
        embedder.embed(texts),
        embedder,
        reranker=_FakeReranker(),
        rerank_pool=5,
    )
    hits = searcher.search("alpha", top_k=2)
    assert hits[0].chunk.chunk_id == "c2"
    assert hits[0].rerank_score == 1.0
