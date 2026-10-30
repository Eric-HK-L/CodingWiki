"""S4 集成测试：fixture 仓库 → 快照 → 事实 → 计划 → 任务包 → mock 生成 → 校验 → 发布。

另覆盖：修复循环（故障注入）与增量复用。
"""

from __future__ import annotations

import shutil
from collections.abc import Generator
from pathlib import Path

import pytest

from codingwiki.backends import MockBackend
from codingwiki.backends.mock import FAIL_BAD_EVIDENCE_REF
from codingwiki.domain.models import RepoType
from codingwiki.domain.wiki import PageStatus, PageType
from codingwiki.facts import FilesystemFactExtractor, FilesystemFactStore
from codingwiki.generate import CoveragePlanner, GeneratePipeline, TaskPackageBuilder
from codingwiki.publish import FilesystemPageStore
from codingwiki.snapshot import FilesystemSnapshotStore
from codingwiki.verify import PageVerifier

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "facts" / "sample"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    base = tmp_path / ".codingwiki"
    monkeypatch.setenv("CODINGWIKI_HOME", str(base))
    yield base


@pytest.fixture
def work(tmp_path: Path) -> Path:
    target = tmp_path / "work"
    shutil.copytree(FIXTURES, target)
    (target / ".DS_Store").unlink(missing_ok=True)
    return target


def _facts(home: Path, work: Path, revision: str = "rev1"):
    snapshots = FilesystemSnapshotStore()
    snapshots.create("sample", revision, work, RepoType.GIT)
    snapshot = snapshots.snapshot(f"sample@{revision}")
    fact_store = FilesystemFactStore()
    facts = FilesystemFactExtractor(fact_store).extract(snapshot, work)
    fact_store.save(facts)
    return facts


def _pipeline(work: Path, facts, store: FilesystemPageStore, backend: MockBackend | None = None):
    plan = CoveragePlanner().plan(facts, store.load_deps())
    known = {t.page_id for t in plan.tasks} | set(store.list_drafts())
    pipeline = GeneratePipeline(
        backend=backend or MockBackend(),
        builder=TaskPackageBuilder(work),
        verifier=PageVerifier(facts, work, known_pages=known),
        store=store,
    )
    return plan, pipeline


def test_plan_covers_page_types(home: Path, work: Path) -> None:
    facts = _facts(home, work)
    plan = CoveragePlanner().plan(facts)
    types = {t.page_type for t in plan.tasks}
    assert PageType.MODULE in types
    assert PageType.BUILD in types
    assert PageType.CONFIG in types
    assert PageType.TEST in types
    assert PageType.OVERVIEW in types
    assert PageType.ARCHITECTURE in types
    assert PageType.HOME in types
    assert PageType.GLOSSARY in types
    # fixture 有公开头文件与 CUDA kernel → 按文件聚合的接口页；main → 流程页
    assert PageType.INTERFACE in types
    assert PageType.FLOW in types
    page_ids = {t.page_id for t in plan.tasks}
    assert "module/src" in page_ids
    assert "overview" in page_ids
    assert "interface/src/engine" in page_ids
    assert not any("/" in pid.split("interface/", 1)[-1] and pid.count("/") > 2 for pid in page_ids)


def test_plan_max_tasks_truncates(home: Path, work: Path) -> None:
    facts = _facts(home, work)
    plan = CoveragePlanner(max_tasks=3).plan(facts)
    assert len(plan.tasks) == 3
    assert len(plan.skipped) > 0
    # 预算截断时先保导航页（首页/总览/架构），否则读者没有入口
    assert {t.page_id for t in plan.tasks} == {"home", "overview", "architecture"}


def test_full_pipeline_generates_and_publishes(home: Path, work: Path) -> None:
    facts = _facts(home, work)
    store = FilesystemPageStore("sample")
    plan, pipeline = _pipeline(work, facts, store)
    outcomes = pipeline.run_plan(plan, facts)

    failed = [o for o in outcomes if not o.ok]
    assert not failed, [
        (o.task.page_id, [i.message for i in o.report.issues] if o.report else None) for o in failed
    ]
    drafts = store.list_drafts()
    assert len(drafts) == len(plan.tasks)

    index = store.publish(facts.snapshot_id)
    assert len(index.pages) == len(plan.tasks)
    assert store.current_version() == index.version_id

    # 抽一页看内容：模块页有 front matter、证据引用、出处块
    page_text, result_text = store.load_draft("module/src")
    assert "id: module/src" in page_text
    assert "[E" in page_text
    assert "## 出处" in page_text
    assert '"page_id": "module/src"' in result_text


def test_task_package_structure(home: Path, work: Path) -> None:
    facts = _facts(home, work)
    plan = CoveragePlanner(max_tasks=1).plan(facts)
    builder = TaskPackageBuilder(work)
    packet = builder.build(plan.tasks[0], facts)

    ws = packet.workspace
    assert (ws / "task.md").is_file()
    assert (ws / "constraints.md").is_file()
    assert (ws / "meta.json").is_file()
    assert (ws / "context" / "facts.json").is_file()
    assert (ws / "context" / "related_pages.json").is_file()
    assert (ws / "output").is_dir()
    assert (ws / "logs").is_dir()
    snippets = list((ws / "context" / "snippets").glob("E*.txt"))
    assert snippets, "证据片段不应为空"
    # 证据片段内容与 meta.json 一致且能在工作树找到
    import json

    meta = json.loads((ws / "meta.json").read_text(encoding="utf-8"))
    first = meta["evidence"][0]
    snippet = (ws / "context" / "snippets" / f"{first['id']}.txt").read_text(encoding="utf-8")
    assert first["quote"] in snippet
    source = (work / first["file"]).read_text(encoding="utf-8")
    assert first["quote"] in source


def test_repair_loop_recovers_on_third_attempt(home: Path, work: Path) -> None:
    facts = _facts(home, work)
    store = FilesystemPageStore("sample")
    plan = CoveragePlanner(max_tasks=1).plan(facts)
    backend = MockBackend(
        failures=[
            {"attempt": 1, "kind": FAIL_BAD_EVIDENCE_REF},
            {"attempt": 2, "kind": FAIL_BAD_EVIDENCE_REF},
        ]
    )
    known = {t.page_id for t in plan.tasks}
    pipeline = GeneratePipeline(
        backend=backend,
        builder=TaskPackageBuilder(work),
        verifier=PageVerifier(facts, work, known_pages=known),
        store=store,
        max_rounds=3,
    )
    outcome = pipeline.run_task(plan.tasks[0], facts)
    assert outcome.ok
    assert outcome.attempts == 3


def test_repair_loop_exhausted_keeps_old_page(home: Path, work: Path) -> None:
    facts = _facts(home, work)
    store = FilesystemPageStore("sample")

    # 第一轮：正常生成成功，页面入草稿区
    plan = CoveragePlanner(max_tasks=1).plan(facts)
    task = plan.tasks[0]
    _, pipeline = _pipeline(work, facts, store)
    assert pipeline.run_task(task, facts).ok
    old_page, _ = store.load_draft(task.page_id)

    # 第二轮：后端连续 3 轮都坏 → failed 且草稿区仍是旧页面
    bad_backend = MockBackend(
        failures=[{"attempt": i, "kind": FAIL_BAD_EVIDENCE_REF} for i in range(1, 4)]
    )
    known = set(store.list_drafts())
    pipeline2 = GeneratePipeline(
        backend=bad_backend,
        builder=TaskPackageBuilder(work),
        verifier=PageVerifier(facts, work, known_pages=known),
        store=store,
        max_rounds=3,
    )
    outcome = pipeline2.run_task(task, facts)
    assert not outcome.ok
    assert outcome.status is PageStatus.FAILED
    assert outcome.attempts == 3
    current_page, _ = store.load_draft(task.page_id)
    assert current_page == old_page


def test_incremental_reuse_unchanged_pages(home: Path, work: Path) -> None:
    facts_v1 = _facts(home, work, revision="rev1")
    store = FilesystemPageStore("sample")
    plan1, pipeline1 = _pipeline(work, facts_v1, store)
    outcomes1 = pipeline1.run_plan(plan1, facts_v1)
    assert all(o.ok for o in outcomes1)

    # 改动一个测试文件 → rev2 事实
    test_file = work / "tests" / "test_sample.py"
    test_file.write_text(
        test_file.read_text(encoding="utf-8") + "\n\ndef test_extra():\n    assert True\n",
        encoding="utf-8",
    )
    facts_v2 = _facts(home, work, revision="rev2")

    plan2 = CoveragePlanner().plan(facts_v2, store.load_deps())
    reused = {t.page_id for t in plan2.tasks if t.reuse}
    fresh = {t.page_id for t in plan2.tasks if not t.reuse}
    # tests/test_sample.py 变化的页面需要重生成；src 模块页复用
    assert "test/overview" in fresh
    assert "module/tests" in fresh
    assert {"module/src", "flow/src/main", "interface/src/engine"} <= reused


def test_empty_facts_plan_is_empty(home: Path, work: Path) -> None:
    from codingwiki.domain.facts import RepoFacts

    facts = RepoFacts(repo_id="r", snapshot_id="r@x", revision="x", extractor_version="facts-v1")
    plan = CoveragePlanner().plan(facts)
    assert plan.tasks == []
