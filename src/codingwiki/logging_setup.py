"""结构化日志。

所有日志输出为一行一个 JSON 对象，方便脚本与日志系统解析；
默认写 stderr，避免污染命令行的正常输出。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

_LOGGER_NAME = "codingwiki"
_RESERVED_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """把日志记录格式化成单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO", *, stream: TextIO | None = None) -> logging.Logger:
    """配置项目根日志器；重复调用会替换处理器，不会叠加输出。"""
    logger = logging.getLogger(_LOGGER_NAME)
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(level.upper())

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger


def get_logger(name: str = "") -> logging.Logger:
    """取得子日志器，名字统一挂在 codingwiki 之下。"""
    suffix = f".{name}" if name else ""
    return logging.getLogger(f"{_LOGGER_NAME}{suffix}")


__all__ = ["JsonFormatter", "configure_logging", "get_logger"]
