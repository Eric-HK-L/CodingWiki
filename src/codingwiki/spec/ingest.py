"""需求文档接入（架构文档 阶段2 §4）。

Markdown 规范化 → 章节树 → doc_id → doc 切块。全部确定性：
同一内容重复处理得到相同 doc_sha256 / doc_id / chunk_id。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from pydantic import BaseModel

from codingwiki.domain.rag import Chunk
from codingwiki.domain.spec import DocSection, ExtractionResult
from codingwiki.errors import InputError
from codingwiki.retrieval.chunking import split_long_text

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
_WS_RE = re.compile(r"\s+")


class QuoteLocation(BaseModel):
    """原文引用在规范化文档中的位置。"""

    line: int
    section: DocSection


# ---------------------------------------------------------------- 规范化


def normalize_markdown(raw: str | bytes) -> str:
    """去 BOM、统一换行、行尾空白、压缩连续空行；不改写语义。"""
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw.lstrip("\ufeff")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    previous_blank = False
    for line in text.split("\n"):
        stripped = line.rstrip()
        if not stripped:
            if previous_blank:
                continue
            previous_blank = True
            lines.append("")
            continue
        previous_blank = False
        lines.append(stripped)
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def doc_sha256(normalized_text: str) -> str:
    return hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()


def make_doc_id(repo_id: str, sha256: str) -> str:
    return f"{repo_id}@{sha256}"


# ---------------------------------------------------------------- 章节树


def parse_sections(normalized_text: str) -> list[DocSection]:
    """按标题解析章节树；代码围栏内的 `#` 不算标题。"""
    lines = normalized_text.splitlines()
    headings: list[tuple[int, str, int]] = []  # level, title, line(1-based)
    fence_char = ""
    fence_len = 0
    for lineno, line in enumerate(lines, start=1):
        fence = _FENCE_RE.match(line)
        if fence_char:
            if fence and fence.group(1)[0] == fence_char and len(fence.group(1)) >= fence_len:
                fence_char = ""
                fence_len = 0
            continue
        if fence:
            fence_char = fence.group(1)[0]
            fence_len = len(fence.group(1))
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            headings.append((len(heading.group(1)), heading.group(2).strip(), lineno))

    sections: list[DocSection] = []
    stack: list[tuple[int, str]] = []
    for index, (level, title, start) in enumerate(headings):
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        end = headings[index + 1][2] - 1 if index + 1 < len(headings) else len(lines)
        sections.append(
            DocSection(
                path=" / ".join(item for _, item in stack),
                title=title,
                level=level,
                start_line=start,
                end_line=max(end, start),
            )
        )
    return sections


def section_at_line(sections: list[DocSection], line: int) -> DocSection | None:
    """取覆盖该行的最内层章节；行落在标题前空行时退到最近的前序章节。"""
    for section in reversed(sections):
        if section.start_line <= line <= section.end_line:
            return section
    for section in reversed(sections):
        if section.start_line <= line:
            return section
    return None


def normalize_for_match(text: str) -> str:
    """引用比对口径：空白折叠为单空格并去首尾。"""
    return _WS_RE.sub(" ", text).strip()


def find_quote_line(quote: str, normalized_text: str) -> int | None:
    """在规范化文档中定位引用，返回起始行号；定位不到返回 None。"""
    target = normalize_for_match(quote)
    if not target:
        return None
    parts: list[str] = []
    line_map: list[int] = []
    for lineno, line in enumerate(normalized_text.splitlines(), start=1):
        for match in re.finditer(r"\S+", line):
            if parts:
                parts.append(" ")
                line_map.append(lineno)
            parts.append(match.group())
            line_map.extend([lineno] * len(match.group()))
    index = "".join(parts).find(target)
    if index < 0:
        return None
    return line_map[index]


def locate_quote(
    quote: str, normalized_text: str, sections: list[DocSection]
) -> QuoteLocation | None:
    line = find_quote_line(quote, normalized_text)
    if line is None:
        return None
    section = section_at_line(sections, line)
    if section is None:
        return None
    return QuoteLocation(line=line, section=section)


# ---------------------------------------------------------------- 切块


def build_doc_chunks(
    *,
    repo_id: str,
    snapshot_id: str,
    doc_id: str,
    source_path: str,
    normalized_text: str,
    sections: list[DocSection],
) -> list[Chunk]:
    """每个章节一个或多个 doc 块。

    模型不变：doc_id 放在 page_id，章节标题路径放在 title，行号放在 start/end_line。
    """
    lines = normalized_text.splitlines()
    chunks: list[Chunk] = []
    for section in sections:
        body = "\n".join(lines[section.start_line : section.end_line]).strip()
        if not body:
            continue
        text = f"{section.path}\n{body}"
        for part_index, part in enumerate(split_long_text(text)):
            chunks.append(
                Chunk(
                    chunk_id=_doc_chunk_id(doc_id, section.path, section.start_line, part_index),
                    source_type="doc",
                    repo_id=repo_id,
                    snapshot_id=snapshot_id,
                    file=source_path,
                    title=section.path,
                    text=part,
                    start_line=section.start_line,
                    end_line=section.end_line,
                    trust_tier="fact",
                    page_id=doc_id,
                )
            )
    return chunks


def _doc_chunk_id(doc_id: str, section_path: str, start_line: int, part_index: int) -> str:
    key = "|".join(("doc", doc_id, section_path, str(start_line), str(part_index)))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]  # noqa: S324


# ---------------------------------------------------------------- 落盘


def specs_root(base_dir: str | Path | None = None) -> Path:
    base = Path(base_dir or os.environ.get("CODINGWIKI_HOME", ".codingwiki")).resolve()
    return base / "specs"


def doc_bundle_dir(repo_id: str, doc_id: str, base_dir: str | Path | None = None) -> Path:
    return specs_root(base_dir) / repo_id / "docs" / doc_id


def read_document(path: str | Path) -> str:
    source = Path(path)
    if not source.is_file():
        raise InputError(f"需求文档不存在: {source}")
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise InputError(f"需求文档不可读: {source}: {exc}") from exc
    return normalize_markdown(raw)


def write_doc_bundle(
    *,
    repo_id: str,
    doc_id: str,
    normalized_text: str,
    chunks: list[Chunk],
    base_dir: str | Path | None = None,
) -> Path:
    bundle = doc_bundle_dir(repo_id, doc_id, base_dir)
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "source.md").write_text(normalized_text, encoding="utf-8")
    payload = [json.loads(chunk.model_dump_json()) for chunk in chunks]
    (bundle / "chunks.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return bundle


def load_doc_chunks(
    repo_id: str, snapshot_id: str, base_dir: str | Path | None = None
) -> list[Chunk]:
    """读取该仓库全部 doc 块；索引构建时统一绑定当前快照。"""
    root = specs_root(base_dir) / repo_id / "docs"
    if not root.is_dir():
        return []
    chunks: list[Chunk] = []
    for path in sorted(root.glob("*/chunks.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InputError(f"doc 切块文件损坏: {path}: {exc}") from exc
        if not isinstance(raw, list):
            raise InputError(f"doc 切块文件顶层必须是数组: {path}")
        for item in raw:
            if not isinstance(item, dict):
                raise InputError(f"doc 切块条目必须是对象: {path}")
            try:
                chunk = Chunk.model_validate(item)
            except ValueError as exc:
                raise InputError(f"doc 切块字段不合法: {path}: {exc}") from exc
            if chunk.source_type != "doc":
                raise InputError(f"非 doc 切块混入 doc 目录: {path}")
            chunks.append(chunk.model_copy(update={"snapshot_id": snapshot_id}))
    return chunks


def load_extractions(repo_id: str, base_dir: str | Path | None = None) -> list[ExtractionResult]:
    root = specs_root(base_dir) / repo_id / "docs"
    if not root.is_dir():
        return []
    results: list[ExtractionResult] = []
    for path in sorted(root.glob("*/extract.json")):
        try:
            results.append(ExtractionResult.model_validate_json(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise InputError(f"抽取产物不可读: {path}: {exc}") from exc
    return results


def save_extraction(result: ExtractionResult, base_dir: str | Path | None = None) -> Path:
    bundle = doc_bundle_dir(result.repo_id, result.doc_id, base_dir)
    bundle.mkdir(parents=True, exist_ok=True)
    path = bundle / "extract.json"
    path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


__all__ = [
    "QuoteLocation",
    "build_doc_chunks",
    "doc_bundle_dir",
    "doc_sha256",
    "find_quote_line",
    "load_doc_chunks",
    "load_extractions",
    "locate_quote",
    "make_doc_id",
    "normalize_for_match",
    "normalize_markdown",
    "parse_sections",
    "read_document",
    "save_extraction",
    "section_at_line",
    "specs_root",
    "write_doc_bundle",
]
