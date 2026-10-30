"""切块（计划文档 10.3，架构文档 v0.5 14.1）。

代码块以符号为单位（SymbolFact.line..end_line 从快照文本截取）；
构建/配置/测试事实各成小块；Wiki 页按标题层级切分。
chunk_id 对来源内容做哈希，同一快照重建结果一致。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from codingwiki.domain.facts import RepoFacts, SymbolKind
from codingwiki.domain.rag import Chunk

# 词元数难以精确计量，用字符近似：代码约 4 字符/词元 → 1200 词元 ≈ 4800 字符
_MAX_CHUNK_CHARS = 4800
_OVERLAP_RATIO = 0.15

# 切成检索块的符号类别；namespace/外层类等容器符号会与成员重复，跳过
_CHUNK_KINDS = {
    SymbolKind.FUNCTION,
    SymbolKind.METHOD,
    SymbolKind.KERNEL,
    SymbolKind.DEVICE_FUNCTION,
    SymbolKind.CLASS,
    SymbolKind.STRUCT,
    SymbolKind.ENUM,
    SymbolKind.MACRO,
    SymbolKind.TYPE_ALIAS,
}


class ChunkBuilder:
    """从事实 + 工作树 + 已发布 Wiki 页构建切块。"""

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir.resolve()
        self._file_cache: dict[str, list[str]] = {}
        self._module_map: dict[str, str] = {}

    def build(
        self,
        facts: RepoFacts,
        wiki_pages: list[tuple[str, str, str]] | None = None,
    ) -> list[Chunk]:
        self._module_map = {f.path: f.module for f in facts.files}
        chunks: list[Chunk] = []
        chunks += self._code_chunks(facts)
        chunks += self._build_chunks(facts)
        chunks += self._config_chunks(facts)
        chunks += self._test_chunks(facts)
        for page_id, title, text in wiki_pages or []:
            chunks += _wiki_chunks(facts.repo_id, facts.snapshot_id, page_id, title, text)
        # 事实层可能因宏展开等原因产出重复符号（真实仓库实测），按 chunk_id 去重保留首个
        seen: set[str] = set()
        unique: list[Chunk] = []
        for chunk in chunks:
            if chunk.chunk_id in seen:
                continue
            seen.add(chunk.chunk_id)
            unique.append(chunk)
        return unique

    # ---------------------------------------------------------------- 代码块

    def _code_chunks(self, facts: RepoFacts) -> list[Chunk]:
        chunks: list[Chunk] = []
        for file in facts.files:
            for sym in file.symbols:
                if sym.kind not in _CHUNK_KINDS:
                    continue
                lines = self._read(facts, file.path)
                if not lines:
                    continue
                body = "\n".join(lines[sym.line - 1 : sym.end_line])
                text = f"{file.path} {sym.qualified_name}\n{sym.signature}\n{body}".strip()
                if not text:
                    continue
                for part_index, part in enumerate(_split_long(text)):
                    chunks.append(
                        Chunk(
                            chunk_id=_chunk_id(
                                "code",
                                file.path,
                                sym.qualified_name,
                                sym.line,
                                sym.end_line,
                                part_index,
                            ),
                            source_type="code",
                            repo_id=facts.repo_id,
                            snapshot_id=facts.snapshot_id,
                            file=file.path,
                            symbol=sym.qualified_name,
                            module=file.module,
                            title=sym.qualified_name,
                            text=part,
                            start_line=sym.line,
                            end_line=sym.end_line,
                            trust_tier="code",
                        )
                    )
        return chunks

    def _build_chunks(self, facts: RepoFacts) -> list[Chunk]:
        chunks: list[Chunk] = []
        for target in facts.build_targets:
            if not target.name:
                continue
            lines = [
                f"构建目标 {target.name}（{target.kind}，{target.build_system}）",
                f"定义于 {target.path}:{target.line}" if target.path else "",
            ]
            if target.sources:
                lines.append("源文件: " + ", ".join(target.sources[:20]))
            if target.deps:
                lines.append("依赖: " + ", ".join(target.deps[:20]))
            if target.compile_definitions:
                lines.append("编译宏: " + ", ".join(target.compile_definitions[:20]))
            chunks.append(
                Chunk(
                    chunk_id=_chunk_id("build", target.path, target.name, target.line, 0, 0),
                    source_type="build",
                    repo_id=facts.repo_id,
                    snapshot_id=facts.snapshot_id,
                    file=target.path,
                    module=self._module_of(target.path),
                    title=f"构建目标 {target.name}",
                    text="\n".join(x for x in lines if x),
                    start_line=target.line,
                    end_line=target.line,
                    trust_tier="fact",
                )
            )
        return chunks

    def _config_chunks(self, facts: RepoFacts) -> list[Chunk]:
        by_file: dict[str, list[str]] = {}
        others: list[Chunk] = []
        for cfg in facts.config_facts:
            if cfg.kind == "file" and cfg.path:
                by_file.setdefault(cfg.path, []).append(cfg.key)
                continue
            text = f"配置项 {cfg.key}（{cfg.kind}）读取于 {cfg.path}:{cfg.line}"
            others.append(
                Chunk(
                    chunk_id=_chunk_id("config", cfg.path, cfg.key, cfg.line, 0, 0),
                    source_type="config",
                    repo_id=facts.repo_id,
                    snapshot_id=facts.snapshot_id,
                    file=cfg.path,
                    module=self._module_of(cfg.path),
                    title=f"配置项 {cfg.key}",
                    text=text,
                    start_line=cfg.line,
                    end_line=cfg.line,
                    trust_tier="fact",
                )
            )
        chunks = [
            Chunk(
                chunk_id=_chunk_id("config", path, "(file)", 0, 0, 0),
                source_type="config",
                repo_id=facts.repo_id,
                snapshot_id=facts.snapshot_id,
                file=path,
                module=self._module_of(path),
                title=f"配置文件 {path}",
                text=f"配置文件 {path} 包含键:\n" + "\n".join(sorted(keys)[:50]),
                trust_tier="fact",
            )
            for path, keys in sorted(by_file.items())
        ]
        return chunks + others

    def _test_chunks(self, facts: RepoFacts) -> list[Chunk]:
        return [
            Chunk(
                chunk_id=_chunk_id("test", test.path, test.name, test.line, 0, 0),
                source_type="test",
                repo_id=facts.repo_id,
                snapshot_id=facts.snapshot_id,
                file=test.path,
                module=self._module_of(test.path),
                title=f"测试 {test.name}",
                text=f"测试 {test.name}（{test.framework}）定义于 {test.path}:{test.line}",
                start_line=test.line,
                end_line=test.line,
                trust_tier="fact",
            )
            for test in facts.tests
            if test.name
        ]

    # ---------------------------------------------------------------- 内部

    def _module_of(self, path: str) -> str:
        return self._module_map.get(path, "")

    def _read(self, facts: RepoFacts, path: str) -> list[str]:
        if path in self._file_cache:
            return self._file_cache[path]
        if path not in facts.blob_by_path:
            self._file_cache[path] = []
            return []
        try:
            text = (self._workdir / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        lines = text.splitlines()
        self._file_cache[path] = lines
        return lines


def _wiki_chunks(
    repo_id: str, snapshot_id: str, page_id: str, title: str, text: str
) -> list[Chunk]:
    """按 Markdown 标题层级切 Wiki 页，保留标题路径（10.3.2）。"""
    text = _strip_front_matter(text)
    chunks: list[Chunk] = []
    section: list[str] = []
    heading_path: list[str] = []

    def flush() -> None:
        body = "\n".join(section).strip()
        if not body:
            return
        path = " / ".join(heading_path) or title
        chunks.append(
            Chunk(
                chunk_id=_chunk_id("wiki", page_id, path, 0, 0, len(chunks)),
                source_type="wiki",
                repo_id=repo_id,
                snapshot_id=snapshot_id,
                module="",
                title=f"{title} / {path}" if path != title else title,
                text=body,
                trust_tier="wiki",
                page_id=page_id,
            )
        )

    for line in text.splitlines():
        if line.startswith("#"):
            flush()
            section = [line]
            level = len(line) - len(line.lstrip("#"))
            heading = line.lstrip("#").strip()
            heading_path = heading_path[: level - 1] + [heading]
        else:
            section.append(line)
    flush()
    return chunks


def _strip_front_matter(text: str) -> str:
    """去掉页面顶部的 YAML front matter（--- 包围的块），避免元数据污染检索文本。"""
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---", 4)
    if end == -1:
        return text
    return text[end + 1 :].lstrip("\n").removeprefix("---").lstrip("\n")


def _split_long(text: str) -> list[str]:
    """超长块按行折分，块间保留重叠（10.3.1）。"""
    if len(text) <= _MAX_CHUNK_CHARS:
        return [text]
    lines = text.splitlines()
    parts: list[str] = []
    current: list[str] = []
    size = 0
    overlap: list[str] = []
    for line in lines:
        current.append(line)
        size += len(line) + 1
        if size >= _MAX_CHUNK_CHARS:
            parts.append("\n".join(current))
            keep = max(1, int(len(current) * _OVERLAP_RATIO))
            overlap = current[-keep:]
            current = list(overlap)
            size = sum(len(x) + 1 for x in current)
    rest = "\n".join(current).strip()
    if rest and (not parts or rest not in parts[-1]):
        parts.append(rest)
    return parts


def _chunk_id(*parts: object) -> str:
    key = "|".join(str(p) for p in parts)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]  # noqa: S324


__all__ = ["ChunkBuilder"]
