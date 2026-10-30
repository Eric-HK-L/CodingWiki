"""事实模型单元测试：字段校验、默认值、不可变。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from codingwiki.domain.facts import (
    Confidence,
    FileFacts,
    QualityReport,
    RepoFacts,
    SymbolFact,
    SymbolKind,
)


def _symbol(**overrides: object) -> SymbolFact:
    base: dict[str, object] = {
        "symbol_id": "src/a.c::foo@3",
        "name": "foo",
        "qualified_name": "foo",
        "kind": SymbolKind.FUNCTION,
        "path": "src/a.c",
        "line": 3,
        "end_line": 7,
    }
    base.update(overrides)
    return SymbolFact(**base)


def test_symbol_accepts_valid_lines() -> None:
    symbol = _symbol()
    assert symbol.line == 3
    assert symbol.end_line == 7
    assert symbol.confidence is Confidence.HIGH


def test_symbol_rejects_end_line_before_start() -> None:
    with pytest.raises(ValidationError, match="end_line"):
        _symbol(line=7, end_line=3)


def test_symbol_rejects_non_positive_line() -> None:
    with pytest.raises(ValidationError):
        _symbol(line=0)


def test_symbol_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        _symbol(unexpected="x")


def test_symbol_is_frozen() -> None:
    symbol = _symbol()
    with pytest.raises(ValidationError):
        symbol.name = "bar"  # type: ignore[misc]


def test_file_facts_defaults_are_empty() -> None:
    facts = FileFacts(path="src/a.c", language="c", blob_sha256="abc")
    assert facts.parse_ok is True
    assert facts.symbols == []
    assert facts.calls == []
    assert facts.module == "(root)"


def test_quality_report_defaults_have_no_fake_numbers() -> None:
    quality = QualityReport()
    assert quality.parse_success_rate is None
    assert quality.unresolved_call_ratio is None
    assert quality.notes == []


def test_repo_facts_counts() -> None:
    facts = RepoFacts(
        snapshot_id="demo@abc",
        repo_id="demo",
        revision="abc",
        extractor_version="v1",
        files=[FileFacts(path="a.c", language="c", blob_sha256="1", symbols=[_symbol()])],
    )
    assert facts.file_count == 1
    assert facts.symbol_count == 1
    assert facts.call_count == 0
