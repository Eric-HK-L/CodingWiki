"""FactIndex：RepoFacts 之上的内存反查表（架构文档 v0.7 §16.3-1）。

二三阶段（Spec 影响分析 13.6 / 追溯矩阵 13.7）需要结构化事实查询，
`facts query` CLI 的字符串过滤无法编程使用。本模块只在内存建反查索引，
落盘形态不变（事实仍是 JSON）；纯查询、无 I/O、无日志。
"""

from __future__ import annotations

from collections.abc import Iterable

from codingwiki.domain.facts import BuildTarget, CallFact, RepoFacts, SymbolFact, TestFact
from codingwiki.errors import InputError


class FactIndex:
    """一次事实批次（RepoFacts）的结构化查询视图。构造即建索引，之后只读。"""

    def __init__(self, facts: RepoFacts) -> None:
        self._facts = facts
        self._symbols_by_id: dict[str, SymbolFact] = {}
        self._symbols_by_module: dict[str, list[SymbolFact]] = {}
        # 反向调用图：被调目标 symbol_id -> 调用点列表
        self._callers: dict[str, list[CallFact]] = {}
        for file in facts.files:
            for symbol in file.symbols:
                self._symbols_by_id[symbol.symbol_id] = symbol
                self._symbols_by_module.setdefault(file.module, []).append(symbol)
            for call in file.calls:
                for target in call.targets:
                    self._callers.setdefault(target, []).append(call)
        self._build_targets = list(facts.build_targets)
        self._tests = list(facts.tests)

    # ---------------------------------------------------------------- 查询 API

    @property
    def repo_id(self) -> str:
        return self._facts.repo_id

    @property
    def snapshot_id(self) -> str:
        return self._facts.snapshot_id

    def symbol_by_id(self, symbol_id: str) -> SymbolFact | None:
        """按 symbol_id 精确取符号。"""
        return self._symbols_by_id.get(symbol_id)

    def symbols_in_module(self, module: str) -> list[SymbolFact]:
        """一个模块（FileFacts.module）下的全部符号，按 symbol_id 排序保证稳定。"""
        return sorted(self._symbols_by_module.get(module, []), key=lambda s: s.symbol_id)

    def callers_of(self, symbol_id: str) -> list[CallFact]:
        """反向调用图：谁调用了这个符号（13.6 影响分析的「调用方」）。

        只含解析到该 symbol_id 的调用点；未解析调用不算任何人的调用方。
        按 (path, line) 排序保证稳定。
        """
        return sorted(self._callers.get(symbol_id, []), key=lambda c: (c.path, c.line))

    def tests_for_path(self, path: str) -> list[TestFact]:
        """与某文件/目录相关的测试：测试文件路径精确匹配或位于该目录前缀下。"""
        prefix = path.rstrip("/") + "/"
        return sorted(
            (t for t in self._tests if t.path == path or t.path.startswith(prefix)),
            key=lambda t: (t.path, t.name),
        )

    def build_targets_touching(self, paths: Iterable[str]) -> list[BuildTarget]:
        """改了这些文件会影响哪些构建目标：target.sources 与给定路径有交集。"""
        changed = set(paths)
        return sorted(
            (t for t in self._build_targets if changed.intersection(t.sources)),
            key=lambda t: (t.name, t.path),
        )

    def assert_symbol_exists(self, symbol_id: str, snapshot: str | None = None) -> None:
        """追溯矩阵校验（13.7）：Spec 引用的代码位置必须在指定快照中存在。

        snapshot 缺省时按索引自身绑定的事实批次校验；传入时两者不一致直接报错，
        防止拿旧快照的索引冒充新快照的现状。
        """
        bound = snapshot if snapshot is not None else self._facts.snapshot_id
        if bound != self._facts.snapshot_id:
            raise InputError(
                f"FactIndex 绑定快照 {self._facts.snapshot_id}，与要求的 {bound} 不一致；"
                "请先对该快照执行 facts extract"
            )
        if symbol_id not in self._symbols_by_id:
            raise InputError(
                f"快照 {self._facts.snapshot_id} 中不存在符号 {symbol_id}；Spec 引用的代码位置失效"
            )


__all__ = ["FactIndex"]
