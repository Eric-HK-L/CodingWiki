"""排除规则 + 语言识别。

默认规则（计划文档 7.5.1）+ 仓库级覆盖。每条排除都带 reason，不"假装不存在"。
语言识别按扩展名 → 文件头特征 → unknown，不瞎猜。
"""

from __future__ import annotations

import os
from pathlib import Path

# 默认排除规则：匹配路径段或扩展名，每条写清楚 reason
_DEFAULT_EXCLUDE_RULES: list[tuple[str, str]] = [
    (r"(^|[/\\])\.git([/\\]|$)", "vcs"),
    (r"(^|[/\\])\.svn([/\\]|$)", "vcs"),
    (r"(^|[/\\])\.hg([/\\]|$)", "vcs"),
    (r"(^|[/\\])build([/\\]|$)", "build_output"),
    (r"(^|[/\\])out([/\\]|$)", "build_output"),
    (r"(^|[/\\])dist([/\\]|$)", "build_output"),
    (r"(^|[/\\])node_modules([/\\]|$)", "vendor"),
    (r"(^|[/\\])third_party([/\\]|$)", "vendor"),
    (r"(^|[/\\])vendor([/\\]|$)", "vendor"),
    (r"(^|[/\\])external([/\\]|$)", "vendor"),
    (r"(^|[/\\])__pycache__([/\\]|$)", "generated"),
    (r"\.pb\.(cc|h)$", "generated"),
    (r"\.min\.js$", "generated"),
    (r"(^|[/\\])CMakeCache\.txt$", "generated"),
    (r"(^|[/\\])CMakeFiles([/\\]|$)", "generated"),
]

# 默认二进制扩展名：命中即标记 binary
_DEFAULT_BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".bin",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".a",
        ".o",
        ".obj",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".webp",
        ".tiff",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".class",
        ".jar",
        ".war",
        ".ear",
        ".pyc",
        ".pyo",
        ".whl",
        ".deb",
        ".rpm",
        ".apk",
        ".ipa",
        ".mov",
        ".mp4",
        ".avi",
        ".mp3",
        ".wav",
        ".flac",
        ".onnx",
        ".pt",
        ".pth",
        ".safetensors",
        ".pb",
        ".ttf",
        ".otf",
        ".woff",
        ".woff2",
        ".eot",
        ".sqlite",
        ".sqlite3",
        ".db",
        ".db3",
        ".iso",
        ".img",
        ".dmg",
    }
)

# 扩展名 → 语言（保守映射，能识别就识别，不能识别就 unknown）
_EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
    ".cu": "cuda",
    ".cuh": "cuda",
    ".py": "python",
    ".pyx": "python",
    ".pxd": "python",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".rb": "ruby",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "css",
    ".sass": "css",
    ".less": "css",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".ps1": "shell",
    ".cmake": "cmake",
    ".proto": "protobuf",
    ".sql": "sql",
    ".md": "markdown",
    ".mdx": "markdown",
    ".txt": "text",
    ".cfg": "config",
    ".ini": "config",
    ".toml": "toml",
}


def _matches_any(path: str, rules: list[tuple[str, str]]) -> str | None:
    """规则是正则串；匹配到就返回 reason，否则返回 None。"""
    import re

    for pattern, reason in rules:
        if re.search(pattern, path, flags=re.IGNORECASE):
            return reason
    return None


def should_exclude(
    path: str,
    *,
    extra_rules: list[str] | None = None,
) -> tuple[bool, str]:
    """判断一个相对路径是否应被排除。返回 (是否排除, reason)。

    extra_rules 是用户自己加的 glob（简化成字符串匹配规则，转成正则）。
    """
    # 先跑 extra 规则，再跑默认规则（extra 匹配到就用 extra，不重复跑）
    if extra_rules:
        converted = _glob_to_regex_list(extra_rules)
        match = _matches_any(path, converted)
        if match is not None:
            return True, match
    reason = _matches_any(path, _DEFAULT_EXCLUDE_RULES)
    if reason is not None:
        return True, reason
    return False, ""


def is_binary_by_extension(path: str, extra_extensions: frozenset[str] | None = None) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if extra_extensions and ext in extra_extensions:
        return True
    return ext in _DEFAULT_BINARY_EXTENSIONS


def detect_language(path: str, overrides: dict[str, str] | None = None) -> str:
    """扩展名优先识别；override 优先级最高。"""
    ext = os.path.splitext(path)[1].lower()
    if overrides:
        for k, v in overrides.items():
            if k == ext or (k.startswith(".") and k == ext):
                return v
    return _EXTENSION_TO_LANGUAGE.get(ext, "unknown")


def _glob_to_regex_list(globs: list[str]) -> list[tuple[str, str]]:
    """把用户的 glob 规则（如 'test/dump/**'）转成正则串，reason 用规则本身。"""

    compiled: list[tuple[str, str]] = []
    for g in globs:
        pattern = _glob_to_regex(g)
        compiled.append((pattern, f"user:{g}"))
    return compiled


def _glob_to_regex(glob: str) -> str:
    """简化 glob → regex。支持 **（多层）、*（单层）、?（单字符）。"""
    import re

    out = []
    i = 0
    while i < len(glob):
        if glob[i : i + 2] == "**":
            out.append(r".*")
            i += 2
        elif glob[i] == "*":
            out.append(r"[^/\\]*")
            i += 1
        elif glob[i] == "?":
            out.append(r"[^/\\]")
            i += 1
        elif glob[i] in ".^$+{}|()\\[]":
            out.append(re.escape(glob[i]))
            i += 1
        else:
            out.append(glob[i])
            i += 1
    return f"(^|[/\\\\]){''.join(out)}([/\\\\]|$)"


def looks_text_by_content(path: Path) -> bool:
    """保守判断：读前 8 KB，若含 null byte 则认为是二进制。"""
    try:
        chunk = path.read_bytes()[: 8 * 1024]
    except OSError:
        return False
    return b"\x00" not in chunk


__all__ = [
    "detect_language",
    "is_binary_by_extension",
    "looks_text_by_content",
    "should_exclude",
]
