"""JUnit XML 测试报告解析（开发计划 14.5.4）。

只读取测试命令在 `test.report_paths` 里声明的报告（相对源码根的 glob），
提取失败/错误与跳过用例；报告缺失或坏 XML 返回错误，由编排器退回退出码/日志。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from codingwiki.domain.coding import TestFailure, TestFailureKind
from codingwiki.domain.models import TestCommands

_FAILURE_LIMIT = 20
_SKIP_LIMIT = 50
_MESSAGE_CHARS = 300


@dataclass(frozen=True)
class JUnitConfig:
    """报告定位参数：源码根 + 相对 glob。"""

    root: Path
    patterns: tuple[str, ...]


@dataclass(frozen=True)
class JUnitResult:
    """一次报告解析结果；files 为实际命中的相对路径。"""

    files: list[str] = field(default_factory=list)
    failures: list[TestFailure] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def load_config(test: TestCommands, *, source_root: Path) -> JUnitConfig | None:
    """报告的格式不是 junit 时返回 None，表示不解析。"""
    if test.report_format != "junit":
        return None
    return JUnitConfig(root=source_root, patterns=tuple(test.report_paths))


def parse_reports(config: JUnitConfig, *, since_ns: int = 0) -> JUnitResult:
    """解析命中的报告；since_ns 用于排除上一轮遗留的旧报告。"""
    files = _report_files(config, since_ns)
    if not files:
        patterns = ", ".join(config.patterns) or "（未配置 report_paths）"
        return JUnitResult(errors=[f"未找到 JUnit 报告：{patterns}"])

    failures: list[TestFailure] = []
    skipped: list[str] = []
    errors: list[str] = []
    seen_failures: set[str] = set()
    seen_skipped: set[str] = set()
    for path in files:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            errors.append(f"{_display(config.root, path)}: XML 解析失败：{exc}")
            continue
        except OSError as exc:
            errors.append(f"{_display(config.root, path)}: 读取失败：{exc}")
            continue
        _collect(root, failures, skipped, seen_failures, seen_skipped)

    return JUnitResult(
        files=[_display(config.root, path) for path in files],
        failures=failures[:_FAILURE_LIMIT],
        skipped=skipped[:_SKIP_LIMIT],
        errors=errors,
    )


def _report_files(config: JUnitConfig, since_ns: int) -> list[Path]:
    found: dict[str, Path] = {}
    for pattern in config.patterns:
        relative = Path(pattern)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        for path in config.root.glob(pattern):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime_ns < since_ns:
                    continue
            except OSError:
                continue
            found[str(path.resolve())] = path
    return [found[key] for key in sorted(found)]


def _collect(
    root: ET.Element,
    failures: list[TestFailure],
    skipped: list[str],
    seen_failures: set[str],
    seen_skipped: set[str],
) -> None:
    for case in root.iter("testcase"):
        ident = _ident(case)
        if not ident:
            continue
        failure = _failure_of(case, ident)
        if failure is not None:
            if failure.id not in seen_failures:
                seen_failures.add(failure.id)
                failures.append(failure)
            continue
        if ident not in seen_skipped and any(child.tag == "skipped" for child in case):
            seen_skipped.add(ident)
            skipped.append(ident)


def _ident(case: ET.Element) -> str:
    name = (case.get("name") or "").strip()
    classname = (case.get("classname") or "").strip()
    if not name:
        return ""
    return f"{classname}::{name}" if classname else name


def _failure_of(case: ET.Element, ident: str) -> TestFailure | None:
    for child in case:
        if child.tag not in {"failure", "error"}:
            continue
        kind = TestFailureKind.ERROR if child.tag == "error" else TestFailureKind.FAILURE
        return TestFailure(
            id=ident,
            kind=kind,
            file=(case.get("file") or "").strip(),
            line=_line(case.get("line")),
            message=_message(child),
        )
    return None


def _message(child: ET.Element) -> str:
    text = (child.get("message") or "").strip() or (child.text or "").strip()
    return text[:_MESSAGE_CHARS]


def _line(value: str | None) -> int:
    try:
        number = int((value or "").strip())
    except ValueError:
        return 0
    return max(number, 0)


def _display(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


__all__ = ["JUnitConfig", "JUnitResult", "load_config", "parse_reports"]
