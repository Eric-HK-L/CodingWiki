"""S10 JobRunner 测试：顺序/断点/跳过/失败语义/审计记录落盘。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from codingwiki.domain.job import StageName, StageStatus
from codingwiki.errors import InputError, RetryableError
from codingwiki.pipeline import runner as runner_mod
from codingwiki.pipeline.runner import JobRunner, save_job_record
from codingwiki.pipeline.stages import JobContext

REPO_YAML = """
repo_id: demorepo
provider: git
url: https://example.com/demo.git
branch: main
"""


def _write_repo_config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    (config_dir / "demorepo.yaml").write_text(REPO_YAML, encoding="utf-8")
    return config_dir


@pytest.fixture
def fake_stages(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """把六个阶段替换为记录调用顺序的假实现，全部成功。"""
    calls: list[str] = []

    def _make(name: str):
        def _stage(ctx: JobContext) -> str:
            calls.append(name)
            return f"{name} 完成"

        return _stage

    fakes = {name.value: _make(name.value) for name in StageName}
    monkeypatch.setitem(runner_mod.STAGES, "sync", fakes["sync"])
    monkeypatch.setitem(runner_mod.STAGES, "facts", fakes["facts"])
    monkeypatch.setitem(runner_mod.STAGES, "wiki", fakes["wiki"])
    monkeypatch.setitem(runner_mod.STAGES, "verify", fakes["verify"])
    monkeypatch.setitem(runner_mod.STAGES, "publish", fakes["publish"])
    monkeypatch.setitem(runner_mod.STAGES, "index", fakes["index"])
    yield calls


def test_run_all_stages_ok_and_record_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_stages: list[str]
) -> None:
    monkeypatch.setenv("CODINGWIKI_HOME", str(tmp_path / ".cw"))
    config_dir = _write_repo_config(tmp_path)

    record, exit_code = JobRunner().run("demorepo", config_dir=str(config_dir))

    assert exit_code == 0
    assert record.status is StageStatus.OK
    assert record.error == ""
    assert [s.stage for s in record.stages] == list(StageName)
    assert all(s.status is StageStatus.OK for s in record.stages)
    assert fake_stages == ["sync", "facts", "wiki", "verify", "publish", "index"]
    assert record.finished_at is not None
    # 审计记录落盘可读回
    job_dir = tmp_path / ".cw" / "jobs" / "demorepo"
    files = sorted(job_dir.glob("*.json"))
    assert len(files) == 1
    saved = json.loads(files[0].read_text(encoding="utf-8"))
    assert saved["repo_id"] == "demorepo"
    assert saved["status"] == "ok"
    assert len(saved["stages"]) == 6


def test_run_failure_stops_and_marks_rest_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODINGWIKI_HOME", str(tmp_path / ".cw"))
    config_dir = _write_repo_config(tmp_path)
    calls: list[str] = []

    def _ok(name: str):
        def _stage(ctx: JobContext) -> str:
            calls.append(name)
            return f"{name} 完成"

        return _stage

    def _fail(ctx: JobContext) -> str:
        calls.append("wiki")
        raise RetryableError("页面生成失败 2 页: a, b")

    monkeypatch.setitem(runner_mod.STAGES, "sync", _ok("sync"))
    monkeypatch.setitem(runner_mod.STAGES, "facts", _ok("facts"))
    monkeypatch.setitem(runner_mod.STAGES, "wiki", _fail)
    monkeypatch.setitem(runner_mod.STAGES, "verify", _ok("verify"))
    monkeypatch.setitem(runner_mod.STAGES, "publish", _ok("publish"))
    monkeypatch.setitem(runner_mod.STAGES, "index", _ok("index"))

    record, exit_code = JobRunner().run("demorepo", config_dir=str(config_dir))

    assert exit_code == 4  # RetryableError.exit_code
    assert record.status is StageStatus.FAILED
    assert record.error.startswith("wiki: ")
    by_stage = {s.stage: s for s in record.stages}
    assert by_stage[StageName.SYNC].status is StageStatus.OK
    assert by_stage[StageName.FACTS].status is StageStatus.OK
    assert by_stage[StageName.WIKI].status is StageStatus.FAILED
    assert "页面生成失败" in by_stage[StageName.WIKI].summary
    for later in (StageName.VERIFY, StageName.PUBLISH, StageName.INDEX):
        assert by_stage[later].status is StageStatus.SKIPPED
    assert calls == ["sync", "facts", "wiki"]  # 失败后不再调用后续阶段


def test_run_from_stage_skips_earlier_without_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_stages: list[str]
) -> None:
    monkeypatch.setenv("CODINGWIKI_HOME", str(tmp_path / ".cw"))
    config_dir = _write_repo_config(tmp_path)

    record, exit_code = JobRunner().run("demorepo", config_dir=str(config_dir), from_stage="verify")

    assert exit_code == 0
    assert [s.stage for s in record.stages] == [
        StageName.VERIFY,
        StageName.PUBLISH,
        StageName.INDEX,
    ]
    assert fake_stages == ["verify", "publish", "index"]


def test_run_skip_records_skipped_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_stages: list[str]
) -> None:
    monkeypatch.setenv("CODINGWIKI_HOME", str(tmp_path / ".cw"))
    config_dir = _write_repo_config(tmp_path)

    record, exit_code = JobRunner().run(
        "demorepo", config_dir=str(config_dir), skip=("wiki", "index")
    )

    assert exit_code == 0
    by_stage = {s.stage: s for s in record.stages}
    assert by_stage[StageName.WIKI].status is StageStatus.SKIPPED
    assert by_stage[StageName.INDEX].status is StageStatus.SKIPPED
    assert by_stage[StageName.PUBLISH].status is StageStatus.OK
    assert fake_stages == ["sync", "facts", "verify", "publish"]


def test_run_missing_repo_config_raises_input_error(tmp_path: Path) -> None:
    with pytest.raises(InputError):
        JobRunner().run("nope", config_dir=str(tmp_path / "configs"))


def test_save_job_record_filename_is_timestamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codingwiki.domain.job import JobRecord, StageResult, StageStatus

    monkeypatch.setenv("CODINGWIKI_HOME", str(tmp_path / ".cw"))
    record = JobRecord(
        job_id="20261107T000000Z",
        repo_id="r1",
        status=StageStatus.OK,
        stages=[StageResult(stage=StageName.SYNC, status=StageStatus.OK, summary="s")],
    )
    path = save_job_record(record, "r1")
    assert path.name.endswith("-20261107T000000Z.json")
    assert path.is_file()
