"""S36：GitHub Wiki 导出的确定性转换。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from codingwiki.domain.facts import (
    CallFact,
    EntryFact,
    FileFacts,
    RepoFacts,
    SymbolFact,
    SymbolKind,
)
from codingwiki.domain.wiki import (
    PageStatus,
    PageType,
    VersionIndex,
    VersionPageEntry,
)
from codingwiki.publish.github_wiki import WikiTarget, build_github_wiki, wiki_page_name

SHA = "4f65f97c1d5f701ce911f7dda8f1b1f3f0c7693c"


def _symbol(path: str, name: str, line: int) -> SymbolFact:
    return SymbolFact(
        symbol_id=f"{path}::{name}@{line}",
        name=name,
        qualified_name=name,
        kind=SymbolKind.FUNCTION,
        path=path,
        line=line,
        end_line=line + 4,
    )


def _facts() -> RepoFacts:
    main = _symbol("src/pkg/a.c", "main", 3)
    helper = _symbol("src/pkg/a.c", "helper", 12)
    init = _symbol("src/other/b.c", "init", 5)
    file_a = FileFacts(
        path="src/pkg/a.c",
        language="c",
        blob_sha256="a" * 64,
        line_count=40,
        symbols=[main, helper],
        entries=[EntryFact(path="src/pkg/a.c", line=3, name="main", kind="main")],
        calls=[
            CallFact(
                path="src/pkg/a.c",
                line=8,
                caller=main.symbol_id,
                callee="helper",
                targets=[helper.symbol_id],
            ),
            CallFact(
                path="src/pkg/a.c",
                line=9,
                caller="",
                callee="init",
                targets=[init.symbol_id],
            ),
        ],
    )
    file_b = FileFacts(
        path="src/other/b.c",
        language="c",
        blob_sha256="b" * 64,
        line_count=20,
        symbols=[init],
        calls=[
            CallFact(
                path="src/other/b.c",
                line=7,
                caller=init.symbol_id,
                callee="helper",
                targets=[helper.symbol_id],
            )
        ],
    )
    return RepoFacts(
        snapshot_id=f"demo@{SHA}",
        repo_id="demo",
        revision=SHA,
        extractor_version="test",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        files=[file_a, file_b],
    )


def _index() -> VersionIndex:
    entries = [
        ("home", PageType.HOME, "首页"),
        ("overview", PageType.OVERVIEW, "项目总览"),
        ("architecture", PageType.ARCHITECTURE, "架构"),
        ("module/src/pkg", PageType.MODULE, "模块 src/pkg"),
        ("flow/src/pkg/a", PageType.FLOW, "流程 main（src/pkg/a.c）"),
        ("glossary", PageType.GLOSSARY, "术语"),
    ]
    return VersionIndex(
        version_id="v1",
        repo_id="demo",
        snapshot_id=f"demo@{SHA}",
        pages=[
            VersionPageEntry(
                page_id=page_id,
                type=page_type,
                status=PageStatus.VERIFIED,
                title=title,
            )
            for page_id, page_type, title in entries
        ],
    )


def _payload() -> list[tuple[str, str, str]]:
    overview_result = json.dumps(
        {
            "page_id": "overview",
            "snapshot": f"demo@{SHA}",
            "claims": [],
            "evidence": [
                {
                    "id": "E1",
                    "file": "src/pkg/a.c",
                    "symbol": "helper",
                    "quote": "helper",
                    "line_hint": 12,
                },
                {
                    "id": "E2",
                    "file": "src/pkg/a.c",
                    "symbol": "",
                    "quote": "int main",
                    "line_hint": 3,
                },
            ],
        }
    )
    return [
        (
            "home",
            "---\nid: home\ntype: home\ntitle: 首页\nsnapshot: demo\n---\n\n"
            "# 首页\n\n> 一句话职责：演示仓库。\n\n"
            "## 导航\n\n- 本任务提供的页面 ID 仅含此项 [E1]\n",
            "{}",
        ),
        (
            "overview",
            "---\nid: overview\ntype: overview\ntitle: 项目总览\nsnapshot: demo\n---\n\n"
            "# 项目总览\n\n正文入口是 `main` [E1][E2]。\n\n"
            "## 出处\n\n[E1]: src/pkg/a.c:12 · `helper` · 快照 demo\n",
            overview_result,
        ),
        (
            "architecture",
            "---\nid: architecture\ntype: architecture\ntitle: 架构\nsnapshot: demo\n---\n\n"
            "# 架构\n\n调用关系见 [模块页](module/src/pkg)。\n",
            "{}",
        ),
        (
            "module/src/pkg",
            "---\nid: module/src/pkg\ntype: module\ntitle: 模块 src/pkg\nsnapshot: demo\n---\n\n"
            "# 模块 src/pkg\n\n## 关键数据结构\n\n| 符号 | 位置 |\n| --- | --- |\n"
            "| `helper` | [E1] |\n",
            overview_result,
        ),
        (
            "flow/src/pkg/a",
            "---\nid: flow/src/pkg/a\ntype: flow\ntitle: 流程 main\nsnapshot: demo\n---\n\n"
            "# 流程 main\n\n从 main 开始。\n",
            "{}",
        ),
        (
            "glossary",
            "---\nid: glossary\ntype: glossary\ntitle: 术语\nsnapshot: demo\n---\n\n# 术语\n",
            "{}",
        ),
    ]


def _build(tmp_path: Path) -> Path:
    version_dir = tmp_path / "versions" / "v1"
    version_dir.mkdir(parents=True)
    target = WikiTarget.from_repo("demo", "https://github.com/acme/demo.git", SHA, root="sub")
    return build_github_wiki(version_dir, _index(), _payload(), _facts(), target)


def test_page_names_are_flat_and_github_safe() -> None:
    assert wiki_page_name("home") == "Home"
    assert wiki_page_name("build/overview") == "Build"
    assert wiki_page_name("module/src/cuphy/ch_est") == "Module-src-cuphy-ch_est"
    assert wiki_page_name("flow/examples/ss/testSS") == "Flow-examples-ss-testSS"
    assert "/" not in wiki_page_name("interface/src/cuphy")


def test_builds_navigation_skeleton(tmp_path: Path) -> None:
    out = _build(tmp_path)
    for name in ("Home.md", "Modules.md", "Flows.md", "Interfaces.md", "_Sidebar.md", "_Footer.md"):
        assert (out / name).is_file(), name

    home = (out / "Home.md").read_text(encoding="utf-8")
    assert "**演示仓库。**" not in home
    assert "演示仓库" in home
    assert "本任务提供" not in home
    # 未生成 build/config/test 时侧栏不应出现死链
    assert "[构建](Build)" not in home
    sidebar = (out / "_Sidebar.md").read_text(encoding="utf-8")
    assert "[构建](Build)" not in sidebar
    assert "[模块索引](Modules)" in sidebar


def test_strips_front_matter_citations_and_machine_appendix(tmp_path: Path) -> None:
    out = _build(tmp_path)
    overview = (out / "Overview.md").read_text(encoding="utf-8")
    assert not overview.startswith("---")
    assert "format_version" not in overview
    assert "[E1]" not in overview
    assert "## 出处" not in overview
    assert "## 参考源码" in overview
    assert f"https://github.com/acme/demo/blob/{SHA}/sub/src/pkg/a.c#L12" in overview


def test_rewrites_internal_links_to_wiki_pages(tmp_path: Path) -> None:
    out = _build(tmp_path)
    architecture = (out / "Architecture.md").read_text(encoding="utf-8")
    assert "(Module-src-pkg)" in architecture
    assert "](module/src/pkg)" not in architecture
    assert "```mermaid" in architecture
    assert "src/pkg" in architecture


def test_module_page_has_call_relation_graph(tmp_path: Path) -> None:
    out = _build(tmp_path)
    module = (out / "Module-src-pkg.md").read_text(encoding="utf-8")
    assert "## 调用关系" in module
    assert "```mermaid" in module
    assert '["src/pkg"]' in module
    # 引用编号被清掉后表格不能出现空单元格
    assert "|  |" not in module


def test_flow_page_starts_from_entry_symbol(tmp_path: Path) -> None:
    out = _build(tmp_path)
    flow = (out / "Flow-src-pkg-a.md").read_text(encoding="utf-8")
    assert "## 调用链" in flow
    assert '["main"] -->|1| ' in flow
    assert '"helper"' in flow
    # 文件顶层调用不应被当作流程起点
    assert '["init"] -->|1| ' not in flow
