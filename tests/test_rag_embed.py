"""嵌入后端测试（架构文档 v0.5 14.3）。"""

from __future__ import annotations

import math

import pytest

from codingwiki.retrieval import HashEmbedding, cosine


def test_deterministic_same_text_same_vector() -> None:
    embedder = HashEmbedding()
    assert embedder.embed(["cuda kernel"]) == embedder.embed(["cuda kernel"])


def test_output_is_l2_normalized() -> None:
    vec = HashEmbedding().embed(["cuda kernel launch"])[0]
    norm = sum(v * v for v in vec) ** 0.5
    assert norm == pytest.approx(1.0)


def test_empty_text_gives_zero_vector() -> None:
    vec = HashEmbedding().embed([""])[0]
    assert all(v == 0.0 for v in vec)
    assert cosine(vec, vec) == 0.0


def test_shared_tokens_give_positive_cosine() -> None:
    embedder = HashEmbedding()
    a, b = embedder.embed(["cuda kernel launch", "cuda driver api"])
    assert cosine(a, b) > 0.0


def test_unrelated_texts_lower_than_identical() -> None:
    embedder = HashEmbedding()
    a, b, c = embedder.embed(["cuda kernel launch", "python pytest fixture", "cuda kernel launch"])
    assert cosine(a, c) > cosine(a, b)
    assert cosine(a, c) == pytest.approx(1.0)


def test_name_and_dim() -> None:
    embedder = HashEmbedding(dim=128)
    assert embedder.dim == 128
    assert embedder.name == "hash-embed-v1-d128"
    assert len(embedder.embed(["x"])[0]) == 128


def test_invalid_dim_rejected() -> None:
    with pytest.raises(ValueError, match="dim"):
        HashEmbedding(dim=0)


def test_cosine_rejects_mismatched_length() -> None:
    with pytest.raises(ValueError):
        cosine([1.0], [1.0, 2.0])


def test_hash_uses_full_token_set() -> None:
    # 词元顺序不影响向量（词袋模型）
    embedder = HashEmbedding()
    a, b = embedder.embed(["alpha beta", "beta alpha"])
    assert math.isclose(cosine(a, b), 1.0, rel_tol=1e-6)
