"""仓库级配置加载测试。

策略：复用 S1 的 StrictLoader 做 YAML 解析，RepoConfig 做业务校验。
正反例边界例和 test_config.py 一致：合法最小/完整配置、非法后缀、缺必填、
provider 枚举、重复 ID、未知字段。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codingwiki.config.repo_loader import load_all_repo_configs, load_repo_config
from codingwiki.domain.models import RepoType
from codingwiki.errors import ConfigFormatError, ConfigNotFoundError, ConfigValidationError


def _write(tmp: Path, name: str, content: str) -> Path:
    p = tmp / name
    p.write_text(content, encoding="utf-8")
    return p


def _valid_content(**overrides) -> str:
    base = {
        "repo_id": "demo",
        "provider": "git",
        "url": "https://github.com/demo/demo.git",
    }
    base.update(overrides)
    return "\n".join(f"{k}: {v}" for k, v in base.items())


# ---------------------------------------------------------------- 正例


def test_minimal_valid_repo_config(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo.yaml", _valid_content())

    cfg = load_repo_config(path)

    assert cfg.repo_id == "demo"
    assert cfg.provider is RepoType.GIT
    assert cfg.branch == "main"
    assert cfg.revision == "latest"
    assert cfg.max_text_file_size_kb == 2048


def test_github_provider_is_accepted(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo.yaml", _valid_content(provider="github"))

    cfg = load_repo_config(path)

    assert cfg.provider is RepoType.GITHUB


def test_p4_provider_is_parsed(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "demo.yaml",
        """
repo_id: demo
provider: p4
url: //depot/vran/main/...
""",
    )

    cfg = load_repo_config(path)

    assert cfg.provider is RepoType.P4


def test_all_fields_are_parsed(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "demo.yaml",
        """
repo_id: demo
provider: git
url: git@github.com:demo/demo.git
branch: dev
revision: abc123
root: src
excludes:
  - "test/dump/**"
max_text_file_size_kb: 4096
language_overrides:
  ".proto": protobuf
""",
    )

    cfg = load_repo_config(path)

    assert cfg.branch == "dev"
    assert cfg.revision == "abc123"
    assert cfg.root == "src"
    assert cfg.excludes == ["test/dump/**"]
    assert cfg.max_text_file_size_kb == 4096
    assert cfg.language_overrides[".proto"] == "protobuf"


def test_yml_suffix_is_accepted(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo.yml", _valid_content())

    cfg = load_repo_config(path)

    assert cfg.repo_id == "demo"


def test_load_all_repo_configs(tmp_path: Path) -> None:
    d = tmp_path / "repos"
    d.mkdir()
    (d / "a.yaml").write_text(_valid_content(repo_id="repo-a"), encoding="utf-8")
    (d / "b.yaml").write_text(_valid_content(repo_id="repo-b"), encoding="utf-8")

    results = load_all_repo_configs(d)

    assert {r.repo_id for r in results} == {"repo-a", "repo-b"}


# ---------------------------------------------------------------- 反例


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigNotFoundError):
        load_repo_config(tmp_path / "nope.yaml")


def test_path_is_directory_is_rejected(tmp_path: Path) -> None:
    d = tmp_path / "dir"
    d.mkdir()

    with pytest.raises(ConfigNotFoundError):
        load_repo_config(d)


def test_json_suffix_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo.json", _valid_content())

    with pytest.raises(ConfigFormatError) as excinfo:
        load_repo_config(path)

    assert ".json" in str(excinfo.value)


def test_empty_file_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo.yaml", "")

    with pytest.raises(ConfigFormatError):
        load_repo_config(path)


def test_yaml_syntax_error_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo.yaml", "repo_id: [unterminated\n")

    with pytest.raises(ConfigFormatError):
        load_repo_config(path)


def test_top_level_not_mapping_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo.yaml", "- item\n- item2\n")

    with pytest.raises(ConfigFormatError):
        load_repo_config(path)


def test_missing_repo_id_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "demo.yaml",
        """
provider: git
url: https://github.com/demo/demo.git
""",
    )

    with pytest.raises(ConfigValidationError) as excinfo:
        load_repo_config(path)

    assert "repo_id" in str(excinfo.value)


def test_missing_url_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "demo.yaml",
        """
repo_id: demo
provider: git
""",
    )

    with pytest.raises(ConfigValidationError) as excinfo:
        load_repo_config(path)

    assert "url" in str(excinfo.value)


def test_invalid_provider_value_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo.yaml", _valid_content(provider="svn"))

    with pytest.raises(ConfigValidationError) as excinfo:
        load_repo_config(path)

    assert "provider" in str(excinfo.value)


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "demo.yaml",
        f"""
{_valid_content()}
typo_field: yes
""",
    )

    with pytest.raises(ConfigValidationError) as excinfo:
        load_repo_config(path)

    assert "typo_field" in str(excinfo.value)


def test_duplicate_key_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "demo.yaml",
        f"""
{_valid_content()}
repo_id: again
""",
    )

    with pytest.raises(ConfigFormatError) as excinfo:
        load_repo_config(path)

    assert "重复" in str(excinfo.value)


def test_duplicate_repo_id_across_files_is_rejected(tmp_path: Path) -> None:
    d = tmp_path / "repos"
    d.mkdir()
    (d / "a.yaml").write_text(_valid_content(repo_id="dup"), encoding="utf-8")
    (d / "b.yaml").write_text(_valid_content(repo_id="dup"), encoding="utf-8")

    with pytest.raises(ConfigValidationError) as excinfo:
        load_all_repo_configs(d)

    assert "repo_id" in str(excinfo.value)


def test_yes_no_boolean_word_is_rejected(tmp_path: Path) -> None:
    # YAML 1.1 词 no 不能被静默当 false
    path = _write(
        tmp_path,
        "demo.yaml",
        f"""
{_valid_content()}
binary_extensions: no
""",
    )

    # "no" 不是合法的 list，会在校验阶段被拒，而不是静默当布尔
    with pytest.raises(ConfigValidationError):
        load_repo_config(path)


# ---------------------------------------------------------------- 边界例


def test_max_text_size_boundaries(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo_ok.yaml", _valid_content(max_text_file_size_kb="1"))
    cfg = load_repo_config(path)
    assert cfg.max_text_file_size_kb == 1

    path = _write(tmp_path, "demo_512m.yaml", _valid_content(max_text_file_size_kb="524288"))
    cfg = load_repo_config(path)
    assert cfg.max_text_file_size_kb == 524288


def test_revision_literal_latest_is_default(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo.yaml", _valid_content())

    cfg = load_repo_config(path)

    assert cfg.revision == "latest"


def test_repo_id_must_match_pattern(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo.yaml", _valid_content(repo_id="AB_BAD"))

    with pytest.raises(ConfigValidationError):
        load_repo_config(path)
