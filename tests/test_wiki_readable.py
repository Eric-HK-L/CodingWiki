"""S6.5-b/c 测试：页面粒度、任务包裁剪、人可读质量门与站点。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from codingwiki.backends import MockBackend
from codingwiki.domain.facts import (
    CallFact,
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
    PageStatus,
    PageType,
    TaskMeta,
)
from codingwiki.generate import CoveragePlanner, TaskPackageBuilder
from codingwiki.generate.modules import call_degrees, rank_symbols
from codingwiki.verify import PageVerifier

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "facts" / "sample"
QUOTE = "int run(void) {\n    return 0;\n}"


def _symbol(index: int) -> SymbolFact:
    path = f"src/pkg{index % 80}/f{index}.c"
    return SymbolFact(
        symbol_id=f"{path}::f{index}@{index + 1}",
        name=f"f{index}",
        qualified_name=f"f{index}",
        kind=SymbolKind.FUNCTION,
        path=path,
        line=index + 1,
        end_line=index + 3,
        signature=f"int f{index}(void)",
    )


def _materialize(facts: RepoFacts, root: Path) -> None:
    for file in facts.files:
        target = root / file.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "\n".join(f"line {number}" for number in range(1, 501)) + "\n",
            encoding="utf-8",
        )


def _large_facts(count: int = 160) -> RepoFacts:
    files: list[FileFacts] = []
    symbols: list[SymbolFact] = []
    calls: list[CallFact] = []
    for index in range(count):
        symbol = _symbol(index)
        path = symbol.path
        symbols.append(symbol)
        calls.append(
            CallFact(
                path=path,
                line=index + 5,
                caller=symbol.symbol_id,
                callee="target",
                targets=[_symbol(0).symbol_id] if index % 5 == 0 else [],
            )
        )
        files.append(
            FileFacts(
                path=path,
                language="c",
                blob_sha256=f"blob{index}",
                module=path.rsplit("/", 1)[0],
                symbols=[symbol],
                calls=[calls[-1]],
            )
        )
    return RepoFacts(
        repo_id="large",
        snapshot_id="large@1",
        revision="1",
        extractor_version="facts-v1",
        files=files,
    )


def test_plan_uses_directory_modules_not_symbols() -> None:
    facts = _large_facts()
    plan = CoveragePlanner().plan(facts)
    modules = [task for task in plan.tasks if task.page_type is PageType.MODULE]
    interfaces = [task for task in plan.tasks if task.page_type is PageType.INTERFACE]
    flows = [task for task in plan.tasks if task.page_type is PageType.FLOW]

    assert len(modules) <= 60
    assert len(interfaces) <= 30
    assert len(flows) <= 15
    # 160 个符号只产生目录级模块页，不再一个符号一页
    assert len(modules) < len(facts.files)
    assert any(task.rollup for task in modules)


def test_plan_fixture_page_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codingwiki.domain.models import RepoType
    from codingwiki.facts import FilesystemFactExtractor, FilesystemFactStore
    from codingwiki.snapshot import FilesystemSnapshotStore

    monkeypatch.setenv("CODINGWIKI_HOME", str(tmp_path / ".cw"))
    work = tmp_path / "work"
    shutil.copytree(FIXTURES, work)
    snapshots = FilesystemSnapshotStore()
    snapshots.create("sample", "rev1", work, RepoType.GIT)
    facts = FilesystemFactExtractor(FilesystemFactStore()).extract(
        snapshots.snapshot("sample@rev1"), work
    )
    plan = CoveragePlanner().plan(facts)
    page_ids = {task.page_id for task in plan.tasks}

    assert "module/src" in page_ids
    assert "module/tests" in page_ids
    assert "interface/src/engine" in page_ids
    assert not any(page_id.startswith("interface/src/engine.h/") for page_id in page_ids)
    assert len(plan.tasks) <= 120


def test_rank_symbols_prefers_called_symbol() -> None:
    facts = _large_facts(40)
    degrees = call_degrees(facts)
    ranked = rank_symbols([symbol for file in facts.files for symbol in file.symbols], degrees, 10)
    assert ranked[0].name == "f0"


def test_task_package_slices_files_symbols_callers_and_terms(tmp_path: Path) -> None:
    facts = _large_facts()
    _materialize(facts, tmp_path)
    plan = CoveragePlanner().plan(facts)
    module = next(task for task in plan.tasks if task.page_type is PageType.MODULE)
    builder = TaskPackageBuilder(tmp_path, base_dir=tmp_path / "jobs")
    packet = builder.build(module, facts)

    context = packet.workspace / "context"
    assert (context / "files.json").is_file()
    assert (context / "callers.json").is_file()
    assert (context / "glossary.json").is_file()
    assert (packet.workspace / "task.md").read_text(encoding="utf-8").count("职责边界") >= 1

    facts_json = json.loads((context / "facts.json").read_text(encoding="utf-8"))
    assert len(facts_json["symbols"]) <= 40
    assert facts_json["files"]
    meta = json.loads((packet.workspace / "meta.json").read_text(encoding="utf-8"))
    assert len(meta["evidence"]) <= 72
    snippets = sorted((context / "snippets").glob("E*.txt"))
    assert snippets
    for snippet in snippets:
        assert len(snippet.read_text(encoding="utf-8").splitlines()) <= 42


# ---------------------------------------------------------------- 质量门


@pytest.fixture
def verifier_env(tmp_path: Path) -> tuple[PageVerifier, TaskMeta]:
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "src" / "a.c").write_text("// header\n" + QUOTE + "\n", encoding="utf-8")
    symbol = SymbolFact(
        symbol_id="src/a.c::run@2",
        name="run",
        qualified_name="run",
        kind=SymbolKind.FUNCTION,
        path="src/a.c",
        line=2,
        end_line=4,
        signature="int run(void)",
    )
    facts = RepoFacts(
        repo_id="r",
        snapshot_id="r@1",
        revision="1",
        extractor_version="facts-v1",
        files=[FileFacts(path="src/a.c", language="c", blob_sha256="x", symbols=[symbol])],
    )
    meta = TaskMeta(
        task_id="t",
        page_id="module/src",
        page_type=PageType.MODULE,
        repo_id="r",
        snapshot_id="r@1",
        quality_profile="readable",
        prompt_version="module_page@2",
        evidence=[Evidence(id="E1", file="src/a.c", symbol="run", quote=QUOTE, line_hint=2)],
    )
    return PageVerifier(facts, work), meta


def _page(meta: TaskMeta, body: str) -> str:
    page_meta = PageMeta(
        id=meta.page_id,
        type=meta.page_type,
        title="模块 src",
        snapshot=meta.snapshot_id,
        status=PageStatus.DRAFT,
        evidence_count=len(meta.evidence),
        generated_by="mock/mock-v1",
        prompt_version=meta.prompt_version,
    )
    evidence_block = "\n".join(
        f"[{ev.id}]: {ev.file}:{ev.line_hint} · {ev.symbol}" for ev in meta.evidence
    )
    return f"{dump_front_matter(page_meta)}\n{body}\n\n## 出处\n\n{evidence_block}\n"


def _result(meta: TaskMeta) -> str:
    return PageResult(
        page_id=meta.page_id,
        snapshot=meta.snapshot_id,
        claims=[Claim(id="C1", text="模块提供 run 函数。[E1]", evidence=["E1"])],
        evidence=list(meta.evidence),
    ).model_dump_json()


def _good_body(extra: str = "") -> str:
    return (
        "# 模块 src\n\n"
        "> 一句话职责：负责把输入参数转换为可验证的状态码，供上层流程调用。\n\n"
        "## 职责边界\n\n"
        "- 负责：实现 run 并返回状态码。[E1]\n"
        "- 不负责：进程启动与外部 I/O。\n\n"
        "本模块的边界以公开入口为准：入口之外的实现细节不承诺兼容，"
        "调用方只能依赖签名与返回码。[E1]\n\n"
        "## 关键数据结构\n\n"
        "| 类型 | 位置 | 作用 |\n|---|---|---|\n"
        "| `run` | src/a.c:2 | 公开函数入口。[E1] |\n\n"
        "## 对外入口\n\n"
        "| 入口 | 签名 | 使用场景 |\n|---|---|---|\n"
        "| `run` | `int run(void)` | 上层流程调用。[E1] |\n\n"
        "## 内部结构\n\n"
        "入口先校验参数，再执行核心逻辑，最后返回状态码；实现只依赖本文件。[E1]\n"
        "内部实现不跨目录访问，所有共享能力都通过公开声明暴露，"
        "因此修改内部函数不会影响其他模块的编译。[E1]\n\n"
        "## 依赖与影响\n\n"
        "- 依赖：本文件的公开声明。[E1]\n"
        "- 被谁使用：上层流程与测试通过 run 入口调用。[E1]\n\n"
        "依赖方向只从上层指向本模块；本模块不反向引用示例或测试，"
        "避免生成页与运行入口形成环。[E1]\n\n"
        "## 常见改动指引\n\n"
        "修改 run 签名时需要同步检查调用方与测试，并重新生成本页。[E1]\n"
        "新增入口时先补公开声明，再补测试与调用方说明，保持页面与代码同步。[E1]\n" + extra
    )


def test_readable_page_passes(verifier_env: tuple[PageVerifier, TaskMeta]) -> None:
    verifier, meta = verifier_env
    report = verifier.verify(_page(meta, _good_body()), _result(meta), meta)
    assert report.status == "passed", report.issues


def test_placeholder_phrase_fails(verifier_env: tuple[PageVerifier, TaskMeta]) -> None:
    verifier, meta = verifier_env
    report = verifier.verify(
        _page(meta, _good_body("\n这段逻辑可能还需要补充。[E1]\n")), _result(meta), meta
    )
    assert report.status == "failed"
    assert "placeholder_phrase" in {issue.rule for issue in report.issues}


def test_short_module_page_fails(verifier_env: tuple[PageVerifier, TaskMeta]) -> None:
    verifier, meta = verifier_env
    short = _good_body().split("## 内部结构")[0]
    report = verifier.verify(_page(meta, short), _result(meta), meta)
    assert report.status == "failed"
    assert "readable_body_too_short" in {issue.rule for issue in report.issues}


def test_low_evidence_density_fails(verifier_env: tuple[PageVerifier, TaskMeta]) -> None:
    verifier, meta = verifier_env
    body = _good_body().replace("[E1]", "")
    report = verifier.verify(
        _page(meta, body + "\n" + "补充说明。" * 80 + "\n"), _result(meta), meta
    )
    assert report.status == "failed"
    assert "evidence_density_low" in {issue.rule for issue in report.issues}


def test_missing_section_fails(verifier_env: tuple[PageVerifier, TaskMeta]) -> None:
    verifier, meta = verifier_env
    body = _good_body().replace("## 依赖与影响", "## 其他")
    report = verifier.verify(_page(meta, body), _result(meta), meta)
    assert report.status == "failed"
    assert "required_section_missing" in {issue.rule for issue in report.issues}


def test_boilerplate_one_liner_fails(verifier_env: tuple[PageVerifier, TaskMeta]) -> None:
    verifier, meta = verifier_env
    body = _good_body().replace(
        "> 一句话职责：负责把输入参数转换为可验证的状态码，供上层流程调用。",
        "> 一句话职责：本模块包含 16 个符号。",
    )
    report = verifier.verify(_page(meta, body), _result(meta), meta)
    assert report.status == "failed"
    assert "responsibility_boilerplate" in {issue.rule for issue in report.issues}


def test_mock_backend_passes_readable_gate(tmp_path: Path) -> None:
    """CI 地板也必须满足人可读质量门，否则真实生成与回归会分叉。"""
    facts = _large_facts()
    _materialize(facts, tmp_path)
    plan = CoveragePlanner().plan(facts)
    module = next(task for task in plan.tasks if task.page_type is PageType.MODULE)
    builder = TaskPackageBuilder(tmp_path, base_dir=tmp_path / "jobs")
    packet = builder.build(module, facts)
    assert MockBackend().run(packet).status.value == "succeeded"
    meta = TaskMeta.model_validate_json(
        (packet.workspace / "meta.json").read_text(encoding="utf-8")
    )
    verifier = PageVerifier(facts, tmp_path)
    page_text = (packet.output_dir / "page.md").read_text(encoding="utf-8")
    result_text = (packet.output_dir / "result.json").read_text(encoding="utf-8")
    report = verifier.verify(page_text, result_text, meta)
    assert report.status == "passed", [(i.rule, i.message) for i in report.issues]
