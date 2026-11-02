"""LocalCommandRunner：用 subprocess 执行 CommandSpec。"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from codingwiki.errors import EnvironmentIssue
from codingwiki.exec.protocol import CommandResult, CommandSpec, CommandStatus


class LocalCommandRunner:
    """本地进程执行器。命令不存在是环境错误（不重试）；非零退出不是异常，
    由调用方按 status/exit_code 决定重试或回喂。"""

    def run(self, spec: CommandSpec) -> CommandResult:
        cwd = str(Path(spec.cwd).resolve()) if spec.cwd else None
        started = time.monotonic()
        try:
            proc = subprocess.run(
                spec.argv,
                cwd=cwd,
                check=False,
                capture_output=True,
                timeout=spec.timeout_s,
                env={**os.environ, **spec.env} if spec.env else None,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                status=CommandStatus.TIMEOUT,
                exit_code=-1,
                stdout=_decode(exc.stdout),
                stderr=_decode(exc.stderr),
                duration_ms=_elapsed_ms(started),
                artifacts=_existing_artifacts(spec),
            )
        except FileNotFoundError as exc:
            raise EnvironmentIssue(f"命令不存在: {spec.argv[0]}") from exc
        except OSError as exc:
            raise EnvironmentIssue(f"命令无法启动: {spec.argv[0]}: {exc}") from exc

        duration_ms = _elapsed_ms(started)
        return CommandResult(
            status=CommandStatus.OK if proc.returncode == 0 else CommandStatus.FAILED,
            exit_code=proc.returncode,
            stdout=_decode(proc.stdout),
            stderr=_decode(proc.stderr),
            duration_ms=duration_ms,
            artifacts=_existing_artifacts(spec),
        )


def _decode(data: bytes | None) -> str:
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _existing_artifacts(spec: CommandSpec) -> list[str]:
    if not spec.artifacts:
        return []
    base = Path(spec.cwd).resolve() if spec.cwd else Path.cwd()
    return [a for a in spec.artifacts if (base / a).exists()]


__all__ = ["LocalCommandRunner"]
