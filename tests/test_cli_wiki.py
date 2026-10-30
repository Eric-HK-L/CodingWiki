"""wiki CLI 端到端测试：repo sync → facts extract → wiki plan/generate/verify/publish/status。"""

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
    return config_dir, env


def test_wiki_full_flow_end_to_end(tmp_path: Path) -> None:
    config_dir, env = _prepare(tmp_path)

    plan = _run(["wiki", "plan", "--repo", "sample"], cwd=tmp_path, env=env)
    assert plan.returncode == 0, plan.stderr
    assert "覆盖计划" in plan.stdout
    assert "新生成" in plan.stdout

    generate = _run(
        ["wiki", "generate", "--repo", "sample", "--config-dir", str(config_dir)],
        cwd=tmp_path,
        env=env,
    )
    assert generate.returncode == 0, generate.stderr
    assert "生成完成" in generate.stdout
    assert "失败 0" in generate.stdout

    # 草稿区已落盘页面与依赖
    drafts = tmp_path / ".cw" / "wiki" / "sample" / "drafts" / "pages"
    assert drafts.is_dir()
    assert (drafts / "overview.md").is_file()
    assert (drafts / "module").is_dir()

    verify = _run(
        ["wiki", "verify", "--repo", "sample", "--config-dir", str(config_dir)],
        cwd=tmp_path,
        env=env,
    )
    assert verify.returncode == 0, verify.stderr
    assert "复验完成" in verify.stdout
    assert "失败 0" in verify.stdout

    publish = _run(
        ["wiki", "publish", "--repo", "sample", "--config-dir", str(config_dir)],
        cwd=tmp_path,
        env=env,
    )
    assert publish.returncode == 0, publish.stderr
    assert "发布成功" in publish.stdout
    assert "verified=" in publish.stdout

    status = _run(["wiki", "status", "--repo", "sample"], cwd=tmp_path, env=env)
    assert status.returncode == 0, status.stderr
    assert "当前版本" in status.stdout
    assert "证据总数" in status.stdout


def test_wiki_generate_second_run_reuses_pages(tmp_path: Path) -> None:
    config_dir, env = _prepare(tmp_path)
    first = _run(
        ["wiki", "generate", "--repo", "sample", "--config-dir", str(config_dir)],
        cwd=tmp_path,
        env=env,
    )
    assert first.returncode == 0, first.stderr

    second = _run(
        ["wiki", "generate", "--repo", "sample", "--config-dir", str(config_dir)],
        cwd=tmp_path,
        env=env,
    )
    assert second.returncode == 0, second.stderr
    # 事实未变 → 多数页面复用，不重复生成
    assert "复用" in second.stdout
    ok_part = second.stdout.split("生成完成:")[1]
    reused_count = int(ok_part.split("复用")[1].split("，")[0].strip())
    assert reused_count > 0


def test_wiki_plan_without_facts_exits_with_input_error(tmp_path: Path) -> None:
    home = tmp_path / ".cw"
    env = {**os.environ, "CODINGWIKI_HOME": str(home), "PYTHONPATH": str(REPO_ROOT / "src")}
    result = _run(["wiki", "plan", "--repo", "nope"], cwd=tmp_path, env=env)

    assert result.returncode == 2
    assert "facts extract" in result.stderr


def test_wiki_publish_gate_blocks_failed_pages(tmp_path: Path) -> None:
    config_dir, env = _prepare(tmp_path)
    generate = _run(
        ["wiki", "generate", "--repo", "sample", "--config-dir", str(config_dir)],
        cwd=tmp_path,
        env=env,
    )
    assert generate.returncode == 0, generate.stderr

    # 篡改一个草稿页面：破坏证据引用 → 复验失败 → 发布门禁拦截
    drafts = tmp_path / ".cw" / "wiki" / "sample" / "drafts" / "pages"
    target = drafts / "module" / "src.md"
    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace("[E1]", "[E999]", 1), encoding="utf-8")

    verify = _run(
        ["wiki", "verify", "--repo", "sample", "--config-dir", str(config_dir)],
        cwd=tmp_path,
        env=env,
    )
    assert verify.returncode == 4, verify.stderr
    assert "module/src" in verify.stdout

    publish = _run(
        ["wiki", "publish", "--repo", "sample", "--config-dir", str(config_dir)],
        cwd=tmp_path,
        env=env,
    )
    assert publish.returncode != 0
