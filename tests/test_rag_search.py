"""混合检索测试（架构文档 v0.5 14.4）。"""

from __future__ import annotations

from codingwiki.domain.rag import Chunk
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
