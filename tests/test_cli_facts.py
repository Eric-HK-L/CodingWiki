"""facts CLI 端到端测试：repo sync → facts extract → facts query。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "facts" / "sample"


def _run(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "codingwiki", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, timeout=30)


def _prepare(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    origin = tmp_path / "origin"
    shutil.copytree(FIXTURES, origin)
    _git(origin, "init", "-b", "main")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "fixture")

    home = tmp_path / ".cw"
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    (config_dir / "sample.yaml").write_text(
        "\n".join(
            [
                "repo_id: sample",
                "provider: git",
                f"url: {origin}",
                "branch: main",
                "",
            ]
        ),
        encoding="utf-8",
    )
    env = {**os.environ, "CODINGWIKI_HOME": str(home), "PYTHONPATH": str(REPO_ROOT / "src")}
    return config_dir, env


def test_facts_extract_and_query_end_to_end(tmp_path: Path) -> None:
    config_dir, env = _prepare(tmp_path)

    sync = _run(
        ["repo", "sync", "--repo", "sample", "--config-dir", str(config_dir)], cwd=tmp_path, env=env
    )
    assert sync.returncode == 0, sync.stderr

    extract = _run(
        ["facts", "extract", "--repo", "sample", "--config-dir", str(config_dir)],
        cwd=tmp_path,
        env=env,
    )
    assert extract.returncode == 0, extract.stderr
    assert "事实就绪" in extract.stdout
    assert "symbols=" in extract.stdout
    assert "质量:" in extract.stdout

    query = _run(
        ["facts", "query", "--repo", "sample", "--kind", "symbol", "--symbol", "add_kernel"],
        cwd=tmp_path,
        env=env,
    )
    assert query.returncode == 0, query.stderr
    assert "src/main.cu:5\tsymbol\tadd_kernel\tkernel" in query.stdout

    build = _run(["facts", "query", "--repo", "sample", "--kind", "build"], cwd=tmp_path, env=env)
    assert "phy\tlibrary" in build.stdout

    tests = _run(["facts", "query", "--repo", "sample", "--kind", "test"], cwd=tmp_path, env=env)
    assert "pytest" in tests.stdout
    assert "ctest" in tests.stdout


def test_facts_query_without_facts_exits_with_input_error(tmp_path: Path) -> None:
    _, env = _prepare(tmp_path)
    result = _run(["facts", "query", "--repo", "sample"], cwd=tmp_path, env=env)

    assert result.returncode == 2
    assert "facts extract" in result.stderr


def test_facts_extract_without_snapshot_exits_with_input_error(tmp_path: Path) -> None:
    config_dir, env = _prepare(tmp_path)
    result = _run(
        ["facts", "extract", "--repo", "sample", "--config-dir", str(config_dir)],
        cwd=tmp_path,
        env=env,
    )

    assert result.returncode == 2
    assert "还没有快照" in result.stderr


def test_facts_extract_unknown_revision_exits_with_input_error(tmp_path: Path) -> None:
    config_dir, env = _prepare(tmp_path)
    _run(
        ["repo", "sync", "--repo", "sample", "--config-dir", str(config_dir)], cwd=tmp_path, env=env
    )

    result = _run(
        [
            "facts",
            "extract",
            "--repo",
            "sample",
            "--config-dir",
            str(config_dir),
            "--revision",
            "deadbeef",
        ],
        cwd=tmp_path,
        env=env,
    )

    assert result.returncode == 2
    assert "不存在" in result.stderr
