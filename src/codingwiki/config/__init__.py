"""配置包：对外只暴露模型、加载函数和允许的后缀集合。"""

from codingwiki.config.loader import ALLOWED_SUFFIXES, load_config, parse_config
from codingwiki.config.models import AppConfig

__all__ = ["ALLOWED_SUFFIXES", "AppConfig", "load_config", "parse_config"]
