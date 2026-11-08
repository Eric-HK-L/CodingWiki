"""本地命令执行：独立进程组 + 超时清理 + 可选 CPU 时间上限（开发计划 14.7.4）。

子进程放进独立进程组（start_new_session），超时时整组 SIGKILL，
避免 shell 派生的编译/测试子进程变成孤儿继续占资源。
cpu_seconds > 0 时在 POSIX 上用 RLIMIT_CPU 硬限 CPU 秒数，
超限进程收到 SIGXCPU（退出码 -24）。
"""

from __future__ import annotations

import os
import resource
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from codingwiki.errors import EnvironmentIssue
from codingwiki.exec.protocol import CommandResult, CommandSpec, CommandStatus

_TIMEOUT_SIGNALS = frozenset({-signal.SIGKILL, -signal.SIGXCPU})


@dataclass(frozen=True)
class ProcessOutcome:
    """一次进程执行的原始结果；状态语义由调用方（CommandRunner / 后端）决定。"""

    exit_code: int
    stdout: bytes
    stderr: bytes
    duration_ms: int
    timed_out: bool


def run_process(
    argv: list[str],
    *,
    cwd: str | Path | None = None,
    timeout_s: int = 600,
    env: Mapping[str, str] | None = None,
    cpu_seconds: int = 0,
) -> ProcessOutcome:
    """启动子进程并等待；超时连整个进程组一起 SIGKILL。

    FileNotFoundError / OSError 由调用方转成环境错误，这里不吞异常。
    """
    started = time.monotonic()
    preexec = _cpu_preexec(cpu_seconds)
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd) if cwd is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env) if env else None,
        start_new_session=True,
        preexec_fn=preexec,
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(proc)
        stdout, stderr = proc.communicate()
    exit_code = proc.returncode if proc.returncode is not None else -1
    return ProcessOutcome(
        exit_code=exit_code,
        stdout=stdout or b"",
        stderr=stderr or b"",
        duration_ms=int((time.monotonic() - started) * 1000),
        timed_out=timed_out,
    )


class LocalCommandRunner:
    """本地进程执行器。命令不存在是环境错误（不重试）；非零退出不是异常，
    由调用方按 status/exit_code 决定重试或回喂。"""

    def run(self, spec: CommandSpec) -> CommandResult:
        cwd = str(Path(spec.cwd).resolve()) if spec.cwd else None
        env = {**os.environ, **spec.env} if spec.env else None
        try:
            outcome = run_process(
                spec.argv,
                cwd=cwd,
                timeout_s=spec.timeout_s,
                env=env,
                cpu_seconds=spec.cpu_seconds,
            )
        except FileNotFoundError as exc:
            raise EnvironmentIssue(f"命令不存在: {spec.argv[0]}") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise EnvironmentIssue(f"命令无法启动: {spec.argv[0]}: {exc}") from exc

        status = _status_of(outcome)
        return CommandResult(
            status=status,
            exit_code=-1 if status is CommandStatus.TIMEOUT else outcome.exit_code,
            stdout=_decode(outcome.stdout),
            stderr=_decode(outcome.stderr),
            duration_ms=outcome.duration_ms,
            artifacts=_existing_artifacts(spec),
        )


def _status_of(outcome: ProcessOutcome) -> CommandStatus:
    if outcome.timed_out or outcome.exit_code in _TIMEOUT_SIGNALS:
        return CommandStatus.TIMEOUT
    return CommandStatus.OK if outcome.exit_code == 0 else CommandStatus.FAILED


def _kill_process_group(proc: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    proc.kill()


def _cpu_preexec(cpu_seconds: int) -> Callable[[], None] | None:
    """返回在 fork 后、exec 前设置 RLIMIT_CPU 的回调；0 或非 POSIX 返回 None。"""
    if cpu_seconds <= 0 or sys.platform == "win32":
        return None

    def _apply() -> None:
        limit = (cpu_seconds, cpu_seconds)
        resource.setrlimit(resource.RLIMIT_CPU, limit)

    return _apply


def _decode(data: bytes | None) -> str:
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")


def _existing_artifacts(spec: CommandSpec) -> list[str]:
    if not spec.artifacts:
        return []
    base = Path(spec.cwd).resolve() if spec.cwd else Path.cwd()
    return [a for a in spec.artifacts if (base / a).exists()]


__all__ = ["LocalCommandRunner", "ProcessOutcome", "run_process"]
