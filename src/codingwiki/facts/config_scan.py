"""配置发现：配置文件键 + 代码里的环境变量/命令行参数读取（计划文档 8.3、8.7）。

配置文件只记录叶子键（容器键没有独立意义）；代码里的读取点记录 file:line。
解析失败不抛异常，只累加 skipped 计数，由质量报告说明。
"""

from __future__ import annotations

import json
import tomllib
from typing import Any

import yaml

from codingwiki.domain.facts import Confidence, ConfigFact
from codingwiki.facts.symbols import scan_env_reads

_CONFIG_LANGUAGES = frozenset({"yaml", "json", "toml"})
_CODE_LANGUAGES = frozenset({"c", "cpp", "cuda", "python", "shell"})

_MAX_DEPTH = 8
_MAX_KEYS_PER_FILE = 2000


def scan_config(
    text_files: list[tuple[str, str, str]],
) -> tuple[list[ConfigFact], int]:
    """text_files: (相对路径, 语言, 文本内容)。返回 (配置事实, 解析失败文件数)。"""
    facts: list[ConfigFact] = []
    skipped = 0
    for rel_path, language, text in text_files:
        if language in _CONFIG_LANGUAGES:
            keys = _file_keys(text, language)
            if keys is None:
                skipped += 1
                continue
            facts.extend(
                ConfigFact(
                    key=key,
                    kind="file",
                    format=language,
                    path=rel_path,
                    confidence=Confidence.LOW,
                )
                for key in keys
            )
        elif language in _CODE_LANGUAGES:
            for key, line, reader in scan_env_reads(text, rel_path):
                kind = "arg" if reader == "argparse" else "env"
                facts.append(
                    ConfigFact(
                        key=key,
                        kind=kind,
                        format="code",
                        path=rel_path,
                        line=line,
                        reader=reader,
                        confidence=Confidence.LOW,
                    )
                )
    return facts, skipped


def _file_keys(text: str, language: str) -> list[str] | None:
    """解析配置文本并展开成叶子键；格式错误返回 None。"""
    try:
        if language == "yaml":
            data: Any = yaml.safe_load(text)
        elif language == "toml":
            data = tomllib.loads(text)
        else:
            data = json.loads(text)
    except (yaml.YAMLError, tomllib.TOMLDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    keys: list[str] = []
    _flatten(data, "", keys, 0)
    return sorted(set(keys))[:_MAX_KEYS_PER_FILE]


def _flatten(value: Any, prefix: str, out: list[str], depth: int) -> None:
    if depth > _MAX_DEPTH or len(out) > _MAX_KEYS_PER_FILE:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(child, (dict, list)):
                _flatten(child, name, out, depth + 1)
            else:
                out.append(name)
    elif isinstance(value, list):
        if prefix and not value:
            out.append(prefix)
        for child in value:
            if isinstance(child, (dict, list)):
                _flatten(child, prefix, out, depth + 1)
            elif prefix:
                out.append(prefix)


__all__ = ["scan_config"]
