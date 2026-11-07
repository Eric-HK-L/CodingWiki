"""需求文档接入测试（S12）：规范化、章节树、定位、切块、落盘。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codingwiki.domain.rag import Chunk
from codingwiki.errors import InputError
from codingwiki.spec.ingest import (
    build_doc_chunks,
    doc_sha256,
    find_quote_line,
    load_doc_chunks,
    locate_quote,
    make_doc_id,
    normalize_markdown,
    parse_sections,
    write_doc_bundle,
)

_DOC = """# 需求文档

## 1 功能需求

### 1.1 配置项

在配置中新增日志级别项。

验收标准：

- 未配置时行为与 info 一致；
- 非法值启动失败。

### 1.2 空章节

## 2 非功能需求

- 变更不影响现有构建目标，
  且不需要重新编译。
"""


def test_normalize_handles_bom_crlf_and_trailing_spaces() -> None:
    raw = "\ufeff# 标题  \r\n\r\n\r\n正文\r\n```python\r\n# 注释不是标题\r\n```\r\n"
    text = normalize_markdown(raw)
    assert text == "# 标题\n\n正文\n```python\n# 注释不是标题\n```\n"


def test_code_fence_hash_is_not_a_heading() -> None:
    sections = parse_sections(normalize_markdown(_DOC))
    paths = [section.path for section in sections]
    assert "需求文档 / 1 功能需求 / 1.1 配置项" in paths
    assert not any("注释" in path for path in paths)
    assert len(sections) == 5


def test_section_line_ranges_and_empty_section() -> None:
    text = normalize_markdown(_DOC)
    sections = parse_sections(text)
    by_path = {section.path: section for section in sections}
    empty = by_path["需求文档 / 1 功能需求 / 1.2 空章节"]
    assert empty.end_line >= empty.start_line
    body_text = "\n".join(text.splitlines()[empty.start_line : empty.end_line])
    assert not body_text.strip()


def test_multiline_quote_is_locatable_with_whitespace_normalization() -> None:
    text = normalize_markdown(_DOC)
    sections = parse_sections(text)
    location = locate_quote("变更不影响现有构建目标，\n且不需要重新编译。", text, sections)
    assert location is not None
    assert location.section.path == "需求文档 / 2 非功能需求"
    # 改写过的引用不允许通过
    assert find_quote_line("变更不影响所有构建目标", text) is None


def test_doc_sha_and_id_are_stable_across_line_endings() -> None:
    lf = normalize_markdown(_DOC)
    crlf = normalize_markdown("\ufeff" + _DOC.replace("\n", "\r\n"))
    assert doc_sha256(lf) == doc_sha256(crlf)
    assert make_doc_id("cuphy", doc_sha256(lf)) == f"cuphy@{doc_sha256(lf)}"


def test_doc_chunks_carry_section_and_doc_identity() -> None:
    text = normalize_markdown(_DOC)
    sections = parse_sections(text)
    doc_id = make_doc_id("cuphy", doc_sha256(text))
    chunks = build_doc_chunks(
        repo_id="cuphy",
        snapshot_id="cuphy@abc",
        doc_id=doc_id,
        source_path="evals/specs/demo.md",
        normalized_text=text,
        sections=sections,
    )
    assert chunks
    for chunk in chunks:
        assert chunk.source_type == "doc"
        assert chunk.trust_tier == "fact"
        assert chunk.page_id == doc_id
        assert chunk.file == "evals/specs/demo.md"
        assert chunk.start_line > 0 and chunk.end_line >= chunk.start_line
    titles = {chunk.title for chunk in chunks}
    assert "需求文档 / 1 功能需求 / 1.2 空章节" not in titles  # 空章节不进索引
    assert "需求文档 / 1 功能需求 / 1.1 配置项" in titles


def test_doc_chunks_are_idempotent() -> None:
    text = normalize_markdown(_DOC)
    sections = parse_sections(text)
    doc_id = make_doc_id("cuphy", doc_sha256(text))
    first = build_doc_chunks(
        repo_id="cuphy",
        snapshot_id="s@r",
        doc_id=doc_id,
        source_path="demo.md",
        normalized_text=text,
        sections=sections,
    )
    second = build_doc_chunks(
        repo_id="cuphy",
        snapshot_id="s@r",
        doc_id=doc_id,
        source_path="demo.md",
        normalized_text=text,
        sections=sections,
    )
    assert [chunk.chunk_id for chunk in first] == [chunk.chunk_id for chunk in second]


def test_load_doc_chunks_binds_current_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODINGWIKI_HOME", str(tmp_path))
    text = normalize_markdown(_DOC)
    sections = parse_sections(text)
    doc_id = make_doc_id("cuphy", doc_sha256(text))
    chunks = build_doc_chunks(
        repo_id="cuphy",
        snapshot_id="old@rev",
        doc_id=doc_id,
        source_path="demo.md",
        normalized_text=text,
        sections=sections,
    )
    write_doc_bundle(
        repo_id="cuphy", doc_id=doc_id, normalized_text=text, chunks=chunks, base_dir=tmp_path
    )
    loaded = load_doc_chunks("cuphy", "new@rev", base_dir=tmp_path)
    assert len(loaded) == len(chunks)
    assert {chunk.snapshot_id for chunk in loaded} == {"new@rev"}
    assert {chunk.source_type for chunk in loaded} == {"doc"}
    # 代码块不受影响：doc 过滤只命中 doc 块
    code = Chunk(
        chunk_id="code-1",
        source_type="code",
        repo_id="cuphy",
        snapshot_id="new@rev",
        file="src/a.c",
        text="int main(void) { return 0; }",
        start_line=1,
        end_line=1,
        trust_tier="code",
    )
    assert [chunk for chunk in loaded + [code] if chunk.source_type == "doc"] == loaded


def test_load_doc_chunks_rejects_broken_json(tmp_path: Path) -> None:
    bundle = tmp_path / "specs" / "cuphy" / "docs" / "cuphy@abc"
    bundle.mkdir(parents=True)
    (bundle / "chunks.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(InputError, match="损坏"):
        load_doc_chunks("cuphy", "s@r", base_dir=tmp_path)


def test_load_doc_chunks_requires_doc_source_type(tmp_path: Path) -> None:
    bundle = tmp_path / "specs" / "cuphy" / "docs" / "cuphy@abc"
    bundle.mkdir(parents=True)
    payload = [
        {
            "chunk_id": "c1",
            "source_type": "code",
            "repo_id": "cuphy",
            "snapshot_id": "s@r",
            "text": "x",
            "trust_tier": "code",
        }
    ]
    (bundle / "chunks.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(InputError, match="非 doc"):
        load_doc_chunks("cuphy", "s@r", base_dir=tmp_path)
