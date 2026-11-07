"""覆盖率看板数据组装（架构文档 v0.10 §19，计划 4.6.2 / 9.13）。

只读：从当前发布版本、快照 manifest、最新评测报告与任务记录计算 DashboardData。
evals/jobs 缺失不报错（看板相应区块显示未评测/未运行），看板是只读产物，
绝不能因辅助数据缺失阻断发布。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from codingwiki.domain.dashboard import (
    CoverageRow,
    DashboardData,
    EvalSummary,
    JobSummary,
)
from codingwiki.domain.facts import FileFacts, RepoFacts
from codingwiki.domain.job import JobRecord
from codingwiki.domain.wiki import PageDeps, PageStatus, PageType, VersionIndex
from codingwiki.errors import InputError
from codingwiki.generate.modules import is_public_symbol
from codingwiki.publish.store import FilesystemPageStore


def build_dashboard(
    repo_id: str,
    facts: RepoFacts,
    index: VersionIndex,
    deps_map: dict[str, PageDeps],
) -> DashboardData:
    """从（正在发布或已发布的）版本组装看板数据——publish 与 coverage 共用。"""
    by_type = {entry.page_id: entry.type for entry in index.pages}

    c1_files: set[str] = set()
    interface_files: set[str] = set()
    flow_files: set[str] = set()
    for page_id, deps in deps_map.items():
        c1_files.update(deps.files)
        page_type = by_type.get(page_id)
        if page_type is PageType.INTERFACE:
            interface_files.update(deps.files)
        elif page_type is PageType.FLOW:
            flow_files.update(deps.files)

    counts = index.status_counts
    return DashboardData(
        repo_id=repo_id,
        version_id=index.version_id,
        snapshot_id=index.snapshot_id,
        pages_total=len(index.pages),
        pages_verified=counts.get(PageStatus.VERIFIED.value, 0),
        pages_stale=counts.get(PageStatus.STALE.value, 0),
        evidence_total=sum(page.evidence_count for page in index.pages),
        coverage=_coverage_rows(facts, c1_files, interface_files, flow_files),
        eval_summary=_latest_eval(repo_id),
        job_summary=_latest_job(repo_id),
    )


def build_current_dashboard(
    repo_id: str, facts: RepoFacts, store: FilesystemPageStore
) -> DashboardData:
    """`wiki coverage` 入口：对当前发布版本组装看板数据。"""
    version = store.current_version()
    if version is None:
        raise InputError(f"仓库 {repo_id} 还没有发布版本，请先执行 wiki publish")
    index = store.version_index(version)
    if index is None:
        raise InputError(f"当前版本 {version} 的 index.json 不存在")
    return build_dashboard(repo_id, facts, index, store.load_version_deps(version))


# ---------------------------------------------------------------- 覆盖矩阵


def _coverage_rows(
    facts: RepoFacts, c1_files: set[str], interface_files: set[str], flow_files: set[str]
) -> list[CoverageRow]:
    """按 FileFacts.module 分组的模块行 + total 行（架构 §19.2 口径）。"""
    by_module: dict[str, list[FileFacts]] = {}
    for file in facts.files:
        by_module.setdefault(file.module, []).append(file)

    rows = [
        _row_for(module, files, c1_files, interface_files, flow_files)
        for module, files in sorted(by_module.items())
    ]
    rows.append(
        CoverageRow(
            module="total",
            files_total=sum(row.files_total for row in rows),
            files_covered=sum(row.files_covered for row in rows),
            public_total=sum(row.public_total for row in rows),
            public_covered=sum(row.public_covered for row in rows),
            entries_total=sum(row.entries_total for row in rows),
            entries_covered=sum(row.entries_covered for row in rows),
        )
    )
    return rows


def _row_for(
    module: str,
    files: list[FileFacts],
    c1_files: set[str],
    interface_files: set[str],
    flow_files: set[str],
) -> CoverageRow:
    public_total = 0
    public_covered = 0
    for file in files:
        for symbol in file.symbols:
            if not is_public_symbol(symbol):
                continue
            public_total += 1
            if file.path in interface_files:
                public_covered += 1
    entries = _dedupe_entries(files)
    return CoverageRow(
        module=module,
        files_total=len(files),
        files_covered=sum(1 for file in files if file.path in c1_files),
        public_total=public_total,
        public_covered=public_covered,
        entries_total=len(entries),
        entries_covered=sum(1 for entry in entries if entry in flow_files),
    )


def _dedupe_entries(files: list[FileFacts]) -> list[str]:
    """main/python_main 入口按目录去重：真 main 优先，再按路径与行号（与流程页规划同规则）。"""
    by_dir: dict[str, tuple[bool, str, int, str]] = {}
    for file in files:
        for entry in file.entries:
            if entry.kind not in {"main", "python_main"}:
                continue
            parent = entry.path.rsplit("/", 1)[0] if "/" in entry.path else "."
            key = (entry.name not in {"main", "__main__"}, entry.path, entry.line, entry.name)
            current = by_dir.get(parent)
            if current is None or key < current:
                by_dir[parent] = key
    return sorted({path for _rank, path, _line, _name in by_dir.values()})


# ---------------------------------------------------------------- 辅助数据（缺省不报错）


def _latest_eval(repo_id: str) -> EvalSummary | None:
    """evals 目录按文件名时间戳取最新；缺失或损坏返回 None。"""
    base = Path(os.environ.get("CODINGWIKI_HOME", ".codingwiki")).resolve()
    report_dir = base / "evals" / repo_id
    if not report_dir.is_dir():
        return None
    for path in sorted(report_dir.glob("*.json"), key=lambda p: p.name, reverse=True):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict) or raw.get("set_id") is None:
            continue
        created = raw.get("created_at") or "1970-01-01T00:00:00+00:00"
        try:
            return EvalSummary(
                set_id=str(raw["set_id"]),
                created_at=datetime.fromisoformat(str(created)),
                recall_at_k=raw.get("recall_at_k"),
                mrr=raw.get("mrr"),
                ndcg_at_k=raw.get("ndcg_at_k"),
                refusal_accuracy=raw.get("refusal_accuracy"),
                citation_valid_rate=raw.get("citation_valid_rate"),
            )
        except ValueError:
            continue
    return None


def _latest_job(repo_id: str) -> JobSummary | None:
    """jobs 目录按文件名时间戳取最新；缺失或损坏返回 None。"""
    base = Path(os.environ.get("CODINGWIKI_HOME", ".codingwiki")).resolve()
    job_dir = base / "jobs" / repo_id
    if not job_dir.is_dir():
        return None
    for path in sorted(job_dir.glob("*.json"), key=lambda p: p.name, reverse=True):
        try:
            record = JobRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        return JobSummary(
            job_id=record.job_id,
            status=record.status.value,
            finished_at=record.finished_at,
            snapshot_id=record.snapshot_id,
            error=record.error,
        )
    return None


__all__ = ["build_current_dashboard", "build_dashboard"]
