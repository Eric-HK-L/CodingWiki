"""嵌入/重排后端注册表（架构文档 v0.6 15.6）。

按模型名或配置构造后端；模型目录按「显式路径 → CODINGWIKI_MODEL_DIR →
常见本地目录」顺序解析。加载已有索引时用 `embedder_for_name` 复原同一后端，
保证查询向量与索引向量来自同一模型。
"""

from __future__ import annotations

import os
from pathlib import Path

from codingwiki.errors import InputError
from codingwiki.retrieval.embed import EmbeddingBackend, HashEmbedding
from codingwiki.retrieval.embed_bge import BgeM3Embedding, ProgressFn
from codingwiki.retrieval.rerank import BgeReranker, RerankerBackend

HASH_PREFIX = "hash-embed-v1-d"
BGE_M3_PREFIX = "bge-m3-d"

# 默认后端参数：hash 保持轻量离线，语义后端由显式参数开启
DEFAULT_HASH_DIM = 256
DEFAULT_BGE_DIM = 1024


def resolve_model_path(directory_name: str, explicit: str = "") -> Path:
    """解析模型目录。显式路径必须存在；否则依次尝试环境变量与常见本地目录。"""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_dir():
            raise InputError(f"模型路径不存在: {path}")
        return path

    tried: list[Path] = []
    env_root = os.environ.get("CODINGWIKI_MODEL_DIR", "").strip()
    candidates: list[Path] = []
    if env_root:
        root = Path(env_root).expanduser()
        candidates.append(root if root.name == directory_name else root / directory_name)
    home = Path.home()
    candidates += [
        home / "models" / "BAAI" / directory_name,
        home / ".cache" / "models" / directory_name,
        Path(".codingwiki") / "models" / directory_name,
        Path(".cache") / "models" / directory_name,
    ]
    for candidate in candidates:
        tried.append(candidate)
        if candidate.is_dir():
            return candidate
    listing = "\n".join(f"- {p}" for p in tried)
    raise InputError(
        f"未找到模型 {directory_name}，已尝试:\n{listing}\n"
        f"请用 --model-path 指定，或设置 CODINGWIKI_MODEL_DIR"
    )


def create_embedder(
    backend: str,
    *,
    model_path: str = "",
    device: str = "auto",
    batch_size: int = 32,
    max_length: int = 512,
    dim: int = 0,
    progress: ProgressFn | None = None,
) -> EmbeddingBackend:
    """按后端名构造嵌入器。backend: hash / bge-m3。"""
    normalized = backend.strip().lower()
    if normalized in ("hash", "hash-embed"):
        hash_dim = dim or DEFAULT_HASH_DIM
        return HashEmbedding(dim=hash_dim)
    if normalized in ("bge-m3", "bge", "local"):
        bge_dim = dim or DEFAULT_BGE_DIM
        path = resolve_model_path("bge-m3", model_path)
        return BgeM3Embedding(
            path,
            device=device,
            batch_size=batch_size,
            max_length=max_length,
            dim=bge_dim,
            progress=progress,
        )
    raise InputError(f"未知嵌入后端: {backend}（可选 hash / bge-m3）")


def embedder_for_name(
    model_name: str, *, model_path: str = "", device: str = "auto"
) -> EmbeddingBackend:
    """按索引里记录的模型名复原嵌入器；模型名不认识就报错，不静默换模型。"""
    if model_name.startswith(HASH_PREFIX):
        suffix = model_name[len(HASH_PREFIX) :]
        if suffix.isdigit():
            return HashEmbedding(dim=int(suffix))
        raise InputError(f"无法解析嵌入模型名: {model_name}")
    if model_name.startswith(BGE_M3_PREFIX):
        suffix = model_name[len(BGE_M3_PREFIX) :]
        if not suffix.isdigit():
            raise InputError(f"无法解析嵌入模型名: {model_name}")
        path = resolve_model_path("bge-m3", model_path)
        return BgeM3Embedding(path, device=device, dim=int(suffix))
    raise InputError(
        f"索引使用了未知嵌入模型 {model_name}；请用 codingwiki index build --embedding hash 重建"
    )


def create_reranker(
    *, model_path: str = "", device: str = "auto", batch_size: int = 16
) -> RerankerBackend:
    path = resolve_model_path("bge-reranker-v2-m3", model_path)
    return BgeReranker(path, device=device, batch_size=batch_size)


__all__ = [
    "create_embedder",
    "create_reranker",
    "embedder_for_name",
    "resolve_model_path",
]
