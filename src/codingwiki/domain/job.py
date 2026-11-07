"""S10 任务记录领域模型（架构文档 v0.9 §18）。

JobRecord 是一次 `codingwiki run` 的完整审计记录，落盘
`.codingwiki/jobs/<repo>/<ts>-<job_id>.json`。纯数据模型，无 I/O。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field

from codingwiki.domain.wiki import _WikiModel


class StageName(StrEnum):
    """回流闭环的六个阶段，顺序固定（架构 §18.1）。"""

    SYNC = "sync"
    FACTS = "facts"
    WIKI = "wiki"
    VERIFY = "verify"
    PUBLISH = "publish"
    INDEX = "index"


STAGE_ORDER: tuple[StageName, ...] = (
    StageName.SYNC,
    StageName.FACTS,
    StageName.WIKI,
    StageName.VERIFY,
    StageName.PUBLISH,
    StageName.INDEX,
)


class StageStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


class StageResult(_WikiModel):
    """单个阶段的执行结果。"""

    stage: StageName
    status: StageStatus
    duration_s: float = Field(default=0.0, ge=0.0)
    summary: str = ""  # ok=阶段产出摘要；failed=错误信息；skipped 为空


class JobRecord(_WikiModel):
    """一次 run 的审计记录（计划 504：可审计——快照、阶段、耗时、结果）。"""

    job_id: str = Field(min_length=1)  # UTC 时间戳，如 20261107T093000Z
    repo_id: str = Field(min_length=1)
    status: StageStatus  # 全部阶段 ok → ok；任一 failed → failed
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    snapshot_id: str = ""  # 闭环产出的快照（未到 sync 失败时为空）
    stages: list[StageResult] = Field(default_factory=list)
    error: str = ""  # 第一个失败阶段的错误摘要；成功为空

    def stage(self, name: StageName) -> StageResult | None:
        for result in self.stages:
            if result.stage is name:
                return result
        return None


__all__ = ["STAGE_ORDER", "JobRecord", "StageName", "StageResult", "StageStatus"]
