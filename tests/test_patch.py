"""S18 补丁协议与隔离工作区测试。

用本地 git 仓库造 fixture，不依赖网络；覆盖路径允许/禁止、diff 规模、
内容扫描（密钥/调试/注释/TODO）、测试保护、回滚/关闭与 CLI 退出码。
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest

from codingwiki.cli import main
from codingwiki.config import load_repo_config
from codingwiki.domain.models import RepoConfig
from codingwiki.domain.patch import PatchPolicy, PatchReport, PatchVerdict, WorkspaceStatus
from codingwiki.domain.spec import (
    SPEC_SECTIONS,
    DesignTask,
    SourceDocument,
    SpecDocument,
    SpecSection,
    SpecStatus,
    ValidationReport,
)
from codingwiki.errors import InputError
from codingwiki.patch import (
    WorkspaceManager,
    check_workspace,
    collect_diffs,
    load_approved_task,
)
from codingwiki.providers import GitProvider

NVLOG_C = "int nvlog_main(void)\n{\n    return 0;\n}\n"
NVLOG_H = "#ifndef NVLOG_H\n#define NVLOG_H\n#endif\n"
TEST_C = "void test_nvlog(void)\n{\n    assert(1);\n}\n"
CMAKE = "add_library(cuphy STATIC)\n"
THIRD_PARTY = "int lib(void) { return 0; }\n"


@dataclass(frozen=True)
class PatchEnv:
    origin: Path
    rev: str
    home: Path
    config_dir: Path
    root: Path


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, timeout=30
    )
    return result.stdout.strip()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[PatchEnv, None, None]:
    home = tmp_path / ".codingwiki"
    monkeypatch.setenv("CODINGWIKI_HOME", str(home))
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init")
    _git(origin, "config", "user.email", "test@test")
    _git(origin, "config", "user.name", "Test")
    _git(origin, "checkout", "-b", "main")
    _write(origin / "nvlog/src/nvlog.c", NVLOG_C)
    _write(origin / "nvlog/include/nvlog.h", NVLOG_H)
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
    yield PatchEnv(origin=origin, rev=rev, home=home, config_dir=config_dir, root=tmp_path)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _repo(env: PatchEnv) -> RepoConfig:
    return load_repo_config(env.config_dir / "demo.yaml")


def _manager(env: PatchEnv) -> WorkspaceManager:
    return WorkspaceManager(GitProvider(), home=env.home)


def _open(env: PatchEnv, task: str = "T-001") -> tuple[RepoConfig, WorkspaceManager, Path]:
    repo = _repo(env)
    manager = _manager(env)
    record = manager.open(repo, task, env.rev)
    return repo, manager, Path(record.path)


def _check(env: PatchEnv, manager: WorkspaceManager, policy: PatchPolicy, task: str = "T-001"):
    record = manager.load("demo", task)
    return check_workspace(GitProvider(), record, policy, manager)


def _codes(report: PatchReport) -> set[str]:
    return {item.code for item in report.findings}


# ---------------------------------------------------------------- 隔离工作区


def test_open_creates_isolated_worktree(env: PatchEnv) -> None:
    repo, manager, worktree = _open(env)
    record = manager.load("demo", "T-001")
    assert record.base_revision == env.rev
    assert record.branch == "cw/demo/T-001"
    assert record.status is WorkspaceStatus.ACTIVE
    assert (worktree / "nvlog/src/nvlog.c").is_file()
    assert _git(worktree, "rev-parse", "HEAD") == env.rev
    # 与默认 materialize 工作树互不共用
    assert worktree != (env.home / "repos" / "demo" / "work")


def test_duplicate_open_rejected(env: PatchEnv) -> None:
    _repo_obj, manager, _worktree = _open(env)
    with pytest.raises(InputError):
        manager.open(_repo(env), "T-001", env.rev)


def test_rollback_restores_baseline_and_cleans_untracked(env: PatchEnv) -> None:
    repo, manager, worktree = _open(env)
    _write(worktree / "nvlog/src/nvlog.c", NVLOG_C + "int hacked;\n")
    _write(worktree / "nvlog/src/extra.c", "int extra;\n")
    _git(worktree, "add", "nvlog/src/extra.c")

    record = manager.rollback("demo", "T-001")
    assert record.last_rollback_at is not None
    assert GitProvider().worktree_status(worktree) == ""
    assert (worktree / "nvlog/src/nvlog.c").read_text(encoding="utf-8") == NVLOG_C
    assert not (worktree / "nvlog/src/extra.c").exists()


def test_close_removes_worktree_and_discards_branch(env: PatchEnv) -> None:
    repo, manager, worktree = _open(env)
    record = manager.close(repo, "T-001")
    assert record.status is WorkspaceStatus.DISCARDED
    assert not worktree.exists()
    assert not GitProvider().branch_exists(repo, "cw/demo/T-001")
    discarded = manager.load("demo", "T-001")
    assert discarded.status is WorkspaceStatus.DISCARDED
    with pytest.raises(InputError):
        check_workspace(GitProvider(), discarded, PatchPolicy(), manager)


def test_close_keep_branch(env: PatchEnv) -> None:
    repo, manager, worktree = _open(env)
    record = manager.close(repo, "T-001", keep_branch=True)
    assert record.status is WorkspaceStatus.KEPT
    assert not worktree.exists()
    assert GitProvider().branch_exists(repo, "cw/demo/T-001")


# ---------------------------------------------------------------- 路径与规模


def test_compliant_patch_passes_and_writes_report(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    _write(worktree / "nvlog/src/nvlog.c", NVLOG_C + "int level = 1;\n")
    report = _check(env, manager, PatchPolicy(allowed_paths=["nvlog/src/nvlog.c"]))
    assert report.verdict is PatchVerdict.PASS
    assert report.stats.files == 1
    assert report.stats.added_lines == 1

    payload = json.loads(manager.report_path("demo", "T-001").read_text(encoding="utf-8"))
    assert payload["verdict"] == "pass"
    assert payload["policy"]["max_files"] == 3
    assert payload["policy"]["max_lines"] == 300


def test_forbidden_build_script_wins_over_allowlist(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    _write(worktree / "src/cuphy/CMakeLists.txt", CMAKE + "add_dependencies(x y)\n")
    report = _check(env, manager, PatchPolicy(allowed_paths=["src/cuphy/CMakeLists.txt"]))
    assert report.verdict is PatchVerdict.FAIL
    assert "path.forbidden" in _codes(report)


def test_not_allowed_path_blocked(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    _write(worktree / "nvlog/include/nvlog.h", NVLOG_H + "int x;\n")
    report = _check(env, manager, PatchPolicy(allowed_paths=["nvlog/src/nvlog.c"]))
    assert "path.not_allowed" in _codes(report)


def test_module_test_file_allowed(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    _write(
        worktree / "nvlog/tests/test_nvlog.c",
        TEST_C + "\nvoid test_more(void)\n{\n    assert(2);\n}\n",
    )
    report = _check(env, manager, PatchPolicy(allowed_paths=["nvlog/src/nvlog.c"]))
    assert "path.not_allowed" not in _codes(report)
    assert report.verdict is PatchVerdict.PASS


def test_too_many_files(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    _write(worktree / "nvlog/src/nvlog.c", NVLOG_C + "int a;\n")
    _write(worktree / "nvlog/include/nvlog.h", NVLOG_H + "int b;\n")
    _write(worktree / "nvlog/src/one.c", "int c;\n")
    _write(worktree / "nvlog/src/two.c", "int d;\n")
    report = _check(
        env,
        manager,
        PatchPolicy(allowed_paths=["nvlog/src/*", "nvlog/include/nvlog.h"]),
    )
    assert "diff.too_many_files" in _codes(report)


def test_too_many_lines(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    _write(worktree / "nvlog/src/nvlog.c", NVLOG_C + "".join(f"int v{i};\n" for i in range(301)))
    report = _check(env, manager, PatchPolicy(allowed_paths=["nvlog/src/nvlog.c"]))
    assert "diff.too_many_lines" in _codes(report)


# ---------------------------------------------------------------- 内容扫描


def test_binary_untracked_file(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    _write(worktree / "nvlog/assets/logo.bin", "")
    (worktree / "nvlog/assets/logo.bin").write_bytes(b"\x00\x01\x02")
    report = _check(env, manager, PatchPolicy(allowed_paths=["nvlog/assets/*"]))
    assert "diff.binary" in _codes(report)


def test_line_endings_only(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    _write(worktree / "nvlog/src/nvlog.c", NVLOG_C.replace("\n", "\r\n"))
    report = _check(env, manager, PatchPolicy(allowed_paths=["nvlog/src/nvlog.c"]))
    assert "diff.line_endings" in _codes(report)
    assert "diff.whitespace_only" not in _codes(report)


def test_whitespace_only(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    _write(worktree / "nvlog/src/nvlog.c", NVLOG_C.replace("    return 0;", "\treturn 0;"))
    report = _check(env, manager, PatchPolicy(allowed_paths=["nvlog/src/nvlog.c"]))
    assert "diff.whitespace_only" in _codes(report)


def test_secret_literal(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    _write(worktree / "nvlog/src/nvlog.c", NVLOG_C + 'const char* api_key = "abcdefgh12345678";\n')
    report = _check(env, manager, PatchPolicy(allowed_paths=["nvlog/src/nvlog.c"]))
    assert "diff.secret" in _codes(report)


def test_debug_output(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    _write(worktree / "nvlog/src/nvlog.c", NVLOG_C + "std::cout << level;\n")
    report = _check(env, manager, PatchPolicy(allowed_paths=["nvlog/src/nvlog.c"]))
    assert "diff.debug_output" in _codes(report)


def test_large_commented_block(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    comments = "".join(f"// removed code {i}\n" for i in range(16))
    _write(worktree / "nvlog/src/nvlog.c", NVLOG_C + comments)
    report = _check(env, manager, PatchPolicy(allowed_paths=["nvlog/src/nvlog.c"]))
    assert "diff.commented_code" in _codes(report)


def test_todo_is_warning_not_blocker(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    _write(worktree / "nvlog/src/nvlog.c", NVLOG_C + "// TODO: follow-up\n")
    report = _check(env, manager, PatchPolicy(allowed_paths=["nvlog/src/nvlog.c"]))
    assert "diff.todo" in _codes(report)
    assert report.verdict is PatchVerdict.PASS
    assert len(report.warnings) >= 1


def test_network_call_is_warning_not_blocker(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    _write(worktree / "nvlog/src/nvlog.c", NVLOG_C + "// curl https://example.com/api\n")
    report = _check(env, manager, PatchPolicy(allowed_paths=["nvlog/src/nvlog.c"]))
    assert "diff.network_call" in _codes(report)
    assert report.verdict is PatchVerdict.PASS
    assert len(report.warnings) >= 1


def test_dangerous_command_blocked(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    _write(worktree / "nvlog/src/nvlog.c", NVLOG_C + 'os.system("rm -rf /tmp/x");\n')
    report = _check(env, manager, PatchPolicy(allowed_paths=["nvlog/src/nvlog.c"]))
    assert "diff.dangerous_call" in _codes(report)
    assert report.verdict is PatchVerdict.FAIL


def test_eval_is_warning_not_blocker(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    _write(worktree / "nvlog/src/nvlog.c", NVLOG_C + "auto value = eval(expr);\n")
    report = _check(env, manager, PatchPolicy(allowed_paths=["nvlog/src/nvlog.c"]))
    assert "diff.dangerous_call" in _codes(report)
    assert report.verdict is PatchVerdict.PASS


def test_source_only_change_warns_test_first(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    _write(worktree / "nvlog/src/nvlog.c", NVLOG_C + "int level = 1;\n")
    report = _check(env, manager, PatchPolicy(allowed_paths=["nvlog/src/nvlog.c"]))
    assert "test.first_missing" in _codes(report)
    assert report.verdict is PatchVerdict.PASS


def test_test_change_satisfies_test_first(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    _write(worktree / "nvlog/src/nvlog.c", NVLOG_C + "int level = 1;\n")
    _write(worktree / "nvlog/tests/test_nvlog.c", TEST_C + "// extra coverage\n")
    report = _check(env, manager, PatchPolicy(allowed_paths=["nvlog/src/nvlog.c"]))
    assert "test.first_missing" not in _codes(report)


def test_test_first_warning_can_be_disabled(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    _write(worktree / "nvlog/src/nvlog.c", NVLOG_C + "int level = 1;\n")
    report = _check(
        env,
        manager,
        PatchPolicy(allowed_paths=["nvlog/src/nvlog.c"], require_test_first=False),
    )
    assert "test.first_missing" not in _codes(report)


def test_generated_header_blocked(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    _write(
        worktree / "nvlog/src/widget.c",
        "// Code generated by tool. DO NOT EDIT.\nint widget;\n",
    )
    report = _check(env, manager, PatchPolicy(allowed_paths=["nvlog/src/widget.c"]))
    assert "file.generated" in _codes(report)


# ---------------------------------------------------------------- 测试保护


def test_deleted_test_blocked(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    (worktree / "nvlog/tests/test_nvlog.c").unlink()
    report = _check(env, manager, PatchPolicy(allowed_paths=["nvlog/src/nvlog.c"]))
    assert "test.deleted" in _codes(report)


def test_skip_marker_blocked(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    _write(worktree / "nvlog/tests/test_nvlog.c", TEST_C + "GTEST_SKIP();\n")
    report = _check(env, manager, PatchPolicy(allowed_paths=["nvlog/src/nvlog.c"]))
    assert "test.skipped" in _codes(report)


def test_removed_assertion_blocked(env: PatchEnv) -> None:
    _repo_obj, manager, worktree = _open(env)
    _write(worktree / "nvlog/tests/test_nvlog.c", "void test_nvlog(void)\n{\n}\n")
    report = _check(env, manager, PatchPolicy(allowed_paths=["nvlog/src/nvlog.c"]))
    assert "test.assertion_removed" in _codes(report)


# ---------------------------------------------------------------- Spec 门禁与 CLI


def _approved_spec(directory: Path, files: list[str]) -> Path:
    spec_dir = directory / "F-101"
    spec_dir.mkdir(parents=True)
    spec = SpecDocument(
        spec_id="F-101",
        title="补丁协议试点",
        snapshot_id="demo@rev",
        source_doc=SourceDocument(path="req.md", doc_sha256="0" * 64, doc_id="demo@doc"),
        status=SpecStatus.APPROVED,
        sections=[
            SpecSection(number=number, title=title, body="不适用")
            for number, title in SPEC_SECTIONS.items()
        ],
        tasks=[
            DesignTask(
                id="T-001",
                goal="修改 nvlog 入口",
                files=files,
                estimated_lines=10,
                acceptance=["示例"],
            )
        ],
        validation=ValidationReport(),
    )
    (spec_dir / "spec.json").write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    return spec_dir


def test_load_approved_task(tmp_path: Path) -> None:
    spec_dir = _approved_spec(tmp_path / "specs", ["nvlog/src/nvlog.c"])
    task = load_approved_task(spec_dir, "T-001")
    assert task.files == ["nvlog/src/nvlog.c"]


def test_load_draft_spec_rejected(tmp_path: Path) -> None:
    spec_dir = _approved_spec(tmp_path / "specs", ["nvlog/src/nvlog.c"])
    payload = json.loads((spec_dir / "spec.json").read_text(encoding="utf-8"))
    payload["status"] = "draft"
    (spec_dir / "spec.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(InputError):
        load_approved_task(spec_dir, "T-001")


def test_cli_open_check_rollback_close(
    env: PatchEnv, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec_dir = _approved_spec(tmp_path / "specs", ["nvlog/src/nvlog.c"])
    common = ["--repo", "demo", "--config-dir", str(env.config_dir)]

    assert (
        main(["patch", "workspace", "open", "--task", "T-001", "--revision", env.rev, *common]) == 0
    )
    worktree = env.home / "worktrees" / "demo" / "T-001"
    _write(worktree / "nvlog/src/nvlog.c", NVLOG_C + "int level = 1;\n")

    capsys.readouterr()
    rc = main(
        [
            "patch",
            "check",
            "--task",
            "T-001",
            "--spec-dir",
            str(spec_dir),
            "--json",
            *common,
        ]
    )
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert rc == 0
    assert payload["verdict"] == "pass"
    assert [item["code"] for item in payload["findings"]] == ["test.first_missing"]

    _write(worktree / "src/cuphy/CMakeLists.txt", CMAKE + "add_dependencies(x y)\n")
    assert main(["patch", "check", "--task", "T-001", "--spec-dir", str(spec_dir), *common]) == 3

    assert main(["patch", "workspace", "rollback", "--task", "T-001", *common]) == 0
    assert GitProvider().worktree_status(worktree) == ""

    assert main(["patch", "workspace", "close", "--task", "T-001", *common]) == 0
    assert not worktree.exists()


def test_collect_diffs_includes_untracked(env: PatchEnv) -> None:
    _repo_obj, _manager_obj, worktree = _open(env)
    _write(worktree / "nvlog/src/new.c", "int new;\n")
    _write(worktree / "nvlog/src/nvlog.c", NVLOG_C + "int level;\n")
    diffs = collect_diffs(GitProvider(), worktree, env.rev)
    paths = {item.path for item in diffs}
    assert paths == {"nvlog/src/new.c", "nvlog/src/nvlog.c"}
