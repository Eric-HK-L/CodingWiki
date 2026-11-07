"""S10 `codingwiki run` 命令端到端测试：真实子进程跑完整回流闭环。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "facts" / "sample"

HASH_APP_CONFIG = """
config_version: "1.0"
embedding:
  backend: hash
"""


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


def _prepare(tmp_path: Path, *, repo_id: str = "sample") -> tuple[Path, Path, dict[str, str]]:
    origin = tmp_path / "origin"
    origin.mkdir(parents=True, exist_ok=True)
    for item in FIXTURES.rglob("*"):
        if item.is_file():
            rel = item.relative_to(FIXTURES)
            dest = origin / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(item.read_bytes())
    _git(origin, "init", "-b", "main")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "fixture")

    home = tmp_path / ".cw"
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    (config_dir / f"{repo_id}.yaml").write_text(
        "\n".join(["repo_id: sample", "provider: git", f"url: {origin}", "branch: main", ""]),
        encoding="utf-8",
    )
    app_config = tmp_path / "app.yaml"
    app_config.write_text(HASH_APP_CONFIG, encoding="utf-8")
    env = {**os.environ, "CODINGWIKI_HOME": str(home), "PYTHONPATH": str(REPO_ROOT / "src")}
    return config_dir, app_config, env


def test_run_full_pipeline_end_to_end(tmp_path: Path) -> None:
    config_dir, app_config, env = _prepare(tmp_path)

    result = _run(
        [
            "run",
            "--repo",
            "sample",
            "--config-dir",
            str(config_dir),
            "--app-config",
            str(app_config),
        ],
        cwd=tmp_path,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "status=ok" in result.stdout
    for stage in ("sync", "facts", "wiki", "verify", "publish", "index"):
        assert "ok" in result.stdout and stage in result.stdout

    # current 指针推进、站点与索引落盘
    site = tmp_path / ".cw" / "wiki" / "sample"
    version = (site / "current").read_text(encoding="utf-8").strip()
    assert (site / "versions" / version / "site" / "index.html").is_file()
    indices = list((tmp_path / ".cw" / "indices" / "sample").glob("i*"))
    assert indices, "索引应已落盘"

    # 审计记录落盘可读回
    jobs = sorted((tmp_path / ".cw" / "jobs" / "sample").glob("*.json"))
    assert len(jobs) == 1
    import json

    saved = json.loads(jobs[0].read_text(encoding="utf-8"))
    assert saved["status"] == "ok"
    assert len(saved["stages"]) == 6
    assert saved["snapshot_id"]


def test_run_skip_stages(tmp_path: Path) -> None:
    config_dir, app_config, env = _prepare(tmp_path)

    result = _run(
        [
            "run",
            "--repo",
            "sample",
            "--config-dir",
            str(config_dir),
            "--app-config",
            str(app_config),
            "--skip",
            "wiki",
            "--skip",
            "verify",
            "--skip",
            "publish",
            "--skip",
            "index",
        ],
        cwd=tmp_path,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "skipped" in result.stdout
    # 只跑了 sync 与 facts：没有发布版本
    current = tmp_path / ".cw" / "wiki" / "sample" / "current"
    assert not current.exists()


def test_run_respects_repo_lock(tmp_path: Path) -> None:
    config_dir, app_config, env = _prepare(tmp_path)
    lock_path = tmp_path / ".cw" / "locks" / "sample.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(f"pid={os.getpid()} started=now host=test\n", encoding="utf-8")

    result = _run(
        ["run", "--repo", "sample", "--config-dir", str(config_dir)],
        cwd=tmp_path,
        env=env,
    )

    assert result.returncode == 3  # EnvironmentIssue
    assert "已有任务在运行" in result.stderr


def test_run_repo_all_skips_disabled(tmp_path: Path) -> None:
    config_dir, app_config, env = _prepare(tmp_path)
    (config_dir / "off.yaml").write_text(
        "\n".join(
            [
                "repo_id: offrepo",
                "provider: git",
                "url: https://example.com/off.git",
                "branch: main",
                "enabled: false",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = _run(
        [
            "run",
            "--repo",
            "all",
            "--config-dir",
            str(config_dir),
            "--app-config",
            str(app_config),
        ],
        cwd=tmp_path,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "repo=sample" in result.stdout
    assert "offrepo" not in result.stdout
