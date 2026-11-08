"""S19 编码任务包与模型后端协议测试。

用本地 git 仓库造 fixture，后端用临时 Python 脚本模拟（不联网、不接真实模型）。
覆盖：Spec → CodedTask 分解、任务包产物与指纹、后端进程契约、首轮编排与 CLI。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from codingwiki.cli import main
from codingwiki.coding import (
    build_packet,
    decompose,
    load_approved_spec,
    load_backend_config,
    run_backend,
    run_task,
)
from codingwiki.coding.commands import split_command, stage_specs
from codingwiki.coding.errors import parse_failures
from codingwiki.config import load_repo_config
from codingwiki.domain.coding import (
    BackendConfig,
    BackendStatus,
    CodedTask,
    CodingRun,
    CodingRunStatus,
    CodingStopReason,
    StageKind,
    StageStatus,
    TaskPacket,
)
from codingwiki.domain.models import RepoConfig
from codingwiki.domain.patch import PatchPolicy, PatchVerdict
from codingwiki.domain.spec import (
    SPEC_SECTIONS,
    DesignTask,
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
