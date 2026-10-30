"""语法级符号提取（tree-sitter）。

架构文档 v0.3 12.1：
- C / C++ / CUDA 用 tree-sitter-c / tree-sitter-cpp；Python 用 tree-sitter-python；
- CUDA 的 `__global__` / `__device__` / `__host__` 不参与语法树，用函数定义前缀文本标记；
- `<<<...>>>` 启动语法不会被 tree-sitter-cpp 正确解析，用正则单独识别；
- 解析失败（`root.has_error`）不静默：记 `parse_ok=False` 与 ERROR 节点数。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from codingwiki.domain.facts import (
    CallFact,
    Confidence,
    EntryFact,
    FileFacts,
    IncludeFact,
    SymbolFact,
    SymbolKind,
)

SUPPORTED_LANGUAGES: frozenset[str] = frozenset({"c", "cpp", "cuda", "python"})

_C_LIKE = frozenset({"c", "cpp", "cuda"})
_PARSER_CACHE: dict[str, Any] = {}

_CUDA_LAUNCH = re.compile(r"\b([A-Za-z_]\w*(?:::\w+)*)\s*<<<")
_CUDA_TOKENS = re.compile(rb"__global__|__device__|__host__|__constant__|__shared__|__managed__")
_CUDA_LAUNCH_BYTES = re.compile(rb"<<<.*?>>>", re.DOTALL)
_PY_MAIN = re.compile(r"__name__\s*==\s*['\"]__main__['\"]")

# `getenv("X")` / `std::getenv("X")` / `os.getenv("X")` / `os.environ["X"]` 等
_ENV_READ = re.compile(
    r"""\b(?:std::)?(?:getenv|GetEnv|get_env)\s*\(\s*["']([A-Za-z_][A-Za-z0-9_]*)"""
    r"""|os\.(?:getenv|environ\.get)\s*\(\s*["']([A-Za-z_][A-Za-z0-9_]*)"""
    r"""|os\.environ\s*\[\s*["']([A-Za-z_][A-Za-z0-9_]*)""",
)
_ARGP_ADD = re.compile(r"""add_argument\s*\(\s*["'](-{1,2}[A-Za-z0-9_-]+)""")


def extract_file_facts(
    path: Path,
    rel_path: str,
    language: str,
    blob_sha256: str,
    module: str,
) -> FileFacts:
    """解析单个文件；语言不支持或读失败都返回结构完整的 FileFacts，不抛异常外泄。"""
    try:
        raw = path.read_bytes()
    except OSError:
        # 读失败不把环境细节写进事实，只标记解析失败
        return FileFacts(
            path=rel_path,
            language=language,
            blob_sha256=blob_sha256,
            module=module,
            parse_ok=False,
            parse_errors=1,
        )

    text = raw.decode("utf-8", errors="replace")
    line_count = len(text.splitlines())

    if language not in SUPPORTED_LANGUAGES:
        return FileFacts(
            path=rel_path,
            language=language,
            blob_sha256=blob_sha256,
            module=module,
            line_count=line_count,
        )

    parser = _get_parser(_grammar_for(path, language))
    # CUDA 的限定符与 <<<...>>> 启动语法不在 tree-sitter-cpp 语法里，
    # 先按字节等长"挖空"（保留换行）再解析，行号与字节偏移仍然对得上。
    parse_bytes = _mask_cuda(raw) if language in {"cuda", "cpp"} else raw
    tree = parser.parse(parse_bytes)
    root = tree.root_node
    errors = _count_errors(root)

    symbols: list[SymbolFact] = []
    includes: list[IncludeFact] = []
    calls: list[CallFact] = []
    entries: list[EntryFact] = []

    if language == "python":
        _walk_python(root, raw, rel_path, [], "", symbols, includes, calls, entries)
        if rel_path.endswith("__main__.py") and not any(e.kind == "python_main" for e in entries):
            entries.append(
                EntryFact(
                    path=rel_path,
                    line=1,
                    name=rel_path,
                    kind="python_main",
                    confidence=Confidence.LOW,
                )
            )
    else:
        _walk_c_like(root, raw, text, rel_path, language, [], "", symbols, includes, calls, entries)
        _scan_cuda_launches(text, rel_path, language, symbols, calls, entries)

    return FileFacts(
        path=rel_path,
        language=language,
        blob_sha256=blob_sha256,
        module=module,
        line_count=line_count,
        parse_ok=errors == 0,
        parse_errors=errors,
        symbols=symbols,
        includes=includes,
        calls=calls,
        entries=entries,
    )


def scan_env_reads(text: str, rel_path: str) -> list[tuple[str, int, str]]:
    """扫描代码里的环境变量/命令行参数读取，返回 (key, line, reader)。"""
    found: list[tuple[str, int, str]] = []
    for match in _ENV_READ.finditer(text):
        key = match.group(1) or match.group(2) or match.group(3)
        line = text.count("\n", 0, match.start()) + 1
        found.append((key, line, match.group(0).split("(")[0].split("[")[0]))
    for match in _ARGP_ADD.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        found.append((match.group(1), line, "argparse"))
    return found


# ---------------------------------------------------------------- tree-sitter 基础


def _get_parser(grammar: str) -> Any:
    parser = _PARSER_CACHE.get(grammar)
    if parser is not None:
        return parser
    from tree_sitter import Language, Parser

    if grammar == "python":
        import tree_sitter_python as tsp

        lang = Language(tsp.language())
    elif grammar == "c":
        import tree_sitter_c as tsc

        lang = Language(tsc.language())
    else:
        import tree_sitter_cpp as tscpp

        lang = Language(tscpp.language())
    parser = Parser(lang)
    _PARSER_CACHE[grammar] = parser
    return parser


def _grammar_for(path: Path, language: str) -> str:
    """`.h` 既可能是 C 也可能是 C++，用 C++ 语法解析更宽容。"""
    if language == "python":
        return "python"
    if language in {"cpp", "cuda"} or path.suffix.lower() == ".h":
        return "cpp"
    return "c"


def _node_text(node: Any, raw: bytes) -> str:
    return str(raw[node.start_byte : node.end_byte].decode("utf-8", errors="replace"))


def _line(node: Any) -> int:
    return int(node.start_point[0]) + 1


def _end_line(node: Any) -> int:
    return int(node.end_point[0]) + 1


def _count_errors(root: Any) -> int:
    """只统计 ERROR 节点；MISSING 节点多由预处理分支引起，不算解析失败。"""
    count = 0
    stack = [root]
    while stack and count < 10_000:
        node = stack.pop()
        if node.is_error or node.type == "ERROR":
            count += 1
        stack.extend(node.named_children)
    return count


def _collapse(text: str, limit: int = 240) -> str:
    flat = " ".join(text.split())
    return flat[:limit]


def _symbol_id(path: str, qualified: str, line: int) -> str:
    return f"{path}::{qualified}@{line}"


# ---------------------------------------------------------------- C / C++ / CUDA


def _walk_c_like(
    node: Any,
    raw: bytes,
    text: str,
    rel_path: str,
    language: str,
    scopes: list[str],
    current: str,
    symbols: list[SymbolFact],
    includes: list[IncludeFact],
    calls: list[CallFact],
    entries: list[EntryFact],
) -> None:
    kind = node.type

    if kind in {"namespace_definition", "class_specifier", "struct_specifier", "enum_specifier"}:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            _walk_children_c(
                node,
                raw,
                text,
                rel_path,
                language,
                scopes,
                current,
                symbols,
                includes,
                calls,
                entries,
            )
            return
        name = _node_text(name_node, raw)
        sym_kind = {
            "namespace_definition": SymbolKind.NAMESPACE,
            "class_specifier": SymbolKind.CLASS,
            "struct_specifier": SymbolKind.STRUCT,
            "enum_specifier": SymbolKind.ENUM,
        }[kind]
        qualified = "::".join([*scopes, name])
        line = _line(node)
        symbols.append(
            SymbolFact(
                symbol_id=_symbol_id(rel_path, qualified, line),
                name=name,
                qualified_name=qualified,
                kind=sym_kind,
                path=rel_path,
                line=line,
                end_line=_end_line(node),
                signature=_collapse(_node_text(node, raw).split("{")[0]),
                parent="::".join(scopes),
            )
        )
        if node.child_by_field_name("body") is not None:
            _walk_children_c(
                node,
                raw,
                text,
                rel_path,
                language,
                [*scopes, name],
                "",
                symbols,
                includes,
                calls,
                entries,
            )
        return

    if kind == "function_definition":
        name_node = _declarator_name(node.child_by_field_name("declarator"))
        body = node.child_by_field_name("body")
        if name_node is None:
            _walk_children_c(
                node,
                raw,
                text,
                rel_path,
                language,
                scopes,
                current,
                symbols,
                includes,
                calls,
                entries,
            )
            return
        name = _node_text(name_node, raw)
        qualified_node = _qualified_identifier_of(name_node)
        is_qualified = qualified_node is not None
        if qualified_node is not None:
            full = _node_text(qualified_node, raw).removeprefix("::")
            name = full.split("::")[-1]
            scope_prefix = "::".join(scopes)
            qualified = (
                full
                if scope_prefix and full.startswith(f"{scope_prefix}::")
                else "::".join([*scopes, full])
            )
            parent_name = qualified.rsplit("::", 1)[0] if "::" in qualified else ""
        else:
            qualified = "::".join([*scopes, name])
            parent_name = "::".join(scopes)
        line = _line(node)
        qualifier = _cuda_qualifier(text, node.start_byte) if language in {"cuda", "cpp"} else ""
        scope_is_class = bool(scopes) and any(
            sym.kind in {SymbolKind.CLASS, SymbolKind.STRUCT}
            and sym.qualified_name == "::".join(scopes)
            for sym in symbols
        )
        if qualifier == "kernel":
            sym_kind = SymbolKind.KERNEL
        elif qualifier == "device":
            sym_kind = SymbolKind.DEVICE_FUNCTION
        elif is_qualified or scope_is_class:
            sym_kind = SymbolKind.METHOD
        else:
            sym_kind = SymbolKind.FUNCTION
        sig_end = body.start_byte if body is not None else node.end_byte
        symbols.append(
            SymbolFact(
                symbol_id=_symbol_id(rel_path, qualified, line),
                name=name,
                qualified_name=qualified,
                kind=sym_kind,
                path=rel_path,
                line=line,
                end_line=_end_line(node),
                signature=_collapse(raw[node.start_byte : sig_end].decode("utf-8", "replace")),
                parent=parent_name,
                confidence=Confidence.HIGH,
            )
        )
        if name == "main" and not scopes:
            entries.append(EntryFact(path=rel_path, line=line, name="main", kind="main"))
        symbol_id = _symbol_id(rel_path, qualified, line)
        if body is not None:
            _walk_children_c(
                node,
                raw,
                text,
                rel_path,
                language,
                scopes,
                symbol_id,
                symbols,
                includes,
                calls,
                entries,
            )
        return

    if kind == "preproc_include":
        path_node = node.child_by_field_name("path")
        if path_node is not None:
            target = _node_text(path_node, raw).strip('<>"')
            includes.append(IncludeFact(path=rel_path, target=target, line=_line(node)))
        return

    if kind in {"preproc_def", "preproc_function_def"}:
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            name = _node_text(name_node, raw)
            qualified = "::".join([*scopes, name])
            line = _line(node)
            symbols.append(
                SymbolFact(
                    symbol_id=_symbol_id(rel_path, qualified, line),
                    name=name,
                    qualified_name=qualified,
                    kind=SymbolKind.MACRO,
                    path=rel_path,
                    line=line,
                    end_line=_end_line(node),
                    signature=_collapse(_node_text(node, raw)),
                    parent="::".join(scopes),
                )
            )
        return

    if kind in {"type_definition", "alias_declaration"}:
        name_node = node.child_by_field_name("declarator") or node.child_by_field_name("name")
        name_node = _declarator_name(name_node)
        if name_node is not None:
            name = _node_text(name_node, raw)
            qualified = "::".join([*scopes, name])
            line = _line(node)
            symbols.append(
                SymbolFact(
                    symbol_id=_symbol_id(rel_path, qualified, line),
                    name=name,
                    qualified_name=qualified,
                    kind=SymbolKind.TYPE_ALIAS,
                    path=rel_path,
                    line=line,
                    end_line=_end_line(node),
                    signature=_collapse(_node_text(node, raw)),
                    parent="::".join(scopes),
                )
            )
        # typedef 里可能内嵌 struct/enum 定义，继续下钻
        _walk_children_c(
            node, raw, text, rel_path, language, scopes, current, symbols, includes, calls, entries
        )
        return

    if kind == "call_expression":
        fn = node.child_by_field_name("function")
        # 被挖空的 CUDA 启动会在语法树上呈现为普通调用，交给 _scan_cuda_launches 单独记录
        if fn is not None and b"<<<" not in raw[node.start_byte : node.end_byte]:
            calls.append(
                CallFact(
                    path=rel_path,
                    line=_line(node),
                    caller=current,
                    callee=_collapse(_node_text(fn, raw)),
                )
            )

    _walk_children_c(
        node, raw, text, rel_path, language, scopes, current, symbols, includes, calls, entries
    )


def _walk_children_c(
    node: Any,
    raw: bytes,
    text: str,
    rel_path: str,
    language: str,
    scopes: list[str],
    current: str,
    symbols: list[SymbolFact],
    includes: list[IncludeFact],
    calls: list[CallFact],
    entries: list[EntryFact],
) -> None:
    for child in node.named_children:
        _walk_c_like(
            child, raw, text, rel_path, language, scopes, current, symbols, includes, calls, entries
        )


def _declarator_name(node: Any) -> Any:
    """在 declarator 子树里找到真正的名字节点（跳过参数表、模板参数）。"""
    if node is None:
        return None
    if node.type == "qualified_identifier":
        return node.child_by_field_name("name")
    if node.type in {
        "identifier",
        "field_identifier",
        "type_identifier",
        "destructor_name",
        "operator_name",
        "operator_cast",
    }:
        return node
    for child in node.named_children:
        if child.type in {"parameter_list", "template_parameter_list", "type_qualifier"}:
            continue
        found = _declarator_name(child)
        if found is not None:
            return found
    return None


def _qualified_identifier_of(node: Any) -> Any:
    """向上找限定标识符（`Engine::run` 里的 `Engine::run` 节点）。"""
    current = node
    while current is not None:
        if current.type == "qualified_identifier":
            return current
        current = current.parent
    return None


def _cuda_qualifier(text: str, start_byte: int) -> str:
    """函数定义前的前缀里找 `__global__` / `__device__`（截到上一个 ; { } 为止）。"""
    prefix = text[max(0, start_byte - 300) : start_byte]
    cut = max(prefix.rfind(";"), prefix.rfind("}"), prefix.rfind("{"))
    prefix = prefix[cut + 1 :]
    if "__global__" in prefix:
        return "kernel"
    if "__device__" in prefix:
        return "device"
    return ""


def _scan_cuda_launches(
    text: str,
    rel_path: str,
    language: str,
    symbols: list[SymbolFact],
    calls: list[CallFact],
    entries: list[EntryFact],
) -> None:
    if language not in {"cuda", "cpp"}:
        return
    masked = _mask_comments(text)
    functions = [
        s
        for s in symbols
        if s.kind
        in {
            SymbolKind.FUNCTION,
            SymbolKind.METHOD,
            SymbolKind.KERNEL,
            SymbolKind.DEVICE_FUNCTION,
        }
    ]
    for match in _CUDA_LAUNCH.finditer(masked):
        name = match.group(1)
        line = masked.count("\n", 0, match.start()) + 1
        caller = ""
        innermost = None
        for sym in functions:
            inside = sym.line <= line <= sym.end_line
            closer = innermost is None or (
                sym.end_line - sym.line < innermost.end_line - innermost.line
            )
            if inside and closer:
                innermost = sym
        if innermost is not None:
            caller = innermost.symbol_id
        calls.append(
            CallFact(
                path=rel_path,
                line=line,
                caller=caller,
                callee=name,
                kind="kernel_launch",
                confidence=Confidence.LOW,
            )
        )
        entries.append(
            EntryFact(
                path=rel_path,
                line=line,
                name=name,
                kind="kernel_launch",
                confidence=Confidence.LOW,
            )
        )


def _mask_cuda(raw: bytes) -> bytes:
    """把 CUDA 限定符与启动配置替换成等长空格（保留换行），供语法解析使用。"""

    def blanks(match: re.Match[bytes]) -> bytes:
        return b"".join(b"\n" if byte == 10 else b" " for byte in match.group(0))

    masked = _CUDA_TOKENS.sub(blanks, raw)
    return _CUDA_LAUNCH_BYTES.sub(blanks, masked)


def _mask_comments(text: str) -> str:
    """把 C/C++ 注释替换成空格，保留换行，避免启动语法误判。"""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end == -1 else end
            out.append(" " * (end - i))
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append("".join(ch if ch == "\n" else " " for ch in text[i:end]))
            i = end
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


# ---------------------------------------------------------------- Python


def _walk_python(
    node: Any,
    raw: bytes,
    rel_path: str,
    scopes: list[str],
    current: str,
    symbols: list[SymbolFact],
    includes: list[IncludeFact],
    calls: list[CallFact],
    entries: list[EntryFact],
) -> None:
    kind = node.type

    if kind == "class_definition":
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            name = _node_text(name_node, raw)
            qualified = ".".join([*scopes, name])
            line = _line(node)
            symbols.append(
                SymbolFact(
                    symbol_id=_symbol_id(rel_path, qualified, line),
                    name=name,
                    qualified_name=qualified,
                    kind=SymbolKind.CLASS,
                    path=rel_path,
                    line=line,
                    end_line=_end_line(node),
                    signature=_collapse(
                        _node_text(node, raw).split("\n")[0],
                    ),
                    parent=".".join(scopes),
                )
            )
            for child in node.named_children:
                _walk_python(
                    child, raw, rel_path, [*scopes, name], "", symbols, includes, calls, entries
                )
            return

    if kind in {"function_definition", "async_function"}:
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            name = _node_text(name_node, raw)
            qualified = ".".join([*scopes, name])
            line = _line(node)
            body = node.child_by_field_name("body")
            sig_end = body.start_byte if body is not None else node.end_byte
            symbols.append(
                SymbolFact(
                    symbol_id=_symbol_id(rel_path, qualified, line),
                    name=name,
                    qualified_name=qualified,
                    kind=SymbolKind.METHOD if scopes else SymbolKind.FUNCTION,
                    path=rel_path,
                    line=line,
                    end_line=_end_line(node),
                    signature=_collapse(raw[node.start_byte : sig_end].decode("utf-8", "replace")),
                    parent=".".join(scopes),
                )
            )
            symbol_id = _symbol_id(rel_path, qualified, line)
            for child in node.named_children:
                if child.type == "block":
                    for stmt in child.named_children:
                        _walk_python(
                            stmt,
                            raw,
                            rel_path,
                            scopes,
                            symbol_id,
                            symbols,
                            includes,
                            calls,
                            entries,
                        )
            return

    if kind == "import_statement":
        line = _line(node)
        for part in _node_text(node, raw).removeprefix("import ").split(","):
            target = part.strip().split(" as ")[0].strip()
            if target:
                includes.append(IncludeFact(path=rel_path, target=target, line=line, kind="import"))
        return

    if kind in {"import_from_statement", "future_import_statement"}:
        line = _line(node)
        module = node.child_by_field_name("module_name")
        if module is not None:
            target = _node_text(module, raw)
        else:
            text = _node_text(node, raw)
            target = text.split(" import ")[0].removeprefix("from ").strip()
        includes.append(IncludeFact(path=rel_path, target=target, line=line, kind="import"))
        return

    if kind == "call":
        fn = node.child_by_field_name("function")
        if fn is not None:
            calls.append(
                CallFact(
                    path=rel_path,
                    line=_line(node),
                    caller=current,
                    callee=_collapse(_node_text(fn, raw)),
                )
            )

    if kind == "if_statement" and _PY_MAIN.search(_node_text(node, raw).split(":")[0]):
        entries.append(
            EntryFact(path=rel_path, line=_line(node), name="__main__", kind="python_main")
        )

    for child in node.named_children:
        _walk_python(child, raw, rel_path, scopes, current, symbols, includes, calls, entries)


__all__ = [
    "SUPPORTED_LANGUAGES",
    "extract_file_facts",
    "scan_env_reads",
]
