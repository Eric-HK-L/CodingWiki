"""结构化日志测试。

要求（架构文档第 2、8 节）：
- 一条日志一行 JSON，能被脚本直接解析；
- 默认写 stderr，不污染命令行的正常输出；
- 重复配置不叠加处理器，否则同一行会打印多次。
"""

from __future__ import annotations

import io
import json
import logging

from codingwiki.logging_setup import JsonFormatter, configure_logging, get_logger


def _lines(stream: io.StringIO) -> list[str]:
    return [line for line in stream.getvalue().splitlines() if line]


def test_log_line_is_single_json_object() -> None:
    stream = io.StringIO()
    logger = configure_logging(level="INFO", stream=stream)

    logger.info("快照创建完成")

    lines = _lines(stream)
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["level"] == "INFO"
    assert payload["logger"] == "codingwiki"
    assert payload["message"] == "快照创建完成"
    assert "ts" in payload


def test_extra_fields_are_included() -> None:
    stream = io.StringIO()
    logger = configure_logging(level="INFO", stream=stream)

    logger.info("发布完成", extra={"snapshot_id": "abc123", "pages": 12})

    payload = json.loads(_lines(stream)[0])
    assert payload["snapshot_id"] == "abc123"
    assert payload["pages"] == 12


def test_level_filter_suppresses_debug() -> None:
    stream = io.StringIO()
    logger = configure_logging(level="INFO", stream=stream)

    logger.debug("不应输出")

    assert _lines(stream) == []


def test_reconfigure_does_not_duplicate_handlers() -> None:
    first = io.StringIO()
    second = io.StringIO()
    configure_logging(level="INFO", stream=first)
    logger = configure_logging(level="INFO", stream=second)

    logger.info("只应写一次")

    assert _lines(first) == []
    assert len(_lines(second)) == 1
    assert len(logger.handlers) == 1


def test_exception_is_rendered_into_payload() -> None:
    stream = io.StringIO()
    logger = configure_logging(level="INFO", stream=stream)

    try:
        raise ValueError("坏配置")
    except ValueError:
        logger.exception("处理失败")

    payload = json.loads(_lines(stream)[0])
    assert "Traceback" in payload["exception"]
    assert "坏配置" in payload["exception"]


def test_get_logger_is_namespaced_under_codingwiki() -> None:
    assert get_logger().name == "codingwiki"
    assert get_logger("facts").name == "codingwiki.facts"


def test_formatter_outputs_ascii_false() -> None:
    # 反例：中文不能变成 \uXXXX 转义，否则日志难以直接阅读。
    record = logging.LogRecord(
        name="codingwiki",
        level=logging.INFO,
        pathname="x.py",
        lineno=1,
        msg="中文消息",
        args=(),
        exc_info=None,
    )

    assert "中文消息" in JsonFormatter().format(record)
