"""模块边界与关键符号排序（S6.5-b）。

页面粒度从「一个符号一页」改为「目录级模块一页」。本文件的规则只依赖事实层：
- 先按目录得到候选模块；
- 目录过大时下沉一级，避免 `src/<pkg>` 把几千个符号塞进一页；
- 关键符号按「被调用次数」排序，让页面写真正被使用的入口，而不是符号表前几行。
"""

from __future__ import annotations

from collections import Counter

from codingwiki.domain.facts import (
    FileFacts,
    RepoFacts,
    SymbolFact,
    SymbolKind,
)

_HEADER_SUFFIXES = (".h", ".hpp", ".hh", ".cuh")
_PUBLIC_KINDS = {
    SymbolKind.FUNCTION,
    SymbolKind.CLASS,
    SymbolKind.STRUCT,
    SymbolKind.KERNEL,
    SymbolKind.ENUM,
}
_KIND_WEIGHT = {
    SymbolKind.CLASS: 6,
    SymbolKind.STRUCT: 5,
    SymbolKind.KERNEL: 4,
    SymbolKind.FUNCTION: 3,
    SymbolKind.ENUM: 2,
    SymbolKind.METHOD: 1,
    SymbolKind.MACRO: 1,
}


def is_public_symbol(symbol: SymbolFact) -> bool:
    return symbol.kind in _PUBLIC_KINDS


def is_header(path: str) -> bool:
    return path.endswith(_HEADER_SUFFIXES)


def directories_of(facts: RepoFacts) -> set[str]:
    dirs: set[str] = set()
    for file in facts.files:
        parts = file.path.split("/")
        for index in range(1, len(parts)):
            dirs.add("/".join(parts[:index]))
    return dirs


def big_threshold(facts: RepoFacts) -> int:
    """超过这个文件数的目录下沉一级；小仓库不会误触发。"""
    return max(120, len(facts.files) // 10)


def module_key(path: str, directories: set[str], large: set[str]) -> str:
    """把文件路径映射到目录级模块键。

    large 是「文件数超过阈值的目录」集合；只有大目录才允许再下沉一级。
    """
    parts = path.split("/")
    if parts[0] == "src":
        base = "/".join(parts[:2])
        if len(parts) >= 2 and base in directories:
            if len(parts) >= 3 and "/".join(parts[:3]) in directories:
                third = "/".join(parts[:3])
                if base in large:
                    if len(parts) >= 4 and "/".join(parts[:4]) in directories:
                        fourth = "/".join(parts[:4])
                        if fourth in large:
                            return fourth
                    return third
                return base
            return base
        return "src"
    if parts[0] in {"examples", "test", "util", "nvlog"} and len(parts) >= 2:
        base = "/".join(parts[:2])
        if base in directories:
            return base
        return parts[0]
    if len(parts) >= 2 and parts[0] in directories:
        return parts[0]
    return "(root)"


def module_groups(facts: RepoFacts) -> dict[str, list[FileFacts]]:
    """返回 模块键 -> 文件列表。"""
    directories = directories_of(facts)
    big = big_threshold(facts)
    counts: Counter[str] = Counter()
    for file in facts.files:
        parts = file.path.split("/")
        if parts[0] != "src":
            continue
        base = "/".join(parts[:2])
        if len(parts) >= 2 and base in directories:
            counts[base] += 1
            if len(parts) >= 3 and "/".join(parts[:3]) in directories:
                counts["/".join(parts[:3])] += 1
            if len(parts) >= 4 and "/".join(parts[:4]) in directories:
                counts["/".join(parts[:4])] += 1
        else:
            counts["src"] += 1
    large = {name for name, count in counts.items() if count >= big}

    groups: dict[str, list[FileFacts]] = {}
    for file in facts.files:
        groups.setdefault(module_key(file.path, directories, large), []).append(file)
    return groups


def call_degrees(facts: RepoFacts) -> Counter[str]:
    """符号被调用次数（按解析到的 target 计）。"""
    degrees: Counter[str] = Counter()
    for file in facts.files:
        for call in file.calls:
            for target in call.targets:
                degrees[target] += 1
    return degrees


def rank_symbols(
    symbols: list[SymbolFact],
    degrees: Counter[str],
    limit: int,
) -> list[SymbolFact]:
    """按 被调用次数 + 公开类别优先级 排序，截断到 limit。"""
    ordered = sorted(
        symbols,
        key=lambda symbol: (
            -degrees.get(symbol.symbol_id, 0),
            -_KIND_WEIGHT.get(symbol.kind, 0),
            symbol.path,
            symbol.line,
        ),
    )
    return ordered[:limit]


def top_terms(symbols: list[SymbolFact], limit: int = 30) -> list[SymbolFact]:
    """术语候选：类/结构体/枚举/宏优先，取名字较短的。"""
    candidates = [
        symbol
        for symbol in symbols
        if symbol.kind in {SymbolKind.CLASS, SymbolKind.STRUCT, SymbolKind.ENUM, SymbolKind.MACRO}
        and len(symbol.name) <= 40
    ]
    candidates.sort(key=lambda symbol: (len(symbol.name), symbol.name.lower()))
    return candidates[:limit]


__all__ = [
    "big_threshold",
    "call_degrees",
    "directories_of",
    "is_header",
    "is_public_symbol",
    "module_groups",
    "module_key",
    "rank_symbols",
    "top_terms",
]
