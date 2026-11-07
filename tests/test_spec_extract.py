"""需求抽取测试（S12）：mock 链路、原文定位、幂等、失败不编造。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codingwiki.backends.mock import MockBackend
from codingwiki.backends.protocol import BackendResult, BackendStatus, TaskPacket
from codingwiki.domain.spec import ExtractionResult, RequirementKind
from codingwiki.errors import InputError
from codingwiki.spec import extract_requirements
from codingwiki.spec.ingest import (
    doc_bundle_dir,
    find_quote_line,
    load_doc_chunks,
    normalize_markdown,
)

_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE = _ROOT / "evals" / "specs" / "fixture-nvlog-requirements.md"


class _StubBackend:
    """测试用后端：写固定 extract.json，模拟模型输出。"""

    def __init__(self, payload: dict[str, object], status: BackendStatus = BackendStatus.SUCCEEDED):
        self._payload = payload
        self._status = status

    def run(self, packet: TaskPacket) -> BackendResult:
        packet.output_dir.mkdir(parents=True, exist_ok=True)
        (packet.output_dir / "extract.json").write_text(
            json.dumps(self._payload, ensure_ascii=False), encoding="utf-8"
        )
        return BackendResult(status=self._status, model="stub-v1")


def _extract(tmp_path: Path, backend: MockBackend | _StubBackend) -> ExtractionResult:
    return extract_requirements(
        repo_id="sample",
        doc_path=_FIXTURE,
        backend=backend,
        snapshot_id="sample@rev",
        base_dir=tmp_path,
        backend_name="mock" if isinstance(backend, MockBackend) else "stub",
        model_name="mock-v1",
    )


def test_mock_extraction_is_traceable_and_numbered(tmp_path: Path) -> None:
    result = _extract(tmp_path, MockBackend())
    assert len(result.requirements) == 6
    functional = [r for r in result.requirements if r.kind is RequirementKind.FUNCTIONAL]
    non_functional = [r for r in result.requirements if r.kind is RequirementKind.NON_FUNCTIONAL]
    assert [r.id for r in functional] == ["R-001", "R-002", "R-003"]
    assert [r.id for r in non_functional] == ["N-001", "N-002", "N-003"]
    assert [q.id for q in result.questions] == ["Q-001", "Q-002"]

    normalized = normalize_markdown(_FIXTURE.read_bytes())
    for requirement in result.requirements:
        assert requirement.source_section
        assert find_quote_line(requirement.source_quote, normalized) is not None
        assert (
            "非功能需求" in requirement.source_section or "功能需求" in requirement.source_section
        )
    assert [ac.id for ac in functional[0].acceptance] == ["AC-001", "AC-002", "AC-003"]
    assert [ac.id for ac in functional[1].acceptance] == ["AC-004", "AC-005"]
    assert functional[2].acceptance == []


def test_same_document_extracts_same_ids(tmp_path: Path) -> None:
    first = _extract(tmp_path, MockBackend())
    second = _extract(tmp_path, MockBackend())
    assert [r.id for r in first.requirements] == [r.id for r in second.requirements]
    assert [r.source_quote for r in first.requirements] == [
        r.source_quote for r in second.requirements
    ]
    assert [q.id for q in first.questions] == [q.id for q in second.questions]


def test_extraction_persists_bundle_and_doc_chunks(tmp_path: Path) -> None:
    result = _extract(tmp_path, MockBackend())
    bundle = doc_bundle_dir("sample", result.doc_id, tmp_path)
    for name in ("source.md", "chunks.json", "extract.json"):
        assert (bundle / name).is_file()
    chunks = load_doc_chunks("sample", "sample@rev", base_dir=tmp_path)
    assert chunks
    assert {chunk.source_type for chunk in chunks} == {"doc"}
    assert {chunk.page_id for chunk in chunks} == {result.doc_id}


def test_unlocatable_quote_is_rejected(tmp_path: Path) -> None:
    backend = _StubBackend(
        {
            "requirements": [
                {
                    "kind": "functional",
                    "text": "编造的需求",
                    "priority": "must",
                    "source_quote": "这段原文根本不存在",
                    "acceptance": [],
                }
            ],
            "questions": [],
        }
    )
    with pytest.raises(InputError, match="无法在原文定位"):
        _extract(tmp_path, backend)


def test_missing_quote_is_rejected(tmp_path: Path) -> None:
    backend = _StubBackend(
        {"requirements": [{"kind": "functional", "text": "没有出处的需求", "source_quote": ""}]}
    )
    with pytest.raises(InputError, match="缺少 source_quote"):
        _extract(tmp_path, backend)


def test_backend_failure_is_reported(tmp_path: Path) -> None:
    backend = _StubBackend({}, status=BackendStatus.FAILED)
    with pytest.raises(InputError, match="抽取失败"):
        _extract(tmp_path, backend)
