"""S4 Wiki 生成、校验与发布的领域模型。

对应架构文档 v0.4 第 13 节与计划文档第 9 章。
只放 Pydantic 字段定义，不读写文件、不打日志。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from codingwiki.domain.facts import (
    BuildTarget,
    CallFact,
    Confidence,
    ConfigFact,
    SymbolFact,
    TestFact,
)


class _WikiModel(BaseModel):
    """Wiki 领域模型基类：拒绝未知字段、不可变、去掉字符串首尾空白。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class PageType(StrEnum):
    """页面类型（计划文档 9.1）。"""

    HOME = "home"
    OVERVIEW = "overview"
    ARCHITECTURE = "architecture"
    MODULE = "module"
    INTERFACE = "interface"
    FLOW = "flow"
    CONFIG = "config"
    BUILD = "build"
    TEST = "test"
    GLOSSARY = "glossary"
    HOWTO = "howto"
    ADR = "adr"
    FILE = "file"


class CoverageLevel(StrEnum):
    """覆盖深度（计划文档 4.2）。"""

    C0 = "C0"
    C1 = "C1"
    C2 = "C2"
    C3 = "C3"


class PageStatus(StrEnum):
    """页面状态（计划文档 9.2.1）。"""

    DRAFT = "draft"
    VERIFIED = "verified"
    STALE = "stale"
    FAILED = "failed"


class PageMeta(_WikiModel):
    """front matter。页面 ID 稳定，不随标题变化。"""

    id: str = Field(min_length=1)
    type: PageType
    title: str = Field(min_length=1)
    snapshot: str = Field(min_length=1)  # snapshot_id，如 cuphy@<sha>
    module: str = ""
    status: PageStatus = PageStatus.DRAFT
    coverage_level: CoverageLevel = CoverageLevel.C1
    evidence_count: int = Field(default=0, ge=0)
    generated_by: str = ""  # 后端/模型标识，如 mock/mock-v1
    prompt_version: str = ""
    last_verified_at: datetime | None = None
    reviewed_by: str = ""
    tags: list[str] = Field(default_factory=list)
    format_version: int = Field(default=1, ge=1)


class Claim(_WikiModel):
    """result.json 中的一条结论。"""

    id: str = Field(pattern=r"^C\d+$")
    text: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)  # E 编号列表
    confidence: Confidence = Confidence.HIGH


class Evidence(_WikiModel):
    """result.json / meta.json 中的一条证据。quote 是逐字引用的代码片段。"""

    id: str = Field(pattern=r"^E\d+$")
    file: str = Field(min_length=1)
    symbol: str = ""
    quote: str = Field(min_length=1)
    line_hint: int = Field(default=0, ge=0)  # 仅提示，不作为唯一依据（4.5.2）


class UnknownItem(_WikiModel):
    """未确认事项：允许"不知道"，不允许"假装知道"。"""

    topic: str = Field(min_length=1)
    reason: str = ""
    needs: str = ""


class PageResult(_WikiModel):
    """output/result.json 的契约（计划文档 9.6.2）。"""

    page_id: str = Field(min_length=1)
    snapshot: str = Field(min_length=1)
    claims: list[Claim] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    unknowns: list[UnknownItem] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ids_unique(self) -> PageResult:
        claim_ids = [c.id for c in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("claim id 重复")
        evidence_ids = [e.id for e in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence id 重复")
        return self


class Page(_WikiModel):
    """一个完整页面 = front matter + 正文 markdown（正文不含 front matter 块）。"""

    meta: PageMeta
    body: str = Field(min_length=1)
    result: PageResult | None = None  # 解析自 result.json；草稿区可能缺失


class PageTask(_WikiModel):
    """覆盖计划中的一个生成任务。"""

    task_id: str = Field(min_length=1)  # 稳定：<page_id 转标识>
    page_id: str = Field(min_length=1)
    page_type: PageType
    title_hint: str = ""
    subject: str = ""  # 模块名 / 头文件路径 / 入口名 / 配置键
    scope: str = ""  # 事实筛选范围：模块键、文件路径前缀或入口路径
    rollup: bool = False  # 聚合页：把多个小模块合并成一页
    exclude: list[str] = Field(default_factory=list)  # 聚合页排除已独立成页的模块
    coverage_level: CoverageLevel = CoverageLevel.C2
    priority: int = Field(default=0, ge=0)  # 越小越优先
    reuse: bool = False  # 增量：依赖未变化，直接复用旧页面


class CoveragePlan(_WikiModel):
    """一次覆盖计划（9.3）。由确定性规则生成，不由模型决定写哪些页面。"""

    repo_id: str
    snapshot_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    tasks: list[PageTask] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)  # 因预算截断留到下一轮的 page_id


class PageDeps(_WikiModel):
    """页面依赖记录（9.10.1），用于增量再生成。"""

    page_id: str
    snapshot_id: str
    files: dict[str, str] = Field(default_factory=dict)  # path -> blob_sha256
    symbols: list[str] = Field(default_factory=list)  # symbol_id
    evidence_ids: list[str] = Field(default_factory=list)
    prompt_version: str = ""
    backend: str = ""
    model: str = ""


class VerifyIssue(_WikiModel):
    """一条校验问题。suggestion 给修复循环用（9.8.3）。"""

    rule: str
    message: str
    severity: str = "error"  # error / warning
    claim_id: str = ""
    evidence_id: str = ""
    suggestion: str = ""


class VerifyReport(_WikiModel):
    """页面校验报告（9.8）。error 存在即 failed。

    page_sha256/result_sha256 绑定"通过校验的确切内容"：发布门禁复算草稿哈希，
    不一致说明页面在通过校验后被改动，拒绝发布（9.12.3）。
    """

    page_id: str
    status: str  # passed / failed
    issues: list[VerifyIssue] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    verifier_version: str = ""
    page_sha256: str = ""
    result_sha256: str = ""

    @field_validator("status")
    @classmethod
    def _status_legal(cls, value: str) -> str:
        if value not in ("passed", "failed"):
            raise ValueError(f"status 只能是 passed/failed，得到 {value!r}")
        return value


class FileSummary(_WikiModel):
    """任务包里的模块文件清单：只给路径与规模，不塞整份源码。"""

    path: str
    module: str = ""
    language: str = ""
    line_count: int = Field(default=0, ge=0)
    symbol_count: int = Field(default=0, ge=0)


class ModuleSummary(_WikiModel):
    """系统页/架构页用的模块画像。"""

    name: str
    files: int = Field(default=0, ge=0)
    symbols: int = Field(default=0, ge=0)
    incoming: int = Field(default=0, ge=0)
    outgoing: int = Field(default=0, ge=0)


class TermEntry(_WikiModel):
    """术语表条目：领域词 + 代码位置，供模型统一用词。"""

    term: str
    path: str = ""
    line: int = Field(default=0, ge=0)
    kind: str = ""


class TaskFacts(_WikiModel):
    """任务包 context/facts.json：与该页面相关的事实子集（生成侧筛选）。"""

    module: str = ""
    files: list[FileSummary] = Field(default_factory=list)
    modules: list[ModuleSummary] = Field(default_factory=list)
    symbols: list[SymbolFact] = Field(default_factory=list)
    calls: list[CallFact] = Field(default_factory=list)
    configs: list[ConfigFact] = Field(default_factory=list)
    build_targets: list[BuildTarget] = Field(default_factory=list)
    tests: list[TestFact] = Field(default_factory=list)
    terms: list[TermEntry] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)  # overview/home 用


class TaskMeta(_WikiModel):
    """任务包 meta.json：生成侧权威元数据（9.5）。

    evidence 由任务包构建器从快照预提取并编号；模型输出只允许引用这些 E 编号。
    """

    task_id: str = Field(min_length=1)
    page_id: str = Field(min_length=1)
    page_type: PageType
    title_hint: str = ""
    subject: str = ""
    repo_id: str
    snapshot_id: str
    module: str = ""
    coverage_level: CoverageLevel = CoverageLevel.C2
    quality_profile: Literal["structural", "readable"] = "structural"
    prompt_version: str = Field(min_length=1)
    evidence: list[Evidence] = Field(default_factory=list)
    related_pages: list[str] = Field(default_factory=list)
    attempt: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _evidence_ids_unique(self) -> TaskMeta:
        ids = [e.id for e in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("meta 中 evidence id 重复")
        return self


class VersionPageEntry(_WikiModel):
    """发布版本中的一个页面条目。"""

    page_id: str
    type: PageType
    status: PageStatus
    title: str
    evidence_count: int = Field(default=0, ge=0)


class VersionIndex(_WikiModel):
    """发布版本清单（versions/<id>/index.json）。"""

    version_id: str
    repo_id: str
    snapshot_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    pages: list[VersionPageEntry] = Field(default_factory=list)
    prompt_versions: dict[str, str] = Field(default_factory=dict)  # page_id -> prompt_version

    @property
    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for page in self.pages:
            counts[page.status.value] = counts.get(page.status.value, 0) + 1
        return counts


__all__ = [
    "Claim",
    "CoverageLevel",
    "CoveragePlan",
    "Evidence",
    "FileSummary",
    "ModuleSummary",
    "Page",
    "PageDeps",
    "PageMeta",
    "PageResult",
    "PageStatus",
    "PageTask",
    "PageType",
    "TaskFacts",
    "TaskMeta",
    "TermEntry",
    "UnknownItem",
    "VerifyIssue",
    "VerifyReport",
    "VersionIndex",
    "VersionPageEntry",
]
