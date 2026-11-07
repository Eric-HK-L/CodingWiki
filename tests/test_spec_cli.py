"""S12 CLI 端到端：spec extract / spec list 的退出码与输出。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "evals" / "specs" / "fixture-nvlog-requirements.md"


def _run(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "codingwiki", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _env(home: Path) -> dict[str, str]:
    return {**os.environ, "CODINGWIKI_HOME": str(home), "PYTHONPATH": str(REPO_ROOT / "src")}


def test_spec_extract_and_list_with_mock(tmp_path: Path) -> None:
    home = tmp_path / ".codingwiki"
    env = _env(home)
    result = _run(
        [
            "spec",
            "extract",
            "--repo",
            "sample",
            "--doc",
            str(FIXTURE),
            "--snapshot",
            "sample@rev",
            "--backend",
            "mock",
        ],
        cwd=tmp_path,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "已抽取" in result.stdout
    assert "R=3" in result.stdout and "N=3" in result.stdout
    assert "questions=2" in result.stdout
    bundles = list((home / "specs" / "sample" / "docs").glob("*/extract.json"))
    assert len(bundles) == 1

    listed = _run(["spec", "list", "--repo", "sample"], cwd=tmp_path, env=env)
    assert listed.returncode == 0, listed.stderr
    assert "sample@" in listed.stdout
    assert "R=3" in listed.stdout


def test_spec_extract_requires_snapshot_without_facts(tmp_path: Path) -> None:
    env = _env(tmp_path / ".codingwiki")
    result = _run(
        ["spec", "extract", "--repo", "sample", "--doc", str(FIXTURE), "--backend", "mock"],
        cwd=tmp_path,
        env=env,
    )
    assert result.returncode == 2
    assert "facts extract" in result.stderr


def test_spec_extract_api_backend_without_config_is_actionable(tmp_path: Path) -> None:
    env = _env(tmp_path / ".codingwiki")
    result = _run(
        [
            "spec",
            "extract",
            "--repo",
            "sample",
            "--doc",
            str(FIXTURE),
            "--snapshot",
            "sample@rev",
            "--backend",
            "api",
        ],
        cwd=tmp_path,
        env=env,
    )
    assert result.returncode == 2
    assert "配置文件不存在" in result.stderr
