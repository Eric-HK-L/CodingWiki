"""S14 Spec 生成流水线测试：mock 端到端、修复循环、确定性、CLI。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codingwiki.backends.mock import (
    FAIL_SPEC_BAD_TASK,
    FAIL_SPEC_MISSING_SECTION,
    MockBackend,
)
from codingwiki.domain.facts import (
    BuildTarget,
    ConfigFact,
    FileFacts,
    RepoFacts,
    SymbolFact,
    SymbolKind,
)
from codingwiki.domain.facts import TestFact as _TestFact
from codingwiki.domain.spec import SPEC_SECTIONS, RequirementKind
from codingwiki.errors import InputError
from codingwiki.facts import FilesystemFactStore
from codingwiki.spec import SpecOutcome, generate_spec
from codingwiki.spec.ingest import specs_root

SNAP = "demo@rev"
_REQ_DOC = """# 日志级别需求

## 1. 功能需求

### 1.1 配置项

在仓库配置中新增日志级别配置项 log_level，默认 info。

验收标准：

- 未配置该项时按 info 输出；
- 配置为 debug 时 debug 级日志可见。

### 1.2 运行期覆盖

允许通过环境变量覆盖配置文件的日志级别。

验收标准：

- 环境变量合法时生效。

## 2. 非功能需求

- 日志级别切换不需要重新编译。
"""


def _facts() -> RepoFacts:
    symbol = "src/app/log.cpp::set_log_level@12"
    files = [
        FileFacts(
            path="src/app/log.cpp",
            language="cpp",
            blob_sha256="a" * 64,
            module="src/app",
            symbols=[
                SymbolFact(
                    symbol_id=symbol,
                    name="set_log_level",
                    qualified_name="set_log_level",
                    kind=SymbolKind.FUNCTION,
                    path="src/app/log.cpp",
                    line=12,
                    end_line=20,
                )
            ],
        )
    ]
    paths = [
        "src/app/log.cpp",
        "configs/app.yaml",
        "CMakeLists.txt",
        "test/log_test.cpp",
    ]
    return RepoFacts(
        snapshot_id=SNAP,
        repo_id="demo",
        revision="rev",
        extractor_version="test-v1",
        files=files,
        build_targets=[
            BuildTarget(
                name="app",
                kind="executable",
                path="CMakeLists.txt",
                line=10,
                sources=["src/app/log.cpp"],
            )
        ],
        config_facts=[
            ConfigFact(key="log_level", kind="file", format="yaml", path="configs/app.yaml")
        ],
        tests=[
            _TestFact(
                name="test_log_level",
                framework="gtest",
                path="test/log_test.cpp",
                line=10,
                target="app",
            )
        ],
        blobs={path: "d" * 64 for path in paths},
    )


def _doc(tmp_path: Path) -> Path:
    path = tmp_path / "requirements.md"
    path.write_text(_REQ_DOC, encoding="utf-8")
    return path


def _generate(
    tmp_path: Path,
    backend: MockBackend | None = None,
    *,
    max_rounds: int = 3,
    home: str | Path | None = None,
) -> SpecOutcome:
    return generate_spec(
        repo_id="demo",
        doc_path=_doc(tmp_path),
        spec_id="F-001",
        backend=backend or MockBackend(),
        facts=_facts(),
        base_dir=home or tmp_path / "home",
        max_rounds=max_rounds,
        backend_name="mock",
        model_name="mock-v1",
    )


def test_mock_pipeline_end_to_end(tmp_path: Path) -> None:
    outcome = _generate(tmp_path)
    spec = outcome.spec
    assert spec.spec_id == "F-001"
    assert spec.snapshot_id == SNAP
    assert [section.title for section in spec.sections] == [
        SPEC_SECTIONS[number] for number in range(1, 12)
    ]
    assert spec.validation is not None
    assert spec.validation.blockers == []

    requirements = [item for item in spec.requirements if item.kind is RequirementKind.FUNCTIONAL]
    assert len(requirements) == 2
    assert len(spec.tasks) == len(spec.requirements)
    for task in spec.tasks:
        assert len(task.files) <= 3
        assert task.estimated_lines <= 300
    assert len(spec.decisions) == 1
    assert len(spec.decisions[0].candidates) >= 2
    names = {candidate.name for candidate in spec.decisions[0].candidates}
    assert spec.decisions[0].choice in names

    traced = [row.requirement_id for row in spec.trace]
    assert sorted(traced) == sorted(item.id for item in spec.requirements)

    directory = outcome.directory
    for name in ("spec.json", "spec.md", "decisions.md", "validation.json", "evidence.json"):
        assert (directory / name).is_file(), name
    assert "## 11 追溯矩阵" in (directory / "spec.md").read_text(encoding="utf-8")


def test_repair_loop_retries_after_missing_section(tmp_path: Path) -> None:
    backend = MockBackend(failures=[{"attempt": 1, "kind": FAIL_SPEC_MISSING_SECTION}])
    outcome = _generate(tmp_path, backend)
    assert outcome.attempts == 2
    assert outcome.spec.validation is not None
    assert outcome.spec.validation.blockers == []


def test_repair_loop_retries_after_oversized_task(tmp_path: Path) -> None:
    backend = MockBackend(failures=[{"attempt": 1, "kind": FAIL_SPEC_BAD_TASK}])
    outcome = _generate(tmp_path, backend)
    assert outcome.attempts == 2
    assert all(len(task.files) <= 3 for task in outcome.spec.tasks)


def test_repair_loop_exhausted_keeps_draft(tmp_path: Path) -> None:
    backend = MockBackend(failures=[{"attempt": 1, "kind": FAIL_SPEC_MISSING_SECTION}])
    with pytest.raises(InputError, match="仍未通过结构校验"):
        _generate(tmp_path, backend, max_rounds=1)
    directory = specs_root(tmp_path / "home") / "demo" / "F-001"
    assert (directory / "draft-task" / "output" / "draft.json").is_file()
    errors = json.loads((directory / "validation-errors.json").read_text(encoding="utf-8"))
    assert errors["attempts"] == 1
    assert errors["errors"]
    assert not (directory / "spec.json").exists()


def test_same_input_is_byte_stable(tmp_path: Path) -> None:
    first = _generate(tmp_path, home=tmp_path / "a")
    second = _generate(tmp_path, home=tmp_path / "b")
    assert (first.directory / "spec.json").read_bytes() == (
        second.directory / "spec.json"
    ).read_bytes()
    assert (first.directory / "evidence.json").read_bytes() == (
        second.directory / "evidence.json"
    ).read_bytes()


def _env(home: Path) -> dict[str, str]:
    repo_root = Path(__file__).resolve().parents[1]
    return {
        **os.environ,
        "CODINGWIKI_HOME": str(home),
        "PYTHONPATH": str(repo_root / "src"),
    }


def test_cli_spec_generate_end_to_end(tmp_path: Path) -> None:
    home = tmp_path / ".codingwiki"
    FilesystemFactStore(base_dir=home).save(_facts())
    doc = _doc(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "codingwiki",
            "spec",
            "generate",
            "--repo",
            "demo",
            "--doc",
            str(doc),
            "--id",
            "F-001",
            "--backend",
            "mock",
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        env=_env(home),
    )
    assert result.returncode == 0, result.stderr
    assert "Spec: id=F-001" in result.stdout
    assert "校验: blockers=0" in result.stdout
    directory = specs_root(home) / "demo" / "F-001"
    payload = json.loads((directory / "spec.json").read_text(encoding="utf-8"))
    assert payload["snapshot_id"] == SNAP
    assert len(payload["sections"]) == 11
    assert payload["validation"]["blockers"] == []
