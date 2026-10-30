"""MockBackend 单元测试：正常生成与故障注入。"""

from __future__ import annotations

from pathlib import Path

from codingwiki.backends import BackendStatus, MockBackend, TaskPacket
from codingwiki.backends.mock import (
    FAIL_BAD_EVIDENCE_REF,
    FAIL_BAD_JSON,
    FAIL_FUZZY_WORD,
    FAIL_MISSING_TITLE,
)
from codingwiki.domain.wiki import (
    Evidence,
    PageResult,
    PageType,
    TaskFacts,
    TaskMeta,
)


def _packet(tmp_path: Path, attempt: int = 1, with_symbol: bool = True) -> TaskPacket:
    workspace = tmp_path / f"job-a{attempt}"
    (workspace / "context").mkdir(parents=True)
    output = workspace / "output"
    output.mkdir()
    evidence = (
        [Evidence(id="E1", file="src/a.c", symbol="run", quote="int run(void)", line_hint=1)]
        if with_symbol
        else []
    )
    meta = TaskMeta(
        task_id="t",
        page_id="module/src",
        page_type=PageType.MODULE,
        title_hint="模块 src",
        repo_id="r",
        snapshot_id="r@1",
        module="src",
        prompt_version="module_page@1",
        evidence=evidence,
        attempt=attempt,
    )
    (workspace / "meta.json").write_text(meta.model_dump_json(), encoding="utf-8")
    facts = TaskFacts(module="src")
    (workspace / "context" / "facts.json").write_text(facts.model_dump_json(), encoding="utf-8")
    (workspace / "task.md").write_text("# 任务", encoding="utf-8")
    return TaskPacket(
        workspace=workspace,
        task_file=workspace / "task.md",
        output_dir=output,
        metadata={"page_id": "module/src"},
    )


def test_mock_generates_page_and_result(tmp_path: Path) -> None:
    packet = _packet(tmp_path)
    result = MockBackend().run(packet)
    assert result.status is BackendStatus.SUCCEEDED
    page = (packet.output_dir / "page.md").read_text(encoding="utf-8")
    assert page.startswith("---")
    assert "id: module/src" in page
    assert "prompt_version: module_page@1" in page
    parsed = PageResult.model_validate_json(
        (packet.output_dir / "result.json").read_text(encoding="utf-8")
    )
    assert parsed.page_id == "module/src"
    assert parsed.evidence[0].id == "E1"
    assert parsed.claims[0].evidence == ["E1"]


def test_mock_inject_bad_evidence_ref(tmp_path: Path) -> None:
    packet = _packet(tmp_path, attempt=1)
    backend = MockBackend(failures=[{"attempt": 1, "kind": FAIL_BAD_EVIDENCE_REF}])
    result = backend.run(packet)
    assert result.status is BackendStatus.SUCCEEDED
    parsed = PageResult.model_validate_json(
        (packet.output_dir / "result.json").read_text(encoding="utf-8")
    )
    all_refs = {ref for claim in parsed.claims for ref in claim.evidence}
    assert "E999" in all_refs


def test_mock_inject_bad_json(tmp_path: Path) -> None:
    packet = _packet(tmp_path, attempt=1)
    backend = MockBackend(failures=[{"attempt": 1, "kind": FAIL_BAD_JSON}])
    result = backend.run(packet)
    assert result.status is BackendStatus.SUCCEEDED
    text = (packet.output_dir / "result.json").read_text(encoding="utf-8")
    assert text.startswith("{ 不是合法 JSON")


def test_mock_inject_missing_title_and_fuzzy(tmp_path: Path) -> None:
    packet = _packet(tmp_path, attempt=1)
    backend = MockBackend(failures=[{"attempt": 1, "kind": FAIL_MISSING_TITLE}])
    backend.run(packet)
    page = (packet.output_dir / "page.md").read_text(encoding="utf-8")
    assert "title:" not in page.split("---")[1]

    packet2_dir = tmp_path / "job-fuzzy"
    packet2 = _packet(tmp_path, attempt=2)
    backend2 = MockBackend(failures=[{"attempt": 2, "kind": FAIL_FUZZY_WORD}])
    backend2.run(packet2)
    page2 = (packet2.output_dir / "page.md").read_text(encoding="utf-8")
    assert "大概" in page2
    assert not packet2_dir.exists() or True  # packet2 用同一 tmp_path 不同目录


def test_mock_unreadable_package(tmp_path: Path) -> None:
    workspace = tmp_path / "empty"
    workspace.mkdir()
    output = workspace / "output"
    output.mkdir()
    packet = TaskPacket(workspace=workspace, task_file=workspace / "task.md", output_dir=output)
    result = MockBackend().run(packet)
    assert result.status is BackendStatus.FAILED
    assert "任务包不可读" in result.error
