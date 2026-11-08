"""S11 覆盖率看板测试：矩阵口径已知值、站点看板页、评测/任务缺省、CLI coverage。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codingwiki.domain.dashboard import CoverageRow
from codingwiki.domain.facts import EntryFact, FileFacts, RepoFacts, SymbolFact, SymbolKind
from codingwiki.domain.wiki import (
    PageDeps,
    PageStatus,
    PageType,
    VersionIndex,
    VersionPageEntry,
)
from codingwiki.publish.dashboard import build_current_dashboard, build_dashboard
from codingwiki.publish.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- fixture 构造


def _symbol(path: str, name: str, kind: SymbolKind, line: int = 1) -> SymbolFact:
    return SymbolFact(
        symbol_id=f"{path}::{name}@{line}",
        name=name,
        qualified_name=name,
        kind=kind,
        path=path,
        line=line,
        end_line=line + 5,
    )


def _entry(path: str, name: str = "main", line: int = 10) -> EntryFact:
    return EntryFact(path=path, line=line, name=name, kind="main")


def _facts() -> RepoFacts:
    files = [
        FileFacts(
            path="src/mod_a/engine.h",
            language="cpp",
            blob_sha256="a" * 64,
            module="src/mod_a",
            symbols=[
                _symbol("src/mod_a/engine.h", "Engine", SymbolKind.CLASS),
                _symbol("src/mod_a/engine.h", "engine_start", SymbolKind.FUNCTION),
            ],
        ),
        FileFacts(
            path="src/mod_a/kernel.cu",
            language="cuda",
            blob_sha256="b" * 64,
            module="src/mod_a",
            symbols=[_symbol("src/mod_a/kernel.cu", "run_kernel", SymbolKind.KERNEL)],
            entries=[_entry("src/mod_a/kernel.cu")],
        ),
        FileFacts(
            path="src/mod_b/util.cpp",
            language="cpp",
            blob_sha256="c" * 64,
            module="src/mod_b",
            symbols=[],  # 无公开符号
        ),
        FileFacts(
            path="src/mod_b/other_main.cu",
            language="cuda",
            blob_sha256="d" * 64,
            module="src/mod_b",
            symbols=[],
            entries=[_entry("src/mod_b/other_main.cu", name="runner")],
        ),
    ]
    return RepoFacts(
        repo_id="demo",
        snapshot_id="demo@rev1",
        revision="rev1",
        extractor_version="test-1",
        files=files,
    )


def _index_and_deps() -> tuple[VersionIndex, dict[str, PageDeps]]:
    pages = [
        VersionPageEntry(
            page_id="module/src/mod_a",
            type=PageType.MODULE,
            status=PageStatus.VERIFIED,
            title="模块 mod_a",
            evidence_count=3,
        ),
        VersionPageEntry(
            page_id="interface/src/mod_a/engine",
            type=PageType.INTERFACE,
            status=PageStatus.VERIFIED,
            title="接口 engine.h",
            evidence_count=2,
        ),
        VersionPageEntry(
            page_id="flow/src/mod_a/kernel",
            type=PageType.FLOW,
            status=PageStatus.VERIFIED,
            title="流程 kernel",
            evidence_count=1,
        ),
    ]
    index = VersionIndex(
        version_id="v20261107-abcdef01",
        repo_id="demo",
        snapshot_id="demo@rev1",
        pages=pages,
    )
    deps = {
        "module/src/mod_a": PageDeps(
            page_id="module/src/mod_a",
            snapshot_id="demo@rev1",
            files={"src/mod_a/engine.h": "a" * 64, "src/mod_a/kernel.cu": "b" * 64},
        ),
        "interface/src/mod_a/engine": PageDeps(
            page_id="interface/src/mod_a/engine",
            snapshot_id="demo@rev1",
            files={"src/mod_a/engine.h": "a" * 64},
        ),
        "flow/src/mod_a/kernel": PageDeps(
            page_id="flow/src/mod_a/kernel",
            snapshot_id="demo@rev1",
            files={"src/mod_a/kernel.cu": "b" * 64},
        ),
    }
    return index, deps


# ---------------------------------------------------------------- 矩阵已知值


def test_build_dashboard_matrix_known_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODINGWIKI_HOME", str(tmp_path / ".cw"))  # evals/jobs 均缺失
    facts = _facts()
    index, deps = _index_and_deps()

    data = build_dashboard("demo", facts, index, deps)

    assert data.version_id == "v20261107-abcdef01"
    assert data.pages_total == 3
    assert data.pages_verified == 3
    assert data.pages_stale == 0
    assert data.evidence_total == 6
    assert data.eval_summary is None  # 缺失不报错
    assert data.job_summary is None

    by_module = {row.module: row for row in data.coverage}
    mod_a = by_module["src/mod_a"]
    # C1：两个文件都被 module 页 deps 引用
    assert (mod_a.files_total, mod_a.files_covered) == (2, 2)
    # C2：公开符号 3 个（CLASS+FUNCTION+KERNEL），interface 页覆盖 engine.h 的 2 个
    assert (mod_a.public_total, mod_a.public_covered) == (3, 2)
    # C3：1 个 main 入口（kernel.cu），flow 页覆盖
    assert (mod_a.entries_total, mod_a.entries_covered) == (1, 1)

    mod_b = by_module["src/mod_b"]
    # C1：无页面引用 → 0；C2：无公开符号 → 0/0；C3：入口存在但无 flow 页
    assert (mod_b.files_total, mod_b.files_covered) == (2, 0)
    assert (mod_b.public_total, mod_b.public_covered) == (0, 0)
    assert (mod_b.entries_total, mod_b.entries_covered) == (1, 0)

    total = by_module["total"]
    assert total == CoverageRow(
        module="total",
        files_total=4,
        files_covered=2,
        public_total=3,
        public_covered=2,
        entries_total=2,
        entries_covered=1,
    )


def test_coverage_dedupes_duplicate_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S17：同路径重复 FileFacts 不得抬高 C1/C2 分母。"""
    monkeypatch.setenv("CODINGWIKI_HOME", str(tmp_path / ".cw"))
    facts = _facts()
    duplicated = facts.model_copy(update={"files": [*facts.files, facts.files[0]]})
    index, deps = _index_and_deps()

    data = build_dashboard("demo", duplicated, index, deps)

    total = next(row for row in data.coverage if row.module == "total")
    assert total.files_total == 4
    assert total.public_total == 3


def test_build_dashboard_entry_dedupe_prefers_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODINGWIKI_HOME", str(tmp_path / ".cw"))
    files = [
        FileFacts(
            path="src/x/a_main.cu",
            language="cuda",
            blob_sha256="a" * 64,
            module="src/x",
            entries=[
                _entry("src/x/a_main.cu", name="runner", line=5),
                _entry("src/x/a_main.cu", name="main", line=20),
            ],
        ),
    ]
    facts = RepoFacts(
        repo_id="demo", snapshot_id="demo@r", revision="r", extractor_version="test-1", files=files
    )
    index = VersionIndex(version_id="v1", repo_id="demo", snapshot_id="demo@r", pages=[])
    data = build_dashboard("demo", facts, index, {})
    row = data.coverage[0]
    assert (row.entries_total, row.entries_covered) == (1, 0)


# ---------------------------------------------------------------- 站点看板页


def test_site_contains_dashboard_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODINGWIKI_HOME", str(tmp_path / ".cw"))
    facts = _facts()
    index, deps = _index_and_deps()
    data = build_dashboard("demo", facts, index, deps)

    version_dir = tmp_path / "version"
    payload = [(page.page_id, f"# {page.title}\n\n正文", "{}") for page in index.pages]
    site = build_site(version_dir, index, payload, tmp_path / "work", {}, dashboard=data)

    dashboard_html = site / "dashboard.html"
    assert dashboard_html.is_file()
    text = dashboard_html.read_text(encoding="utf-8")
    assert "覆盖率矩阵" in text
    assert "src/mod_a" in text
    assert "2/3" in text  # C2：2/3 公开符号覆盖
    assert "未评测" in text and "未运行" in text

    home = (site / "index.html").read_text(encoding="utf-8")
    assert "dashboard.html" in home  # 首页有看板入口链接


# ---------------------------------------------------------------- CLI coverage（子进程端到端）


def _run(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "codingwiki", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, timeout=30)


def test_cli_coverage_end_to_end(tmp_path: Path) -> None:
    fixtures = REPO_ROOT / "tests" / "fixtures" / "facts" / "sample"
    origin = tmp_path / "origin"
    origin.mkdir()
    for item in fixtures.rglob("*"):
        if item.is_file():
            dest = origin / item.relative_to(fixtures)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(item.read_bytes())
    _git(origin, "init", "-b", "main")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "fixture")

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    (config_dir / "sample.yaml").write_text(
        "\n".join(["repo_id: sample", "provider: git", f"url: {origin}", "branch: main", ""]),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "CODINGWIKI_HOME": str(tmp_path / ".cw"),
        "PYTHONPATH": str(REPO_ROOT / "src"),
    }

    for cmd in (
        ["repo", "sync", "--repo", "sample", "--config-dir", str(config_dir)],
        ["facts", "extract", "--repo", "sample", "--config-dir", str(config_dir)],
        ["wiki", "generate", "--repo", "sample", "--config-dir", str(config_dir)],
        ["wiki", "publish", "--repo", "sample", "--config-dir", str(config_dir)],
    ):
        result = _run(cmd, cwd=tmp_path, env=env)
        assert result.returncode == 0, result.stderr

    coverage = _run(["wiki", "coverage", "--repo", "sample"], cwd=tmp_path, env=env)
    assert coverage.returncode == 0, coverage.stderr
    assert "覆盖率矩阵" in coverage.stdout
    assert "total" in coverage.stdout
    assert "未评测" in coverage.stdout  # 没跑过 eval

    # 看板页进站点且数据同源
    site = tmp_path / ".cw" / "wiki" / "sample"
    version = (site / "current").read_text(encoding="utf-8").strip()
    dashboard = site / "versions" / version / "site" / "dashboard.html"
    assert dashboard.is_file()
    assert "覆盖率矩阵" in dashboard.read_text(encoding="utf-8")


def test_coverage_without_published_version_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODINGWIKI_HOME", str(tmp_path / ".cw"))
    store = __import__("codingwiki.publish", fromlist=["FilesystemPageStore"]).FilesystemPageStore(
        "demo", base_dir=tmp_path / ".cw"
    )
    with pytest.raises(Exception, match="还没有发布版本"):
        build_current_dashboard("demo", _facts(), store)


def test_dashboard_reads_latest_eval_and_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODINGWIKI_HOME", str(tmp_path / ".cw"))
    evals = tmp_path / ".cw" / "evals" / "demo"
    evals.mkdir(parents=True)
    evals.joinpath("20261107-000000-cuphy-v2.json").write_text(
        json.dumps(
            {
                "set_id": "cuphy-v2",
                "created_at": "2026-11-07T00:00:00Z",
                "recall_at_k": 1.0,
                "mrr": 0.86,
                "refusal_accuracy": 0.946,
            }
        ),
        encoding="utf-8",
    )
    jobs = tmp_path / ".cw" / "jobs" / "demo"
    jobs.mkdir(parents=True)
    jobs.joinpath("20261107-010101-20261107T010101Z.json").write_text(
        json.dumps(
            {
                "job_id": "20261107T010101Z",
                "repo_id": "demo",
                "status": "failed",
                "error": "wiki: 页面生成失败",
                "snapshot_id": "demo@r",
            }
        ),
        encoding="utf-8",
    )

    index = VersionIndex(version_id="v1", repo_id="demo", snapshot_id="demo@r", pages=[])
    data = build_dashboard("demo", _facts(), index, {})
    assert data.eval_summary is not None
    assert data.eval_summary.set_id == "cuphy-v2"
    assert data.eval_summary.recall_at_k == 1.0
    assert data.job_summary is not None
    assert data.job_summary.status == "failed"
    assert "页面生成失败" in data.job_summary.error
