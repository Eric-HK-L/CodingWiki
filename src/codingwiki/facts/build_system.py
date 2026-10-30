"""构建与测试目标发现（计划文档 8.7）。

三种来源，按可靠度从高到低：
1. CMake File API 的 reply JSON（机器生成，可直接信任）；
2. `compile_commands.json`（只用于统计编译命令覆盖率，不做语义分析）；
3. CMakeLists.txt 文本解析（近似，confidence=medium）。

解析不了的东西不假装：缺失/损坏的文件记进 notes，由调用方写进质量报告。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from codingwiki.domain.facts import BuildTarget, Confidence, TestFact

_COMMAND_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_SOURCE_SUFFIXES = frozenset(
    {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".cu", ".cuh", ".s", ".asm"}
)
_TARGET_KEYWORDS = frozenset(
    {
        "STATIC",
        "SHARED",
        "MODULE",
        "OBJECT",
        "INTERFACE",
        "IMPORTED",
        "ALIAS",
        "UNKNOWN",
        "GLOBAL",
        "EXCLUDE_FROM_ALL",
        "WIN32",
        "MACOSX_BUNDLE",
        "ON",
        "OFF",
        "PUBLIC",
        "PRIVATE",
        "REQUIRED",
        "SYSTEM",
        "BEFORE",
    }
)
_FILE_API_TYPES = {
    "STATIC_LIBRARY": "library",
    "SHARED_LIBRARY": "library",
    "MODULE_LIBRARY": "library",
    "OBJECT_LIBRARY": "library",
    "INTERFACE_LIBRARY": "library",
    "EXECUTABLE": "executable",
    "UTILITY": "custom",
}


# ---------------------------------------------------------------- CMake 文本解析


def parse_cmake_files(workdir: Path, cmake_paths: list[str]) -> list[BuildTarget]:
    """解析一组 CMakeLists.txt / *.cmake。变量只在同一文件内展开。"""
    workdir = workdir.resolve()
    targets: dict[str, BuildTarget] = {}
    pending_links: list[tuple[str, str, str]] = []  # (命令, 目标, 参数)
    pending_defs: list[tuple[str, list[str]]] = []
    pending_includes: list[tuple[str, list[str]]] = []

    for rel in sorted(cmake_paths):
        file_path = workdir / rel
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        cmake_dir = file_path.parent
        variables = _collect_variables(text)
        for command, args, offset in _iter_commands(text):
            line = text.count("\n", 0, offset) + 1
            expanded = _expand_args(args, variables, cmake_dir, workdir)
            if command in {"add_library", "add_executable", "add_test"}:
                target = _make_target(command, expanded, rel, line, cmake_dir, workdir)
                if target is not None:
                    targets[target.name] = target
            elif command in {"cuda_add_library", "cuda_add_executable"}:
                kind = "library" if "library" in command else "executable"
                target = _make_target(
                    "add_library" if kind == "library" else "add_executable",
                    expanded,
                    rel,
                    line,
                    cmake_dir,
                    workdir,
                )
                if target is not None:
                    targets[target.name] = target
            elif command == "target_link_libraries" and len(expanded) >= 2:
                pending_links.extend((expanded[0], dep, rel) for dep in expanded[1:])
            elif command == "target_compile_definitions" and len(expanded) >= 2:
                pending_defs.append((expanded[0], expanded[1:]))
            elif command == "target_include_directories" and len(expanded) >= 2:
                pending_includes.append(
                    (expanded[0], [_dir_arg(d, cmake_dir, workdir) for d in expanded[1:]])
                )

    for name, deps, _ in _group_links(pending_links):
        existing = targets.get(name)
        if existing is not None:
            values = [d for d in deps if d not in _TARGET_KEYWORDS]
            targets[name] = existing.model_copy(update={"deps": sorted(set(values))})
    for name, defs in pending_defs:
        existing = targets.get(name)
        if existing is not None:
            values = [d for d in defs if d not in _TARGET_KEYWORDS]
            targets[name] = existing.model_copy(
                update={"compile_definitions": sorted(set(existing.compile_definitions + values))}
            )
    for name, dirs in pending_includes:
        existing = targets.get(name)
        if existing is not None:
            values = [d for d in dirs if d not in _TARGET_KEYWORDS]
            targets[name] = existing.model_copy(
                update={"include_dirs": sorted(set(existing.include_dirs + values))}
            )

    return sorted(targets.values(), key=lambda t: (t.path, t.line, t.name))


def _group_links(items: list[tuple[str, str, str]]) -> list[tuple[str, list[str], str]]:
    grouped: dict[tuple[str, str], list[str]] = {}
    for target, dep, rel in items:
        grouped.setdefault((target, rel), []).append(dep)
    return [(target, deps, rel) for (target, rel), deps in grouped.items()]


def _make_target(
    command: str,
    args: list[str],
    rel: str,
    line: int,
    cmake_dir: Path,
    workdir: Path,
) -> BuildTarget | None:
    if not args:
        return None
    name = args[0]
    if not _valid_name(name):
        return None
    if command == "add_test":
        return _make_ctest_target(args, rel, line, cmake_dir, workdir)
    kind = "library" if command == "add_library" else "executable"
    sources = [
        p
        for p in (
            _normalize_path(a, cmake_dir, workdir) for a in args[1:] if a not in _TARGET_KEYWORDS
        )
        if p is not None and Path(p).suffix.lower() in _SOURCE_SUFFIXES
    ]
    return BuildTarget(
        name=name,
        kind=kind,
        path=rel,
        line=line,
        sources=sorted(set(sources)),
        confidence=Confidence.MEDIUM,
    )


def _make_ctest_target(
    args: list[str], rel: str, line: int, cmake_dir: Path, workdir: Path
) -> BuildTarget | None:
    name = ""
    command = ""
    if args and args[0].upper() == "NAME" and len(args) >= 2:
        name = args[1]
        rest = args[2:]
        if rest and rest[0].upper() == "COMMAND" and len(rest) >= 2:
            command = rest[1]
    elif len(args) >= 2:
        name = args[0]
        command = args[1]
    if not _valid_name(name):
        return None
    return BuildTarget(
        name=name,
        kind="test",
        path=rel,
        line=line,
        deps=[command] if command else [],
        confidence=Confidence.MEDIUM,
    )


def _valid_name(name: str) -> bool:
    return bool(name) and name not in _TARGET_KEYWORDS and not name.startswith("-")


def _collect_variables(text: str) -> dict[str, str]:
    variables: dict[str, str] = {}
    for command, args, _ in _iter_commands(text):
        if command == "set" and len(args) >= 2:
            variables[args[0]] = " ".join(args[1:])
    return variables


def _expand_args(
    args: list[str], variables: dict[str, str], cmake_dir: Path, workdir: Path
) -> list[str]:
    """展开变量；含变量的参数展开后按空白再切分（`${SRC}` 常代表多个文件）。"""
    out: list[str] = []
    for arg in args:
        expanded = _expand(arg, variables, cmake_dir, workdir)
        if "${" in arg:
            out.extend(expanded.split())
        elif expanded:
            out.append(expanded)
    return out


def _expand(arg: str, variables: dict[str, str], cmake_dir: Path, workdir: Path) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name == "CMAKE_CURRENT_SOURCE_DIR":
            return str(cmake_dir)
        if name == "PROJECT_SOURCE_DIR":
            return str(workdir)
        return variables.get(name, match.group(0))

    expanded = re.sub(r"\$\{([A-Za-z0-9_]+)\}", repl, arg)
    return expanded.strip().strip('"')


def _normalize_path(arg: str, cmake_dir: Path, workdir: Path) -> str | None:
    if "$<" in arg:
        return None  # generator expression：静态解析不了
    candidate = Path(arg)
    if not candidate.is_absolute():
        candidate = cmake_dir / candidate
    try:
        rel = candidate.resolve().relative_to(workdir)
    except (ValueError, OSError):
        return None
    return str(rel).replace("\\", "/")


def _dir_arg(arg: str, cmake_dir: Path, workdir: Path) -> str:
    """目录参数：能归一到仓库内相对路径就归一，否则保留原值。"""
    normalized = _normalize_path(arg, cmake_dir, workdir)
    return normalized if normalized is not None else arg


def _iter_commands(text: str) -> list[tuple[str, list[str], int]]:
    """扫描 `命令(参数)`；跳过注释和字符串里的伪命令。"""
    results: list[tuple[str, list[str], int]] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "#":
            end = text.find("\n", i)
            i = n if end == -1 else end
            continue
        if ch == '"':
            i = _skip_quoted(text, i)
            continue
        if ch.isalpha() or ch == "_":
            match = _COMMAND_RE.match(text, i)
            if match is not None:
                command = match.group(1).lower()
                close = _find_close_paren(text, match.end() - 1)
                if close is not None:
                    body = text[match.end() : close]
                    results.append((command, _split_args(body), match.start()))
                    i = close + 1
                    continue
            i += 1
            continue
        i += 1
    return results


def _skip_quoted(text: str, start: int) -> int:
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i + 1
        i += 1
    return len(text)


def _find_close_paren(text: str, open_index: int) -> int | None:
    depth = 0
    i = open_index
    while i < len(text):
        ch = text[i]
        if ch == '"':
            i = _skip_quoted(text, i)
            continue
        if ch == "#":
            end = text.find("\n", i)
            i = len(text) if end == -1 else end
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _split_args(body: str) -> list[str]:
    args: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for ch in body:
        if quote:
            if ch == quote:
                quote = None
            else:
                current.append(ch)
            continue
        if ch in {'"', "'"}:
            quote = ch
            continue
        if ch.isspace():
            if current:
                args.append("".join(current))
                current = []
            continue
        current.append(ch)
    if current:
        args.append("".join(current))
    return args


# ---------------------------------------------------------------- compile_commands


def read_compile_commands(workdir: Path) -> tuple[set[str], bool, list[str]]:
    """返回 (有编译命令的文件相对路径集合, 是否找到可解析的 compile_commands.json, notes)。"""
    workdir = workdir.resolve()
    candidates = [workdir / "compile_commands.json", workdir / "build" / "compile_commands.json"]
    candidates.extend(sorted(workdir.glob("cmake-build-*/compile_commands.json")))
    notes: list[str] = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            data: Any = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            notes.append(f"{candidate.name} 读取失败：{exc}")
            continue
        covered: set[str] = set()
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("file"), str):
                    covered.add(_rel_to_workdir(item["file"], workdir))
        notes.append(f"compile_commands.json 覆盖 {len(covered)} 个文件")
        return covered, True, notes
    return set(), False, notes


def _rel_to_workdir(raw: str, workdir: Path) -> str:
    candidate = Path(raw)
    if not candidate.is_absolute():
        return raw.replace("\\", "/")
    try:
        return str(candidate.resolve().relative_to(workdir)).replace("\\", "/")
    except (ValueError, OSError):
        return raw.replace("\\", "/")


# ---------------------------------------------------------------- CMake File API


def find_cmake_file_api_dirs(workdir: Path) -> list[Path]:
    """找已有构建目录里的 `.cmake/api/v1/reply`（只读，不重新配置）。"""
    workdir = workdir.resolve()
    dirs: list[Path] = []
    direct = workdir / ".cmake" / "api" / "v1" / "reply"
    if direct.is_dir():
        dirs.append(direct)
    dirs.extend(sorted(p for p in workdir.glob("*/.cmake/api/v1/reply") if p.is_dir()))
    return dirs


def read_cmake_file_api(reply_dir: Path) -> tuple[list[BuildTarget], list[str]]:
    """读取 target-*.json；损坏的单个文件跳过并记 note。"""
    targets: list[BuildTarget] = []
    notes: list[str] = []
    for path in sorted(reply_dir.glob("target-*.json")):
        try:
            data: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            notes.append(f"{path.name} 读取失败：{exc}")
            continue
        if not isinstance(data, dict) or not isinstance(data.get("name"), str):
            notes.append(f"{path.name} 缺少 name，跳过")
            continue
        name = data["name"]
        raw_type = str(data.get("type", "")).upper()
        kind = _FILE_API_TYPES.get(raw_type, "unknown")
        sources: list[str] = []
        for source in data.get("sources", []) or []:
            if isinstance(source, dict) and isinstance(source.get("path"), str):
                sources.append(source["path"].replace("\\", "/"))
        defines: list[str] = []
        include_dirs: list[str] = []
        for group in data.get("compileGroups", []) or []:
            if not isinstance(group, dict):
                continue
            defines.extend(str(d) for d in group.get("defines", []) or [])
            for inc in group.get("includes", []) or []:
                if isinstance(inc, dict) and isinstance(inc.get("path"), str):
                    include_dirs.append(inc["path"])
        targets.append(
            BuildTarget(
                name=name,
                kind=kind,
                build_system="cmake-file-api",
                path=str(path),
                sources=sorted(set(sources)),
                compile_definitions=sorted(set(defines)),
                include_dirs=sorted(set(include_dirs)),
                confidence=Confidence.HIGH,
            )
        )
    if targets:
        notes.append(f"CMake File API 读取 {len(targets)} 个目标")
    return targets, notes


# ---------------------------------------------------------------- CTest


def ctest_facts(targets: list[BuildTarget]) -> list[TestFact]:
    return [
        TestFact(
            name=t.name,
            framework="ctest",
            path=t.path,
            line=t.line,
            target=t.deps[0] if t.deps else "",
            confidence=t.confidence,
        )
        for t in targets
        if t.kind == "test"
    ]


__all__ = [
    "ctest_facts",
    "find_cmake_file_api_dirs",
    "parse_cmake_files",
    "read_cmake_file_api",
    "read_compile_commands",
]
