"""逐条检索与证据聚合（架构文档 阶段2 §5）。

对每条需求，从已落盘的事实批次与只读 Wiki 页面中确定性地检索证据：
wiki / symbol / caller / config / build / test / similar。
不调用模型、不读时间、不用随机数：同输入同输出。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from codingwiki.domain.facts import (
    BuildTarget,
    CallFact,
    ConfigFact,
    RepoFacts,
    SymbolFact,
    TestFact,
)
from codingwiki.domain.spec import (
    EvidenceBundle,
    EvidenceItem,
    EvidenceKind,
    Requirement,
    RequirementEvidence,
)
from codingwiki.errors import InputError
from codingwiki.facts.index import FactIndex
from codingwiki.retrieval.tokenize import tokenize
from codingwiki.spec.impact import INTERFACE_KINDS, build_impact, caller_ref

# 编程/连接词停用：保留 log/level/config/error/info/debug/warn/test 等域内词
_STOP_TOKENS = frozenset(
    {
        "void",
        "int",
        "bool",
        "const",
        "return",
        "class",
        "struct",
        "enum",
        "static",
        "template",
        "typename",
        "namespace",
        "using",
        "public",
        "private",
        "protected",
        "operator",
        "true",
        "false",
        "null",
        "nullptr",
        "auto",
        "include",
        "define",
        "ifndef",
        "endif",
        "pragma",
        "std",
        "string",
        "vector",
        "size_t",
        "uint",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
        "int8",
        "int16",
        "int32",
        "int64",
        "float",
        "double",
        "char",
        "unsigned",
        "signed",
        "long",
        "short",
        "extern",
        "inline",
        "virtual",
        "override",
        "while",
        "for",
        "if",
        "else",
        "switch",
        "case",
        "break",
        "continue",
        "new",
        "delete",
        "this",
        "the",
        "and",
        "with",
        "from",
        "that",
        "into",
        "are",
        "must",
        "should",
        "may",
    }
)
_CJK_STOP = frozenset(
    {
        "的",
        "了",
        "与",
        "和",
        "及",
        "或",
        "在",
        "为",
        "是",
        "不",
        "需",
        "要",
        "中",
        "对",
        "将",
        "该",
        "其",
        "以",
        "并",
        "可",
        "从",
        "到",
        "后",
        "前",
        "时",
        "得",
        "把",
        "被",
        "让",
        "给",
        "向",
        "由",
    }
)
_ASCII_TOKEN = re.compile(r"^[a-z][a-z0-9_]*$")

_MAX_SYMBOLS = 8
_MAX_CONFIGS = 10
_MAX_TESTS = 10
_MAX_CALLERS = 10
_MAX_SIMILAR = 5
_MAX_WIKI = 3


@dataclass(frozen=True)
class WikiPageRef:
    """证据层只读需要的 Wiki 页面视图。"""

    page_id: str
    title: str
    text: str
    snapshot_id: str = ""


class WikiPageProvider(Protocol):
    """已发布页面的只读来源；适配器由入口层注入，spec 不 import publish。"""

    def pages(self) -> list[WikiPageRef]: ...


def _ascii_tokens(text: str) -> set[str]:
    return {
        token
        for token in tokenize(text)
        if len(token) >= 3 and _ASCII_TOKEN.match(token) and token not in _STOP_TOKENS
    }


def _all_tokens(text: str) -> set[str]:
    """ASCII 与 CJK 二元组都保留，供 Wiki 页面中文匹配。"""
    return {
        token
        for token in tokenize(text)
        if len(token) >= 2 and token not in _STOP_TOKENS and token not in _CJK_STOP
    }


class _TermIndex:
    """一次事实批次的符号词元倒排；构造一次，按需求复用。"""

    def __init__(self, facts: RepoFacts) -> None:
        self._symbols: dict[str, SymbolFact] = {}
        self._by_token: dict[str, list[str]] = {}
        self._by_name: dict[str, list[str]] = {}
        self._lower_names: list[tuple[str, str]] = []
        for file in facts.files:
            for symbol in file.symbols:
                self._symbols[symbol.symbol_id] = symbol
                tokens = (
                    _ascii_tokens(symbol.name)
                    | _ascii_tokens(symbol.qualified_name)
                    | _ascii_tokens(symbol.path.replace("/", " "))
                )
                for token in tokens:
                    self._by_token.setdefault(token, []).append(symbol.symbol_id)
                lowered = symbol.name.lower()
                self._by_name.setdefault(lowered, []).append(symbol.symbol_id)
                self._lower_names.append((lowered, symbol.symbol_id))

    def select(self, tokens: set[str], limit: int = _MAX_SYMBOLS) -> list[SymbolFact]:
        """按「词元倒排得分 + 名称子串」排序，取前 limit 个符号。"""
        scores: dict[str, int] = {}
        ordered = sorted(tokens)
        for token in ordered:
            for symbol_id in self._by_token.get(token, ()):
                scores[symbol_id] = scores.get(symbol_id, 0) + 1
            for symbol_id in self._by_name.get(token, ()):
                scores[symbol_id] = scores.get(symbol_id, 0) + 8
        for token in ordered:
            if len(token) < 3:
                continue
            for lowered, symbol_id in self._lower_names:
                if token in lowered:
                    scores[symbol_id] = scores.get(symbol_id, 0) + 2
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
        return [self._symbols[symbol_id] for symbol_id, _score in ranked]

    def same_name_elsewhere(
        self, selected: Sequence[SymbolFact], limit: int = _MAX_SIMILAR
    ) -> list[SymbolFact]:
        """同名但位于其他文件的符号，作为类似实现证据。"""
        chosen = {symbol.symbol_id for symbol in selected}
        picked: set[str] = set()
        for symbol in selected:
            for symbol_id in self._by_name.get(symbol.name.lower(), ()):
                if symbol_id in chosen or symbol_id in picked:
                    continue
                picked.add(symbol_id)
                if len(picked) >= limit:
                    return [self._symbols[sid] for sid in sorted(picked)]
        return [self._symbols[sid] for sid in sorted(picked)]


def _select_configs(
    facts: RepoFacts, tokens: set[str], limit: int = _MAX_CONFIGS
) -> list[ConfigFact]:
    scored: dict[int, int] = {}
    for index, config in enumerate(facts.config_facts):
        key_text = config.key.replace(".", " ").replace("-", "_").replace("/", " ")
        parts = _ascii_tokens(key_text)
        shared = tokens & parts
        score = 2 * len(shared)
        lowered = config.key.lower()
        for token in tokens:
            if len(token) >= 3 and token not in shared and token in lowered:
                score += 1
        if score:
            scored[index] = score
    ranked = sorted(
        scored.items(),
        key=lambda item: (
            -item[1],
            facts.config_facts[item[0]].key,
            facts.config_facts[item[0]].path,
            facts.config_facts[item[0]].line,
        ),
    )
    picked: list[ConfigFact] = []
    seen: set[tuple[str, str, int]] = set()
    for index, _score in ranked:
        config = facts.config_facts[index]
        marker = (config.key, config.path, config.line)
        if marker in seen:
            continue
        seen.add(marker)
        picked.append(config)
        if len(picked) >= limit:
            break
    return picked


def _select_tests(
    facts: RepoFacts,
    targets: Sequence[BuildTarget],
    tokens: set[str],
    selected_paths: Sequence[str],
    index: FactIndex,
) -> list[TestFact]:
    target_names = {target.name for target in targets}
    scored: dict[tuple[str, str, int], int] = {}
    for test in facts.tests:
        score = 0
        if test.target and test.target in target_names:
            score += 2
        parts = _ascii_tokens(test.name) | _ascii_tokens(test.path.replace("/", " "))
        score += len(tokens & parts)
        if score:
            scored[(test.path, test.name, test.line)] = score
    for path in selected_paths:
        if not path.startswith(("test/", "tests/")):
            continue
        for test in index.tests_for_path(path):
            marker = (test.path, test.name, test.line)
            scored[marker] = max(scored.get(marker, 0), 1)
    ranked = sorted(scored.items(), key=lambda item: (-item[1], item[0][0], item[0][1]))
    picked: list[TestFact] = []
    for (path, name, line), _score in ranked[: _MAX_TESTS * 2]:
        for test in facts.tests:
            if (test.path, test.name, test.line) == (path, name, line):
                picked.append(test)
                break
        if len(picked) >= _MAX_TESTS:
            break
    return picked


def _select_callers(index: FactIndex, symbols: Sequence[SymbolFact]) -> list[CallFact]:
    picked: set[tuple[str, int, str, str]] = set()
    calls: list[CallFact] = []
    for symbol in symbols:
        for call in index.callers_of(symbol.symbol_id):
            marker = (call.path, call.line, call.caller, call.callee)
            if marker in picked:
                continue
            picked.add(marker)
            calls.append(call)
            if len(calls) >= _MAX_CALLERS:
                return calls
    return calls


def _select_wiki(
    pages: Sequence[WikiPageRef], tokens: set[str], snapshot_id: str
) -> list[WikiPageRef]:
    scored: dict[str, int] = {}
    seen: set[str] = set()
    for page in pages:
        if page.page_id in seen:
            continue
        if page.snapshot_id and page.snapshot_id != snapshot_id:
            continue
        shared = tokens & _all_tokens(f"{page.title}\n{page.text}")
        if shared:
            seen.add(page.page_id)
            scored[page.page_id] = len(shared)
    ranked = sorted(scored.items(), key=lambda item: (-item[1], item[0]))[:_MAX_WIKI]
    by_id = {page.page_id: page for page in pages}
    return [by_id[page_id] for page_id, _score in ranked]


@dataclass
class _Selection:
    symbols: list[SymbolFact]
    calls: list[CallFact]
    configs: list[ConfigFact]
    targets: list[BuildTarget]
    tests: list[TestFact]
    similar: list[SymbolFact]
    wiki: list[WikiPageRef]


def _gather(
    *,
    ascii_tokens: set[str],
    all_tokens: set[str],
    facts: RepoFacts,
    index: FactIndex,
    terms: _TermIndex,
    pages: Sequence[WikiPageRef],
) -> _Selection:
    ranked = terms.select(ascii_tokens)
    # 同名符号保留一个主匹配，其余归入 similar，避免影响面被重复实现撑大
    primary: list[SymbolFact] = []
    seen_names: set[str] = set()
    for symbol in ranked:
        if symbol.name in seen_names:
            continue
        seen_names.add(symbol.name)
        primary.append(symbol)
    symbols = primary
    calls = _select_callers(index, symbols)
    configs = _select_configs(facts, ascii_tokens)
    targets = index.build_targets_touching(symbol.path for symbol in symbols)
    tests = _select_tests(
        facts,
        targets,
        ascii_tokens,
        [symbol.path for symbol in symbols],
        index,
    )
    similar = terms.same_name_elsewhere(symbols)
    wiki = _select_wiki(pages, all_tokens, facts.snapshot_id)
    return _Selection(
        symbols=symbols,
        calls=calls,
        configs=configs,
        targets=targets,
        tests=tests,
        similar=similar,
        wiki=wiki,
    )


def _has_code_evidence(selection: _Selection) -> bool:
    return bool(selection.symbols or selection.configs or selection.targets or selection.tests)


def _selection_items(selection: _Selection, snapshot_id: str) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    for symbol in selection.symbols:
        items.append(
            EvidenceItem(
                kind=EvidenceKind.SYMBOL,
                path=symbol.path,
                line=symbol.line,
                symbol_id=symbol.symbol_id,
                snapshot_id=snapshot_id,
            )
        )
    for call in selection.calls:
        items.append(
            EvidenceItem(
                kind=EvidenceKind.CALLER,
                path=call.path,
                line=call.line,
                symbol_id=call.caller,
                snapshot_id=snapshot_id,
            )
        )
    for config in selection.configs:
        if not config.path:
            continue
        items.append(
            EvidenceItem(
                kind=EvidenceKind.CONFIG,
                path=config.path,
                line=config.line,
                snapshot_id=snapshot_id,
            )
        )
    for target in selection.targets:
        if not target.path:
            continue
        items.append(
            EvidenceItem(
                kind=EvidenceKind.BUILD,
                path=target.path,
                line=target.line,
                snapshot_id=snapshot_id,
            )
        )
    for test in selection.tests:
        if not test.path:
            continue
        items.append(
            EvidenceItem(
                kind=EvidenceKind.TEST,
                path=test.path,
                line=test.line,
                snapshot_id=snapshot_id,
            )
        )
    for similar in selection.similar:
        items.append(
            EvidenceItem(
                kind=EvidenceKind.SIMILAR,
                path=similar.path,
                line=similar.line,
                symbol_id=similar.symbol_id,
                snapshot_id=snapshot_id,
            )
        )
    for page in selection.wiki:
        items.append(
            EvidenceItem(
                kind=EvidenceKind.WIKI,
                page_id=page.page_id,
                snapshot_id=page.snapshot_id or snapshot_id,
            )
        )
    deduped: list[EvidenceItem] = []
    seen: set[tuple[str, str, int, str, str, str]] = set()
    for item in items:
        marker = (
            item.kind.value,
            item.path,
            item.line,
            item.symbol_id,
            item.page_id,
            item.snapshot_id,
        )
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(item)
    return deduped


def _requirement_evidence(
    requirement: Requirement,
    *,
    doc_tokens: set[str],
    doc_all_tokens: set[str],
    facts: RepoFacts,
    index: FactIndex,
    terms: _TermIndex,
    pages: Sequence[WikiPageRef],
) -> RequirementEvidence:
    own_text = f"{requirement.text}\n{requirement.source_quote}\n{requirement.source_section}"
    selection = _gather(
        ascii_tokens=_ascii_tokens(own_text),
        all_tokens=_all_tokens(own_text),
        facts=facts,
        index=index,
        terms=terms,
        pages=pages,
    )
    if not _has_code_evidence(selection):
        # 文档级兜底：中文需求词面命不中英文符号时，用同文档主题词补上下文
        selection = _gather(
            ascii_tokens=doc_tokens,
            all_tokens=doc_all_tokens,
            facts=facts,
            index=index,
            terms=terms,
            pages=pages,
        )
    items = _selection_items(selection, facts.snapshot_id)
    if not items:
        raise InputError(
            f"需求 {requirement.id} 没有任何可定位证据（wiki/symbol/caller/config/build/test）；"
            "请补充更具体的需求文本，或确认 facts 快照包含相关模块"
        )

    callers = [caller_ref(call.caller, call.path, call.line) for call in selection.calls]
    impact = build_impact(
        requirement_id=requirement.id,
        files=[symbol.path for symbol in selection.symbols]
        + [config.path for config in selection.configs if config.path],
        interfaces=[
            symbol.symbol_id for symbol in selection.symbols if symbol.kind.value in INTERFACE_KINDS
        ],
        callers=callers,
        config=[config.key for config in selection.configs],
        build=[target.name for target in selection.targets],
        tests=[test.name or test.path for test in selection.tests],
    )
    return RequirementEvidence(requirement_id=requirement.id, items=items, impact=impact)


def collect_evidence(
    *,
    repo_id: str,
    doc_id: str,
    doc_title: str,
    requirements: Sequence[Requirement],
    facts: RepoFacts,
    pages: Sequence[WikiPageRef] = (),
    spec_id: str = "",
    snapshot_id: str = "",
) -> EvidenceBundle:
    """按需求聚合证据并校验；零证据或悬空引用直接报错。"""
    if snapshot_id and snapshot_id != facts.snapshot_id:
        raise InputError(
            f"--snapshot {snapshot_id} 与 facts 快照 {facts.snapshot_id} 不一致；"
            "请先对该快照执行 facts extract"
        )
    if not requirements:
        raise InputError("需求集合为空，无法生成证据包")

    page_list = list(pages)
    index = FactIndex(facts)
    terms = _TermIndex(facts)
    doc_tokens = _ascii_tokens(f"{doc_title}\n" + "\n".join(req.text for req in requirements))
    doc_all = _all_tokens(f"{doc_title}\n" + "\n".join(req.text for req in requirements))

    bundle = EvidenceBundle(
        repo_id=repo_id,
        snapshot_id=facts.snapshot_id,
        doc_id=doc_id,
        spec_id=spec_id,
        requirements=[
            _requirement_evidence(
                requirement,
                doc_tokens=doc_tokens,
                doc_all_tokens=doc_all,
                facts=facts,
                index=index,
                terms=terms,
                pages=page_list,
            )
            for requirement in requirements
        ],
    )
    validate_bundle(bundle, facts, page_list)
    return bundle


def validate_bundle(
    bundle: EvidenceBundle,
    facts: RepoFacts,
    pages: Iterable[WikiPageRef] = (),
) -> None:
    """快照纪律：symbol 必须存在、路径必须在快照内、Wiki 页不能过期。"""
    if bundle.snapshot_id != facts.snapshot_id:
        raise InputError(
            f"证据包绑定快照 {bundle.snapshot_id} 与 facts 快照 {facts.snapshot_id} 不一致"
        )
    index = FactIndex(facts)
    known_paths = facts.blob_by_path
    page_snapshots = {page.page_id: page.snapshot_id for page in pages}
    problems: list[str] = []
    for requirement in bundle.requirements:
        for item in requirement.items:
            if item.symbol_id and item.kind in (
                EvidenceKind.SYMBOL,
                EvidenceKind.SIMILAR,
                EvidenceKind.CALLER,
            ):
                try:
                    index.assert_symbol_exists(item.symbol_id, bundle.snapshot_id)
                except InputError as exc:
                    problems.append(f"{requirement.requirement_id} {item.symbol_id}: {exc}")
            if item.path and item.path not in known_paths:
                problems.append(f"{requirement.requirement_id} 悬空路径 {item.path}:{item.line}")
            if item.kind is EvidenceKind.WIKI:
                if item.page_id not in page_snapshots:
                    problems.append(f"{requirement.requirement_id} Wiki 页不存在: {item.page_id}")
                elif page_snapshots[item.page_id] and (
                    page_snapshots[item.page_id] != bundle.snapshot_id
                ):
                    problems.append(
                        f"{requirement.requirement_id} Wiki 页 {item.page_id} 快照 "
                        f"{page_snapshots[item.page_id]} 与 {bundle.snapshot_id} 不一致"
                    )
    if problems:
        shown = "\n- ".join(problems[:10])
        more = f"（共 {len(problems)} 条）" if len(problems) > 10 else ""
        raise InputError(f"证据存在悬空引用{more}:\n- {shown}")


def save_evidence(bundle: EvidenceBundle, directory: str | Path) -> Path:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / "evidence.json"
    path.write_text(bundle.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


__all__ = [
    "WikiPageProvider",
    "WikiPageRef",
    "collect_evidence",
    "save_evidence",
    "validate_bundle",
]
