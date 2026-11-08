"""进程级模型后端（开发计划 11.2/11.3 的文件交换契约，S19 最小实现）。

后端 = 一个 argv 模板启动的进程；它只允许改工作树文件，可选写 result.json。
判定以退出码 + PatchGuard 为准，不采信模型自述。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from codingwiki.domain.coding import BackendConfig, BackendResult, BackendStatus, TaskPacket
from codingwiki.errors import InputError

_TAIL_CHARS = 4000


def load_backend_config(path: str | Path) -> BackendConfig:
    config_path = Path(path)
    if not config_path.is_file():
        raise InputError(f"后端配置不存在：{config_path}")
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InputError(f"后端配置不可读：{config_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise InputError(f"后端配置必须是 YAML 映射：{config_path}")
    try:
        return BackendConfig.model_validate(payload)
    except ValueError as exc:
        raise InputError(f"后端配置不合法：{config_path}: {exc}") from exc


def run_backend(
    config: BackendConfig,
    packet: TaskPacket,
    workspace: Path,
    result_path: Path,
    log_path: Path,
    *,
    now: datetime | None = None,
) -> BackendResult:
    """启动后端进程并归一化结果；命令缺失直接拒绝（不静默 mock）。"""
    argv = _render_command(config, packet.packet_dir, workspace, result_path)
    executable = shutil.which(argv[0])
    if executable is None:
        raise InputError(f"后端命令不存在或不可执行：{argv[0]}")
    argv[0] = executable
    result_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=str(workspace.resolve()),
            check=False,
            capture_output=True,
            timeout=config.timeout_seconds,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        status = BackendStatus.OK if completed.returncode == 0 else BackendStatus.FAILED
        exit_code = completed.returncode
        error = ""
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = _decode(exc.stdout)
        stderr = _decode(exc.stderr)
        status = BackendStatus.TIMEOUT
        exit_code = -1
        error = f"后端超时（>{config.timeout_seconds}s）"

    result_json, parse_error = _read_result(result_path)
    if parse_error:
        status = BackendStatus.ERROR
        error = f"{error}；{parse_error}".strip("；")

    result = BackendResult(
        kind=config.kind,
        status=status,
        exit_code=exit_code,
        duration_ms=duration_ms,
        stdout_tail=_tail(stdout),
        stderr_tail=_tail(stderr),
        result_json=result_json,
        error=error,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(_log_text(argv, result), encoding="utf-8")
    return result


def _render_command(
    config: BackendConfig, packet_dir: str, workspace: Path, result_path: Path
) -> list[str]:
    values = {
        "packet_dir": packet_dir,
        "workspace": str(workspace.resolve()),
        "result_path": str(result_path.resolve()),
    }
    rendered: list[str] = []
    for token in config.command:
        try:
            rendered.append(token.format(**values))
        except (KeyError, IndexError, ValueError) as exc:
            raise InputError(f"后端命令占位符不合法：{token!r}: {exc}") from exc
    return rendered


def _read_result(path: Path) -> tuple[dict[str, Any], str]:
    if not path.is_file():
        return {}, ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, f"result.json 不是合法 JSON：{exc}"
    if not isinstance(payload, dict):
        return {}, "result.json 必须是 JSON 对象"
    return payload, ""


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _tail(value: str) -> str:
    return value[-_TAIL_CHARS:]


def _log_text(argv: list[str], result: BackendResult) -> str:
    lines = [
        f"# backend run: {result.kind}",
        f"# argv: {json.dumps(argv, ensure_ascii=False)}",
        f"# status: {result.status.value} exit={result.exit_code} duration_ms={result.duration_ms}",
    ]
    if result.error:
        lines.append(f"# error: {result.error}")
    lines += ["", "## stdout", result.stdout_tail, "", "## stderr", result.stderr_tail, ""]
    return "\n".join(lines)


__all__ = ["load_backend_config", "run_backend"]
