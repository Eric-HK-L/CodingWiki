"""分词（架构文档 v0.5 14.2）。

- 代码标识符按驼峰/下划线/连字符/数字边界拆词，并保留原词；
- 英文与数字小写化；
- CJK 按二元组（bigram）切分，不引入分词依赖。
"""

from __future__ import annotations

import re

# 连续 CJK 段
_CJK_RUN = re.compile(r"[一-鿿]+")
# 标识符/单词段：字母数字下划线组合
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+(?:\.[0-9]+)?")
# 驼峰与数字边界
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def tokenize(text: str) -> list[str]:
    """把文本切成检索词元。保留原词 + 拆分词，全部小写。"""
    tokens: list[str] = []
    for match in _WORD.finditer(text):
        word = match.group(0)
        tokens.append(word.lower())
        for part in _CAMEL.sub("_", word).replace("-", "_").split("_"):
            part = part.strip("_").lower()
            if part and part != word.lower():
                tokens.append(part)
    for match in _CJK_RUN.finditer(text):
        run = match.group(0)
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def identifiers(text: str) -> set[str]:
    """提取文本中的代码标识符（含下划线或驼峰的词），用于精确命中加权。"""
    found: set[str] = set()
    for match in _WORD.finditer(text):
        word = match.group(0)
        if "_" in word or _CAMEL.search(word):
            found.add(word)
    return found


__all__ = ["identifiers", "tokenize"]
