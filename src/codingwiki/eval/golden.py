"""金标准集加载（计划文档 10.9.1）。

复用 config 层的 StrictLoader（重复键报错、YAML 1.1 布尔词禁用），
只换目标模型为 GoldenSet；错误翻译成 InputError 家族。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from codingwiki.config.loader import ALLOWED_SUFFIXES, StrictLoader
from codingwiki.domain.eval import GoldenSet
from codingwiki.errors import ConfigFormatError, ConfigNotFoundError, ConfigValidationError


def load_golden(path: str | Path) -> GoldenSet:
    """读取一份金标准集 YAML 并校验。"""
    file_path = Path(path)
    if not file_path.exists():
        raise ConfigNotFoundError(f"金标准集文件不存在: {file_path}")
    if not file_path.is_file():
        raise ConfigFormatError(f"金标准集路径不是文件: {file_path}")
    if file_path.suffix.lower() not in ALLOWED_SUFFIXES:
        raise ConfigFormatError(f"金标准集必须是 .yaml/.yml: {file_path}")

    text = file_path.read_text(encoding="utf-8")
    if not text.strip():
        raise ConfigFormatError(f"金标准集为空: {file_path}")
    try:
        data = yaml.load(text, Loader=StrictLoader)
    except yaml.YAMLError as exc:
        raise ConfigFormatError(f"金标准集 YAML 语法错误: {file_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigFormatError(f"金标准集顶层必须是键值对: {file_path}")

    try:
        return GoldenSet.model_validate(data)
    except ValidationError as exc:
        raise ConfigValidationError(f"金标准集校验失败: {file_path}: {exc}") from exc


__all__ = ["load_golden"]
