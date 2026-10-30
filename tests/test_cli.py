"""命令行端到端测试：真实启动子进程，检查输出与退出码。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

VALID_CONFIG = """
config_version: "1.0"
embedding:
  model_path: ".cache/models/bge-m3"
"""


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "codingwiki", *args],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


def test_version_flag_exits_zero() -> None:
    result = _run("--version")

    assert result.returncode == 0
    assert "codingwiki" in result.stdout


def test_version_subcommand_exits_zero() -> None:
    result = _run("version")

    assert result.returncode == 0
    assert "codingwiki 0.1.0" in result.stdout


def test_config_validate_accepts_repository_default() -> None:
    result = _run("config", "validate", "--file", "configs/default.yaml")

    assert result.returncode == 0, result.stderr
    assert "配置有效" in result.stdout
    assert "backend=cline" in result.stdout


def test_config_validate_accepts_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")

    result = _run("config", "validate", "--file", str(path), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "gauss=未启用" in result.stdout


def test_config_validate_rejects_invalid_file(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    # 空 model_path 合法（运行时自动解析），用越界 dimension 构造真正的非法配置
    path.write_text('config_version: "1.0"\nembedding: {dimension: 0}\n', encoding="utf-8")

    result = _run("config", "validate", "--file", str(path), cwd=tmp_path)

    assert result.returncode == 2
    assert "配置校验失败" in result.stderr
    assert "dimension" in result.stderr


def test_config_validate_reports_missing_file(tmp_path: Path) -> None:
    result = _run("config", "validate", "--file", str(tmp_path / "missing.yaml"), cwd=tmp_path)

    assert result.returncode == 2
    assert "不存在" in result.stderr


def test_config_validate_rejects_json_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(VALID_CONFIG, encoding="utf-8")

    result = _run("config", "validate", "--file", str(path), cwd=tmp_path)

    assert result.returncode == 2
    assert ".yaml" in result.stderr


@pytest.mark.parametrize(
    ("extra_args", "expected_code"),
    [
        ((), 2),
        (("config",), 2),
        (("config", "validate"), 2),
    ],
)
def test_missing_arguments_exit_with_input_error(
    extra_args: tuple[str, ...], expected_code: int
) -> None:
    result = _run(*extra_args)

    assert result.returncode == expected_code
