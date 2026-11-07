"""S11 覆盖率看板领域模型（架构文档 v0.10 §19）。

DashboardData 由 `publish/dashboard.py` 组装，进入版本站点 dashboard.html
与 CLI `wiki coverage`。纯数据模型，无 I/O。
覆盖口径详注见 CoverageRow 与架构 §19.2。
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import Field

from codingwiki.domain.wiki import _WikiModel


class CoverageRow(_WikiModel):
    """覆盖矩阵的一行（一个模块或 total）：C0–C3 的基数与覆盖数。

    口径（架构 §19.2）：
    - C0 已登记：基数 = manifest 发现数（in_scope+excluded），covered = in_scope；
    - C1 已说明：基数 = facts 在范围文件，covered = 被任一已发布页面 deps 引用；
    - C2 已说清接口：基数 = 公开符号数，covered = 符号文件是某 interface 页 deps 文件；
    - C3 已说清流程：基数 = main/python_main 入口（按目录去重），
      covered = 入口文件是某 flow 页 deps 文件。
    """

    module: str
    files_total: int = Field(default=0, ge=0)  # C0 基数
    files_covered: int = Field(default=0, ge=0)  # C1
    public_total: int = Field(default=0, ge=0)  # C2 基数
    public_covered: int = Field(default=0, ge=0)
    entries_total: int = Field(default=0, ge=0)  # C3 基数
    entries_covered: int = Field(default=0, ge=0)


class EvalSummary(_WikiModel):
    """最新一次金标准评测的关键指标（读 .codingwiki/evals 最新报告）。"""

    set_id: str
    created_at: datetime
    recall_at_k: float | None = None
    mrr: float | None = None
    ndcg_at_k: float | None = None
    refusal_accuracy: float | None = None
    citation_valid_rate: float | None = None


class JobSummary(_WikiModel):
    """最近一次 run 的摘要（读 .codingwiki/jobs 最新记录）。"""

    job_id: str
    status: str  # ok / failed
    finished_at: datetime | None = None
    snapshot_id: str = ""
    error: str = ""


class DashboardData(_WikiModel):
    """看板完整数据：站点 dashboard.html 与 `wiki coverage` 的共同来源。"""

    repo_id: str
    version_id: str
    snapshot_id: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    pages_total: int = Field(default=0, ge=0)
    pages_verified: int = Field(default=0, ge=0)
    pages_stale: int = Field(default=0, ge=0)
    evidence_total: int = Field(default=0, ge=0)
    coverage: list[CoverageRow] = Field(default_factory=list)
    eval_summary: EvalSummary | None = None
    job_summary: JobSummary | None = None


__all__ = ["CoverageRow", "DashboardData", "EvalSummary", "JobSummary"]
