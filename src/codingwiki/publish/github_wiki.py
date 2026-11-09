"""GitHub Wiki 导出（S36）。

把已发布版本的页面转成可直接 push 到 ``<repo>.wiki.git`` 的 Markdown：

- 去掉 front matter、行内 ``[E#]`` 与机器格式的「出处」段；
- 页尾生成「参考源码」表，链接到 GitHub blob 的具体行；
- 按事实层聚合调用边，生成 Mermaid 模块依赖图 / 模块调用关系 / 流程调用链；
- 生成 ``Home`` / ``_Sidebar`` / ``_Footer`` 与模块、接口、流程三类索引页。

导出是纯确定性转换，不调模型；页面集合与发布版本一一对应，
因此 RAG 索引仍按发布页全量切块，不受导出影响。
"""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from codingwiki.domain.facts import CallFact, FileFacts, RepoFacts, SymbolFact, SymbolKind
from codingwiki.domain.front_matter import parse_front_matter
from codingwiki.domain.wiki import VersionIndex
from codingwiki.generate.modules import is_public_symbol, module_groups

_INTERNAL_LINK = re.compile(
    r"\]\(((?:module|interface|flow|config|build|test|overview|home|architecture|glossary)"
    r"(?:/[^)\s]+)?)\)"
)
_CITATION = re.compile(r"[ \t]*(?:\[E\d+\])+")
_CITED_ID = re.compile(r"\[E(\d+)\]")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")

_PAGE_NAME = {
    "home": "Home",
    "overview": "Overview",
    "architecture": "Architecture",
    "glossary": "Glossary",
    "build/overview": "Build",
    "config/overview": "Configuration",
    "test/overview": "Testing",
}

_MODULE_GRAPH_EDGES = 24
_FOCUS_EDGES = 8
_FLOW_EDGES = 18
_FLOW_BRANCH = 6
_FLOW_HOPS = 3
_MAX_LABEL = 56

# 流程图画函数调用优先，把宏/变量/类型别名排到后面，避免日志宏淹没真实链路
_CALL_PRIORITY = {
    SymbolKind.KERNEL: 0,
    SymbolKind.FUNCTION: 0,
    SymbolKind.DEVICE_FUNCTION: 0,
    SymbolKind.METHOD: 1,
    SymbolKind.CLASS: 2,
    SymbolKind.STRUCT: 2,
    SymbolKind.ENUM: 3,
    SymbolKind.MACRO: 4,
    SymbolKind.TYPE_ALIAS: 5,
    SymbolKind.VARIABLE: 6,
    SymbolKind.NAMESPACE: 7,
    SymbolKind.MODULE: 7,
}


def _call_priority(symbol: SymbolFact) -> int:
    return _CALL_PRIORITY.get(symbol.kind, 5)


@dataclass(frozen=True)
class WikiTarget:
    """GitHub 侧的目标信息：源码仓库地址、快照 revision 与仓库内子目录。"""

    repo_id: str
    repo_url: str
    revision: str
    root: str = ""

    @classmethod
    def from_repo(cls, repo_id: str, url: str, revision: str, root: str = "") -> WikiTarget:
        clean = url.strip()
        if clean.endswith(".git"):
            clean = clean[: -len(".git")]
        return cls(repo_id=repo_id, repo_url=clean.rstrip("/"), revision=revision, root=root)

    def source_url(self, path: str, line: int = 0) -> str:
        rel = f"{self.root.strip('/')}/{path}" if self.root.strip("/") else path
        url = f"{self.repo_url}/blob/{self.revision}/{rel}"
        return f"{url}#L{line}" if line > 0 else url


def wiki_page_name(page_id: str) -> str:
    """页面 ID → GitHub Wiki 文件名（扁平、只用 [A-Za-z0-9._-]）。"""
    if page_id in _PAGE_NAME:
        return _PAGE_NAME[page_id]
    page_type, _sep, rest = page_id.partition("/")
    prefix = {"module": "Module", "interface": "Interface", "flow": "Flow"}.get(page_type)
    if prefix is None:
        return _sanitize(page_id)
    return f"{prefix}-{_sanitize(rest)}"


def build_github_wiki(
    version_dir: Path,
    index: VersionIndex,
    payload: list[tuple[str, str, str]],
    facts: RepoFacts,
    target: WikiTarget,
) -> Path:
    """生成 ``versions/<id>/wiki/``；先写临时目录再原子替换。"""
    tmp = version_dir / "wiki.tmp"
    site = version_dir / "wiki"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)

    names = {page_id: wiki_page_name(page_id) for page_id, _text, _result in payload}
    titles = {entry.page_id: entry.title for entry in index.pages}
    deps = _deps_files(version_dir)
    edges = _module_edges(facts)
    symbols = {symbol.symbol_id: symbol for file in facts.files for symbol in file.symbols}
    modules = module_groups(facts)

    for page_id, text, result_text in payload:
        body = _page_body(text)
        evidence = _evidence_map(result_text)
        if page_id == "home":
            rendered = _home_md(index, facts, modules, edges, names, body, target)
        else:
            rendered = _render_page(
                page_id=page_id,
                body=body,
                evidence=evidence,
                target=target,
                facts=facts,
                edges=edges,
                symbols=symbols,
                names=names,
                titles=titles,
            )
        _write(tmp / f"{names[page_id]}.md", rendered)

    _write(tmp / "Modules.md", _modules_index(index, modules, edges, deps, names))
    _write(tmp / "Interfaces.md", _interfaces_index(index, names))
    _write(tmp / "Flows.md", _flows_index(index, names, facts))
    _write(tmp / "_Sidebar.md", _sidebar(index, names, titles, modules, facts))
    _write(tmp / "_Footer.md", _footer(index, target))

    if site.exists():
        shutil.rmtree(site)
    tmp.rename(site)
    return site


# ---------------------------------------------------------------- 页面转换


def _render_page(
    *,
    page_id: str,
    body: str,
    evidence: dict[str, tuple[str, int, str]],
    target: WikiTarget,
    facts: RepoFacts,
    edges: Counter[tuple[str, str]],
    symbols: dict[str, SymbolFact],
    names: dict[str, str],
    titles: dict[str, str],
) -> str:
    body = _drop_section(body, "出处")
    cited = _cited_ids(body)
    body = _clean(body)
    body = _rewrite_links(body, names)

    parts = [body.rstrip()]
    if page_id == "architecture":
        graph = _module_graph_section(edges, names)
        if graph:
            parts.append(graph)
    if page_id.startswith("module/"):
        graph = _module_focus_section(page_id[len("module/") :], edges, names)
        if graph:
            parts.append(graph)
    if page_id.startswith("flow/"):
        graph = _flow_section(page_id, facts, symbols)
        if graph:
            parts.append(graph)
    related = _related_section(page_id, facts, names, titles)
    if related:
        parts.append(related)
    references = _reference_section(cited, evidence, target)
    if references:
        parts.append(references)
    return "\n\n".join(part for part in parts if part.strip()) + "\n"


def _clean(body: str) -> str:
    """去行内证据编号、压缩多余空格、修补被清空的表格单元格。"""
    out: list[str] = []
    in_code = False
    for line in body.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        if in_code:
            out.append(line)
            continue
        cleaned = _MULTI_SPACE.sub(" ", _CITATION.sub("", line))
        # 「见 [E2] 的签名」去编号后会留下中文之间的单空格
        cleaned = re.sub(r"(?<=[\u4e00-\u9fff]) (?=[\u4e00-\u9fff])", "", cleaned)
        if cleaned.lstrip().startswith("|") and not _is_separator(cleaned):
            cleaned = _fill_empty_cells(cleaned)
        out.append(cleaned.rstrip())
    return "\n".join(out)


def _is_separator(line: str) -> bool:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(set(cell) <= {"-", ":"} and cell for cell in cells)


def _fill_empty_cells(line: str) -> str:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    if any(not cell for cell in cells):
        cells = [cell or "—" for cell in cells]
        return "| " + " | ".join(cells) + " |"
    return line


def _rewrite_links(body: str, names: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        page_id = match.group(1)
        name = names.get(page_id)
        return f"]({name})" if name else match.group(0)

    return _INTERNAL_LINK.sub(replace, body)


def _page_body(text: str) -> str:
    try:
        _meta, body = parse_front_matter(text)
        return body
    except Exception:  # noqa: BLE001 - 导出不应因单页 front matter 失败而中断
        return _drop_front_matter(text)


def _drop_front_matter(text: str) -> str:
    stripped = text.lstrip("\ufeff\n")
    if not stripped.startswith("---"):
        return stripped
    rest = stripped[3:].lstrip("\n")
    end = rest.find("\n---")
    if end < 0:
        return stripped
    return rest[end + 4 :].lstrip("\n")


def _drop_section(body: str, heading: str) -> str:
    lines = body.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        if line.strip().startswith("## "):
            skipping = line.strip()[3:].strip() == heading
        if not skipping:
            out.append(line)
    return "\n".join(out)


def _cited_ids(body: str) -> list[str]:
    numbers = sorted({int(value) for value in _CITED_ID.findall(body)})
    return [f"E{number}" for number in numbers]


def _evidence_map(result_text: str) -> dict[str, tuple[str, int, str]]:
    try:
        data = json.loads(result_text)
    except (ValueError, TypeError):
        return {}
    items = data.get("evidence") if isinstance(data, dict) else None
    evidence: dict[str, tuple[str, int, str]] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("id") or "")
        path = str(item.get("file") or "")
        if not evidence_id or not path:
            continue
        line = int(item.get("line_hint") or 0)
        evidence[evidence_id] = (path, line, str(item.get("symbol") or ""))
    return evidence


def _reference_section(
    cited: list[str],
    evidence: dict[str, tuple[str, int, str]],
    target: WikiTarget,
) -> str:
    rows: list[str] = []
    fallback: list[str] = []
    for evidence_id in cited:
        item = evidence.get(evidence_id)
        if item is None:
            continue
        path, line, symbol = item
        name = path.rsplit("/", 1)[-1]
        location = f"{name}:{line}" if line > 0 else name
        row = f"| `{symbol}` | [`{location}`]({target.source_url(path, line)}) |"
        if symbol:
            rows.append(row)
        else:
            fallback.append(f"| — | [`{location}`]({target.source_url(path, line)}) |")
    if not rows:
        rows = fallback
    if not rows:
        return ""
    return "## 参考源码\n\n| 符号 | 源码位置 |\n| --- | --- |\n" + "\n".join(rows)


# ---------------------------------------------------------------- 调用关系图


def _module_edges(facts: RepoFacts) -> Counter[tuple[str, str]]:
    """模块级调用边：source_module → target_module，计数为解析到的调用次数。"""
    groups = module_groups(facts)
    by_path: dict[str, str] = {}
    by_symbol: dict[str, str] = {}
    for module, files in groups.items():
        for file in files:
            by_path[file.path] = module
            for symbol in file.symbols:
                by_symbol.setdefault(symbol.symbol_id, module)

    edges: Counter[tuple[str, str]] = Counter()
    for file in facts.files:
        source = by_path.get(file.path, "")
        if not source:
            continue
        for call in file.calls:
            for target in call.targets:
                destination = by_symbol.get(target)
                if destination and destination != source:
                    edges[(source, destination)] += 1
    return edges


def _mermaid(edges: list[tuple[str, str, int]]) -> str:
    nodes: list[str] = []
    for source, destination, _count in edges:
        for node in (source, destination):
            if node not in nodes:
                nodes.append(node)
    index = {node: f"n{position}" for position, node in enumerate(nodes)}
    lines = ["```mermaid", "graph LR"]
    for source, destination, count in edges:
        label = _mermaid_label(source)
        target_label = _mermaid_label(destination)
        lines.append(
            f'    {index[source]}["{label}"] -->|{count}| {index[destination]}["{target_label}"]'
        )
    lines.append("```")
    return "\n".join(lines)


def _mermaid_label(text: str) -> str:
    label = text.replace('"', "'").replace("|", "/")
    label = label if len(label) <= _MAX_LABEL else label[: _MAX_LABEL - 1] + "…"
    return label


def _module_graph_section(edges: Counter[tuple[str, str]], names: dict[str, str]) -> str:
    if not edges:
        return ""
    rows = sorted(edges.items(), key=lambda item: (-item[1], item[0]))[:_MODULE_GRAPH_EDGES]
    graph = _mermaid([(source, destination, count) for (source, destination), count in rows])
    total_edges = sum(edges.values())
    return (
        "## 模块依赖图\n\n"
        f"下图按调用边聚合：箭头 `A --> B` 表示 A 内的代码调用了 B 内的符号，"
        f"边上的数字是聚合后的调用次数。全仓库共 {total_edges} 条跨模块调用边，"
        f"这里取调用最频繁的 {len(rows)} 条。\n\n"
        f"{graph}"
    )


def _module_focus_section(
    module: str, edges: Counter[tuple[str, str]], names: dict[str, str]
) -> str:
    incoming = [
        (source, module, count) for (source, target), count in edges.items() if target == module
    ]
    outgoing = [
        (module, target, count) for (source, target), count in edges.items() if source == module
    ]
    if not incoming and not outgoing:
        return ""
    incoming.sort(key=lambda item: (-item[2], item[0]))
    outgoing.sort(key=lambda item: (-item[2], item[1]))

    parts = ["## 调用关系"]
    sections: list[str] = []
    if outgoing:
        sections.append(
            f"**本模块调用的模块（{len(outgoing)} 个）**\n\n"
            + _mermaid(
                [(source, target, count) for source, target, count in outgoing[:_FOCUS_EDGES]]
            )
        )
    if incoming:
        sections.append(
            f"**调用本模块的模块（{len(incoming)} 个）**\n\n"
            + _mermaid(
                [(source, target, count) for source, target, count in incoming[:_FOCUS_EDGES]]
            )
        )
    if len(outgoing) > _FOCUS_EDGES or len(incoming) > _FOCUS_EDGES:
        sections.append(
            f"> 每侧只展示调用次数最多的 {_FOCUS_EDGES} 条边；"
            "完整依赖可在[架构](Architecture)页与源码中查看。"
        )
    parts.append("\n\n".join(sections))
    return "\n\n".join(parts)


def _flow_section(page_id: str, facts: RepoFacts, symbols: dict[str, SymbolFact]) -> str:
    entry_path = _entry_path_for_flow(page_id, facts)
    if not entry_path:
        return ""
    edges = _flow_edges(facts, entry_path, symbols)
    if not edges:
        return ""
    return (
        "## 调用链\n\n"
        f"从 `{entry_path}` 的入口出发、{_FLOW_HOPS} 层内解析到的调用关系"
        f"（最多 {_FLOW_EDGES} 条边）：\n\n" + _mermaid(edges)
    )


def _flow_edges(
    facts: RepoFacts, entry_path: str, symbols: dict[str, SymbolFact]
) -> list[tuple[str, str, int]]:
    """从入口符号出发按符号做 BFS，避免把同文件里无关 helper 当作流程起点。"""
    file = next((item for item in facts.files if item.path == entry_path), None)
    if file is None:
        return []

    calls_by_caller: dict[str, list[CallFact]] = {}
    top_level: list[CallFact] = []
    for item in facts.files:
        for call in item.calls:
            if call.caller:
                calls_by_caller.setdefault(call.caller, []).append(call)
            elif item.path == entry_path:
                top_level.append(call)

    entry_names = {entry.name for entry in file.entries}
    roots = [symbol for symbol in file.symbols if symbol.name in entry_names]
    if not roots:
        roots = [symbol for symbol in file.symbols if symbol.name == "main"]

    edges: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str]] = set()

    def add(source: str, targets: list[str]) -> list[str]:
        ordered = sorted(
            {target for target in targets if target in symbols},
            key=lambda target: (_call_priority(symbols[target]), symbols[target].name),
        )
        next_ids: list[str] = []
        for target in ordered[:_FLOW_BRANCH]:
            symbol = symbols[target]
            edge = (source, symbol.name)
            if edge in seen:
                continue
            seen.add(edge)
            edges.append((source, symbol.name, 1))
            next_ids.append(target)
            if len(edges) >= _FLOW_EDGES:
                break
        return next_ids

    frontier: list[str] = []
    if roots:
        for root in roots:
            frontier.extend(
                add(
                    root.name,
                    [
                        target
                        for call in calls_by_caller.get(root.symbol_id, [])
                        for target in call.targets
                    ],
                )
            )
    else:
        stem = entry_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        frontier = add(stem, [target for call in top_level for target in call.targets])
    visited = set(frontier)
    for _hop in range(_FLOW_HOPS - 1):
        if len(edges) >= _FLOW_EDGES or not frontier:
            break
        next_frontier: list[str] = []
        for caller in frontier:
            label = symbols[caller].name if caller in symbols else "call"
            targets = [
                target for call in calls_by_caller.get(caller, []) for target in call.targets
            ]
            for target in add(label, targets):
                if target not in visited:
                    visited.add(target)
                    next_frontier.append(target)
        frontier = next_frontier
    return edges


def _entry_path_for_flow(page_id: str, facts: RepoFacts) -> str:
    stem = page_id[len("flow/") :]
    for file in facts.files:
        if file.path.rsplit(".", 1)[0] == stem:
            return file.path
    return ""


# ---------------------------------------------------------------- 导航与索引


def _home_md(
    index: VersionIndex,
    facts: RepoFacts,
    modules: dict[str, list[FileFacts]],
    edges: Counter[tuple[str, str]],
    names: dict[str, str],
    body: str,
    target: WikiTarget,
) -> str:
    quote = _one_line_quote(body) or f"{index.repo_id} 的代码 Wiki。"
    module_pages = sum(1 for key in names if key.startswith("module/"))
    top_modules = sorted(
        (module for module in modules if f"module/{module}" in names),
        key=lambda name: (-len(modules[name]), name),
    )[:8]
    module_rows = "\n".join(
        f"| [`{module}`]({names[f'module/{module}']}) | {len(modules[module])} |"
        for module in top_modules
    )
    scale = (
        f"| {len(facts.files)} | {facts.symbol_count} | {facts.call_count} "
        f"| {module_pages} | {len(facts.build_targets)} | {len(facts.tests)} |"
    )
    start = "\n".join(
        f"- [{label}]({_link_target(names, page_id)})"
        for page_id, label in [
            ("overview", "项目总览：仓库职责、技术栈与阅读路径"),
            ("architecture", "架构：分层、模块依赖图与典型调用链"),
            ("module", "模块索引：按目录聚合的模块页"),
            ("flow", "流程索引：从程序入口出发的端到端调用链"),
            ("interface", "接口索引：目录级公共 API"),
            ("build/overview", "构建"),
            ("config/overview", "配置"),
            ("test/overview", "测试"),
            ("glossary", "术语"),
        ]
        if _has_page(names, page_id)
    )
    module_section = (
        "## 模块速览\n\n| 模块 | 文件 |\n| --- | --- |\n" + module_rows if module_rows else ""
    )
    return f"""# {index.repo_id} 代码 Wiki

> {quote}

本 Wiki 由 CodingWiki 从快照 `{index.snapshot_id}` 生成，覆盖 {len(facts.files)} 个文件、
{facts.symbol_count} 个符号、{facts.call_count} 条调用与 {module_pages} 个模块页。

## 从这里开始

{start}

## 仓库规模

| 文件 | 符号 | 调用 | 模块 | 构建目标 | 测试 |
| --- | --- | --- | --- | --- | --- |
{scale}

## 模块速览

{module_section}

## 如何更新

```bash
git clone {target.repo_url}.wiki.git
# 把 CodingWiki 导出的 wiki/ 目录内容覆盖进去后提交
```"""


def _one_line_quote(body: str) -> str:
    for line in body.splitlines():
        text = line.strip()
        if text.startswith(">"):
            return text.lstrip(">").strip()
    return ""


def _modules_index(
    index: VersionIndex,
    modules: dict[str, list[FileFacts]],
    edges: Counter[tuple[str, str]],
    deps: dict[str, list[str]],
    names: dict[str, str],
) -> str:
    incoming: Counter[str] = Counter()
    outgoing: Counter[str] = Counter()
    for (source, target), count in edges.items():
        incoming[target] += count
        outgoing[source] += count

    page_modules = {
        entry.page_id[len("module/") :]
        for entry in index.pages
        if entry.page_id.startswith("module/")
    }
    rows: list[str] = []
    ordered = sorted(
        page_modules,
        key=lambda module: (-len(modules.get(module, [])), module),
    )
    for module in ordered:
        page_id = f"module/{module}"
        files = len(modules.get(module, [])) or len(deps.get(page_id, []))
        symbols = sum(len(file.symbols) for file in modules.get(module, []))
        public = (
            sum(
                1
                for file in modules.get(module, [])
                for symbol in file.symbols
                if is_public_symbol(symbol)
            )
            if module in modules
            else 0
        )
        rows.append(
            f"| [`{module}`]({names[page_id]}) | {files} | {symbols or '—'} | {public or '—'} "
            f"| {incoming.get(module, 0) or '—'} | {outgoing.get(module, 0) or '—'} |"
        )
    return (
        "# 模块索引\n\n"
        f"共 {len(rows)} 个模块页，按文件数排序。扇入/扇出是该模块与其他模块之间的调用次数。\n\n"
        "| 模块 | 文件 | 符号 | 公开符号 | 扇入 | 扇出 |\n"
        "| --- | --- | --- | --- | --- | --- |\n" + "\n".join(rows)
    )


def _interfaces_index(index: VersionIndex, names: dict[str, str]) -> str:
    rows = [
        f"| `{entry.page_id[len('interface/') :]}` | [{entry.title}]({names[entry.page_id]}) |"
        for entry in sorted(index.pages, key=lambda item: item.page_id)
        if entry.page_id.startswith("interface/")
    ]
    return (
        "# 接口索引\n\n"
        f"共 {len(rows)} 个目录级接口页。\n\n"
        "| 目录 | 页面 |\n| --- | --- |\n" + "\n".join(rows)
    )


def _flows_index(index: VersionIndex, names: dict[str, str], facts: RepoFacts) -> str:
    rows: list[str] = []
    for entry in sorted(index.pages, key=lambda item: item.page_id):
        if not entry.page_id.startswith("flow/"):
            continue
        stem = entry.page_id[len("flow/") :]
        path = next(
            (file.path for file in facts.files if file.path.rsplit(".", 1)[0] == stem), stem
        )
        rows.append(f"| [`{path}`]({names[entry.page_id]}) | {entry.title} |")
    return (
        "# 流程索引\n\n"
        f"共 {len(rows)} 条从程序入口出发的流程页，每条包含该入口可达的调用链图。\n\n"
        "| 入口 | 页面 |\n| --- | --- |\n" + "\n".join(rows)
    )


def _related_section(
    page_id: str, facts: RepoFacts, names: dict[str, str], titles: dict[str, str]
) -> str:
    links: list[tuple[str, str]] = []
    if page_id.startswith("module/"):
        module = page_id[len("module/") :]
        for file in module_groups(facts).get(module, []):
            directory = file.path.rsplit("/", 1)[0] if "/" in file.path else "."
            if f"interface/{directory}" in names:
                item = (f"interface/{directory}", titles.get(f"interface/{directory}", directory))
                if item not in links:
                    links.append(item)
        links = links[:8]
    elif page_id.startswith("interface/"):
        directory = page_id[len("interface/") :]
        for module, files in module_groups(facts).items():
            if any(
                (file.path.rsplit("/", 1)[0] if "/" in file.path else ".") == directory
                for file in files
            ):
                if f"module/{module}" in names:
                    links.append((f"module/{module}", titles.get(f"module/{module}", module)))
                break
    if "overview" in names and page_id != "overview":
        links.append(("overview", "项目总览"))
    if "architecture" in names and page_id != "architecture":
        links.append(("architecture", "架构"))
    if not links:
        return ""
    items = "\n".join(
        f"- [{titles.get(item, label)}]({names[item]})" for item, label in links if item in names
    )
    return f"## 相关页面\n\n{items}" if items else ""


def _sidebar(
    index: VersionIndex,
    names: dict[str, str],
    titles: dict[str, str],
    modules: dict[str, list[FileFacts]],
    facts: RepoFacts,
) -> str:
    module_pages = [entry.page_id for entry in index.pages if entry.page_id.startswith("module/")]
    module_pages.sort(
        key=lambda page_id: (
            -len(modules.get(page_id[len("module/") :], [])),
            page_id,
        )
    )
    flow_pages = [entry.page_id for entry in index.pages if entry.page_id.startswith("flow/")]
    flow_pages.sort(
        key=lambda page_id: (
            -_flow_call_volume(facts, page_id),
            page_id,
        )
    )

    def label(page_id: str) -> str:
        if page_id.startswith("module/"):
            return page_id[len("module/") :]
        if page_id.startswith("flow/"):
            return _entry_path_for_flow(page_id, facts) or page_id[len("flow/") :]
        if page_id.startswith("interface/"):
            return page_id[len("interface/") :]
        return _short_title(titles.get(page_id, page_id))

    lines = ["**开始**", "- [首页](Home)"]
    for page_id, text in [("overview", "项目总览"), ("architecture", "架构")]:
        if _has_page(names, page_id):
            lines.append(f"- [{text}]({names[page_id]})")
    if module_pages:
        lines += ["", "**模块**", "- [模块索引](Modules)"]
        lines += [f"- [{_short_title(label(pid))}]({names[pid]})" for pid in module_pages[:12]]
    if flow_pages:
        lines += ["", "**流程**", "- [流程索引](Flows)"]
        lines += [f"- [{_short_title(label(pid))}]({names[pid]})" for pid in flow_pages[:8]]
    references = [
        (page_id, text)
        for page_id, text in [
            ("interface", "接口索引"),
            ("build/overview", "构建"),
            ("config/overview", "配置"),
            ("test/overview", "测试"),
            ("glossary", "术语"),
        ]
        if _has_page(names, page_id)
    ]
    if references:
        lines += ["", "**参考**"]
        lines += [f"- [{text}]({_link_target(names, page_id)})" for page_id, text in references]
    return "\n".join(lines) + "\n"


_INDEX_PAGE = {"module": "Modules", "flow": "Flows", "interface": "Interfaces"}


def _link_target(names: dict[str, str], page_id: str) -> str:
    return names.get(page_id) or _INDEX_PAGE[page_id]


def _has_page(names: dict[str, str], page_id: str) -> bool:
    """索引类虚拟 ID（module/flow/interface）只要存在任一成员页就算存在。"""
    if page_id in {"module", "flow", "interface"}:
        return any(key.startswith(f"{page_id}/") for key in names)
    return page_id in names


def _flow_call_volume(facts: RepoFacts, page_id: str) -> int:
    entry_path = _entry_path_for_flow(page_id, facts)
    return sum(len(file.calls) for file in facts.files if file.path == entry_path)


def _short_title(title: str) -> str:
    text = title.strip()
    if len(text) <= 40:
        return text
    return text[:39] + "…"


def _footer(index: VersionIndex, target: WikiTarget) -> str:
    sha = index.snapshot_id.split("@", 1)[-1]
    short = sha[:8] if sha else "unknown"
    return (
        "---\n\n"
        f"本页由 CodingWiki 从快照 `{short}` 自动生成。"
        f"[查看源码]({target.repo_url}/tree/{target.revision})"
    )


# ---------------------------------------------------------------- 杂项


def _deps_files(version_dir: Path) -> dict[str, list[str]]:
    pages = version_dir / "pages"
    result: dict[str, list[str]] = {}
    if not pages.is_dir():
        return result
    for path in pages.rglob("*.deps.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        page_id = str(data.get("page_id") or "")
        files = data.get("files")
        if page_id and isinstance(files, dict):
            result[page_id] = [str(item) for item in files]
    return result


def _sanitize(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text.replace("/", "-"))
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned[:120] or "page"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


__all__ = ["WikiTarget", "build_github_wiki", "wiki_page_name"]
