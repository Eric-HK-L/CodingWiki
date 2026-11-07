"""确定性影响面（架构文档 阶段2 §5.2）。

只做去重与稳定排序：输入是已由 facts 检索出的文件名/符号/调用方/配置/
构建目标/测试，不调用模型、不读文件、不做推断。
"""

from __future__ import annotations

from collections.abc import Iterable

from codingwiki.domain.spec import ImpactAnalysis

# 公开可调用接口的符号类别（方法/函数/核函数/设备函数）
INTERFACE_KINDS = frozenset({"function", "method", "kernel", "device_function"})


def _stable_unique(values: Iterable[str]) -> list[str]:
    """去空、去重、按字符串排序；同输入必得同列表。"""
    return sorted({value.strip() for value in values if value.strip()})


def build_impact(
    *,
    requirement_id: str,
    files: Iterable[str] = (),
    interfaces: Iterable[str] = (),
    callers: Iterable[str] = (),
    config: Iterable[str] = (),
    build: Iterable[str] = (),
    tests: Iterable[str] = (),
) -> ImpactAnalysis:
    """把证据集合折叠成一份影响面。"""
    return ImpactAnalysis(
        requirement_id=requirement_id,
        files=_stable_unique(files),
        interfaces=_stable_unique(interfaces),
        callers=_stable_unique(callers),
        config=_stable_unique(config),
        build=_stable_unique(build),
        tests=_stable_unique(tests),
    )


def caller_ref(caller: str, path: str, line: int) -> str:
    """调用方的稳定标识：有符号用 symbol_id，否则退到 path:line。"""
    if caller:
        return caller
    return f"{path}:{line}"


__all__ = ["INTERFACE_KINDS", "build_impact", "caller_ref"]
