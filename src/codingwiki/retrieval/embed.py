"""嵌入后端（架构文档 v0.5 14.3）。

`EmbeddingBackend` 是协议：真实模型（BGE-M3 / Qwen3-Embedding 等）后续实现该协议接入。
内置 `HashEmbedding`：词元哈希投影 + L2 归一，确定性、零依赖，是阶段一的默认实现。
同一索引只能用一个模型版本，换模型必须重建（计划文档 10.4.2）。
"""

from __future__ import annotations

import hashlib
import math
from typing import Protocol

from codingwiki.retrieval.tokenize import tokenize


class EmbeddingBackend(Protocol):
    """把文本批量编码为归一化向量。"""

    @property
    def name(self) -> str: ...

    @property
    def dim(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedding:
    """词元哈希投影嵌入：每个词元散列到固定维度并累加，最后 L2 归一。

    无语义理解能力，但保证「词面重合度高则相似度高」，配合 BM25 的精确检索，
    在阶段一提供可用的向量通道；确定性输出让测试可复现。
    """

    def __init__(self, dim: int = 256) -> None:
        if dim <= 0:
            raise ValueError(f"dim 必须为正，得到 {dim}")
        self._dim = dim

    @property
    def name(self) -> str:
        return f"hash-embed-v1-d{self._dim}"

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for token in tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            slot = int.from_bytes(digest[:4], "little") % self._dim
            # 符号位让冲突词元部分抵消，降低哈希碰撞的假相似
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[slot] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度；输入已归一化时即点积。"""
    return sum(x * y for x, y in zip(a, b, strict=True))


__all__ = ["EmbeddingBackend", "HashEmbedding", "cosine"]
