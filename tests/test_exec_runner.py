"""CommandRunner / LocalCommandRunner 测试（S7 §16.3-3）。"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from codingwiki.errors import EnvironmentIssue
from codingwiki.exec import (
    CommandRunner,
    CommandSpec,
    CommandStatus,
    LocalCommandRunner,
)

_PY = sys.executable


def test_runner_satisfies_protocol() -> None:
    assert isinstance(LocalCommandRunner(), CommandRunner)


def test_run_success_captures_output() -> None:
    result = LocalCommandRunner().run(CommandSpec(argv=[_PY, "-c", "print('hello')"]))
    assert result.status is CommandStatus.OK
    assert result.exit_code == 0
    assert result.stdout.strip() == "hello"
    assert result.duration_ms >= 0


def test_run_nonzero_exit_is_failed_not_exception() -> None:
    result = LocalCommandRunner().run(CommandSpec(argv=[_PY, "-c", "import sys; sys.exit(3)"]))
    assert result.status is CommandStatus.FAILED
    assert result.exit_code == 3


def test_run_timeout() -> None:
    result = LocalCommandRunner().run(
        CommandSpec(argv=[_PY, "-c", "import time; time.sleep(10)"], timeout_s=1)
    )
    assert result.status is CommandStatus.TIMEOUT
    assert result.exit_code == -1


def test_timeout_kills_grandchild_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(30)\n"
    )

    result = LocalCommandRunner().run(CommandSpec(argv=[_PY, "-c", script], timeout_s=1))

    assert result.status is CommandStatus.TIMEOUT
    pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid)


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_CPU 仅在 POSIX 可用")
def test_cpu_limit_reports_timeout() -> None:
    result = LocalCommandRunner().run(
        CommandSpec(argv=[_PY, "-c", "while True: pass"], timeout_s=10, cpu_seconds=1)
    )
    assert result.status is CommandStatus.TIMEOUT
    assert result.exit_code == -1


def test_other_signal_termination_is_failed_not_timeout() -> None:
    result = LocalCommandRunner().run(
        CommandSpec(argv=[_PY, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"])
    )
    assert result.status is CommandStatus.FAILED
    assert result.exit_code == -signal.SIGTERM


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_run_cwd_and_env() -> None:
    result = LocalCommandRunner().run(
        CommandSpec(
            argv=[_PY, "-c", "import os; print(os.environ['CW_TEST_VAR'])"],
            env={"CW_TEST_VAR": "v42"},
        )
    )
    assert result.stdout.strip() == "v42"


def test_artifacts_filtered_to_existing(tmp_path: Path) -> None:
    (tmp_path / "real.out").write_text("x", encoding="utf-8")
    result = LocalCommandRunner().run(
        CommandSpec(
            argv=[_PY, "-c", "pass"],
            cwd=str(tmp_path),
            artifacts=["real.out", "missing.out"],
        )
    )
    assert result.artifacts == ["real.out"]


def test_missing_binary_raises_environment_issue() -> None:
    with pytest.raises(EnvironmentIssue, match="命令不存在"):
        LocalCommandRunner().run(CommandSpec(argv=["definitely-not-a-command-xyz"]))


def test_spec_rejects_blank_argv() -> None:
    with pytest.raises(ValueError):
        CommandSpec(argv=["  "])


def test_repo_commands_config_defaults_and_parsing(tmp_path: Path) -> None:
    from codingwiki.config import load_repo_config
    from codingwiki.domain.models import RepoType

    path = tmp_path / "demo.yaml"
    path.write_text(
        "\n".join(
            [
                "repo_id: demo",
                "provider: git",
                "url: https://example.com/demo.git",
                "commands:",
                "  build:",
                '    configure: "cmake -S . -B build"',
                '    compile: "cmake --build build -j 8"',
                "    timeout_s: 900",
                "  test:",
                '    fast: "ctest -R fast"',
                "  lint:",
                '    command: "clang-tidy -p build src"',
            ]
        ),
        encoding="utf-8",
    )
    repo = load_repo_config(path)
    assert repo.provider is RepoType.GIT
    assert repo.commands.build.configure == "cmake -S . -B build"
    assert repo.commands.build.timeout_s == 900
    assert repo.commands.test.full == ""
    assert repo.commands.lint.timeout_s == 600  # 默认值

    # 不写 commands 段的旧配置仍然合法（默认全空）
    plain = tmp_path / "plain.yaml"
    plain.write_text(
        "repo_id: plain\nprovider: git\nurl: https://example.com/p.git\n",
        encoding="utf-8",
    )
    assert load_repo_config(plain).commands.build.compile == ""
