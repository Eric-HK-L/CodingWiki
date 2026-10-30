"""问答流水线测试（架构文档 v0.5 14.5）：校验规则、拒答、修复循环。"""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path

import pytest

from codingwiki.backends import MockBackend
from codingwiki.backends.mock import FAIL_BAD_CITATION, FAIL_BAD_JSON
from codingwiki.domain.rag import Chunk, SourcePassage
from codingwiki.retrieval import AnswerPipeline, HashEmbedding, HybridSearcher, verify_answer

PASSAGES = [
    SourcePassage(sid="S1", reference="src/engine.h:8-13", trust_tier="code", text="class Engine"),
    SourcePassage(sid="S2", reference="wiki:overview", trust_tier="wiki", text="总览页"),
]


def _result_text(citations: list[str] | None = None, confidence: str = "medium") -> str:
    return json.dumps(
        {
            "question": "q",
            "answer_type": "answered",
            "citations": citations if citations is not None else ["S1"],
            "confidence": confidence,
            "unsupported": [],
            "followups": [],
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------- verify_answer


def test_verify_answer_accepts_valid() -> None:
    issues = verify_answer("答案 [S1] 依据 [S2]。", _result_text(["S1", "S2"], "high"), PASSAGES)
    assert issues == []


def test_verify_answer_rejects_unknown_citation_in_text() -> None:
    issues = verify_answer("答案 [S9]。", _result_text(), PASSAGES)
    assert any(i.rule == "citation_not_in_passages" for i in issues)


def test_verify_answer_rejects_unknown_citation_in_result() -> None:
    issues = verify_answer("答案 [S1]。", _result_text(["S1", "S99"]), PASSAGES)
    assert any(i.rule == "result_citation_not_in_passages" for i in issues)


def test_verify_answer_requires_citation_when_answered() -> None:
    issues = verify_answer("没有任何引用的答案。", _result_text([]), PASSAGES)
    assert any(i.rule == "no_citation" for i in issues)


def test_verify_answer_high_confidence_needs_two_sources() -> None:
    issues = verify_answer("答案 [S1]。", _result_text(["S1"], "high"), PASSAGES)
    assert any(i.rule == "high_confidence_needs_two_sources" for i in issues)


def test_verify_answer_rejects_invalid_json() -> None:
    issues = verify_answer("答案 [S1]。", "{ 坏 JSON", PASSAGES)
    assert issues[0].rule == "result_json_invalid"


def test_verify_answer_refused_needs_no_citation() -> None:
    result = json.dumps(
        {
            "question": "q",
            "answer_type": "refused",
            "citations": [],
            "confidence": "low",
            "unsupported": [],
            "followups": [],
        }
    )
    assert verify_answer("无法确认。", result, PASSAGES) == []


# ---------------------------------------------------------------- AnswerPipeline


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    base = tmp_path / ".codingwiki"
    monkeypatch.setenv("CODINGWIKI_HOME", str(base))
    yield base


def _searcher() -> HybridSearcher:
    chunks = [
        Chunk(
            chunk_id="c1",
            source_type="code",
            repo_id="sample",
            snapshot_id="sample@rev1",
            file="src/engine.h",
            title="phy::Engine",
            text="class Engine 调度器，负责线程管理",
            start_line=8,
            end_line=13,
            trust_tier="code",
        ),
        Chunk(
            chunk_id="c2",
            source_type="wiki",
            repo_id="sample",
            snapshot_id="sample@rev1",
            title="总览",
            text="Engine 是 sample 的核心类",
            trust_tier="wiki",
            page_id="overview",
        ),
    ]
    embedder = HashEmbedding()
    texts = [f"{c.title}\n{c.text}" for c in chunks]
    return HybridSearcher(chunks, embedder.embed(texts), embedder)


def test_ask_success_with_citations(home: Path) -> None:
    pipeline = AnswerPipeline(backend=MockBackend(), searcher=_searcher())
    outcome = pipeline.ask("sample", "Engine 是什么？")
    assert not outcome.refused
    assert outcome.attempts == 1
    assert "[S1]" in outcome.answer
    assert outcome.result.citations
    assert outcome.passages[0].sid == "S1"
    # 任务包落盘，含问题与资料
    jobs = list(home.glob("wiki/sample/jobs/*ask*"))
    assert jobs
    assert (jobs[0] / "context" / "question.txt").is_file()
    assert (jobs[0] / "context" / "passages.json").is_file()


def test_ask_refuses_when_no_hits(home: Path) -> None:
    embedder = HashEmbedding()
    empty = HybridSearcher([], [], embedder)
    pipeline = AnswerPipeline(backend=MockBackend(), searcher=empty)
    outcome = pipeline.ask("sample", "任意问题")
    assert outcome.refused
    assert outcome.attempts == 0  # 未调用模型
    assert outcome.result.answer_type == "refused"


def test_ask_retries_and_recovers_from_bad_citation(home: Path) -> None:
    backend = MockBackend(failures=[{"attempt": 1, "kind": FAIL_BAD_CITATION}])
    pipeline = AnswerPipeline(backend=backend, searcher=_searcher())
    outcome = pipeline.ask("sample", "Engine 是什么？")
    assert not outcome.refused
    assert outcome.attempts == 2  # 第 1 轮坏引用被拒，第 2 轮修复
    # 第 2 轮任务包带 errors.json
    jobs = sorted(home.glob("wiki/sample/jobs/*ask-a2"))
    assert (jobs[0] / "errors.json").is_file()


def test_ask_refuses_after_max_rounds(home: Path) -> None:
    backend = MockBackend(failures=[{"attempt": i, "kind": FAIL_BAD_CITATION} for i in range(1, 6)])
    pipeline = AnswerPipeline(backend=backend, searcher=_searcher(), max_rounds=3)
    outcome = pipeline.ask("sample", "Engine 是什么？")
    assert outcome.refused
    assert outcome.attempts == 3


def test_ask_retries_on_invalid_json(home: Path) -> None:
    backend = MockBackend(failures=[{"attempt": 1, "kind": FAIL_BAD_JSON}])
    pipeline = AnswerPipeline(backend=backend, searcher=_searcher())
    outcome = pipeline.ask("sample", "Engine 是什么？")
    assert not outcome.refused
    assert outcome.attempts == 2


def test_ask_uses_codingwiki_home_env(home: Path) -> None:
    # base_dir 缺省时走 CODINGWIKI_HOME，而不是 cwd 下的 .codingwiki
    AnswerPipeline(backend=MockBackend(), searcher=_searcher()).ask("sample", "Engine？")
    assert list(home.glob("wiki/sample/jobs/*ask*"))
