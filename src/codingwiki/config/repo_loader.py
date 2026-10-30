"""仓库级配置加载。

与 S1 的 config/loader.py 平行：复用 StrictLoader（YAML only、安全加载、
重复键报错、禁用 YAML 1.1 布尔词），但返回 RepoConfig 而不是 AppConfig。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from codingwiki.config.loader import ALLOWED_SUFFIXES, StrictLoader, _format_validation_error
from codingwiki.domain.models import RepoConfig
from codingwiki.errors import ConfigFormatError, ConfigNotFoundError, ConfigValidationError


def load_repo_config(path: str | Path) -> RepoConfig:
    """加载单个仓库 YAML 并校验。

    与 load_config 分离：仓库配置模型不同、必填项不同，
    错误信息里明确说明是哪个仓库。
    """
    p = Path(path)
    if not p.exists():
        raise ConfigNotFoundError(f"仓库配置文件不存在: {p}")
    if not p.is_file():
        raise ConfigNotFoundError(f"仓库配置路径不是文件: {p}")
    suffix = p.suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ConfigFormatError(f"仓库配置必须是 .yaml 或 .yml（收到 {suffix}）: {p}")

    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigFormatError(f"仓库配置不是 UTF-8: {p}") from exc

    if not text.strip():
        raise ConfigFormatError(f"仓库配置为空: {p}")

    try:
        data = yaml.load(text, Loader=StrictLoader)
    except yaml.YAMLError as exc:
        raise ConfigFormatError(f"仓库配置 YAML 语法错误: {p}: {exc}") from exc
    except ConfigFormatError:
        raise

    if not isinstance(data, dict):
        raise ConfigFormatError(f"仓库配置顶层必须是键值对: {p}")

    try:
        return RepoConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigValidationError(_format_validation_error(exc, str(p))) from exc


def load_all_repo_configs(directory: str | Path) -> list[RepoConfig]:
    """扫描目录下所有 .yaml/.yml，按文件名排序后逐个加载。"""
    d = Path(directory)
    if not d.is_dir():
        raise ConfigNotFoundError(f"仓库配置目录不存在或不是目录: {d}")

    files = sorted([f for f in d.iterdir() if f.is_file() and f.suffix in ALLOWED_SUFFIXES])
    results: list[RepoConfig] = []
    seen_ids: set[str] = set()
    for f in files:
        cfg = load_repo_config(f)
        if cfg.repo_id in seen_ids:
            raise ConfigValidationError(f"仓库 ID 重复: {cfg.repo_id}（文件 {f}）")
        seen_ids.add(cfg.repo_id)
        results.append(cfg)
    return results


__all__ = ["load_all_repo_configs", "load_repo_config"]
