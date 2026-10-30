"""配置发现单元测试：YAML/JSON/TOML 键 + 环境变量/参数读取。"""

from __future__ import annotations

from codingwiki.domain.facts import Confidence
from codingwiki.facts.config_scan import scan_config

YAML_BODY = """
engine:
  name: demo
  threads: 4
  features:
    - turbo
logging:
  level: info
"""

JSON_BODY = '{"engine": {"name": "demo", "threads": 4}, "flags": ["a", "b"]}'

TOML_BODY = """
[engine]
name = "demo"
threads = 4

[logging]
level = "info"
"""

CODE_BODY = """
level = os.getenv("CW_LEVEL", "info")
mode = os.environ["CW_MODE"]
"""


def _keys(facts: list[object]) -> list[str]:
    from codingwiki.domain.facts import ConfigFact

    return sorted({fact.key for fact in facts if isinstance(fact, ConfigFact)})


def test_yaml_keys_are_flattened_to_leaves() -> None:
    facts, skipped = scan_config([("config.yaml", "yaml", YAML_BODY)])
    assert skipped == 0
    assert _keys(facts) == ["engine.features", "engine.name", "engine.threads", "logging.level"]
    assert all(fact.format == "yaml" for fact in facts)
    assert all(fact.confidence is Confidence.LOW for fact in facts)


def test_json_keys_are_flattened() -> None:
    facts, skipped = scan_config([("config.json", "json", JSON_BODY)])
    assert skipped == 0
    assert _keys(facts) == ["engine.name", "engine.threads", "flags"]


def test_toml_keys_are_flattened() -> None:
    facts, skipped = scan_config([("config.toml", "toml", TOML_BODY)])
    assert skipped == 0
    assert _keys(facts) == ["engine.name", "engine.threads", "logging.level"]


def test_broken_config_file_is_counted_not_raised() -> None:
    facts, skipped = scan_config([("bad.yaml", "yaml", "a: [\n")])
    assert facts == []
    assert skipped == 1


def test_non_mapping_config_is_ignored() -> None:
    facts, skipped = scan_config([("notes.yaml", "yaml", "- just\n- a\n- list\n")])
    assert skipped == 1
    assert facts == []


def test_code_env_reads_carry_location() -> None:
    facts, skipped = scan_config([("src/settings.py", "python", CODE_BODY)])
    assert skipped == 0
    env = [fact for fact in facts if fact.kind == "env"]
    assert sorted(fact.key for fact in env) == ["CW_LEVEL", "CW_MODE"]
    assert all(fact.line >= 1 for fact in env)
    assert all(fact.path == "src/settings.py" for fact in env)


def test_empty_config_text_is_skipped() -> None:
    facts, skipped = scan_config([("empty.yaml", "yaml", "")])
    assert facts == []
    assert skipped == 1
