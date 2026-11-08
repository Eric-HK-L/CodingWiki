"""S19–S21 编码任务包、后端协议与独立验证测试。

用本地 git 仓库造 fixture，后端/验证器用临时 Python 脚本模拟（不联网、不接真实模型）。
覆盖：Spec → CodedTask 分解、任务包产物与指纹、后端进程契约、编排与 CLI、独立验证。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from codingwiki.cli import main
from codingwiki.coding import (
    assert_command_allowed,
    audit_count,
    build_escalation,
    build_packet,
    decompose,
    load_approved_spec,
    load_backend_config,
    mechanical_report,
    render_escalation,
    replay_run,
    run_backend,
    run_task,
)
from codingwiki.coding.commands import split_command, stage_specs
from codingwiki.coding.errors import parse_failures
from codingwiki.coding.junit import JUnitConfig, load_config, parse_reports
from codingwiki.coding.metrics import aggregate
from codingwiki.config import load_repo_config
from codingwiki.domain.coding import (
    BackendConfig,
    BackendStatus,
    CodedTask,
    CodingRun,
    CodingRunStatus,
    CodingStopReason,
    EscalationReason,
    EscalationReport,
    ReplayStatus,
    RunMetrics,
    RunReport,
    StageKind,
    StageReport,
    StageStatus,
    TaskPacket,
    VerifierVerdict,
)
from codingwiki.domain.models import RepoConfig
from codingwiki.domain.patch import (
    FindingSeverity,
    PatchFinding,
    PatchPolicy,
    PatchReport,
    PatchStats,
    PatchVerdict,
)
from codingwiki.domain.spec import (
    SPEC_SECTIONS,
    DesignTask,
    OpenQuestion,
    SourceDocument,
    SpecDocument,
    SpecSection,
    SpecStatus,
    TraceRow,
    ValidationReport,
)
from codingwiki.errors import InputError
from codingwiki.exec import CommandResult, CommandSpec, CommandStatus
from codingwiki.patch import WorkspaceManager
from codingwiki.providers import GitProvider

NVLOG_C = "int nvlog_main(void)\n{\n    return 0;\n}\n"
TEST_C = "void test_nvlog(void)\n{\n    assert(1);\n}\n"
CMAKE = "add_library(cuphy STATIC)\n"
THIRD_PARTY = "int lib(void) { return 0; }\n"

SUCCESS_BACKEND = """\
import json
import sys
from pathlib import Path

workspace = Path(sys.argv[2])
result_path = Path(sys.argv[3])
target = workspace / "nvlog/src/nvlog.c"
target.write_text(target.read_text(encoding="utf-8") + "int level = 1;" + chr(10), encoding="utf-8")
result_path.write_text(json.dumps({"status": "ok", "summary": "done"}), encoding="utf-8")
print("backend ok")
"""

FORBIDDEN_BACKEND = """\
import sys
from pathlib import Path

workspace = Path(sys.argv[2])
target = workspace / "src/cuphy/CMakeLists.txt"
text = target.read_text(encoding="utf-8") + "add_dependencies(a b)" + chr(10)
target.write_text(text, encoding="utf-8")
print("edited build script")
"""

FAILING_BACKEND = """\
import sys

print("boom", file=sys.stderr)
sys.exit(2)
"""

TIMEOUT_BACKEND = """\
import time

time.sleep(2)
"""

INVALID_RESULT_BACKEND = """\
import sys
from pathlib import Path

result_path = Path(sys.argv[3])
result_path.write_text("not json", encoding="utf-8")
"""


@dataclass(frozen=True)
class CodingEnv:
    origin: Path
    rev: str
    home: Path
    config_dir: Path


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, timeout=30
    )
    return result.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[CodingEnv, None, None]:
    home = tmp_path / ".codingwiki"
    monkeypatch.setenv("CODINGWIKI_HOME", str(home))
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init")
    _git(origin, "config", "user.email", "test@test")
    _git(origin, "config", "user.name", "Test")
    _git(origin, "checkout", "-b", "main")
    _write(origin / "nvlog/src/nvlog.c", NVLOG_C)
    _write(origin / "nvlog/tests/test_nvlog.c", TEST_C)
    _write(origin / "src/cuphy/CMakeLists.txt", CMAKE)
    _write(origin / "third_party/lib/x.c", THIRD_PARTY)
    _write(origin / ".gitignore", "reports/\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "base")
    rev = _git(origin, "rev-parse", "HEAD")

    config_dir = tmp_path / "configs" / "repos"
    config_dir.mkdir(parents=True)
    (config_dir / "demo.yaml").write_text(
        f"repo_id: demo\nprovider: git\nurl: {origin}\nbranch: main\n", encoding="utf-8"
    )
    yield CodingEnv(origin=origin, rev=rev, home=home, config_dir=config_dir)


def _repo(env: CodingEnv) -> RepoConfig:
    return load_repo_config(env.config_dir / "demo.yaml")


def _manager(env: CodingEnv) -> WorkspaceManager:
    return WorkspaceManager(GitProvider(), home=env.home)


# ---------------------------------------------------------------- Spec 工厂


def _design_tasks() -> list[DesignTask]:
    return [
        DesignTask(
            id="T-001",
            goal="为 nvlog 增加日志级别",
            files=["nvlog/src/nvlog.c"],
            estimated_lines=20,
            acceptance=["编译通过", "新增级别生效"],
        ),
        DesignTask(
            id="T-002",
            goal="补充 nvlog 测试",
            files=["nvlog/tests/test_nvlog.c"],
            estimated_lines=40,
            acceptance=["测试覆盖新级别"],
            depends_on=["T-001"],
        ),
    ]


def _trace_rows() -> list[TraceRow]:
    return [
        TraceRow(
            requirement_id="R-001",
            design_section="5 方案设计",
            code_location="nvlog/src/nvlog.c",
            test="nvlog/tests/test_nvlog.c",
            task="T-001",
        ),
        TraceRow(
            requirement_id="R-002",
            design_section="7 测试计划",
            code_location="nvlog/tests/test_nvlog.c",
            test="nvlog/tests/test_nvlog.c",
            task="T-002",
        ),
    ]


def _spec_doc(
    *,
    snapshot_id: str,
    status: SpecStatus = SpecStatus.APPROVED,
    trace: list[TraceRow] | None = None,
    tasks: list[DesignTask] | None = None,
) -> SpecDocument:
    sections = [
        SpecSection(
            number=number,
            title=title,
            body=f"第 {number} 章内容。" if number in {4, 5, 6, 7, 8} else "不适用",
        )
        for number, title in SPEC_SECTIONS.items()
    ]
    return SpecDocument(
        spec_id="F-101",
        title="编码任务包试点",
        snapshot_id=snapshot_id,
        source_doc=SourceDocument(path="req.md", doc_sha256="0" * 64, doc_id="demo@doc"),
        status=status,
        sections=sections,
        tasks=_design_tasks() if tasks is None else tasks,
        trace=_trace_rows() if trace is None else trace,
        validation=ValidationReport(),
    )


def _write_spec_dir(
    directory: Path,
    *,
    snapshot_id: str,
    status: SpecStatus = SpecStatus.APPROVED,
) -> Path:
    spec_dir = directory / "F-101"
    spec_dir.mkdir(parents=True)
    spec = _spec_doc(snapshot_id=snapshot_id, status=status)
    (spec_dir / "spec.json").write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    return spec_dir


def _setup(
    env: CodingEnv, *, task: str = "T-001"
) -> tuple[RepoConfig, WorkspaceManager, Path, SpecDocument, CodedTask]:
    repo = _repo(env)
    manager = _manager(env)
    record = manager.open(repo, task, env.rev)
    worktree = Path(record.path)
    spec = _spec_doc(snapshot_id=f"demo@{env.rev}")
    coded = next(item for item in decompose(spec) if item.id == task)
    return repo, manager, worktree, spec, coded


def _packet_for(
    manager: WorkspaceManager,
    worktree: Path,
    spec: SpecDocument,
    task: CodedTask,
    directory: Path,
) -> TaskPacket:
    packet_dir = directory / "packet"
    return build_packet(
        repo_id="demo",
        spec=spec,
        task=task,
        workspace=worktree,
        base_revision=_rev_of(manager),
        packet_dir=packet_dir,
        policy=PatchPolicy(allowed_paths=list(task.files_allowed)),
    )


def _rev_of(manager: WorkspaceManager) -> str:
    return manager.load("demo", "T-001").base_revision


def _script(directory: Path, body: str, name: str = "backend.py") -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def _config(script: Path, *, timeout: int = 30) -> BackendConfig:
    return BackendConfig(
        command=[sys.executable, str(script), "{packet_dir}", "{workspace}", "{result_path}"],
        timeout_seconds=timeout,
    )


# ---------------------------------------------------------------- 任务分解


def test_decompose_builds_coded_tasks() -> None:
    spec = _spec_doc(snapshot_id="demo@rev")
    tasks = decompose(spec)

    first, second = tasks
    assert first.id == "T-001"
    assert first.spec_id == "F-101"
    assert first.title == "为 nvlog 增加日志级别"
    assert first.requirement_ids == ["R-001"]
    assert first.files_allowed == ["nvlog/src/nvlog.c"]
    assert first.acceptance == ["编译通过", "新增级别生效"]
    assert first.estimated_lines == 20
    assert first.constraints[0] == "目标需求：R-001"
    assert first.constraints[1] == "只允许修改：nvlog/src/nvlog.c"
    assert "Spec 追溯：设计 5 方案设计；测试 nvlog/tests/test_nvlog.c" in first.constraints
    assert second.depends_on == ["T-001"]
    assert second.requirement_ids == ["R-002"]


def test_decompose_without_trace_marks_manual_confirmation() -> None:
    spec = _spec_doc(snapshot_id="demo@rev", trace=[])
    task = decompose(spec)[0]

    assert task.requirement_ids == []
    assert task.constraints[0] == "Spec 未提供追溯行，需求归属需人工确认"


def test_decompose_truncates_long_title() -> None:
    long_goal = "改" * 100
    tasks = [
        DesignTask(
            id="T-001",
            goal=long_goal,
            files=["nvlog/src/nvlog.c"],
            estimated_lines=20,
            acceptance=["编译通过"],
        )
    ]
    spec = _spec_doc(snapshot_id="demo@rev", tasks=tasks)
    task = decompose(spec)[0]

    assert len(task.title) == 80
    assert task.title.endswith("...")


# ---------------------------------------------------------------- 任务包


def test_build_packet_writes_artifacts_and_fingerprints(env: CodingEnv, tmp_path: Path) -> None:
    repo, manager, worktree, spec, task = _setup(env)
    packet = _packet_for(manager, worktree, spec, task, tmp_path)

    packet_dir = Path(packet.packet_dir)
    for name in ("task.json", "prompt.md", "schema.json", "spec-excerpt.md"):
        assert (packet_dir / name).is_file(), name

    source = worktree / "nvlog/src/nvlog.c"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    paths = {item.path for item in packet.context_files}
    assert paths == {"nvlog/src/nvlog.c", "spec-excerpt.md"}
    recorded = next(item for item in packet.context_files if item.path == "nvlog/src/nvlog.c")
    assert recorded.sha256 == digest
    assert recorded.size == len(source.read_bytes())

    excerpt = (packet_dir / "spec-excerpt.md").read_text(encoding="utf-8")
    assert "# Spec F-101：编码任务包试点" in excerpt
    assert "## 4 需求" in excerpt
    assert "## 5 方案设计" in excerpt
    assert "## 1 背景与目标" not in excerpt

    prompt = (packet_dir / "prompt.md").read_text(encoding="utf-8")
    assert "# 编码任务 T-001" in prompt
    assert "nvlog/src/nvlog.c" in prompt
    assert "不要执行构建、测试或网络命令" in prompt

    reloaded = TaskPacket.model_validate_json((packet_dir / "task.json").read_text("utf-8"))
    assert reloaded.packet_id == packet.packet_id
    assert reloaded.policy.allowed_paths == ["nvlog/src/nvlog.c"]
    assert reloaded.repo_id == repo.repo_id


def test_build_packet_rejects_missing_allowed_file(env: CodingEnv, tmp_path: Path) -> None:
    _repo_obj, manager, worktree, spec, task = _setup(env)
    task = task.model_copy(update={"files_allowed": ["nvlog/src/missing.c"]})

    with pytest.raises(InputError):
        _packet_for(manager, worktree, spec, task, tmp_path)


def test_build_packet_resolves_files_under_repo_root(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _write(workspace / "cuPHY/nvlog/src/nvlog.c", NVLOG_C)
    spec = _spec_doc(snapshot_id="demo@rev")
    task = decompose(spec)[0]
    packet_dir = tmp_path / "packet"

    packet = build_packet(
        repo_id="demo",
        spec=spec,
        task=task,
        workspace=workspace,
        base_revision="rev",
        packet_dir=packet_dir,
        policy=PatchPolicy(allowed_paths=list(task.files_allowed)),
        root="cuPHY",
    )

    assert {item.path for item in packet.context_files} == {
        "nvlog/src/nvlog.c",
        "spec-excerpt.md",
    }
    prompt = (packet_dir / "prompt.md").read_text(encoding="utf-8")
    assert "代码根：" in prompt
    assert str((workspace / "cuPHY").resolve()) in prompt


# ---------------------------------------------------------------- 后端配置与进程


def test_load_backend_config(tmp_path: Path) -> None:
    script = _script(tmp_path, SUCCESS_BACKEND)
    config_path = tmp_path / "backend.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "kind": "command",
                "command": [sys.executable, str(script), "{packet_dir}", "{workspace}"],
                "timeout_seconds": 60,
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    config = load_backend_config(config_path)
    assert config.timeout_seconds == 60
    assert config.command[-1] == "{workspace}"

    with pytest.raises(InputError):
        load_backend_config(tmp_path / "missing.yaml")

    bad_mapping = tmp_path / "bad.yaml"
    bad_mapping.write_text("- not-a-mapping\n", encoding="utf-8")
    with pytest.raises(InputError):
        load_backend_config(bad_mapping)

    bad_command = tmp_path / "empty.yaml"
    bad_command.write_text("kind: command\ncommand: []\n", encoding="utf-8")
    with pytest.raises(InputError):
        load_backend_config(bad_command)


def test_run_backend_success(env: CodingEnv, tmp_path: Path) -> None:
    _repo_obj, manager, worktree, spec, task = _setup(env)
    packet = _packet_for(manager, worktree, spec, task, tmp_path)
    script = _script(tmp_path, SUCCESS_BACKEND)
    result_path = tmp_path / "run" / "result.json"
    log_path = tmp_path / "run" / "backend.log"

    result = run_backend(_config(script), packet, worktree, result_path, log_path)

    assert result.status is BackendStatus.OK
    assert result.exit_code == 0
    assert result.result_json["status"] == "ok"
    assert "backend ok" in result.stdout_tail
    assert log_path.is_file()
    assert "int level = 1;" in (worktree / "nvlog/src/nvlog.c").read_text(encoding="utf-8")


def test_run_backend_nonzero_exit(env: CodingEnv, tmp_path: Path) -> None:
    _repo_obj, manager, worktree, spec, task = _setup(env)
    packet = _packet_for(manager, worktree, spec, task, tmp_path)
    script = _script(tmp_path, FAILING_BACKEND, "failing.py")
    result_path = tmp_path / "run" / "result.json"

    result = run_backend(_config(script), packet, worktree, result_path, tmp_path / "run" / "log")

    assert result.status is BackendStatus.FAILED
    assert result.exit_code == 2
    assert result.result_json == {}
    assert "boom" in result.stderr_tail


def test_run_backend_invalid_result_json(env: CodingEnv, tmp_path: Path) -> None:
    _repo_obj, manager, worktree, spec, task = _setup(env)
    packet = _packet_for(manager, worktree, spec, task, tmp_path)
    script = _script(tmp_path, INVALID_RESULT_BACKEND, "invalid.py")
    result_path = tmp_path / "run" / "result.json"

    result = run_backend(_config(script), packet, worktree, result_path, tmp_path / "run" / "log")

    assert result.status is BackendStatus.ERROR
    assert "result.json" in result.error


def test_run_backend_missing_command(env: CodingEnv, tmp_path: Path) -> None:
    _repo_obj, manager, worktree, spec, task = _setup(env)
    packet = _packet_for(manager, worktree, spec, task, tmp_path)
    config = BackendConfig(command=["definitely-not-a-command"], timeout_seconds=30)

    with pytest.raises(InputError):
        run_backend(config, packet, worktree, tmp_path / "result.json", tmp_path / "log")


def test_run_backend_bad_placeholder(env: CodingEnv, tmp_path: Path) -> None:
    _repo_obj, manager, worktree, spec, task = _setup(env)
    packet = _packet_for(manager, worktree, spec, task, tmp_path)
    config = BackendConfig(command=[sys.executable, "{unknown}"], timeout_seconds=30)

    with pytest.raises(InputError):
        run_backend(config, packet, worktree, tmp_path / "result.json", tmp_path / "log")


def test_run_backend_timeout(env: CodingEnv, tmp_path: Path) -> None:
    _repo_obj, manager, worktree, spec, task = _setup(env)
    packet = _packet_for(manager, worktree, spec, task, tmp_path)
    script = _script(tmp_path, TIMEOUT_BACKEND, "slow.py")

    result = run_backend(
        _config(script, timeout=1),
        packet,
        worktree,
        tmp_path / "run" / "result.json",
        tmp_path / "run" / "log",
    )

    assert result.status is BackendStatus.TIMEOUT
    assert result.exit_code == -1
    assert "超时" in result.error


# ---------------------------------------------------------------- 首轮编排


def _run(
    env: CodingEnv,
    tmp_path: Path,
    body: str,
    *,
    name: str = "backend.py",
) -> tuple[RepoConfig, WorkspaceManager, CodingRun]:
    repo, manager, worktree, spec, task = _setup(env)
    script = _script(tmp_path, body, name)
    return repo, manager, run_task(GitProvider(), manager, repo, spec, task, _config(script))


def _run_dir(env: CodingEnv, run_id: str, task: str = "T-001") -> Path:
    return env.home / "coding" / "demo" / "F-101" / task / "runs" / run_id


def test_run_task_success(env: CodingEnv, tmp_path: Path) -> None:
    _repo_obj, manager, run = _run(env, tmp_path, SUCCESS_BACKEND)

    assert run.status is CodingRunStatus.SUCCEEDED
    assert run.stop_reason is CodingStopReason.SUCCEEDED
    assert run.backend is not None and run.backend.status is BackendStatus.OK
    assert run.guard is not None and run.guard.verdict is PatchVerdict.PASS
    assert run.rounds == 1
    assert run.guard.stats.files == 1
    assert run.guard.stats.added_lines >= 1
    assert run.message == "第 1 轮通过：无命令"

    run_dir = _run_dir(env, run.run_id)
    for name in ("run.json", "check.json", "round-1/backend.log", "round-1/result.json"):
        assert (run_dir / name).is_file(), name
    assert (manager.home / "patches" / "demo" / "T-001" / "check.json").is_file()


def test_run_task_guard_rejects_forbidden_patch(env: CodingEnv, tmp_path: Path) -> None:
    _repo_obj, manager, run = _run(env, tmp_path, FORBIDDEN_BACKEND, name="forbidden.py")

    assert run.status is CodingRunStatus.FAILED
    assert run.stop_reason is CodingStopReason.GUARD_REJECTED
    assert run.backend is not None and run.backend.status is BackendStatus.OK
    assert run.guard is not None and run.guard.verdict is PatchVerdict.FAIL
    assert "path.forbidden" in {item.code for item in run.guard.findings}
    assert run.message.startswith("PatchGuard 拒绝：")
    assert manager.load("demo", "T-001").active


def test_run_task_backend_failure(env: CodingEnv, tmp_path: Path) -> None:
    _repo_obj, manager, run = _run(env, tmp_path, FAILING_BACKEND, name="failing.py")

    assert run.status is CodingRunStatus.FAILED
    assert run.stop_reason is CodingStopReason.BACKEND_FAILED
    assert run.backend is not None and run.backend.status is BackendStatus.FAILED
    assert run.guard is not None and run.guard.verdict is PatchVerdict.PASS
    assert run.message == "后端未成功：failed"


def test_run_task_rejects_dirty_workspace(env: CodingEnv, tmp_path: Path) -> None:
    repo, manager, worktree, spec, task = _setup(env)
    _write(worktree / "scratch.txt", "leftover\n")
    script = _script(tmp_path, SUCCESS_BACKEND)

    with pytest.raises(InputError):
        run_task(GitProvider(), manager, repo, spec, task, _config(script))


def test_run_task_rejects_revision_mismatch(env: CodingEnv, tmp_path: Path) -> None:
    repo, manager, worktree, _spec, task = _setup(env)
    spec = _spec_doc(snapshot_id="demo@deadbeefdeadbeef")
    script = _script(tmp_path, SUCCESS_BACKEND)

    with pytest.raises(InputError):
        run_task(GitProvider(), manager, repo, spec, task, _config(script))


def test_run_task_rejects_task_from_other_spec(env: CodingEnv, tmp_path: Path) -> None:
    repo, manager, worktree, spec, task = _setup(env)
    task = task.model_copy(update={"spec_id": "F-102"})
    script = _script(tmp_path, SUCCESS_BACKEND)

    with pytest.raises(InputError):
        run_task(GitProvider(), manager, repo, spec, task, _config(script))


def test_run_task_rejects_inactive_workspace(env: CodingEnv, tmp_path: Path) -> None:
    repo, manager, worktree, spec, task = _setup(env)
    manager.close(repo, "T-001")
    script = _script(tmp_path, SUCCESS_BACKEND)

    with pytest.raises(InputError):
        run_task(GitProvider(), manager, repo, spec, task, _config(script))


def test_load_approved_spec_reads_directory(tmp_path: Path) -> None:
    spec_dir = _write_spec_dir(tmp_path / "specs", snapshot_id="demo@rev")
    spec = load_approved_spec(spec_dir)

    assert spec.spec_id == "F-101"
    assert [item.id for item in decompose(spec)] == ["T-001", "T-002"]


def test_load_approved_spec_rejects_draft(tmp_path: Path) -> None:
    spec_dir = _write_spec_dir(tmp_path / "specs", snapshot_id="demo@rev", status=SpecStatus.DRAFT)

    with pytest.raises(InputError):
        load_approved_spec(spec_dir)


# ---------------------------------------------------------------- CLI


def test_cli_coding_task_list(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    spec_dir = _write_spec_dir(tmp_path / "specs", snapshot_id="demo@rev")

    rc = main(["coding", "task", "list", "--spec-dir", str(spec_dir)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "编码任务: spec=F-101, tasks=2" in out
    assert "T-001" in out
    assert "T-002" in out


def test_cli_coding_packet_and_run(
    env: CodingEnv, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, manager, worktree, spec, task = _setup(env)
    spec_dir = _write_spec_dir(tmp_path / "specs", snapshot_id=f"demo@{env.rev}")
    common = ["--repo", "demo", "--config-dir", str(env.config_dir)]

    capsys.readouterr()
    rc = main(
        ["coding", "packet", "build", "--spec-dir", str(spec_dir), "--task", "T-001", *common]
    )
    out = capsys.readouterr().out
    packet_dir = env.home / "coding" / "demo" / "F-101" / "T-001" / "packet"
    assert rc == 0
    assert "任务包: PKT-T-001-" in out
    assert (packet_dir / "task.json").is_file()

    script = _script(tmp_path, SUCCESS_BACKEND)
    backend_path = tmp_path / "backend.yaml"
    backend_path.write_text(
        yaml.safe_dump(
            {
                "kind": "command",
                "command": [
                    sys.executable,
                    str(script),
                    "{packet_dir}",
                    "{workspace}",
                    "{result_path}",
                ],
                "timeout_seconds": 30,
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "coding",
            "run",
            "--spec-dir",
            str(spec_dir),
            "--task",
            "T-001",
            "--backend-config",
            str(backend_path),
            *common,
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "status=succeeded" in out
    assert "stop=succeeded" in out
    assert "guard=pass" in out
    assert "说明: 第 1 轮通过：无命令" in out


# ---------------------------------------------------------------- S20 命令与多轮


class FakeRunner:
    """按队列返回预置结果，并记录收到的 CommandSpec。"""

    def __init__(self, results: list[CommandResult]) -> None:
        self._results = list(results)
        self.calls: list[CommandSpec] = []

    def run(self, spec: CommandSpec) -> CommandResult:
        self.calls.append(spec)
        if not self._results:
            raise AssertionError("FakeRunner 结果已耗尽")
        return self._results.pop(0)


def _result(
    status: CommandStatus = CommandStatus.OK,
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int | None = None,
) -> CommandResult:
    if exit_code is None:
        exit_code = 0 if status is CommandStatus.OK else 1
    return CommandResult(
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=10,
    )


GCC_ERROR = "nvlog/src/nvlog.c:12:5: error: use of undeclared identifier 'level'\n"
COMPILE_ONLY = 'commands:\n  build:\n    compile: "fake-compiler build"\n'


def _with_commands(env: CodingEnv, block: str) -> None:
    (env.config_dir / "demo.yaml").write_text(
        f"repo_id: demo\nprovider: git\nurl: {env.origin}\nbranch: main\n{block}\n",
        encoding="utf-8",
    )


def _run_with_runner(
    env: CodingEnv,
    tmp_path: Path,
    runner: FakeRunner,
    *,
    max_rounds: int = 5,
    body: str = SUCCESS_BACKEND,
) -> tuple[CodingRun, Path]:
    repo, manager, worktree, spec, task = _setup(env)
    script = _script(tmp_path, body)
    run = run_task(
        GitProvider(),
        manager,
        repo,
        spec,
        task,
        _config(script),
        max_rounds=max_rounds,
        runner=runner,
    )
    return run, worktree


def test_split_command_handles_quotes_and_rejects_bad_input() -> None:
    assert split_command("cmake --build build -j 8") == [
        "cmake",
        "--build",
        "build",
        "-j",
        "8",
    ]
    assert split_command("ctest --test-dir build -R 'nvlog.*'") == [
        "ctest",
        "--test-dir",
        "build",
        "-R",
        "nvlog.*",
    ]
    with pytest.raises(InputError):
        split_command("echo 'unterminated")
    with pytest.raises(InputError):
        split_command("   ")


def test_stage_specs_order_cwd_and_timeouts(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "demo.yaml").write_text(
        "repo_id: demo\n"
        "provider: git\n"
        "url: ./origin\n"
        "commands:\n"
        '  build: {configure: "cfg-cmd", compile: "build-cmd", timeout_s: 120}\n'
        '  lint: {command: "lint-cmd", timeout_s: 30}\n'
        '  test: {fast: "fast-cmd", full: "full-cmd", timeout_s: 900}\n',
        encoding="utf-8",
    )
    repo = load_repo_config(config_dir / "demo.yaml")
    root = tmp_path / "ws"

    pairs = stage_specs(repo, workspace_root=root)

    assert [kind for kind, _spec in pairs] == [
        StageKind.CONFIGURE,
        StageKind.LINT,
        StageKind.COMPILE,
        StageKind.TEST_FAST,
        StageKind.TEST_FULL,
    ]
    assert all(spec.cwd == str(root) for _kind, spec in pairs)
    assert pairs[0][1].timeout_s == 120
    assert pairs[1][1].timeout_s == 30
    assert pairs[3][1].timeout_s == 900


def test_parse_failures_gcc_diagnostic() -> None:
    items = parse_failures(StageKind.COMPILE, "", GCC_ERROR)

    assert len(items) == 1
    item = items[0]
    assert item.file == "nvlog/src/nvlog.c"
    assert item.line == 12
    assert item.code == "compile.diagnostic"
    assert "undeclared identifier" in item.message


def test_parse_failures_cmake_and_ctest() -> None:
    cmake = parse_failures(StageKind.CONFIGURE, "", "CMake Error at CMakeLists.txt:23 (x):\n")
    assert cmake[0].code == "cmake.error"
    assert cmake[0].file == "CMakeLists.txt"
    assert cmake[0].line == 23

    ctest = parse_failures(
        StageKind.TEST_FAST,
        "The following tests FAILED:\n\t 12 - test_nvlog (Failed)\n",
        "",
    )
    assert ctest[0].code == "test.failed"
    assert ctest[0].message == "test_nvlog"


def test_parse_failures_fallback_and_limit() -> None:
    fallback = parse_failures(StageKind.LINT, "", "some opaque output\n")
    assert fallback[0].code == "lint.failed"
    assert "opaque output" in fallback[0].message

    lines = "\n".join(f"src/f{index}.c:{index}:1: error: e{index}" for index in range(30))
    items = parse_failures(StageKind.COMPILE, "", lines)
    assert len(items) == 20


def test_run_task_with_stages_one_round(env: CodingEnv, tmp_path: Path) -> None:
    _with_commands(env, COMPILE_ONLY)
    runner = FakeRunner([_result(), _result()])
    run, worktree = _run_with_runner(env, tmp_path, runner)

    assert run.status is CodingRunStatus.SUCCEEDED
    assert run.stop_reason is CodingStopReason.SUCCEEDED
    assert run.rounds == 1
    assert [item.kind for item in run.baseline] == [StageKind.COMPILE]
    assert run.baseline[0].status is StageStatus.OK
    assert [item.kind for item in run.stages] == [StageKind.COMPILE]
    assert len(runner.calls) == 2
    assert runner.calls[0].cwd == str(worktree)
    assert run.message.startswith("第 1 轮通过：compile:ok")


def test_run_task_round_two_converges(env: CodingEnv, tmp_path: Path) -> None:
    _with_commands(env, COMPILE_ONLY)
    runner = FakeRunner([_result(), _result(CommandStatus.FAILED, stderr=GCC_ERROR), _result()])
    run, _worktree = _run_with_runner(env, tmp_path, runner)

    assert run.status is CodingRunStatus.SUCCEEDED
    assert run.stop_reason is CodingStopReason.SUCCEEDED
    assert run.rounds == 2
    assert run.round_reports[0].errors[0].file == "nvlog/src/nvlog.c"
    assert run.round_reports[0].stages[0].status is StageStatus.FAILED
    assert run.round_reports[1].errors == []
    assert run.stages[0].status is StageStatus.OK

    run_dir = _run_dir(env, run.run_id)
    assert (run_dir / "round-1" / "errors.json").is_file()
    round2_packet = run_dir / "round-2" / "packet"
    assert (round2_packet / "errors.json").is_file()
    assert "上一轮失败" in (round2_packet / "prompt.md").read_text(encoding="utf-8")


def test_run_task_no_progress_stops(env: CodingEnv, tmp_path: Path) -> None:
    _with_commands(env, COMPILE_ONLY)
    runner = FakeRunner(
        [
            _result(),
            _result(CommandStatus.FAILED, stderr=GCC_ERROR),
            _result(CommandStatus.FAILED, stderr=GCC_ERROR),
        ]
    )
    run, _worktree = _run_with_runner(env, tmp_path, runner, max_rounds=5)

    assert run.status is CodingRunStatus.FAILED
    assert run.stop_reason is CodingStopReason.NO_PROGRESS
    assert run.rounds == 2
    assert run.message == "连续两轮同一错误，停止并转人工"


def test_run_task_rounds_exhausted(env: CodingEnv, tmp_path: Path) -> None:
    _with_commands(env, COMPILE_ONLY)
    runner = FakeRunner(
        [
            _result(),
            _result(CommandStatus.FAILED, stderr=GCC_ERROR),
            _result(
                CommandStatus.FAILED,
                stderr="nvlog/src/nvlog.c:20:5: error: another failure\n",
            ),
        ]
    )
    run, _worktree = _run_with_runner(env, tmp_path, runner, max_rounds=2)

    assert run.status is CodingRunStatus.FAILED
    assert run.stop_reason is CodingStopReason.ROUNDS_EXHAUSTED
    assert run.rounds == 2
    assert run.message == "2 轮后仍有 1 项失败"


def test_run_task_baseline_failure_blocks_backend(env: CodingEnv, tmp_path: Path) -> None:
    _with_commands(env, COMPILE_ONLY)
    runner = FakeRunner([_result(CommandStatus.FAILED, stderr=GCC_ERROR)])
    run, worktree = _run_with_runner(env, tmp_path, runner)

    assert run.status is CodingRunStatus.FAILED
    assert run.stop_reason is CodingStopReason.BASELINE_FAILED
    assert run.rounds == 0
    assert run.backend is None
    assert run.guard is None
    assert run.baseline[0].status is StageStatus.FAILED
    assert run.message.startswith("基线未通过：compile")
    assert len(runner.calls) == 1

    run_dir = _run_dir(env, run.run_id)
    assert (run_dir / "baseline" / "stages.json").is_file()
    assert (run_dir / "baseline" / "errors.json").is_file()
    assert "int level = 1;" not in (worktree / "nvlog/src/nvlog.c").read_text(encoding="utf-8")


def test_cli_coding_run_with_commands(
    env: CodingEnv, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(env)
    compile_ok = _script(tmp_path, "import sys\nsys.exit(0)\n", "compile_ok.py")
    _with_commands(
        env,
        f'commands:\n  build:\n    compile: "{sys.executable} {compile_ok}"\n',
    )
    spec_dir = _write_spec_dir(tmp_path / "specs", snapshot_id=f"demo@{env.rev}")
    script = _script(tmp_path, SUCCESS_BACKEND, "backend_cli.py")
    backend_path = tmp_path / "backend-cli.yaml"
    backend_path.write_text(
        yaml.safe_dump(
            {
                "kind": "command",
                "command": [
                    sys.executable,
                    str(script),
                    "{packet_dir}",
                    "{workspace}",
                    "{result_path}",
                ],
                "timeout_seconds": 30,
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "coding",
            "run",
            "--spec-dir",
            str(spec_dir),
            "--task",
            "T-001",
            "--backend-config",
            str(backend_path),
            "--max-rounds",
            "2",
            "--config-dir",
            str(env.config_dir),
            "--repo",
            "demo",
        ]
    )
    captured = capsys.readouterr()
    out = captured.out

    assert rc == 0, captured.err
    assert "status=succeeded" in out
    assert "rounds=1/2" in out
    assert "基线: compile:ok" in out
    assert "阶段: compile:ok" in out


# ---------------------------------------------------------------- S21 独立验证


VERIFIER_PASS = """\
import json
import sys
from pathlib import Path

result_path = Path(sys.argv[3])
result_path.write_text(
    json.dumps(
        {
            "verdict": "pass",
            "issues": [],
            "evidence": ["diff 与需求逐条核对", "现有测试覆盖新增分支"],
            "confidence": 0.9,
            "residual_risks": ["未验证性能开销", "未覆盖并发场景"],
        },
        ensure_ascii=False,
    ),
    encoding="utf-8",
)
"""

VERIFIER_FAIL = """\
import json
import sys
from pathlib import Path

result_path = Path(sys.argv[3])
result_path.write_text(
    json.dumps(
        {
            "verdict": "fail",
            "issues": [
                {
                    "code": "verify.semantic",
                    "severity": "blocker",
                    "message": "级别过滤只在入口判断，未覆盖日志输出路径",
                    "file": "nvlog/src/nvlog.c",
                    "line": 12,
                    "evidence": "diff 只改了入口函数",
                }
            ],
            "confidence": 0.8,
            "residual_risks": [],
        },
        ensure_ascii=False,
    ),
    encoding="utf-8",
)
"""

VERIFIER_NO_EVIDENCE = """\
import json
import sys
from pathlib import Path

result_path = Path(sys.argv[3])
result_path.write_text(
    json.dumps(
        {
            "verdict": "fail",
            "issues": [{"code": "verify.guess", "severity": "blocker", "message": "感觉不对"}],
            "confidence": 0.5,
            "residual_risks": [],
        },
        ensure_ascii=False,
    ),
    encoding="utf-8",
)
"""

VERIFIER_NO_RISK = """\
import json
import sys
from pathlib import Path

result_path = Path(sys.argv[3])
result_path.write_text(
    json.dumps(
        {"verdict": "pass", "issues": [], "evidence": ["ok"], "confidence": 1.0},
        ensure_ascii=False,
    ),
    encoding="utf-8",
)
"""


def _patch_report(*, verdict: PatchVerdict = PatchVerdict.PASS) -> PatchReport:
    return PatchReport(
        repo_id="demo",
        task_id="T-001",
        base_revision="rev-1",
        worktree="/tmp/worktree",
        verdict=verdict,
        stats=PatchStats(files=1, added_lines=2, deleted_lines=0),
        policy=PatchPolicy(allowed_paths=["nvlog/src/nvlog.c"]),
        created_at=datetime.now(UTC),
    )


def _stage(
    kind: StageKind = StageKind.COMPILE, status: StageStatus = StageStatus.OK
) -> StageReport:
    return StageReport(
        kind=kind,
        command=["fake"],
        status=status,
        exit_code=0 if status is StageStatus.OK else 1,
        duration_ms=10,
    )


def test_mechanical_report_passes_with_trace_test(env: CodingEnv) -> None:
    _repo_obj, _manager, worktree, spec, task = _setup(env)

    report = mechanical_report(spec, task, _patch_report(), [_stage()], worktree, "", "")

    assert report.verdict is VerifierVerdict.PASS
    assert report.confidence == 1.0
    assert report.residual_risks
    assert any("trace" in item for item in report.evidence)


def test_mechanical_report_finds_missing_test_file(env: CodingEnv, tmp_path: Path) -> None:
    _repo_obj, _manager, _worktree, spec, task = _setup(env)
    empty = tmp_path / "empty"
    empty.mkdir()

    report = mechanical_report(spec, task, _patch_report(), [_stage()], empty, "", "")

    assert report.verdict is VerifierVerdict.FAIL
    assert report.blockers[0].code == "verify.missing-test"
    assert report.blockers[0].file == "nvlog/tests/test_nvlog.c"


def test_mechanical_report_rejects_disabled_test(env: CodingEnv) -> None:
    _repo_obj, _manager, worktree, spec, task = _setup(env)
    diff = (
        "+++ b/nvlog/tests/test_nvlog.c\n"
        "+TEST_F(NvlogTest, DISABLED_LevelFilter) { EXPECT_TRUE(true); }\n"
    )

    report = mechanical_report(spec, task, _patch_report(), [_stage()], worktree, "", diff)

    assert report.verdict is VerifierVerdict.FAIL
    assert report.blockers[0].code == "verify.disabled-test"
    assert report.blockers[0].file == "nvlog/tests/test_nvlog.c"


def test_mechanical_report_accepts_test_target_names(env: CodingEnv) -> None:
    _repo_obj, _manager, worktree, _spec, task = _setup(env)
    spec = _spec_doc(
        snapshot_id="demo@rev",
        trace=[
            TraceRow(
                requirement_id="R-001",
                design_section="5 方案设计",
                code_location="nvlog/src/nvlog.c",
                test="cuphy_hpp_tests",
                task="T-001",
            )
        ],
    )

    report = mechanical_report(spec, task, _patch_report(), [_stage()], worktree, "", "")

    assert report.verdict is VerifierVerdict.PASS
    assert any("test target: cuphy_hpp_tests" in item for item in report.evidence)


def _run_with_verifier(
    env: CodingEnv, tmp_path: Path, verifier_body: str, *, max_rounds: int = 2
) -> tuple[CodingRun, Path]:
    _with_commands(env, COMPILE_ONLY)
    runner = FakeRunner([_result(), _result()])
    repo, manager, worktree, spec, task = _setup(env)
    backend = _config(_script(tmp_path, SUCCESS_BACKEND))
    verifier_script = _script(tmp_path, verifier_body, "verifier.py")
    run = run_task(
        GitProvider(),
        manager,
        repo,
        spec,
        task,
        backend,
        max_rounds=max_rounds,
        verifier=_config(verifier_script),
        runner=runner,
    )
    return run, worktree


def test_run_task_with_verifier_pass(env: CodingEnv, tmp_path: Path) -> None:
    run, _worktree = _run_with_verifier(env, tmp_path, VERIFIER_PASS)

    assert run.status is CodingRunStatus.SUCCEEDED
    assert run.stop_reason is CodingStopReason.SUCCEEDED
    assert run.verifier is not None
    assert run.verifier.verdict is VerifierVerdict.PASS
    assert run.verifier.external is True
    assert run.verifier.confidence == 0.9
    assert "独立验证通过" in run.message

    run_dir = _run_dir(env, run.run_id)
    assert (run_dir / "verification.json").is_file()
    assert (run_dir / "round-1" / "verification.json").is_file()
    assert (run_dir / "round-1" / "verify" / "verification-request.json").is_file()
    assert run.round_reports[0].verifier is not None


def test_run_task_with_verifier_rejection(env: CodingEnv, tmp_path: Path) -> None:
    run, worktree = _run_with_verifier(env, tmp_path, VERIFIER_FAIL)

    assert run.status is CodingRunStatus.FAILED
    assert run.stop_reason is CodingStopReason.VERIFY_REJECTED
    assert run.rounds == 1
    assert run.verifier is not None
    assert run.verifier.blockers[0].code == "verify.semantic"
    assert run.message == "独立验证未通过：1 项 blocker"
    assert "int level = 1;" in (worktree / "nvlog/src/nvlog.c").read_text(encoding="utf-8")


def test_verifier_rejects_issue_without_evidence(env: CodingEnv, tmp_path: Path) -> None:
    run, _worktree = _run_with_verifier(env, tmp_path, VERIFIER_NO_EVIDENCE)

    assert run.stop_reason is CodingStopReason.VERIFY_REJECTED
    assert run.verifier is not None
    assert run.verifier.blockers[0].code == "verify.invalid-evidence"


def test_verifier_requires_residual_risks_on_pass(env: CodingEnv, tmp_path: Path) -> None:
    run, _worktree = _run_with_verifier(env, tmp_path, VERIFIER_NO_RISK)

    assert run.stop_reason is CodingStopReason.VERIFY_REJECTED
    assert run.verifier is not None
    assert run.verifier.blockers[0].code == "verify.no-residual-risk"


def test_cli_coding_run_with_verifier(
    env: CodingEnv, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(env)
    compile_ok = _script(tmp_path, "import sys\nsys.exit(0)\n", "compile_ok.py")
    _with_commands(
        env,
        f'commands:\n  build:\n    compile: "{sys.executable} {compile_ok}"\n',
    )
    spec_dir = _write_spec_dir(tmp_path / "specs", snapshot_id=f"demo@{env.rev}")
    backend_path = tmp_path / "backend-cli.yaml"
    backend_path.write_text(
        yaml.safe_dump(
            {
                "kind": "command",
                "command": [
                    sys.executable,
                    str(_script(tmp_path, SUCCESS_BACKEND, "backend_cli.py")),
                    "{packet_dir}",
                    "{workspace}",
                    "{result_path}",
                ],
                "timeout_seconds": 30,
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    verifier_path = tmp_path / "verifier-cli.yaml"
    verifier_path.write_text(
        yaml.safe_dump(
            {
                "kind": "command",
                "command": [
                    sys.executable,
                    str(_script(tmp_path, VERIFIER_PASS, "verifier_cli.py")),
                    "{packet_dir}",
                    "{workspace}",
                    "{result_path}",
                ],
                "timeout_seconds": 30,
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "coding",
            "run",
            "--spec-dir",
            str(spec_dir),
            "--task",
            "T-001",
            "--backend-config",
            str(backend_path),
            "--verifier-config",
            str(verifier_path),
            "--max-rounds",
            "2",
            "--config-dir",
            str(env.config_dir),
            "--repo",
            "demo",
        ]
    )
    captured = capsys.readouterr()

    assert rc == 0, captured.err
    assert "verify=pass" in captured.out
    assert "独立验证通过" in captured.out


# ---------------------------------------------------------------- S22 失败升级


def _escalation_of(run: CodingRun) -> EscalationReport:
    assert run.escalation_path, "失败运行必须生成 escalation.json"
    return EscalationReport.model_validate_json(
        Path(run.escalation_path).read_text(encoding="utf-8")
    )


def test_run_task_success_has_no_escalation(env: CodingEnv, tmp_path: Path) -> None:
    _repo_obj, _manager, run = _run(env, tmp_path, SUCCESS_BACKEND)

    assert run.status is CodingRunStatus.SUCCEEDED
    assert run.escalation_path == ""
    run_dir = _run_dir(env, run.run_id)
    assert not (run_dir / "escalation.json").exists()
    assert not (run_dir / "escalation.md").exists()


def test_guard_rejection_escalates_forbidden_file(env: CodingEnv, tmp_path: Path) -> None:
    _repo_obj, _manager, run = _run(env, tmp_path, FORBIDDEN_BACKEND, name="forbidden.py")
    report = _escalation_of(run)

    assert report.reason is EscalationReason.FORBIDDEN_FILE
    assert report.stop_reason is CodingStopReason.GUARD_REJECTED
    assert report.attempted_rounds == 1
    assert any(item.source == "guard" for item in report.failures)
    assert any("path.forbidden" in item.code for item in report.failures)

    run_dir = _run_dir(env, run.run_id)
    text = (run_dir / "escalation.md").read_text(encoding="utf-8")
    for section in (
        "## 任务与需求",
        "## 已尝试",
        "## 失败信息",
        "## 需要人做的决定",
        "## 建议下一步",
        "## 产物",
    ):
        assert section in text


def test_backend_failure_escalates_backend_failed(env: CodingEnv, tmp_path: Path) -> None:
    _repo_obj, _manager, run = _run(env, tmp_path, FAILING_BACKEND, name="failing.py")
    report = _escalation_of(run)

    assert report.reason is EscalationReason.BACKEND_FAILED
    assert report.stop_reason is CodingStopReason.BACKEND_FAILED
    assert report.summary == "后端未成功：failed"
    assert report.failures[0].source == "run"


def test_no_progress_escalates_no_progress(env: CodingEnv, tmp_path: Path) -> None:
    _with_commands(env, COMPILE_ONLY)
    runner = FakeRunner(
        [
            _result(),
            _result(CommandStatus.FAILED, stderr=GCC_ERROR),
            _result(CommandStatus.FAILED, stderr=GCC_ERROR),
        ]
    )
    run, _worktree = _run_with_runner(env, tmp_path, runner, max_rounds=5)
    report = _escalation_of(run)

    assert report.reason is EscalationReason.NO_PROGRESS
    assert report.attempted_rounds == 2
    assert len(report.attempts) == 2
    assert report.failures[0].code == "compile.diagnostic"


def test_rounds_exhausted_escalates_rounds_exhausted(env: CodingEnv, tmp_path: Path) -> None:
    _with_commands(env, COMPILE_ONLY)
    runner = FakeRunner(
        [
            _result(),
            _result(CommandStatus.FAILED, stderr=GCC_ERROR),
            _result(
                CommandStatus.FAILED,
                stderr="nvlog/src/nvlog.c:20:5: error: another failure\n",
            ),
        ]
    )
    run, _worktree = _run_with_runner(env, tmp_path, runner, max_rounds=2)
    report = _escalation_of(run)

    assert report.reason is EscalationReason.ROUNDS_EXHAUSTED
    assert report.attempted_rounds == 2
    assert report.max_rounds == 2
    assert report.summary == "2 轮后仍有 1 项失败"


def test_verifier_rejection_escalates_verifier_rejected(env: CodingEnv, tmp_path: Path) -> None:
    run, _worktree = _run_with_verifier(env, tmp_path, VERIFIER_FAIL)
    report = _escalation_of(run)

    assert report.reason is EscalationReason.VERIFIER_REJECTED
    assert report.stop_reason is CodingStopReason.VERIFY_REJECTED
    assert report.failures[0].source == "verifier"
    assert report.failures[0].code == "verify.semantic"
    assert "逐条核对 verification.json" in report.suggested_next_steps[0]


def test_baseline_failure_escalates_environment(env: CodingEnv, tmp_path: Path) -> None:
    _with_commands(env, COMPILE_ONLY)
    runner = FakeRunner([_result(CommandStatus.FAILED, stderr=GCC_ERROR)])
    run, _worktree = _run_with_runner(env, tmp_path, runner)
    report = _escalation_of(run)

    assert report.reason is EscalationReason.ENVIRONMENT_BROKEN
    assert report.attempted_rounds == 0
    assert report.failures[0].source == "baseline"
    assert report.failures[0].code == "compile.failed"
    assert report.attempts[0].startswith("基线：")


def test_escalation_reason_security_and_open_question(env: CodingEnv, tmp_path: Path) -> None:
    _repo_obj, _manager, run = _run(env, tmp_path, FORBIDDEN_BACKEND, name="forbidden.py")
    spec = _spec_doc(snapshot_id=f"demo@{env.rev}")
    task = next(item for item in decompose(spec) if item.id == "T-001")
    run_dir = _run_dir(env, run.run_id)

    assert run.guard is not None
    secret_guard = run.guard.model_copy(
        update={
            "findings": [
                PatchFinding(
                    code="diff.secret",
                    severity=FindingSeverity.BLOCKER,
                    message="新增内容疑似密钥",
                    path="nvlog/src/nvlog.c",
                )
            ]
        }
    )
    secret_run = run.model_copy(update={"guard": secret_guard})
    secret = build_escalation(secret_run, spec, task, run_dir)
    assert secret.reason is EscalationReason.SECURITY_ALERT
    assert any("人工审计命中的规则" in item for item in secret.decisions)
    assert any(item.source == "guard" for item in secret.failures)

    open_spec = spec.model_copy(
        update={"questions": [OpenQuestion(id="Q-001", question="级别取值如何映射？")]}
    )
    pending = build_escalation(run, open_spec, task, run_dir)
    assert pending.reason is EscalationReason.REQUIREMENT_DECISION
    assert any("Q-001" in item for item in pending.decisions)
    assert "关闭或转交 Spec 开放问题：Q-001" in render_escalation(pending, open_spec, task)


def test_cli_coding_run_prints_escalation(
    env: CodingEnv, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(env)
    spec_dir = _write_spec_dir(tmp_path / "specs", snapshot_id=f"demo@{env.rev}")
    backend_path = tmp_path / "backend-cli.yaml"
    backend_path.write_text(
        yaml.safe_dump(
            {
                "kind": "command",
                "command": [
                    sys.executable,
                    str(_script(tmp_path, FAILING_BACKEND, "failing_cli.py")),
                    "{packet_dir}",
                    "{workspace}",
                    "{result_path}",
                ],
                "timeout_seconds": 30,
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "coding",
            "run",
            "--spec-dir",
            str(spec_dir),
            "--task",
            "T-001",
            "--backend-config",
            str(backend_path),
            "--max-rounds",
            "1",
            "--config-dir",
            str(env.config_dir),
            "--repo",
            "demo",
        ]
    )
    captured = capsys.readouterr()

    assert rc == 3, captured.err
    assert "升级: " in captured.out
    assert "原因=backend_failed" in captured.out
    assert "待决定: 检查模型后端进程、凭据与超时配置" in captured.out


# ---------------------------------------------------------------- S23 命令审计


def _audit_entries(run: CodingRun) -> list[dict[str, object]]:
    assert run.audit_path, "每次运行都应写审计"
    path = Path(run.audit_path)
    assert path.is_file()
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert audit_count(path) == len(lines)
    return [json.loads(line) for line in lines]


def test_assert_command_allowed_blocks_destructive() -> None:
    for argv in (
        ["rm", "-rf", "/"],
        ["bash", "-lc", "rm -rf build"],
        ["sudo", "make"],
        ["git", "push", "origin", "main"],
        ["git", "reset", "--hard", "HEAD~1"],
        ["mkfs.ext4", "/dev/sda1"],
    ):
        with pytest.raises(InputError, match="拒绝执行危险命令"):
            assert_command_allowed(argv, source="test")

    assert_command_allowed(["cmake", "--build", "build", "-j", "8"], source="test")
    assert_command_allowed(["pytest", "-q"], source="test")


def test_forbidden_stage_command_rejected(env: CodingEnv, tmp_path: Path) -> None:
    _with_commands(env, 'commands:\n  build:\n    compile: "rm -rf /"\n')

    with pytest.raises(InputError, match="递归强制删除"):
        _run(env, tmp_path, SUCCESS_BACKEND)


def test_forbidden_backend_command_rejected(env: CodingEnv, tmp_path: Path) -> None:
    repo, manager, _worktree, spec, task = _setup(env)
    config = BackendConfig(kind="command", command=["sudo", "python"], timeout_seconds=30)

    with pytest.raises(InputError, match="提权"):
        run_task(GitProvider(), manager, repo, spec, task, config)


def test_audit_records_stage_commands(env: CodingEnv, tmp_path: Path) -> None:
    _with_commands(env, COMPILE_ONLY)
    runner = FakeRunner([_result(), _result()])
    run, _worktree = _run_with_runner(env, tmp_path, runner)

    entries = _audit_entries(run)
    assert [entry["kind"] for entry in entries] == ["stage:compile", "backend", "stage:compile"]
    assert [entry["seq"] for entry in entries] == [1, 2, 3]
    assert all(entry["status"] == "ok" and entry["exit_code"] == 0 for entry in entries)
    assert run.audit_path.endswith("audit/commands.jsonl")


def test_audit_records_backend_and_verifier(env: CodingEnv, tmp_path: Path) -> None:
    run, _worktree = _run_with_verifier(env, tmp_path, VERIFIER_PASS)

    entries = _audit_entries(run)
    assert [entry["kind"] for entry in entries] == [
        "stage:compile",
        "backend",
        "stage:compile",
        "verifier",
    ]
    assert entries[1]["argv"][1].endswith("backend.py")
    assert entries[3]["argv"][1].endswith("verifier.py")


def test_audit_records_failed_stage(env: CodingEnv, tmp_path: Path) -> None:
    _with_commands(env, COMPILE_ONLY)
    runner = FakeRunner(
        [
            _result(),
            _result(CommandStatus.FAILED, stderr=GCC_ERROR),
            _result(CommandStatus.FAILED, stderr=GCC_ERROR),
        ]
    )
    run, _worktree = _run_with_runner(env, tmp_path, runner, max_rounds=2)

    entries = _audit_entries(run)
    assert len(entries) == 5
    assert entries[-1]["status"] == "failed"
    assert entries[-1]["exit_code"] == 1


def test_failure_run_audit_listed_in_escalation(env: CodingEnv, tmp_path: Path) -> None:
    run, _worktree = _run_with_verifier(env, tmp_path, VERIFIER_FAIL)

    entries = _audit_entries(run)
    assert entries[-1]["kind"] == "verifier"
    assert run.audit_path in _escalation_of(run).artifacts


def test_cli_coding_run_prints_audit(
    env: CodingEnv, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(env)
    spec_dir = _write_spec_dir(tmp_path / "specs", snapshot_id=f"demo@{env.rev}")
    backend_path = tmp_path / "backend-audit.yaml"
    backend_path.write_text(
        yaml.safe_dump(
            {
                "kind": "command",
                "command": [
                    sys.executable,
                    str(_script(tmp_path, SUCCESS_BACKEND, "backend_audit.py")),
                    "{packet_dir}",
                    "{workspace}",
                    "{result_path}",
                ],
                "timeout_seconds": 30,
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "coding",
            "run",
            "--spec-dir",
            str(spec_dir),
            "--task",
            "T-001",
            "--backend-config",
            str(backend_path),
            "--max-rounds",
            "1",
            "--config-dir",
            str(env.config_dir),
            "--repo",
            "demo",
        ]
    )
    captured = capsys.readouterr()

    assert rc == 0, captured.err
    assert "审计: " in captured.out
    assert "（1 条）" in captured.out


# ---------------------------------------------------------------- S24 补丁重放


def _replay(env: CodingEnv, run: CodingRun, results: list[CommandResult]) -> object:
    spec = _spec_doc(snapshot_id=f"demo@{env.rev}")
    task = next(item for item in decompose(spec) if item.id == run.task_id)
    return replay_run(
        GitProvider(),
        _manager(env),
        _repo(env),
        spec,
        task,
        run,
        runner=FakeRunner(results),
    )


def test_replay_reproduces_successful_run(env: CodingEnv, tmp_path: Path) -> None:
    _with_commands(env, COMPILE_ONLY)
    run, _worktree = _run_with_runner(env, tmp_path, FakeRunner([_result(), _result()]))

    report = _replay(env, run, [_result()])

    assert report.status is ReplayStatus.REPRODUCED
    assert len(report.checks) == 1
    assert report.checks[0].kind is StageKind.COMPILE
    assert report.checks[0].matched
    assert report.message == "1/1 个阶段与原运行一致"
    assert len(report.patch_sha256) == 64
    assert "diff --git" in Path(report.patch_path).read_text(encoding="utf-8")
    assert (Path(report.replay_dir) / "stages.json").is_file()
    assert (_run_dir(env, run.run_id) / "replay.json").is_file()
    assert not (Path(report.replay_dir) / "worktree").exists()


def test_replay_detects_divergence(env: CodingEnv, tmp_path: Path) -> None:
    _with_commands(env, COMPILE_ONLY)
    run, _worktree = _run_with_runner(env, tmp_path, FakeRunner([_result(), _result()]))

    report = _replay(env, run, [_result(CommandStatus.FAILED, stderr=GCC_ERROR)])

    assert report.status is ReplayStatus.DIVERGED
    assert not report.checks[0].matched
    assert report.checks[0].actual_status is StageStatus.FAILED
    assert report.message == "0/1 个阶段与原运行一致"


def test_replay_refuses_guard_rejected_run(env: CodingEnv, tmp_path: Path) -> None:
    _repo_obj, _manager_obj, run = _run(env, tmp_path, FORBIDDEN_BACKEND, name="forbidden.py")

    with pytest.raises(InputError, match="PatchGuard 未通过"):
        _replay(env, run, [])


def test_cli_replay_reproduces(
    env: CodingEnv, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_commands(env, COMPILE_ONLY)
    run, _worktree = _run_with_runner(env, tmp_path, FakeRunner([_result(), _result()]))
    _with_commands(env, 'commands:\n  build:\n    compile: "true"\n')
    spec_dir = _write_spec_dir(tmp_path / "specs", snapshot_id=f"demo@{env.rev}")

    rc = main(
        [
            "coding",
            "replay",
            "--run",
            str(_run_dir(env, run.run_id) / "run.json"),
            "--repo",
            "demo",
            "--spec-dir",
            str(spec_dir),
            "--config-dir",
            str(env.config_dir),
        ]
    )
    captured = capsys.readouterr()

    assert rc == 0, captured.err
    assert "重放: status=reproduced stages=1/1" in captured.out
    assert "结果: " in captured.out


# ---------------------------------------------------------------- S25 指标


TOKEN_BACKEND = """\
import json
import sys
from pathlib import Path

workspace = Path(sys.argv[2])
target = workspace / "nvlog/src/nvlog.c"
target.write_text(target.read_text(encoding="utf-8") + "int level = 1;" + chr(10), encoding="utf-8")
result_path = Path(sys.argv[3])
result_path.write_text(
    json.dumps({"status": "ok", "summary": "done", "tokens_in": 120, "tokens_out": 30}),
    encoding="utf-8",
)
print("backend ok")
"""


def _report_of(env: CodingEnv, run_id: str) -> RunReport:
    path = _run_dir(env, run_id) / "report.json"
    return RunReport.model_validate_json(path.read_text(encoding="utf-8"))


def test_run_writes_report_with_metrics(env: CodingEnv, tmp_path: Path) -> None:
    run, _worktree = _run_with_runner(env, tmp_path, FakeRunner([]))

    report = _report_of(env, run.run_id)

    assert report.status is CodingRunStatus.SUCCEEDED
    assert report.stop_reason is CodingStopReason.SUCCEEDED
    assert report.metrics.succeeded
    assert report.metrics.first_pass
    assert report.metrics.rounds == 1
    assert report.metrics.total_duration_ms == (
        report.metrics.backend_duration_ms + report.metrics.stage_duration_ms
    )
    assert not report.metrics.regression
    assert not report.metrics.escalated


def test_report_marks_regression_and_escalation(env: CodingEnv, tmp_path: Path) -> None:
    _with_commands(env, COMPILE_ONLY)
    runner = FakeRunner(
        [
            _result(),
            _result(CommandStatus.FAILED, stderr=GCC_ERROR),
            _result(CommandStatus.FAILED, stderr=GCC_ERROR),
        ]
    )
    run, _worktree = _run_with_runner(env, tmp_path, runner, max_rounds=2)

    report = _report_of(env, run.run_id)

    assert report.status is CodingRunStatus.FAILED
    assert report.stop_reason is CodingStopReason.NO_PROGRESS
    assert report.metrics.regression
    assert report.metrics.escalated
    assert report.metrics.baseline_failures == 0
    assert report.metrics.final_failures == 1


def test_report_baseline_failure_is_not_regression(env: CodingEnv, tmp_path: Path) -> None:
    _with_commands(env, COMPILE_ONLY)
    run, _worktree = _run_with_runner(
        env, tmp_path, FakeRunner([_result(CommandStatus.FAILED, stderr=GCC_ERROR)])
    )

    report = _report_of(env, run.run_id)

    assert report.stop_reason is CodingStopReason.BASELINE_FAILED
    assert report.metrics.rounds == 0
    assert report.metrics.baseline_failures == 1
    assert not report.metrics.regression
    assert report.metrics.escalated
    assert not report.metrics.first_pass


def test_report_sums_backend_tokens(env: CodingEnv, tmp_path: Path) -> None:
    run, _worktree = _run_with_runner(env, tmp_path, FakeRunner([]), body=TOKEN_BACKEND)

    report = _report_of(env, run.run_id)

    assert report.metrics.tokens_in == 120
    assert report.metrics.tokens_out == 30


def test_aggregate_counts_runs() -> None:
    created = datetime(2026, 1, 1, tzinfo=UTC)
    reports = [
        RunReport(
            run_id="RUN-DEMO-A",
            repo_id="demo",
            spec_id="F-101",
            task_id="T-001",
            status=CodingRunStatus.SUCCEEDED,
            stop_reason=CodingStopReason.SUCCEEDED,
            metrics=RunMetrics(
                succeeded=True,
                first_pass=True,
                rounds=1,
                total_duration_ms=100,
                tokens_in=10,
                tokens_out=5,
            ),
            created_at=created,
        ),
        RunReport(
            run_id="RUN-DEMO-B",
            repo_id="demo",
            spec_id="F-101",
            task_id="T-001",
            status=CodingRunStatus.FAILED,
            stop_reason=CodingStopReason.ROUNDS_EXHAUSTED,
            metrics=RunMetrics(
                succeeded=False,
                rounds=2,
                total_duration_ms=200,
                tokens_in=20,
                tokens_out=10,
                regression=True,
                escalated=True,
            ),
            created_at=created,
        ),
    ]

    stats = aggregate(reports)

    assert stats.runs == 2
    assert stats.succeeded == 1
    assert stats.first_pass == 1
    assert stats.regression == 1
    assert stats.escalated == 1
    assert stats.total_rounds == 3
    assert stats.total_duration_ms == 300
    assert stats.tokens_in == 30
    assert stats.success_rate == 0.5
    assert stats.avg_rounds == 1.5


def test_cli_report_aggregates(
    env: CodingEnv, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run, _worktree = _run_with_runner(env, tmp_path, FakeRunner([]))
    spec_dir = _write_spec_dir(tmp_path / "specs", snapshot_id=f"demo@{env.rev}")

    rc = main(["coding", "report", "--repo", "demo", "--spec-dir", str(spec_dir)])
    captured = capsys.readouterr()

    assert rc == 0, captured.err
    assert "统计: runs=1 成功=1（100%）" in captured.out
    assert "一次通过=1（100%）" in captured.out
    assert f"运行: {run.run_id} status=succeeded" in captured.out


# ---------------------------------------------------------------- S26 JUnit


JUNIT_A = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="nvlog" tests="2" failures="1">
    <testcase classname="nvlog.case" name="A" file="nvlog/src/nvlog.c" line="12">
      <failure message="assert A"/>
    </testcase>
    <testcase classname="nvlog.case" name="ok"/>
  </testsuite>
</testsuites>
"""

JUNIT_A_B = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="nvlog" tests="3" failures="2">
    <testcase classname="nvlog.case" name="A" file="nvlog/src/nvlog.c" line="12">
      <failure message="assert A"/>
    </testcase>
    <testcase classname="nvlog.case" name="B">
      <error message="boom B">traceback</error>
    </testcase>
    <testcase classname="nvlog.case" name="ok"/>
  </testsuite>
</testsuites>
"""

JUNIT_SKIPPED = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="nvlog" tests="2" skipped="1">
    <testcase classname="nvlog.case" name="ok"/>
    <testcase classname="nvlog.case" name="C">
      <skipped message="not implemented"/>
    </testcase>
  </testsuite>
</testsuites>
"""

JUNIT_SKIPPED_CASE = """    <testcase classname="nvlog.case" name="C">
      <skipped message="not implemented"/>
    </testcase>
"""

JUNIT_COMMANDS = """commands:
  test:
    fast: "fake-tests"
    report_format: junit
    report_paths: ["reports/*.xml"]
"""


class XmlRunner:
    """按队列返回结果，并在返回前写入 JUnit 报告。"""

    def __init__(self, steps: list[tuple[CommandResult, str]], report: Path) -> None:
        self._steps = list(steps)
        self._report = report
        self.calls: list[CommandSpec] = []

    def run(self, spec: CommandSpec) -> CommandResult:
        self.calls.append(spec)
        if not self._steps:
            raise AssertionError("XmlRunner 结果已耗尽")
        result, xml = self._steps.pop(0)
        self._report.parent.mkdir(parents=True, exist_ok=True)
        self._report.write_text(xml, encoding="utf-8")
        return result


def _run_with_xml(
    env: CodingEnv,
    tmp_path: Path,
    steps: list[tuple[CommandResult, str]],
    *,
    max_rounds: int = 5,
) -> tuple[CodingRun, Path, XmlRunner]:
    repo, manager, worktree, spec, task = _setup(env)
    script = _script(tmp_path, SUCCESS_BACKEND)
    runner = XmlRunner(steps, worktree / "reports" / "tests.xml")
    run = run_task(
        GitProvider(),
        manager,
        repo,
        spec,
        task,
        _config(script),
        max_rounds=max_rounds,
        runner=runner,
    )
    return run, worktree, runner


def test_junit_parse_reports_extracts_failures_and_skipped(tmp_path: Path) -> None:
    from codingwiki.domain.coding import TestFailureKind

    report = tmp_path / "reports" / "tests.xml"
    _write(report, JUNIT_A_B.replace("</testsuite>", JUNIT_SKIPPED_CASE + "</testsuite>"))

    result = parse_reports(JUnitConfig(root=tmp_path, patterns=("reports/*.xml",)))

    assert result.files == ["reports/tests.xml"]
    assert result.errors == []
    by_id = {item.id: item for item in result.failures}
    assert by_id["nvlog.case::A"].kind is TestFailureKind.FAILURE
    assert by_id["nvlog.case::A"].file == "nvlog/src/nvlog.c"
    assert by_id["nvlog.case::A"].line == 12
    assert by_id["nvlog.case::B"].kind is TestFailureKind.ERROR
    assert by_id["nvlog.case::B"].message == "boom B"
    assert result.skipped == ["nvlog.case::C"]


def test_junit_parse_reports_missing_bad_xml_and_stale(tmp_path: Path) -> None:
    config = JUnitConfig(root=tmp_path, patterns=("reports/*.xml",))
    missing = parse_reports(config)
    assert missing.files == []
    assert "未找到 JUnit 报告" in missing.errors[0]

    _write(tmp_path / "reports" / "tests.xml", "<testsuite><testcase>")
    bad = parse_reports(config)
    assert bad.files == ["reports/tests.xml"]
    assert "XML 解析失败" in bad.errors[0]

    stale = parse_reports(
        config, since_ns=(tmp_path / "reports" / "tests.xml").stat().st_mtime_ns + 1
    )
    assert stale.files == []
    assert "未找到 JUnit 报告" in stale.errors[0]


def test_test_commands_junit_config() -> None:
    from codingwiki.domain.models import TestCommands

    commands = TestCommands(report_format="junit", report_paths=["build/reports/*.xml"])
    config = load_config(commands, source_root=Path("/tmp"))

    assert config is not None
    assert config.patterns == ("build/reports/*.xml",)
    assert load_config(TestCommands(fast="ctest"), source_root=Path("/tmp")) is None
    with pytest.raises(ValidationError):
        TestCommands(report_format="junit", report_paths=["../outside.xml"])
    with pytest.raises(ValidationError):
        TestCommands(report_format="junit", report_paths=["/abs/report.xml"])


def test_run_task_junit_new_failure_is_regression(env: CodingEnv, tmp_path: Path) -> None:
    _with_commands(env, JUNIT_COMMANDS)
    run, _worktree, runner = _run_with_xml(
        env,
        tmp_path,
        [
            (_result(), JUNIT_A),
            (_result(CommandStatus.FAILED, stdout="FAILED B\n"), JUNIT_A_B),
        ],
        max_rounds=1,
    )

    assert run.status is CodingRunStatus.FAILED
    assert run.stop_reason is CodingStopReason.ROUNDS_EXHAUSTED
    assert {item.code for item in run.errors} == {"test:nvlog.case::A", "test:nvlog.case::B"}
    assert len(runner.calls) == 2

    report = _report_of(env, run.run_id)
    assert report.metrics.tests_parsed
    assert report.metrics.new_failures == 1
    assert report.metrics.pre_existing_failures == 1
    assert report.metrics.regression


def test_run_task_junit_pre_existing_failure_is_reported(env: CodingEnv, tmp_path: Path) -> None:
    _with_commands(env, JUNIT_COMMANDS)
    run, _worktree, _runner = _run_with_xml(
        env,
        tmp_path,
        [
            (_result(), JUNIT_A),
            (_result(CommandStatus.FAILED, stdout="FAILED B\n"), JUNIT_A_B),
            (_result(), JUNIT_A),
        ],
        max_rounds=2,
    )

    assert run.status is CodingRunStatus.SUCCEEDED
    assert run.rounds == 2
    assert {item.code for item in run.round_reports[0].errors} == {
        "test:nvlog.case::A",
        "test:nvlog.case::B",
    }
    packet_errors = (_run_dir(env, run.run_id) / "round-2" / "packet" / "errors.json").read_text(
        encoding="utf-8"
    )
    assert "nvlog.case::B" in packet_errors

    report = _report_of(env, run.run_id)
    assert report.metrics.tests_parsed
    assert report.metrics.new_failures == 0
    assert report.metrics.pre_existing_failures == 1
    assert not report.metrics.regression


def test_run_task_junit_skipped_is_not_pass(env: CodingEnv, tmp_path: Path) -> None:
    _with_commands(env, JUNIT_COMMANDS)
    run, _worktree, _runner = _run_with_xml(
        env, tmp_path, [(_result(), JUNIT_A), (_result(), JUNIT_SKIPPED)], max_rounds=1
    )

    assert run.status is CodingRunStatus.FAILED
    assert run.stop_reason is CodingStopReason.ROUNDS_EXHAUSTED
    assert "项测试被跳过" in run.round_reports[0].message
    assert run.errors[0].code == "test.skipped"

    report = _report_of(env, run.run_id)
    assert report.metrics.skipped_tests == 1
    assert not report.metrics.regression


def test_run_task_junit_baseline_skipped_blocks_backend(env: CodingEnv, tmp_path: Path) -> None:
    _with_commands(env, JUNIT_COMMANDS)
    run, _worktree, runner = _run_with_xml(env, tmp_path, [(_result(), JUNIT_SKIPPED)])

    assert run.status is CodingRunStatus.FAILED
    assert run.stop_reason is CodingStopReason.BASELINE_FAILED
    assert run.backend is None
    assert "跳过不算通过" in run.message
    assert len(runner.calls) == 1


def test_run_task_junit_bad_xml_falls_back_to_log(env: CodingEnv, tmp_path: Path) -> None:
    _with_commands(env, JUNIT_COMMANDS)
    run, _worktree, _runner = _run_with_xml(
        env,
        tmp_path,
        [
            (_result(), JUNIT_A),
            (_result(CommandStatus.FAILED, stderr="suite crashed\n"), "<not-junit>"),
        ],
        max_rounds=1,
    )

    stage = run.stages[0]
    assert stage.report_error
    assert not stage.tests_parsed
    assert stage.test_failures == []
    assert run.errors[0].code == "test-fast.failed"
    assert "suite crashed" in run.errors[0].message

    report = _report_of(env, run.run_id)
    assert not report.metrics.tests_parsed
    assert report.metrics.regression
