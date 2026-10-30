"""BM25 测试（架构文档 v0.5 14.2）。"""

from __future__ import annotations

from codingwiki.retrieval import Bm25Index

DOCS = [
    "cuda kernel launch 核函数启动",
    "python test helper 测试辅助",
    "memory allocation 显存分配",
]


def test_query_ranks_matching_doc_first() -> None:
    hits = Bm25Index(DOCS).query("kernel")
    assert hits
    assert hits[0][0] == 0
    assert hits[0][1] > 0.0


def test_zero_score_docs_excluded() -> None:
    hits = Bm25Index(DOCS).query("不存在的词元")
    assert hits == []


def test_empty_index() -> None:
    index = Bm25Index([])
    assert len(index) == 0
    assert index.vocab_size == 0
    assert index.query("anything") == []


def test_rare_term_beats_common_term() -> None:
    docs = ["common common rare", "common common", "common common"]
    hits = Bm25Index(docs).query("common rare")
    assert hits[0][0] == 0  # 含稀有词的文档排第一


def test_identifier_query_matches_split_tokens() -> None:
    docs = ["int cudaLaunchKernel(void);", "int main();"]
    hits = Bm25Index(docs).query("launch")
    assert hits[0][0] == 0


def test_cjk_query_matches_bigram() -> None:
    hits = Bm25Index(DOCS).query("显存")
    assert hits[0][0] == 2


def test_top_n_limits_results() -> None:
    docs = [f"shared term doc{i}" for i in range(10)]
    hits = Bm25Index(docs).query("shared", top_n=3)
    assert len(hits) == 3
