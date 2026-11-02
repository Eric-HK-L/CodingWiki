"""SearchFilters 与检索范围过滤测试（S7 §16.3-2）。"""

from __future__ import annotations

import pytest

from codingwiki.domain.rag import Chunk, SearchFilters
from codingwiki.retrieval import HashEmbedding, HybridSearcher


def _chunk(  # noqa: PLR0913
    chunk_id: str,
    text: str,
    *,
    module: str = "",
    file: str = "",
    source_type: str = "code",
    trust_tier: str = "code",
    snapshot_id: str = "r@rev",
    page_id: str = "",
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        source_type=source_type,
        repo_id="r",
        snapshot_id=snapshot_id,
        file=file,
        module=module,
        title=chunk_id,
        text=text,
        start_line=1,
        end_line=5,
        trust_tier=trust_tier,
        page_id=page_id,
    )


def _searcher(chunks: list[Chunk]) -> HybridSearcher:
    embedder = HashEmbedding()
    texts = [f"{c.title}\n{c.text}" for c in chunks]
    return HybridSearcher(chunks, embedder.embed(texts), embedder)


_CORE = _chunk("core", "tensor descriptor 张量描述符", module="core", file="src/core.c")
_APP = _chunk("app", "tensor descriptor 张量描述符", module="app", file="src/app/main.c")
_WIKI = _chunk("wiki", "tensor descriptor 张量描述符", source_type="wiki", trust_tier="wiki")


def test_default_filters_behave_like_no_filters() -> None:
    chunks = [_CORE, _APP]
    searcher = _searcher(chunks)
    plain = searcher.search("tensor")
    filtered = searcher.search("tensor", filters=SearchFilters())
    assert [h.chunk.chunk_id for h in plain] == [h.chunk.chunk_id for h in filtered]
    assert SearchFilters().is_empty() is True


def test_filter_by_module() -> None:
    searcher = _searcher([_CORE, _APP])
    hits = searcher.search("tensor", filters=SearchFilters(module="app"))
    assert [h.chunk.chunk_id for h in hits] == ["app"]


def test_filter_by_file_prefix() -> None:
    searcher = _searcher([_CORE, _APP])
    hits = searcher.search("tensor", filters=SearchFilters(file_prefix="src/app/"))
    assert [h.chunk.chunk_id for h in hits] == ["app"]


def test_filter_by_source_type_and_trust_tier() -> None:
    searcher = _searcher([_CORE, _WIKI])
    hits = searcher.search(
        "tensor", filters=SearchFilters(source_types=("wiki",), trust_tiers=("wiki",))
    )
    assert [h.chunk.chunk_id for h in hits] == ["wiki"]


def test_filter_by_snapshot_id_excludes_other_snapshot() -> None:
    other = _chunk("old", "tensor descriptor", snapshot_id="r@old")
    searcher = _searcher([other, _CORE])
    hits = searcher.search("tensor", filters=SearchFilters(snapshot_id="r@rev"))
    assert [h.chunk.chunk_id for h in hits] == ["core"]
    assert searcher.search("tensor", filters=SearchFilters(snapshot_id="r@none")) == []


def test_filter_by_page_ids() -> None:
    page_chunk = _chunk(
        "p1", "tensor descriptor", source_type="wiki", trust_tier="wiki", page_id="module/core"
    )
    searcher = _searcher([page_chunk, _CORE])
    hits = searcher.search("tensor", filters=SearchFilters(page_ids=("module/core",)))
    assert [h.chunk.chunk_id for h in hits] == ["p1"]


def test_combined_filters_intersect() -> None:
    # module=core 与 file_prefix=src/app/ 交集为空 → 无结果
    searcher = _searcher([_CORE, _APP])
    filters = SearchFilters(module="core", file_prefix="src/app/")
    assert searcher.search("tensor", filters=filters) == []


def test_filter_applies_to_both_channels() -> None:
    # app 块在 BM25 与向量里都排第一，但被 module 过滤掉：
    # 过滤必须在候选阶段生效，而不是事后丢弃 → 返回的是 core 块
    searcher = _searcher([_CORE, _APP])
    hits = searcher.search("tensor", top_k=2, filters=SearchFilters(module="core"))
    assert len(hits) == 1
    assert hits[0].chunk.chunk_id == "core"
    assert hits[0].bm25_rank >= 1 and hits[0].vector_rank >= 1


def test_filters_reject_unknown_fields() -> None:
    with pytest.raises(ValueError):
        SearchFilters(ghost=1)  # type: ignore[call-arg]


def test_search_cli_exposes_filter_flags() -> None:
    from codingwiki.cli import build_parser

    args = build_parser().parse_args(
        ["search", "--repo", "r", "--query", "q", "--module", "core", "--file-prefix", "src/"]
    )
    assert args.module == "core"
    assert args.file_prefix == "src/"
