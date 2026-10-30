"""S4 wiki 领域模型单元测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from codingwiki.domain.wiki import (
    Claim,
    CoveragePlan,
    Evidence,
    PageMeta,
    PageResult,
    PageStatus,
    PageTask,
    PageType,
    TaskMeta,
    VerifyReport,
)


def test_page_meta_defaults() -> None:
    meta = PageMeta(id="module/a", type=PageType.MODULE, title="t", snapshot="r@1")
    assert meta.status is PageStatus.DRAFT
    assert meta.format_version == 1
    assert meta.evidence_count == 0


def test_page_meta_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        PageMeta.model_validate(
            {"id": "x", "type": "module", "title": "t", "snapshot": "r@1", "bogus": 1}
        )


def test_claim_id_pattern() -> None:
    with pytest.raises(ValidationError):
        Claim(id="X1", text="结论")
    with pytest.raises(ValidationError):
        Evidence(id="c1", file="a.c", quote="int main(){}")


def test_page_result_duplicate_ids_rejected() -> None:
    ev = Evidence(id="E1", file="a.c", quote="x = 1;")
    with pytest.raises(ValidationError, match="claim id 重复"):
        PageResult(
            page_id="p",
            snapshot="r@1",
            claims=[Claim(id="C1", text="a"), Claim(id="C1", text="b")],
            evidence=[ev],
        )
    with pytest.raises(ValidationError, match="evidence id 重复"):
        PageResult(page_id="p", snapshot="r@1", evidence=[ev, ev])


def test_task_meta_evidence_ids_unique() -> None:
    ev = Evidence(id="E1", file="a.c", quote="x")
    with pytest.raises(ValidationError, match="evidence id 重复"):
        TaskMeta(
            task_id="t",
            page_id="p",
            page_type=PageType.MODULE,
            repo_id="r",
            snapshot_id="r@1",
            prompt_version="module_page@1",
            evidence=[ev, ev],
        )


def test_verify_report_status_legal() -> None:
    with pytest.raises(ValidationError, match="passed/failed"):
        VerifyReport(page_id="p", status="ok")


def test_coverage_plan_and_task() -> None:
    task = PageTask(
        task_id="module__a",
        page_id="module/a",
        page_type=PageType.MODULE,
        subject="a",
    )
    plan = CoveragePlan(repo_id="r", snapshot_id="r@1", tasks=[task], skipped=["module/b"])
    assert plan.tasks[0].reuse is False
    assert plan.skipped == ["module/b"]
