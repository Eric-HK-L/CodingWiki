"""S6 检索后端注册表测试：模型解析、后端构造、索引模型名复原。"""

from __future__ import annotations

from pathlib import Path

import pytest

from codingwiki.errors import InputError
from codingwiki.retrieval.registry import (
    create_embedder,
    embedder_for_name,
    resolve_model_path,
)


def test_hash_embedder_default_dim() -> None:
    embedder = create_embedder("hash")
    assert embedder.name == "hash-embed-v1-d256"
    assert embedder.dim == 256


def test_hash_embedder_honours_explicit_dim() -> None:
    embedder = create_embedder("hash", dim=128)
    assert embedder.name == "hash-embed-v1-d128"
    assert embedder.dim == 128


def test_local_backend_alias_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 只验证别名归一，不实际加载权重（BgeM3Embedding 在首次 embed 才加载）
    model_dir = tmp_path / "bge-m3"
    model_dir.mkdir()
    monkeypatch.setenv("CODINGWIKI_MODEL_DIR", str(tmp_path))
    embedder = create_embedder("local")
    assert embedder.name == "bge-m3-d1024"
    assert embedder.dim == 1024


def test_unknown_embedding_backend_raises() -> None:
    with pytest.raises(InputError, match="未知嵌入后端"):
        create_embedder("sentence-transformers")


def test_embedder_for_hash_name_roundtrips() -> None:
    embedder = embedder_for_name("hash-embed-v1-d64")
    assert embedder.name == "hash-embed-v1-d64"
    assert embedder.dim == 64


def test_embedder_for_name_rejects_unknown_model() -> None:
    with pytest.raises(InputError, match="未知嵌入模型"):
        embedder_for_name("mystery-d1024")


def test_embedder_for_name_rejects_malformed_hash_name() -> None:
    with pytest.raises(InputError, match="无法解析"):
        embedder_for_name("hash-embed-v1-dabc")


def test_resolve_model_path_explicit_missing_raises(tmp_path: Path) -> None:
    missing = tmp_path / "no-model"
    with pytest.raises(InputError, match="模型路径不存在"):
        resolve_model_path("bge-m3", explicit=str(missing))


def test_resolve_model_path_uses_env_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model_dir = tmp_path / "bge-m3"
    model_dir.mkdir()
    monkeypatch.setenv("CODINGWIKI_MODEL_DIR", str(tmp_path))
    assert resolve_model_path("bge-m3") == model_dir


def test_resolve_model_path_explicit_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "explicit-model"
    explicit.mkdir()
    monkeypatch.setenv("CODINGWIKI_MODEL_DIR", str(tmp_path / "elsewhere"))
    assert resolve_model_path("bge-m3", explicit=str(explicit)) == explicit
