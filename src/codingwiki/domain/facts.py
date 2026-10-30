"""S3 代码事实层的领域模型。

对应架构文档 v0.3 第 3.4、12 节与计划文档第 8 章。
只放 Pydantic 字段定义，不读写文件、不打日志；行号统一 1-based。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _FactModel(BaseModel):
    """事实模型基类：拒绝未知字段、不可变、去掉字符串首尾空白。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class Confidence(StrEnum):
    """事实可靠度。high=语法结构，medium=构建文本解析，low=正则启发式。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SymbolKind(StrEnum):
    """符号类别。CUDA 核函数/设备函数单列，避免被当成普通 C++ 函数。"""

    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    STRUCT = "struct"
    ENUM = "enum"
    MACRO = "macro"
    TYPE_ALIAS = "type_alias"
    VARIABLE = "variable"
    NAMESPACE = "namespace"
    MODULE = "module"
    KERNEL = "kernel"
    DEVICE_FUNCTION = "device_function"


class SymbolFact(_FactModel):
    """一个可定位的代码符号。"""

    symbol_id: str  # <path>::<qualified_name>@<line>
    name: str
    qualified_name: str
    kind: SymbolKind
    path: str
    line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    signature: str = ""
    parent: str = ""  # 所属类/结构体的限定名，空表示顶层
    confidence: Confidence = Confidence.HIGH

    @model_validator(mode="after")
    def _line_order(self) -> SymbolFact:
        if self.end_line < self.line:
            raise ValueError(f"{self.path}: end_line({self.end_line}) < line({self.line})")
        return self


class IncludeFact(_FactModel):
    """#include 或 import 边。"""

    path: str
    target: str
    line: int = Field(ge=1)
    kind: str = "include"  # include / import / using
    resolved: bool = False


class CallFact(_FactModel):
    """直接调用（或 CUDA 核函数启动）。"""

    path: str
    line: int = Field(ge=1)
    caller: str = ""  # 调用方 symbol_id；空表示文件顶层（如全局初始化）
    callee: str
    kind: str = "call"  # call / kernel_launch
    targets: list[str] = Field(default_factory=list)  # 解析到的 symbol_id
    confidence: Confidence = Confidence.HIGH

    @property
    def resolved(self) -> bool:
        return bool(self.targets)


class EntryFact(_FactModel):
    """确定的程序入口。"""

    path: str
    line: int = Field(ge=1)
    name: str
    kind: str  # main / python_main / kernel_launch / cli_command
    confidence: Confidence = Confidence.MEDIUM


class FileFacts(_FactModel):
    """单个文件的全部事实。缓存单位就是它。"""

    path: str
    language: str
    blob_sha256: str
    module: str = "(root)"
    line_count: int = Field(default=0, ge=0)
    parse_ok: bool = True
    parse_errors: int = Field(default=0, ge=0)
    symbols: list[SymbolFact] = Field(default_factory=list)
    includes: list[IncludeFact] = Field(default_factory=list)
    calls: list[CallFact] = Field(default_factory=list)
    entries: list[EntryFact] = Field(default_factory=list)


class BuildTarget(_FactModel):
    """构建系统里的一个目标（库/可执行/测试/自定义）。"""

    name: str
    kind: str = "unknown"  # library / executable / test / custom / unknown
    build_system: str = "cmake"
    path: str = ""
    line: int = Field(default=0, ge=0)
    sources: list[str] = Field(default_factory=list)
    deps: list[str] = Field(default_factory=list)
    compile_definitions: list[str] = Field(default_factory=list)
    include_dirs: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.MEDIUM
    note: str = ""


class ConfigFact(_FactModel):
    """配置项：配置文件的键，或代码里的环境变量/命令行参数读取。"""

    key: str
    kind: str = "file"  # file / env / arg
    format: str = ""  # yaml / json / toml / env / code
    path: str = ""
    line: int = Field(default=0, ge=0)
    reader: str = ""
    confidence: Confidence = Confidence.LOW


class TestFact(_FactModel):
    """测试用例或测试目标。"""

    name: str
    framework: str  # gtest / pytest / ctest / custom
    path: str = ""
    line: int = Field(default=0, ge=0)
    suite: str = ""
    target: str = ""
    confidence: Confidence = Confidence.MEDIUM


class QualityReport(_FactModel):
    """事实质量指标（计划文档 8.10）。无法测量时用 None，不填假数。"""

    files_total: int = Field(default=0, ge=0)
    files_parsed: int = Field(default=0, ge=0)
    parse_success_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    unresolved_call_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    compdb_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    build_target_source_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    env_read_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    tests_found: int = Field(default=0, ge=0)
    parse_failures: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class RepoFacts(_FactModel):
    """一次快照的完整事实批次，落盘为 JSON。"""

    snapshot_id: str
    repo_id: str
    revision: str
    extractor_version: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    files: list[FileFacts] = Field(default_factory=list)
    modules: dict[str, list[str]] = Field(default_factory=dict)
    build_targets: list[BuildTarget] = Field(default_factory=list)
    config_facts: list[ConfigFact] = Field(default_factory=list)
    tests: list[TestFact] = Field(default_factory=list)
    quality: QualityReport = Field(default_factory=QualityReport)

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def symbol_count(self) -> int:
        return sum(len(f.symbols) for f in self.files)

    @property
    def call_count(self) -> int:
        return sum(len(f.calls) for f in self.files)


__all__ = [
    "BuildTarget",
    "CallFact",
    "Confidence",
    "ConfigFact",
    "EntryFact",
    "FileFacts",
    "IncludeFact",
    "QualityReport",
    "RepoFacts",
    "SymbolFact",
    "SymbolKind",
    "TestFact",
]
