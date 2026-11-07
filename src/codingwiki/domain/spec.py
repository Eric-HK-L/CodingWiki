"""阶段二 Spec 领域模型（架构文档 阶段2 §3）。

只放 Pydantic 字段定义与不变量：不读写文件、不打日志、不做 IO。
确定性计算（编号、覆盖比对、追溯）不在这里，由 spec/ 能力层负责。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from codingwiki.domain.wiki import _WikiModel

# 计划 13.3 的 11 章模板：编号与标题固定，缺章或改标题视为结构缺失
SPEC_SECTIONS: dict[int, str] = {
    1: "背景与目标",
    2: "术语",
    3: "现状",
    4: "需求",
    5: "方案设计",
    6: "影响分析",
    7: "测试计划",
    8: "编码任务拆分",
    9: "风险与回滚",
    10: "开放问题与假设",
    11: "追溯矩阵",
}


class SpecStatus(StrEnum):
    """Spec 状态（架构 阶段2 §8 状态机）。"""

    NEEDS_INFO = "needs-info"
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


class RequirementKind(StrEnum):
    """需求类别：功能需求用 R 编号，非功能需求用 N 编号。"""

    FUNCTIONAL = "functional"
    NON_FUNCTIONAL = "non_functional"


class RequirementPriority(StrEnum):
    """优先级（计划 13.4.1）。"""

    MUST = "must"
    SHOULD = "should"
    MAY = "may"


class QuestionStatus(StrEnum):
    """开放问题状态；未 closed 时 Spec 只能是 needs-info。"""

    OPEN = "open"
    CLOSED = "closed"


class EvidenceKind(StrEnum):
    """证据类别（架构 阶段2 §5）。"""

    WIKI = "wiki"
    SYMBOL = "symbol"
    CALLER = "caller"
    CONFIG = "config"
    BUILD = "build"
    TEST = "test"
    SIMILAR = "similar"


class DocSection(_WikiModel):
    """需求文档中的一个章节。行号为 1 起、闭区间。"""

    path: str = Field(min_length=1)  # 标题路径，如 "2 功能需求 / 2.1 配置项"
    title: str = Field(min_length=1)
    level: int = Field(ge=1, le=6)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def _range_ordered(self) -> DocSection:
        if self.end_line < self.start_line:
            raise ValueError(f"章节 {self.path} 的 end_line 小于 start_line")
        return self


class AcceptanceCriterion(_WikiModel):
    """一条验收标准：可观察、可判定。"""

    id: str = Field(pattern=r"^AC-\d{3}$")
    text: str = Field(min_length=1)
    observable: bool = True


class Requirement(_WikiModel):
    """一条原子需求。

    source_quote 必须是需求文档中可定位的原文；缺失视为编造，校验阻断。
    """

    id: str = Field(pattern=r"^[RN]-\d{3}$")
    kind: RequirementKind
    text: str = Field(min_length=1)
    priority: RequirementPriority = RequirementPriority.SHOULD
    acceptance: list[AcceptanceCriterion] = Field(default_factory=list)
    source_section: str = Field(min_length=1)
    source_quote: str = Field(min_length=1)

    @model_validator(mode="after")
    def _prefix_matches_kind(self) -> Requirement:
        prefix = "R-" if self.kind is RequirementKind.FUNCTIONAL else "N-"
        if not self.id.startswith(prefix):
            raise ValueError(
                f"需求 {self.id} 与类别 {self.kind.value} 的前缀不一致（应为 {prefix}）"
            )
        ac_ids = [ac.id for ac in self.acceptance]
        if len(ac_ids) != len(set(ac_ids)):
            raise ValueError(f"需求 {self.id} 的验收标准编号重复")
        return self


class OpenQuestion(_WikiModel):
    """开放问题：不明确处不猜，指定确认人。"""

    id: str = Field(pattern=r"^Q-\d{3}$")
    question: str = Field(min_length=1)
    impact: str = ""
    owner: str = ""
    status: QuestionStatus = QuestionStatus.OPEN


class DesignTask(_WikiModel):
    """一个可编码任务：≤3 个文件、≤300 行、单一目标。"""

    id: str = Field(pattern=r"^T-\d{3}$")
    goal: str = Field(min_length=1)
    files: list[str] = Field(min_length=1, max_length=3)
    estimated_lines: int = Field(ge=1, le=300)
    acceptance: list[str] = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("files")
    @classmethod
    def _files_nonempty(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("DesignTask.files 不能包含空路径")
        return value


class TraceRow(_WikiModel):
    """追溯矩阵一行：需求 → 设计 → 代码位置 → 测试 → 任务。"""

    requirement_id: str = Field(pattern=r"^[RN]-\d{3}$")
    design_section: str = Field(min_length=1)
    code_location: str = Field(min_length=1)
    test: str = Field(min_length=1)
    task: str = Field(pattern=r"^T-\d{3}$")


class EvidenceItem(_WikiModel):
    """一条可回到固定快照的证据。"""

    kind: EvidenceKind
    path: str = ""
    line: int = Field(default=0, ge=0)
    symbol_id: str = ""
    page_id: str = ""
    snapshot_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _locator_present(self) -> EvidenceItem:
        if not (self.path or self.symbol_id or self.page_id):
            raise ValueError("证据必须至少给出 path / symbol_id / page_id 之一")
        if self.kind is EvidenceKind.WIKI and not self.page_id:
            raise ValueError("wiki 证据必须给出 page_id")
        if self.kind is EvidenceKind.SYMBOL and not self.symbol_id:
            raise ValueError("symbol 证据必须给出 symbol_id")
        return self


class SpecSection(_WikiModel):
    """Spec 正文的一章。无内容章节写「不适用」和原因，不能删除。"""

    number: int = Field(ge=1, le=11)
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)


class SourceDocument(_WikiModel):
    """需求文档的来源信息。"""

    path: str = Field(min_length=1)
    doc_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    doc_id: str = Field(min_length=1)


class SpecIssue(_WikiModel):
    """一条校验问题；ref 指向需求/任务/证据编号。"""

    rule: str = Field(min_length=1)
    message: str = Field(min_length=1)
    ref: str = ""


class ValidationReport(_WikiModel):
    """自动校验报告（S15 产出，这里先定契约）。"""

    spec_id: str = ""
    blockers: list[SpecIssue] = Field(default_factory=list)
    warnings: list[SpecIssue] = Field(default_factory=list)
    info: list[SpecIssue] = Field(default_factory=list)

    @property
    def has_blockers(self) -> bool:
        return bool(self.blockers)


class ImpactAnalysis(_WikiModel):
    """确定性影响面（架构 阶段2 §5.2）。全部为排序去重后的稳定列表。"""

    requirement_id: str = Field(pattern=r"^[RN]-\d{3}$")
    files: list[str] = Field(default_factory=list)
    interfaces: list[str] = Field(default_factory=list)
    callers: list[str] = Field(default_factory=list)
    config: list[str] = Field(default_factory=list)
    build: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)


class RequirementEvidence(_WikiModel):
    """一条需求的证据包：至少 1 条可定位证据，附确定性影响面。"""

    requirement_id: str = Field(pattern=r"^[RN]-\d{3}$")
    items: list[EvidenceItem] = Field(min_length=1)
    impact: ImpactAnalysis

    @model_validator(mode="after")
    def _impact_matches(self) -> RequirementEvidence:
        if self.impact.requirement_id != self.requirement_id:
            raise ValueError(
                f"影响面归属 {self.impact.requirement_id} 与证据 {self.requirement_id} 不一致"
            )
        return self


class EvidenceBundle(_WikiModel):
    """一份需求文档/一份 Spec 的证据包（evidence.json）。不含时间戳，可逐字节复算。"""

    repo_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    spec_id: str = ""
    requirements: list[RequirementEvidence] = Field(min_length=1)

    @model_validator(mode="after")
    def _requirement_ids_unique(self) -> EvidenceBundle:
        ids = [item.requirement_id for item in self.requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("证据包内需求编号重复")
        return self


class CandidateOption(_WikiModel):
    """方案设计中的一个候选。"""

    name: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    pros: str = ""
    cons: str = ""


class DecisionRecord(_WikiModel):
    """一条方案决策（ADR）。至少 2 个候选；唯一方案需写明依据。"""

    id: str = Field(pattern=r"^D-\d{3}$")
    title: str = Field(min_length=1)
    context: str = Field(min_length=1)
    candidates: list[CandidateOption] = Field(default_factory=list)
    choice: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    unique_justification: str = ""

    @model_validator(mode="after")
    def _candidates_valid(self) -> DecisionRecord:
        names = [candidate.name for candidate in self.candidates]
        if len(names) != len(set(names)):
            raise ValueError(f"决策 {self.id} 的候选名重复")
        if len(self.candidates) < 2 and not self.unique_justification:
            raise ValueError(
                f"决策 {self.id} 至少需要 2 个候选；唯一方案必须填写 unique_justification"
            )
        if names and self.choice not in names:
            raise ValueError(f"决策 {self.id} 的选择 {self.choice!r} 不在候选列表中")
        return self


class SpecDocument(_WikiModel):
    """一份完整 Spec。元信息由系统写入，不采信模型输出。"""

    spec_id: str = Field(pattern=r"^F-\d{3}$")
    title: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    source_doc: SourceDocument
    status: SpecStatus = SpecStatus.DRAFT
    sections: list[SpecSection]
    requirements: list[Requirement] = Field(default_factory=list)
    questions: list[OpenQuestion] = Field(default_factory=list)
    tasks: list[DesignTask] = Field(default_factory=list)
    trace: list[TraceRow] = Field(default_factory=list)
    decisions: list[DecisionRecord] = Field(default_factory=list)
    validation: ValidationReport | None = None

    @model_validator(mode="after")
    def _structure_complete(self) -> SpecDocument:
        if len(self.sections) != len(SPEC_SECTIONS):
            raise ValueError(f"Spec 必须包含 {len(SPEC_SECTIONS)} 章，实际 {len(self.sections)} 章")
        for expected, section in enumerate(self.sections, start=1):
            if section.number != expected:
                raise ValueError(f"第 {expected} 章编号错位：{section.number}")
            if section.title != SPEC_SECTIONS[expected]:
                raise ValueError(
                    f"第 {expected} 章标题应为 {SPEC_SECTIONS[expected]!r}，实际 {section.title!r}"
                )
        self._assert_ids_unique()
        if self.status is SpecStatus.APPROVED:
            open_questions = [q.id for q in self.questions if q.status is QuestionStatus.OPEN]
            if open_questions:
                raise ValueError(
                    f"仍有未关闭的开放问题，不能 approved: {', '.join(open_questions)}"
                )
        return self

    def _assert_ids_unique(self) -> None:
        ids = [r.id for r in self.requirements]
        ids += [q.id for q in self.questions]
        ids += [t.id for t in self.tasks]
        ids += [d.id for d in self.decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("Spec 内需求/问题/任务/决策编号存在重复")


class ExtractionResult(_WikiModel):
    """需求抽取产物（extract.json）。编号由系统按章节顺序确定性生成。"""

    repo_id: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    doc_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_path: str = Field(min_length=1)
    title: str = Field(min_length=1)
    sections: list[DocSection]
    requirements: list[Requirement] = Field(default_factory=list)
    questions: list[OpenQuestion] = Field(default_factory=list)
    backend: str = ""
    model: str = ""

    @model_validator(mode="after")
    def _ids_unique(self) -> ExtractionResult:
        ids = [r.id for r in self.requirements] + [q.id for q in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("抽取产物内需求/问题编号重复")
        return self


__all__ = [
    "AcceptanceCriterion",
    "CandidateOption",
    "DecisionRecord",
    "DesignTask",
    "DocSection",
    "EvidenceBundle",
    "EvidenceItem",
    "EvidenceKind",
    "ExtractionResult",
    "ImpactAnalysis",
    "OpenQuestion",
    "QuestionStatus",
    "Requirement",
    "RequirementEvidence",
    "RequirementKind",
    "RequirementPriority",
    "SPEC_SECTIONS",
    "SourceDocument",
    "SpecDocument",
    "SpecIssue",
    "SpecSection",
    "SpecStatus",
    "TraceRow",
    "ValidationReport",
]
