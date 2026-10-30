"""PageVerifier 单元测试：每条校验规则的正例与反例。"""

from __future__ import annotations

from pathlib import Path

import pytest

from codingwiki.domain.facts import (
    FileFacts,
    RepoFacts,
    SymbolFact,
    SymbolKind,
)
from codingwiki.domain.front_matter import dump_front_matter
from codingwiki.domain.wiki import (
    Claim,
    Evidence,
    PageMeta,
    PageResult,
    PageType,
    TaskMeta,
    VerifyReport,
)
from codingwiki.verify import PageVerifier

SYMBOL = SymbolFact(
    symbol_id="src/a.c::run@10",
    name="run",
    qualified_name="run",
    kind=SymbolKind.FUNCTION,
    path="src/a.c",
    line=10,
    end_line=12,
    signature="int run(void)",
)
QUOTE = "int run(void) {\n    return 0;\n}"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.c").write_text("// header\n\n\n\n\n\n\n\n\n" + QUOTE + "\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def facts() -> RepoFacts:
    return RepoFacts(
        repo_id="r",
        snapshot_id="r@1",
        revision="1",
        extractor_version="facts-v1",
        files=[FileFacts(path="src/a.c", language="c", blob_sha256="x", symbols=[SYMBOL])],
    )


def _meta(evidence: list[Evidence] | None = None) -> TaskMeta:
    return TaskMeta(
        task_id="t",
        page_id="module/src",
        page_type=PageType.MODULE,
        repo_id="r",
        snapshot_id="r@1",
        prompt_version="module_page@1",
        evidence=evidence
        or [Evidence(id="E1", file="src/a.c", symbol="run", quote=QUOTE, line_hint=10)],
    )


def _page(meta: TaskMeta, extra_body: str = "", title: str = "模块 src") -> str:
    page_meta = PageMeta(
        id=meta.page_id,
        type=meta.page_type,
        title=title,
        snapshot=meta.snapshot_id,
        prompt_version=meta.prompt_version,
        evidence_count=len(meta.evidence),
        generated_by="mock/mock-v1",
    )
    body = (
        "这个模块提供 run 函数，返回整型状态码，"
        "用于演示校验器的证据闭合规则，此处补足字数。"
        "[E1]\n" + extra_body
    )
    evidence_block = "\n".join(f"[{ev.id}]: {ev.file} · `{ev.symbol}`" for ev in meta.evidence)
    return f"{dump_front_matter(page_meta)}\n{body}\n\n## 出处\n\n{evidence_block}\n"


def _result(meta: TaskMeta) -> PageResult:
    return PageResult(
        page_id=meta.page_id,
        snapshot=meta.snapshot_id,
        claims=[Claim(id="C1", text="模块提供 run 函数。[E1]", evidence=["E1"])],
        evidence=list(meta.evidence),
    )


def _rules(report: VerifyReport) -> set[str]:
    return {issue.rule for issue in report.issues}


def test_valid_page_passes(workdir: Path, facts: RepoFacts) -> None:
    meta = _meta()
    verifier = PageVerifier(facts, workdir)
    report = verifier.verify(_page(meta), _result(meta).model_dump_json(), meta)
    assert report.status == "passed", report.issues


def test_bad_json_fails(workdir: Path, facts: RepoFacts) -> None:
    meta = _meta()
    report = PageVerifier(facts, workdir).verify(_page(meta), "{ 不是 JSON", meta)
    assert report.status == "failed"
    assert "result_json_invalid" in _rules(report)


def test_page_id_mismatch_fails(workdir: Path, facts: RepoFacts) -> None:
    meta = _meta()
    result = _result(meta).model_copy(update={"page_id": "module/other"})
    report = PageVerifier(facts, workdir).verify(_page(meta), result.model_dump_json(), meta)
    assert report.status == "failed"
    assert "page_id_mismatch" in _rules(report)


def test_missing_front_matter_fails(workdir: Path, facts: RepoFacts) -> None:
    meta = _meta()
    report = PageVerifier(facts, workdir).verify(
        "# 没有 front matter\n正文。", _result(meta).model_dump_json(), meta
    )
    assert report.status == "failed"
    assert "front_matter_invalid" in _rules(report)


def test_page_refs_unknown_evidence_fails(workdir: Path, facts: RepoFacts) -> None:
    meta = _meta()
    page = _page(meta, extra_body="另一句引用了 [E9]。\n")
    report = PageVerifier(facts, workdir).verify(page, _result(meta).model_dump_json(), meta)
    assert report.status == "failed"
    assert "page_evidence_missing_in_result" in _rules(report)


def test_claim_refs_unknown_evidence_fails(workdir: Path, facts: RepoFacts) -> None:
    meta = _meta()
    result = PageResult(
        page_id=meta.page_id,
        snapshot=meta.snapshot_id,
        claims=[Claim(id="C1", text="x", evidence=["E999"])],
        evidence=list(meta.evidence),
    )
    report = PageVerifier(facts, workdir).verify(_page(meta), result.model_dump_json(), meta)
    assert report.status == "failed"
    assert "claim_evidence_missing" in _rules(report)


def test_evidence_not_in_task_fails(workdir: Path, facts: RepoFacts) -> None:
    meta = _meta()
    result = PageResult(
        page_id=meta.page_id,
        snapshot=meta.snapshot_id,
        evidence=[
            *meta.evidence,
            Evidence(id="E2", file="src/a.c", quote=QUOTE, line_hint=10),
        ],
    )
    page = _page(meta, extra_body="补充引用 [E2]。\n")
    report = PageVerifier(facts, workdir).verify(page, result.model_dump_json(), meta)
    assert report.status == "failed"
    assert "evidence_not_in_task" in _rules(report)


def test_tampered_quote_fails(workdir: Path, facts: RepoFacts) -> None:
    meta = _meta()
    tampered = Evidence(id="E1", file="src/a.c", symbol="run", quote="int run(void) { return 1; }")
    result = PageResult(
        page_id=meta.page_id,
        snapshot=meta.snapshot_id,
        claims=[Claim(id="C1", text="x.[E1]", evidence=["E1"])],
        evidence=[tampered],
    )
    report = PageVerifier(facts, workdir).verify(_page(meta), result.model_dump_json(), meta)
    assert report.status == "failed"
    assert "evidence_quote_tampered" in _rules(report)


def test_quote_not_found_in_snapshot_fails(workdir: Path, facts: RepoFacts) -> None:
    missing = Evidence(id="E1", file="src/a.c", symbol="run", quote="int run(int x, int y)")
    meta = _meta(evidence=[missing])
    result = PageResult(
        page_id=meta.page_id,
        snapshot=meta.snapshot_id,
        claims=[Claim(id="C1", text="x.[E1]", evidence=["E1"])],
        evidence=[missing],
    )
    report = PageVerifier(facts, workdir).verify(_page(meta), result.model_dump_json(), meta)
    assert report.status == "failed"
    assert "evidence_quote_not_found" in _rules(report)


def test_whitespace_difference_allowed(workdir: Path, facts: RepoFacts) -> None:
    spaced = Evidence(
        id="E1", file="src/a.c", symbol="run", quote="int run(void) {\nreturn 0;\n}", line_hint=10
    )
    meta = _meta(evidence=[spaced])
    result = PageResult(
        page_id=meta.page_id,
        snapshot=meta.snapshot_id,
        claims=[Claim(id="C1", text="x.[E1]", evidence=["E1"])],
        evidence=[spaced],
    )
    report = PageVerifier(facts, workdir).verify(_page(meta), result.model_dump_json(), meta)
    assert report.status == "passed", report.issues


def test_unknown_symbol_fails(workdir: Path, facts: RepoFacts) -> None:
    ev = Evidence(id="E1", file="src/a.c", symbol="ghost", quote=QUOTE, line_hint=10)
    meta = _meta(evidence=[ev])
    result = PageResult(
        page_id=meta.page_id,
        snapshot=meta.snapshot_id,
        claims=[Claim(id="C1", text="x.[E1]", evidence=["E1"])],
        evidence=[ev],
    )
    report = PageVerifier(facts, workdir).verify(_page(meta), result.model_dump_json(), meta)
    assert report.status == "failed"
    assert "symbol_not_in_facts" in _rules(report)


def test_fuzzy_word_fails(workdir: Path, facts: RepoFacts) -> None:
    meta = _meta()
    page = _page(meta, extra_body="这个函数大概是性能关键路径。\n")
    report = PageVerifier(facts, workdir).verify(page, _result(meta).model_dump_json(), meta)
    assert report.status == "failed"
    assert "fuzzy_word" in _rules(report)


def test_broken_link_is_warning(workdir: Path, facts: RepoFacts) -> None:
    meta = _meta()
    page = _page(meta, extra_body="参见 [别的页](module/ghost)。\n")
    report = PageVerifier(facts, workdir).verify(page, _result(meta).model_dump_json(), meta)
    assert report.status == "passed"
    assert "broken_internal_link" in _rules(report)


def test_meta_mismatch_fails(workdir: Path, facts: RepoFacts) -> None:
    meta = _meta()
    other = meta.model_copy(update={"snapshot_id": "r@2"})
    report = PageVerifier(facts, workdir).verify(
        _page(meta), _result(meta).model_dump_json(), other
    )
    assert report.status == "failed"
    assert "front_matter_snapshot_mismatch" in _rules(report)
    assert "snapshot_mismatch" in _rules(report)
