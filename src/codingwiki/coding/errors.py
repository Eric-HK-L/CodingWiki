"""把编译/测试输出收窄成结构化失败项（开发计划 14.5.4）。

只提取可回喂的骨架：文件、行号、错误码、短消息；不把整段日志塞给模型。
"""

from __future__ import annotations

import re

from codingwiki.domain.coding import FailureItem, StageKind

_LIMIT = 20
_TAIL_CHARS = 300

# path/to/file.cpp:12[:5]: error: message
_DIAGNOSTIC = re.compile(
    r"^(?P<file>[^:\s][^:]*):(?P<line>\d+)(?::(?P<col>\d+))?:\s*(?:fatal\s+)?error:\s*(?P<msg>.+)$"
)
# CMake Error at CMakeLists.txt:23 (target_link_libraries):
_CMAKE_ERROR = re.compile(r"^CMake Error(?: at)?\s+(?P<file>[^:]+):(?P<line>\d+).*$")
# CTest: "  12 - test_nvlog (Failed)"
_CTEST_SUMMARY = re.compile(r"^\s*\d+\s*-\s*(?P<name>.+?)\s*\(Failed\)\s*$")
# CTest: "12/20 Test #12: test_nvlog ........***Failed    0.01 sec"
_CTEST_STREAM = re.compile(r"^.*Test\s+#\d+:\s*(?P<name>[^ ]+)\s*\.*\*+Failed")


def parse_failures(
    stage: StageKind, stdout: str, stderr: str, *, limit: int = _LIMIT
) -> list[FailureItem]:
    """按 stderr/stderr→stdout 顺序提取失败项；无匹配时保留截断尾部。"""
    if limit <= 0:
        return []
    items: list[FailureItem] = []
    items.extend(_scan(stage, stderr, limit))
    items.extend(_scan(stage, stdout, limit))

    unique: list[FailureItem] = []
    seen: set[tuple[str, str, int, str]] = set()
    for item in items:
        if item.key in seen:
            continue
        seen.add(item.key)
        unique.append(item)
        if len(unique) >= limit:
            break

    if not unique:
        tail = (stderr.strip() or stdout.strip())[-_TAIL_CHARS:]
        unique = [
            FailureItem(
                stage=stage,
                code=f"{stage.value}.failed",
                message=tail or f"{stage.value} 阶段失败",
            )
        ]
    return unique


def timeout_failure(stage: StageKind, duration_ms: int) -> FailureItem:
    return FailureItem(
        stage=stage,
        code=f"{stage.value}.timeout",
        message=f"命令超时（{duration_ms} ms）",
    )


def _scan(stage: StageKind, text: str, limit: int) -> list[FailureItem]:
    items: list[FailureItem] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if len(items) >= limit:
            break
        match = _DIAGNOSTIC.match(line)
        if match:
            items.append(
                FailureItem(
                    stage=stage,
                    code=f"{stage.value}.diagnostic",
                    file=match.group("file"),
                    line=int(match.group("line")),
                    message=match.group("msg").strip(),
                )
            )
            continue
        match = _CMAKE_ERROR.match(line)
        if match:
            items.append(
                FailureItem(
                    stage=stage,
                    code="cmake.error",
                    file=match.group("file"),
                    line=int(match.group("line")),
                    message=line,
                )
            )
            continue
        match = _CTEST_SUMMARY.match(line) or _CTEST_STREAM.match(line)
        if match:
            items.append(
                FailureItem(
                    stage=stage,
                    code="test.failed",
                    message=match.group("name").strip(),
                )
            )
    return items


__all__ = ["parse_failures", "timeout_failure"]
