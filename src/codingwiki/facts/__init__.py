"""代码事实层（S3）。

对外只暴露编排器、存储和 Protocol；解析细节留在各实现模块里。
"""

from __future__ import annotations

from codingwiki.facts.extractor import EXTRACTOR_VERSION, FilesystemFactExtractor
from codingwiki.facts.protocol import FactExtractor, FactStore
from codingwiki.facts.store import FilesystemFactStore

__all__ = [
    "EXTRACTOR_VERSION",
    "FactExtractor",
    "FactStore",
    "FilesystemFactExtractor",
    "FilesystemFactStore",
]
