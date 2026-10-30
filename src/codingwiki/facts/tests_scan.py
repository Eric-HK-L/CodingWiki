"""测试发现：gtest 宏、pytest 测试函数、CTest 用例（计划文档 8.7.3）。"""

from __future__ import annotations

import re

from codingwiki.domain.facts import Confidence, FileFacts, TestFact

_GTEST_RE = re.compile(
    r"\b(TEST|TEST_F|TEST_P|TYPED_TEST|TYPED_TEST_P|TEST_F_P)\s*\(\s*"
    r"([A-Za-z_]\w*)\s*,\s*([A-Za-z_]\w*)"
)


def scan_gtest(text_files: list[tuple[str, str]]) -> tuple[list[TestFact], int]:
    """返回 (测试事实, 命中文件数)。text_files 只传 C/C++/CUDA 源文件。"""
    facts: list[TestFact] = []
    hit_files = 0
    for rel_path, text in text_files:
        matched_here = False
        for match in _GTEST_RE.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            facts.append(
                TestFact(
                    name=match.group(3),
                    framework="gtest",
                    path=rel_path,
                    line=line,
                    suite=match.group(2),
                    confidence=Confidence.MEDIUM,
                )
            )
            matched_here = True
        if matched_here:
            hit_files += 1
    return facts, hit_files


def scan_pytest(file_facts: list[FileFacts]) -> list[TestFact]:
    """只看 Python 文件里命中的测试函数；路径或类名不构成证据的就不算。"""
    facts: list[TestFact] = []
    for file in file_facts:
        if file.language != "python":
            continue
        is_test_file = "/tests/" in f"/{file.path}" or file.path.rsplit("/", 1)[-1].startswith(
            "test_"
        )
        if not is_test_file:
            continue
        for symbol in file.symbols:
            if not symbol.name.startswith("test_"):
                continue
            facts.append(
                TestFact(
                    name=symbol.name,
                    framework="pytest",
                    path=file.path,
                    line=symbol.line,
                    suite=symbol.parent,
                    confidence=Confidence.HIGH,
                )
            )
    return facts


__all__ = ["scan_gtest", "scan_pytest"]
