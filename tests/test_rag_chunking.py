"""切块测试（架构文档 v0.5 14.1）：fixture 仓库事实 → 代码/事实块；合成 Wiki 页切块。"""

from __future__ import annotations

import shutil
from collections.abc import Generator
from pathlib import Path

import pytest

from codingwiki.domain.facts import RepoFacts
from codingwiki.domain.models import RepoType
from codingwiki.domain.rag import Chunk
from codingwiki.facts import FilesystemFactExtractor, FilesystemFactStore
from codingwiki.retrieval import ChunkBuilder
from codingwiki.retrieval.chunking import _split_long, _strip_front_matter, _wiki_chunks
from codingwiki.snapshot import FilesystemSnapshotStore

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


def _facts(home: Path, work: Path) -> RepoFacts:
    snapshots = FilesystemSnapshotStore()
    snapshots.create("sample", "rev1", work, RepoType.GIT)
    snapshot = snapshots.snapshot("sample@rev1")
    store = FilesystemFactStore()
    facts = FilesystemFactExtractor(store).extract(snapshot, work)
    store.save(facts)
    return facts


def test_code_chunks_from_fixture(home: Path, work: Path) -> None:
    facts = _facts(home, work)
    chunks = ChunkBuilder(work).build(facts)
    code = [c for c in chunks if c.source_type == "code"]
    assert code
    for chunk in code:
        assert chunk.trust_tier == "code"
        assert chunk.repo_id == "sample"
        assert chunk.snapshot_id == facts.snapshot_id
        assert chunk.start_line >= 1
        assert chunk.end_line >= chunk.start_line
        assert chunk.text.strip()
    # fixture：Engine 类声明在 engine.h，run 方法定义在 main.cu
    engine = [c for c in code if c.symbol == "phy::Engine"]
    assert engine and engine[0].file == "src/engine.h"
    assert "class Engine" in engine[0].text
    run = [c for c in code if c.symbol == "phy::Engine::run"]
    assert run and run[0].file == "src/main.cu"


def test_fact_chunks_cover_build_config_test(home: Path, work: Path) -> None:
    facts = _facts(home, work)
    chunks = ChunkBuilder(work).build(facts)
    fact_chunks = [c for c in chunks if c.trust_tier == "fact"]
    kinds = {c.source_type for c in fact_chunks}
    assert "build" in kinds  # CMakeLists.txt 目标
    assert "config" in kinds  # config.yaml 键
    assert "test" in kinds  # pytest 用例
    for chunk in fact_chunks:
        assert chunk.reference  # 事实块必须有可读出处


def test_chunk_id_deterministic_across_builds(home: Path, work: Path) -> None:
    facts = _facts(home, work)
    first = [c.chunk_id for c in ChunkBuilder(work).build(facts)]
    second = [c.chunk_id for c in ChunkBuilder(work).build(facts)]
    assert first == second
    assert len(set(first)) == len(first)  # 无重复


def test_wiki_chunks_split_by_heading_and_strip_front_matter() -> None:
    text = (
        "---\nid: overview\ntitle: 总览\n---\n\n"
        "# 总览\n\n简介段。\n\n## 小节A\n\n内容A。\n\n## 小节B\n\n内容B\n"
    )
    chunks = _wiki_chunks("sample", "snap1", "overview", "总览", text)
    assert len(chunks) == 3
    assert all(c.trust_tier == "wiki" and c.page_id == "overview" for c in chunks)
    assert chunks[0].text.startswith("# 总览")
    assert "id: overview" not in chunks[0].text  # front matter 已剥离
    assert chunks[1].title == "总览 / 总览 / 小节A" or chunks[1].title.endswith("小节A")
    assert chunks[2].text.endswith("内容B")


def test_wiki_chunks_reference_uses_page_id() -> None:
    chunks = _wiki_chunks("sample", "snap1", "home", "首页", "# 首页\n\n内容\n")
    assert chunks[0].reference == "wiki:home"


def test_split_long_preserves_content_with_overlap() -> None:
    lines = [f"line {i} " + "x" * 100 for i in range(200)]
    text = "\n".join(lines)
    parts = _split_long(text)
    assert len(parts) > 1
    joined = "\n".join(parts)
    assert "line 0" in joined
    assert "line 199" in joined
    # 块间有重叠：第 1 块末尾的行会出现在第 2 块开头
    assert parts[0].splitlines()[-1] in parts[1].splitlines()[0 : len(parts[0].splitlines())]


def test_strip_front_matter_without_block() -> None:
    assert _strip_front_matter("# 标题\n正文\n") == "# 标题\n正文\n"


def test_build_with_wiki_pages(home: Path, work: Path) -> None:
    facts = _facts(home, work)
    pages = [("module/src", "src 模块", "# src 模块\n\n负责核心调度。\n")]
    chunks = ChunkBuilder(work).build(facts, pages)
    wiki = [c for c in chunks if c.source_type == "wiki"]
    assert len(wiki) == 1
    assert wiki[0].page_id == "module/src"
    assert "核心调度" in wiki[0].text


def test_missing_file_symbols_skipped(home: Path, work: Path) -> None:
    facts = _facts(home, work)
    # 删掉一个源文件：其符号跳过，其余不受影响
    (work / "src" / "helper.c").unlink()
    chunks = ChunkBuilder(work).build(facts)
    assert all(c.file != "src/helper.c" or c.source_type != "code" for c in chunks)
    assert any(c.source_type == "code" for c in chunks)


def test_chunk_model_forbids_extra_fields() -> None:
    with pytest.raises(ValueError):
        Chunk(
            chunk_id="x",
            source_type="code",
            repo_id="r",
            snapshot_id="s",
            text="t",
            trust_tier="code",
            bogus_field=1,  # type: ignore[call-arg]
        )
