"""配置加载与校验的单元测试。

覆盖三类用例：
- 正例：正常配置能加载，默认值符合约定；
- 反例：非法配置被拒绝，且错误信息指出具体字段或原因；
- 边界例：上限/下限、空文件、YAML 1.1 布尔词等容易出错的地方。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codingwiki.config import load_config, parse_config
from codingwiki.errors import ConfigFormatError, ConfigNotFoundError, ConfigValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]

MINIMAL_CONFIG = """
config_version: "1.0"
embedding:
  model_path: ".cache/models/bge-m3"
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 正例
# ---------------------------------------------------------------------------


def test_minimal_config_uses_documented_defaults() -> None:
    config = parse_config(MINIMAL_CONFIG)

    assert config.id == "default"
    assert config.model.backend.value == "cline"
    assert config.model.gauss.enabled is False
    assert config.model.cline.timeout_s == 1800
    assert config.embedding.device == "auto"
    assert config.embedding.dimension == 1024
    assert config.reranker.enabled is False
    assert config.retrieval.top_k == 20


def test_repository_default_config_is_valid() -> None:
    config = load_config(REPO_ROOT / "configs" / "default.yaml")

    assert config.id == "default"
    assert config.model.backend.value == "cline"
    assert config.model.gauss.max_requests_per_minute == 30


def test_yml_suffix_is_accepted(tmp_path: Path) -> None:
    path = _write(tmp_path, "config.yml", MINIMAL_CONFIG)

    assert load_config(path).config_version == "1.0"


def test_gauss_enabled_with_required_fields_is_valid(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "config.yaml",
        """
config_version: "1.0"
model:
  backend: gauss
  gauss:
    enabled: true
    base_url: "http://gauss.internal/v1"
    model: "gauss-model-v1"
embedding:
  model_path: ".cache/models/bge-m3"
""",
    )

    config = load_config(path)

    assert config.model.gauss.enabled is True
    assert config.model.gauss.api_key_env == "CODINGWIKI_GAUSS_KEY"


def test_reranker_enabled_with_path_is_valid(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "config.yaml",
        """
config_version: "1.0"
embedding:
  model_path: ".cache/models/bge-m3"
reranker:
  enabled: true
  model_path: ".cache/models/bge-reranker-v2-m3"
""",
    )

    assert load_config(path).reranker.enabled is True


# ---------------------------------------------------------------------------
# 反例：格式类错误
# ---------------------------------------------------------------------------


def test_json_suffix_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "config.json", MINIMAL_CONFIG)

    with pytest.raises(ConfigFormatError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert ".yaml" in message
    assert ".json" in message


def test_missing_file_reports_path(tmp_path: Path) -> None:
    missing = tmp_path / "not-exists.yaml"

    with pytest.raises(ConfigNotFoundError) as excinfo:
        load_config(missing)

    assert "不存在" in str(excinfo.value)
    assert str(missing) in str(excinfo.value)


def test_directory_is_not_a_config_file(tmp_path: Path) -> None:
    directory = tmp_path / "configs.yaml"
    directory.mkdir()

    with pytest.raises(ConfigFormatError) as excinfo:
        load_config(directory)

    assert "不是文件" in str(excinfo.value)


def test_empty_file_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "config.yaml", "   \n\n")

    with pytest.raises(ConfigFormatError) as excinfo:
        load_config(path)

    assert "为空" in str(excinfo.value)


def test_invalid_yaml_syntax_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "config.yaml", 'config_version: "1.0\nembedding: [\n')

    with pytest.raises(ConfigFormatError) as excinfo:
        load_config(path)

    assert "YAML 语法错误" in str(excinfo.value)


def test_top_level_list_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "config.yaml", "- config_version: '1.0'\n")

    with pytest.raises(ConfigFormatError) as excinfo:
        load_config(path)

    assert "顶层" in str(excinfo.value)


def test_duplicate_top_level_key_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "config.yaml",
        """
config_version: "1.0"
config_version: "1.1"
embedding:
  model_path: ".cache/models/bge-m3"
""",
    )

    with pytest.raises(ConfigFormatError) as excinfo:
        load_config(path)

    assert "重复" in str(excinfo.value)
    assert "config_version" in str(excinfo.value)


def test_duplicate_nested_key_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "config.yaml",
        """
config_version: "1.0"
embedding:
  model_path: ".cache/models/bge-m3"
  model_path: ".cache/models/other"
""",
    )

    with pytest.raises(ConfigFormatError) as excinfo:
        load_config(path)

    assert "model_path" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 反例：校验类错误
# ---------------------------------------------------------------------------


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "config.yaml",
        """
config_version: "1.0"
embedding:
  model_path: ".cache/models/bge-m3"
unknown_section: true
""",
    )

    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert "unknown_section" in message
    assert "配置校验失败" in message


def test_missing_config_version_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "config.yaml",
        """
embedding:
  model_path: ".cache/models/bge-m3"
""",
    )

    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)

    assert "config_version" in str(excinfo.value)


def test_empty_model_path_means_autodiscovery(tmp_path: Path) -> None:
    """空 model_path 表示运行时自动解析模型目录，配置层不报错。"""
    path = _write(tmp_path, "config.yaml", 'config_version: "1.0"\nembedding: {}\n')

    config = load_config(path)

    assert config.embedding.model_path == ""
    assert config.embedding.backend == "local"


def test_gauss_backend_requires_gauss_enabled(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "config.yaml",
        """
config_version: "1.0"
model:
  backend: gauss
  gauss:
    enabled: false
embedding:
  model_path: ".cache/models/bge-m3"
""",
    )

    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)

    assert "gauss" in str(excinfo.value)


def test_api_backend_requires_enabled_and_fields(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "config.yaml",
        """
config_version: "1.0"
model:
  backend: api
embedding:
  model_path: ".cache/models/bge-m3"
""",
    )

    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)

    assert "api" in str(excinfo.value)


def test_api_generation_defaults_are_safe_for_long_pages(tmp_path: Path) -> None:
    """默认 max_tokens 必须 ≥16384，避免页面 JSON 在 8192 处截断。"""
    path = _write(
        tmp_path,
        "config.yaml",
        """
config_version: "1.0"
model:
  backend: api
  api:
    enabled: true
    base_url: "http://127.0.0.1:9999"
    model: "stub-model"
embedding:
  model_path: ".cache/models/bge-m3"
""",
    )

    api = load_config(path).model.api

    assert api.timeout_s == 300
    assert api.max_retries == 3
    assert api.temperature == 0.2
    assert api.max_tokens == 16_384


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_s", 0),
        ("max_retries", -1),
        ("max_retries", 11),
        ("temperature", -0.1),
        ("temperature", 2.1),
        ("max_tokens", 8192),
        ("max_tokens", 131_073),
    ],
)
def test_api_generation_bounds_are_enforced(tmp_path: Path, field: str, value: int | float) -> None:
    path = _write(
        tmp_path,
        "config.yaml",
        f"""
config_version: "1.0"
model:
  backend: api
  api:
    enabled: true
    base_url: "http://127.0.0.1:9999"
    model: "stub-model"
    {field}: {value}
embedding:
  model_path: ".cache/models/bge-m3"
""",
    )

    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)

    assert field in str(excinfo.value)


def test_api_max_tokens_floor_is_accepted(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "config.yaml",
        """
config_version: "1.0"
model:
  backend: api
  api:
    enabled: true
    base_url: "http://127.0.0.1:9999"
    model: "stub-model"
    max_tokens: 16384
embedding:
  model_path: ".cache/models/bge-m3"
""",
    )

    assert load_config(path).model.api.max_tokens == 16_384


def test_reranker_enabled_with_autodiscovery_path(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "config.yaml",
        """
config_version: "1.0"
embedding:
  model_path: ".cache/models/bge-m3"
reranker:
  enabled: true
""",
    )

    config = load_config(path)

    assert config.reranker.enabled is True
    assert config.reranker.model_path == ""


def test_retrieval_weights_both_zero_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "config.yaml",
        """
config_version: "1.0"
embedding:
  model_path: ".cache/models/bge-m3"
retrieval:
  keyword_weight: 0
  vector_weight: 0
""",
    )

    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)

    assert "keyword_weight" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 边界例
# ---------------------------------------------------------------------------


def test_gauss_rate_limit_at_company_cap_is_accepted(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "config.yaml",
        """
config_version: "1.0"
model:
  gauss:
    max_requests_per_minute: 30
embedding:
  model_path: ".cache/models/bge-m3"
""",
    )

    assert load_config(path).model.gauss.max_requests_per_minute == 30


def test_gauss_rate_limit_above_company_cap_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "config.yaml",
        """
config_version: "1.0"
model:
  gauss:
    max_requests_per_minute: 31
embedding:
  model_path: ".cache/models/bge-m3"
""",
    )

    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert "max_requests_per_minute" in message
    assert "30" in message


def test_gauss_rate_limit_zero_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "config.yaml",
        """
config_version: "1.0"
model:
  gauss:
    max_requests_per_minute: 0
embedding:
  model_path: ".cache/models/bge-m3"
""",
    )

    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)

    assert "max_requests_per_minute" in str(excinfo.value)


def test_cline_timeout_zero_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "config.yaml",
        """
config_version: "1.0"
model:
  cline:
    timeout_s: 0
embedding:
  model_path: ".cache/models/bge-m3"
""",
    )

    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)

    assert "timeout_s" in str(excinfo.value)


def test_cline_timeout_one_second_is_accepted(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "config.yaml",
        """
config_version: "1.0"
model:
  cline:
    timeout_s: 1
embedding:
  model_path: ".cache/models/bge-m3"
""",
    )

    assert load_config(path).model.cline.timeout_s == 1


@pytest.mark.parametrize("dimension", [1, 8192])
def test_embedding_dimension_bounds_are_accepted(tmp_path: Path, dimension: int) -> None:
    path = _write(
        tmp_path,
        "config.yaml",
        f"""
config_version: "1.0"
embedding:
  model_path: ".cache/models/bge-m3"
  dimension: {dimension}
""",
    )

    assert load_config(path).embedding.dimension == dimension


@pytest.mark.parametrize("dimension", [0, 8193])
def test_embedding_dimension_out_of_range_is_rejected(tmp_path: Path, dimension: int) -> None:
    path = _write(
        tmp_path,
        "config.yaml",
        f"""
config_version: "1.0"
embedding:
  model_path: ".cache/models/bge-m3"
  dimension: {dimension}
""",
    )

    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)

    assert "dimension" in str(excinfo.value)


@pytest.mark.parametrize("version", ["1", "1.0.0", "v1.0", ""])
def test_config_version_format_is_enforced(tmp_path: Path, version: str) -> None:
    path = _write(
        tmp_path,
        "config.yaml",
        f"""
config_version: "{version}"
embedding:
  model_path: ".cache/models/bge-m3"
""",
    )

    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)

    assert "config_version" in str(excinfo.value)


def test_yaml11_boolean_word_is_not_silently_converted(tmp_path: Path) -> None:
    """`yes` 在 YAML 1.1 里是 True；本项目要求显式 true/false，避免含义漂移。"""
    path = _write(
        tmp_path,
        "config.yaml",
        """
config_version: "1.0"
model:
  cline:
    telemetry: yes
embedding:
  model_path: ".cache/models/bge-m3"
""",
    )

    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)

    assert "telemetry" in str(excinfo.value)
