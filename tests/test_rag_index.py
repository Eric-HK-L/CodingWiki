"""索引存储测试（架构文档 v0.5 14.6）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from codingwiki.domain.rag import Chunk
from codingwiki.errors import InputError
from codingwiki.retrieval import FilesystemIndexStore


def _chunk(chunk_id: str, text: str = "正文") -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        source_type="code",
        repo_id="sample",
        snapshot_id="sample@rev1234567890",
        file="src/a.c",
        text=text,
        start_line=1,
        end_line=2,
        trust_tier="code",
    )


def _save(store: FilesystemIndexStore, chunks: list[Chunk] | None = None) -> str:
    chunks = chunks or [_chunk("c1"), _chunk("c2")]
    vectors = [[1.0, 0.0], [0.0, 1.0]][: len(chunks)]
    meta = store.save(chunks, vectors, "hash-embed-v1-d2", 2, 10)
    return meta.index_id


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    store = FilesystemIndexStore("sample", tmp_path)
    index_id = _save(store)
    meta, chunks, vectors = store.load()
    assert meta.index_id == index_id
    assert meta.repo_id == "sample"
    assert meta.snapshot_id == "sample@rev1234567890"
    assert meta.dim == 2
    assert meta.chunk_count == 2
    assert meta.vocab_size == 10
    assert [c.chunk_id for c in chunks] == ["c1", "c2"]
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert store.current_version() == index_id
    assert index_id in store.list_versions()


def test_save_rejects_mismatched_lengths(tmp_path: Path) -> None:
    store = FilesystemIndexStore("sample", tmp_path)
    with pytest.raises(InputError, match="不一致"):
        store.save([_chunk("c1")], [[1.0], [2.0]], "m", 1, 0)


def test_save_rejects_empty_chunks(tmp_path: Path) -> None:
    store = FilesystemIndexStore("sample", tmp_path)
    with pytest.raises(InputError, match="空索引"):
        store.save([], [], "m", 1, 0)


def test_save_rejects_duplicate_chunk_ids(tmp_path: Path) -> None:
    store = FilesystemIndexStore("sample", tmp_path)
    with pytest.raises(InputError, match="重复"):
        store.save([_chunk("c1"), _chunk("c1")], [[1.0], [2.0]], "m", 1, 0)


def test_load_without_index_raises(tmp_path: Path) -> None:
    store = FilesystemIndexStore("sample", tmp_path)
    assert store.current_version() is None
    with pytest.raises(InputError, match="index build"):
        store.load()


def test_load_unknown_version_raises(tmp_path: Path) -> None:
    store = FilesystemIndexStore("sample", tmp_path)
    _save(store)
    with pytest.raises(InputError, match="不存在"):
        store.load("i20990101000000-deadbeef")


def test_load_detects_corrupted_vectors(tmp_path: Path) -> None:
    store = FilesystemIndexStore("sample", tmp_path)
    index_id = _save(store)
    vectors_bin = tmp_path / "indices" / "sample" / index_id / "vectors.bin"
    vectors_bin.write_bytes(vectors_bin.read_bytes()[:4])  # 截断
    with pytest.raises(InputError, match="索引损坏"):
        store.load()


def test_multiple_versions_keep_old_and_switch_current(tmp_path: Path) -> None:
    store = FilesystemIndexStore("sample", tmp_path)
    first = _save(store)
    second = _save(store, [_chunk("c3", "新文本")])
    assert store.current_version() == second
    assert store.list_versions() == sorted([first, second])
    # 旧版本仍可显式加载（可回滚）
    meta, chunks, _ = store.load(first)
    assert meta.index_id == first
    assert chunks[0].chunk_id == "c1"
