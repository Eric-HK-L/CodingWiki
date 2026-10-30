"""配置文件加载。

只做三件事：检查文件、按 YAML 安全解析、交给 Pydantic 校验。
任何一步失败都抛出带原因的错误，不返回半成品对象。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from codingwiki.config.models import AppConfig
from codingwiki.errors import ConfigFormatError, ConfigNotFoundError, ConfigValidationError

ALLOWED_SUFFIXES = frozenset({".yaml", ".yml"})


class _StrictLoader(yaml.SafeLoader):
    """安全加载器：重复键报错；布尔只认 true/false（YAML 1.2 语义）。"""


# 去掉 YAML 1.1 的隐式布尔词（yes/no/on/off），只保留 true/false，
# 避免 `telemetry: no` 被悄悄解析成 False 或字符串，导致配置含义漂移。
_StrictLoader.yaml_implicit_resolvers = {
    key: [pair for pair in pairs if pair[0] != "tag:yaml.org,2002:bool"]
    for key, pairs in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_StrictLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def _construct_mapping(
    loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError:
            raise ConfigFormatError(f"配置键必须是标量，不能是: {key!r}") from None
        if key in mapping:
            raise ConfigFormatError(f"配置键重复: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def load_config(path: str | Path) -> AppConfig:
    """读取一个 YAML 配置文件并校验。"""
    file_path = Path(path)
    if not file_path.exists():
        raise ConfigNotFoundError(f"配置文件不存在: {file_path}")
    if not file_path.is_file():
        raise ConfigFormatError(f"配置路径不是文件: {file_path}")

    suffix = file_path.suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        shown = suffix if suffix else "无后缀"
        raise ConfigFormatError(f"配置文件必须是 .yaml 或 .yml（收到 {shown}）: {file_path}")

    try:
        text = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigFormatError(f"配置文件不是 UTF-8 文本: {file_path}") from exc
    except OSError as exc:
        raise ConfigFormatError(f"无法读取配置文件: {file_path}: {exc}") from exc

    return parse_config(text, source=str(file_path))


def parse_config(text: str, *, source: str = "<string>") -> AppConfig:
    """把 YAML 文本解析成 AppConfig；主要用于测试和内存中的配置。"""
    if not text.strip():
        raise ConfigFormatError(f"配置文件为空: {source}")

    try:
        data = yaml.load(text, Loader=_StrictLoader)
    except ConfigFormatError:
        raise
    except yaml.YAMLError as exc:
        raise ConfigFormatError(f"YAML 语法错误: {source}: {_first_line(exc)}") from exc

    if not isinstance(data, dict):
        raise ConfigFormatError(f"配置顶层必须是键值对: {source}")

    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigValidationError(_format_validation_error(exc, source)) from exc


def _first_line(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__


def _format_validation_error(exc: ValidationError, source: str) -> str:
    lines = [f"配置校验失败: {source}"]
    for item in exc.errors():
        location = ".".join(str(part) for part in item["loc"]) or "<顶层>"
        lines.append(f"  - {location}: {item['msg']}")
    return "\n".join(lines)


__all__ = ["ALLOWED_SUFFIXES", "load_config", "parse_config"]
