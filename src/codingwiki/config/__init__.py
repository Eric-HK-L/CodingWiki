"""配置层对外导出。

这里只导出 loader 函数和 StrictLoader。
AppConfig 在 config/models.py，仓库级加载从这里走 load_repo_config / load_all_repo_configs。
"""

from __future__ import annotations

from codingwiki.config.loader import (
    ALLOWED_SUFFIXES,
    StrictLoader,
    load_config,
    parse_config,
)
from codingwiki.config.repo_loader import load_all_repo_configs, load_repo_config

__all__ = [
    "ALLOWED_SUFFIXES",
    "StrictLoader",
    "load_all_repo_configs",
    "load_config",
    "load_repo_config",
    "parse_config",
]
