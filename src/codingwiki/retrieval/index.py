"""索引存储（计划文档 10.8，架构文档 v0.5 14.6）。

布局：indices/<repo_id>/<index_id>/{chunks.jsonl,vectors.bin,index.json} + current 指针。
加载时校验：切块数 == 向量数、chunk_id 无重复。
"""

from __future__ import annotations

import os
from array import array
from datetime import UTC, datetime
from pathlib import Path

from codingwiki.domain.rag import Chunk, IndexMeta
from codingwiki.errors import InputError


class FilesystemIndexStore:
    def __init__(self, repo_id: str, base_dir: str | Path | None = None) -> None:
        base = Path(base_dir or os.environ.get("CODINGWIKI_HOME", ".codingwiki")).resolve()
        self._repo_id = repo_id
        self._root = base / "indices" / repo_id

    # ---------------------------------------------------------------- 写入

    def save(
        self,
        chunks: list[Chunk],
        vectors: list[list[float]],
        embedding_model: str,
        dim: int,
        vocab_size: int,
    ) -> IndexMeta:
        """写入新版本索引并原子切换 current。chunks 与 vectors 必须等长。"""
        if len(chunks) != len(vectors):
            raise InputError(f"切块数 {len(chunks)} 与向量数 {len(vectors)} 不一致")
        if not chunks:
            raise InputError("没有任何切块，拒绝建立空索引")
        for vec in vectors:
            if len(vec) != dim:
                raise InputError(f"向量维度 {len(vec)} 与声明的 dim={dim} 不一致")
        ids = [c.chunk_id for c in chunks]
        if len(set(ids)) != len(ids):
            raise InputError("chunk_id 存在重复")

        snapshot_id = chunks[0].snapshot_id
        revision_part = snapshot_id.split("@", 1)[-1][:8] or "unknown"
        base_id = f"i{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{revision_part}"
        index_id = base_id
        suffix = 2
        while (self._root / index_id).exists():
            # 同一秒内重复构建：追加序号，避免静默覆盖上一份索引
            index_id = f"{base_id}-{suffix}"
            suffix += 1
        index_dir = self._root / index_id
        index_dir.mkdir(parents=True, exist_ok=False)

        with (index_dir / "chunks.jsonl").open("w", encoding="utf-8") as fh:
            for chunk in chunks:
                fh.write(chunk.model_dump_json() + "\n")

        flat = array("f", (v for vec in vectors for v in vec))
        (index_dir / "vectors.bin").write_bytes(flat.tobytes())

        meta = IndexMeta(
            index_id=index_id,
            repo_id=self._repo_id,
            snapshot_id=snapshot_id,
            embedding_model=embedding_model,
            dim=dim,
            chunk_count=len(chunks),
            vocab_size=vocab_size,
        )
        (index_dir / "index.json").write_text(meta.model_dump_json(indent=2), encoding="utf-8")

        # 全部写完再切换指针，旧版本保留可回滚
        (self._root / "current").write_text(index_id, encoding="utf-8")
        return meta

    # ---------------------------------------------------------------- 读取

    def current_version(self) -> str | None:
        pointer = self._root / "current"
        if not pointer.is_file():
            return None
        version = pointer.read_text(encoding="utf-8").strip()
        return version or None

    def load(self, index_id: str | None = None) -> tuple[IndexMeta, list[Chunk], list[list[float]]]:
        """加载索引（默认当前版本），并做一致性校验。"""
        version = index_id or self.current_version()
        if version is None:
            raise InputError(f"仓库 {self._repo_id} 还没有索引，请先执行: codingwiki index build")
        index_dir = self._root / version
        if not index_dir.is_dir():
            raise InputError(f"索引版本不存在: {version}")

        meta = IndexMeta.model_validate_json((index_dir / "index.json").read_text(encoding="utf-8"))
        chunks = [
            Chunk.model_validate_json(line)
            for line in (index_dir / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        raw = (index_dir / "vectors.bin").read_bytes()
        floats = array("f")
        floats.frombytes(raw)
        if len(floats) != len(chunks) * meta.dim:
            raise InputError(
                f"索引损坏: 向量数 {len(floats) // meta.dim} 与切块数 {len(chunks)} 不一致"
            )
        vectors = [list(floats[i * meta.dim : (i + 1) * meta.dim]) for i in range(len(chunks))]
        if meta.chunk_count != len(chunks):
            raise InputError(
                f"索引损坏: index.json 记录 {meta.chunk_count} 块，实际 {len(chunks)} 块"
            )
        return meta, chunks, vectors

    def list_versions(self) -> list[str]:
        if not self._root.is_dir():
            return []
        return sorted(
            p.name for p in self._root.iterdir() if p.is_dir() and (p / "index.json").is_file()
        )


__all__ = ["FilesystemIndexStore"]
