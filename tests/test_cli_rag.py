"""S5 CLI 端到端测试：facts → wiki 发布 → index build → search → ask。"""

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
    """同步 fixture 仓库、提取事实、生成并发布 Wiki。"""
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

    for args in (
        ["repo", "sync", "--repo", "sample", "--config-dir", str(config_dir)],
        ["facts", "extract", "--repo", "sample", "--config-dir", str(config_dir)],
        ["wiki", "generate", "--repo", "sample", "--config-dir", str(config_dir)],
        ["wiki", "publish", "--repo", "sample", "--config-dir", str(config_dir)],
    ):
        result = _run(args, cwd=tmp_path, env=env)
        assert result.returncode == 0, f"{args}: {result.stderr}"
    return config_dir, env


def test_index_search_ask_end_to_end(tmp_path: Path) -> None:
    config_dir, env = _prepare(tmp_path)

    build = _run(
        [
            "index",
            "build",
            "--repo",
            "sample",
            "--config-dir",
            str(config_dir),
            "--embedding",
            "hash",
        ],
        cwd=tmp_path,
        env=env,
    )
    assert build.returncode == 0, build.stderr
    assert "索引就绪" in build.stdout
    assert "model=hash-embed-v1-d256" in build.stdout
    assert "wiki=" in build.stdout  # 已发布 Wiki 页进入索引
    # 索引目录落盘
    indices = tmp_path / ".cw" / "indices" / "sample"
    assert (indices / "current").is_file()
    version = (indices / "current").read_text(encoding="utf-8").strip()
    assert (indices / version / "chunks.jsonl").is_file()
    assert (indices / version / "vectors.bin").is_file()

    search = _run(
        ["search", "--repo", "sample", "--query", "Engine run", "--no-rerank"],
        cwd=tmp_path,
        env=env,
    )
    assert search.returncode == 0, search.stderr
    assert "Engine" in search.stdout

    ask = _run(
        ["ask", "--repo", "sample", "--question", "Engine 是什么？", "--no-rerank"],
        cwd=tmp_path,
        env=env,
    )
    assert ask.returncode == 0, ask.stderr
    assert "[S1]" in ask.stdout
    assert "confidence=" in ask.stdout


def test_index_build_without_facts_exits_with_input_error(tmp_path: Path) -> None:
    home = tmp_path / ".cw"
    env = {**os.environ, "CODINGWIKI_HOME": str(home), "PYTHONPATH": str(REPO_ROOT / "src")}
    result = _run(["index", "build", "--repo", "nope"], cwd=tmp_path, env=env)
    assert result.returncode == 2
    assert "facts extract" in result.stderr


def test_search_without_index_exits_with_input_error(tmp_path: Path) -> None:
    home = tmp_path / ".cw"
    env = {**os.environ, "CODINGWIKI_HOME": str(home), "PYTHONPATH": str(REPO_ROOT / "src")}
    result = _run(["search", "--repo", "nope", "--query", "x"], cwd=tmp_path, env=env)
    assert result.returncode == 2
    assert "index build" in result.stderr


def test_ask_refuses_unanswerable_question(tmp_path: Path) -> None:
    config_dir, env = _prepare(tmp_path)
    build = _run(
        [
            "index",
            "build",
            "--repo",
            "sample",
            "--config-dir",
            str(config_dir),
            "--embedding",
            "hash",
        ],
        cwd=tmp_path,
        env=env,
    )
    assert build.returncode == 0, build.stderr

    ask = _run(
        ["ask", "--repo", "sample", "--question", "量子引力波速是多少", "--no-rerank"],
        cwd=tmp_path,
        env=env,
    )
    assert ask.returncode == 0, ask.stderr
    assert "无法确认" in ask.stdout


def test_second_index_build_keeps_both_versions(tmp_path: Path) -> None:
    config_dir, env = _prepare(tmp_path)
    for _ in range(2):
        build = _run(
            [
                "index",
                "build",
                "--repo",
                "sample",
                "--config-dir",
                str(config_dir),
                "--embedding",
                "hash",
            ],
            cwd=tmp_path,
            env=env,
        )
        assert build.returncode == 0, build.stderr
    indices = tmp_path / ".cw" / "indices" / "sample"
    versions = [p for p in indices.iterdir() if p.is_dir()]
    assert len(versions) == 2  # 同秒重建也不互相覆盖
