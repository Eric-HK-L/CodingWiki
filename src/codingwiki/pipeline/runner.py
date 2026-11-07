"""JobRunner：按顺序执行回流闭环的六个阶段（架构文档 v0.9 §18）。

规则：
- 顺序 `sync → facts → wiki → verify → publish → index`，全程持仓库锁；
- `from_stage` 断点进入（更早的阶段既不执行也不记录），`skip` 跳过（记 skipped）；
- 阶段失败 → 后续全部 skipped → job failed，退出码沿用异常的 ExitCode；
- JobRecord 落盘 `.codingwiki/jobs/<repo>/<stamp>-<job_id>.json`（审计）。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from codingwiki.config import load_repo_config
from codingwiki.domain.job import STAGE_ORDER, JobRecord, StageName, StageResult, StageStatus
from codingwiki.errors import CodingWikiError, ExitCode
from codingwiki.pipeline.lock import RepoLock
from codingwiki.pipeline.stages import STAGES, JobContext

StageFn = Callable[[JobContext], str]


def save_job_record(record: JobRecord, repo_id: str) -> Path:
    """审计记录落盘；同 repo 目录下按文件名时间戳排序即历史。"""
    base = Path(os.environ.get("CODINGWIKI_HOME", ".codingwiki")).resolve()
    job_dir = base / "jobs" / repo_id
    job_dir.mkdir(parents=True, exist_ok=True)
    stamp = record.started_at.astimezone(UTC).strftime("%Y%m%d-%H%M%S")
    path = job_dir / f"{stamp}-{record.job_id}.json"
    path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    return path


class JobRunner:
    """串行执行六个阶段；阶段实现见 pipeline.stages。"""

    def run(
        self,
        repo_id: str,
        *,
        config_dir: str = "configs/repos",
        revision: str = "",
        backend: str = "mock",
        app_config: str = "configs/default.yaml",
        from_stage: str = "",
        skip: tuple[str, ...] = (),
    ) -> tuple[JobRecord, int]:
        job_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        repo = load_repo_config(os.path.join(config_dir, f"{repo_id}.yaml"))
        ctx = JobContext(
            repo=repo,
            config_dir=config_dir,
            revision=revision,
            backend=backend,
            app_config=app_config,
        )

        record = JobRecord(job_id=job_id, repo_id=repo_id, status=StageStatus.OK)
        start_idx = 0
        if from_stage:
            start_idx = STAGE_ORDER.index(StageName(from_stage))
        exit_code = int(ExitCode.OK)
        snapshot_id = ""

        with RepoLock(repo_id):
            failed = False
            error = ""
            for position, name in enumerate(STAGE_ORDER):
                if position < start_idx:
                    continue
                if failed or name.value in skip:
                    record.stages.append(StageResult(stage=name, status=StageStatus.SKIPPED))
                    continue
                stage_fn: StageFn = STAGES[name.value]  # type: ignore[assignment]
                began = time.perf_counter()
                try:
                    summary = stage_fn(ctx)
                except Exception as exc:  # noqa: BLE001 —— 阶段边界必须兜住，转成审计记录
                    duration = time.perf_counter() - began
                    message = str(exc) or type(exc).__name__
                    record.stages.append(
                        StageResult(
                            stage=name,
                            status=StageStatus.FAILED,
                            duration_s=round(duration, 3),
                            summary=message,
                        )
                    )
                    failed = True
                    error = f"{name.value}: {message}"
                    if isinstance(exc, CodingWikiError):
                        exit_code = int(exc.exit_code)
                    else:
                        exit_code = int(ExitCode.INTERNAL)
                    continue
                duration = time.perf_counter() - began
                record.stages.append(
                    StageResult(
                        stage=name,
                        status=StageStatus.OK,
                        duration_s=round(duration, 3),
                        summary=summary,
                    )
                )
                if name is StageName.SYNC and ctx.snapshot is not None:
                    snapshot_id = ctx.snapshot.snapshot_id

        record = record.model_copy(
            update={
                "finished_at": datetime.now(UTC),
                "status": StageStatus.FAILED if failed else StageStatus.OK,
                "error": error,
                "snapshot_id": snapshot_id,
            }
        )
        save_job_record(record, repo_id)
        return record, exit_code


__all__ = ["JobRunner", "save_job_record"]
