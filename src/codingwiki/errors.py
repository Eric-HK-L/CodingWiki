"""异常分类与退出码。

设计见 docs/架构设计/阶段1-总体架构.md 第 6 节：
异常按“用户输入 / 环境 / 可重试 / 内部”四类划分，每类对应一个稳定的退出码，
入口层只负责把异常翻译成提示与退出码，不在这里做重试。
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """命令行退出码。数值一旦发布不要改动，脚本会据此判断结果。"""

    OK = 0
    INTERNAL = 1
    INPUT = 2
    ENVIRONMENT = 3
    RETRYABLE = 4


class CodingWikiError(Exception):
    """所有可预期错误的基类。"""

    exit_code: ExitCode = ExitCode.INTERNAL

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InputError(CodingWikiError):
    """用户输入或配置文件有误：不重试，直接提示怎么改。"""

    exit_code = ExitCode.INPUT


class ConfigError(InputError):
    """配置相关的输入错误。"""


class ConfigNotFoundError(ConfigError):
    """配置文件不存在或路径不是文件。"""


class ConfigFormatError(ConfigError):
    """配置文件格式不对：后缀、编码、语法、重复键、顶层类型。"""


class ConfigValidationError(ConfigError):
    """配置内容不满足字段规则。"""


class EnvironmentIssue(CodingWikiError):
    """环境问题：命令缺失、权限不足、磁盘满。不重试，提示修环境。"""

    exit_code = ExitCode.ENVIRONMENT


class RetryableError(CodingWikiError):
    """暂时性错误：超时、锁冲突、网络抖动。有上限地重试后放弃。"""

    exit_code = ExitCode.RETRYABLE


class InternalError(CodingWikiError):
    """不应出现的情况：断言失败、未处理状态。记录后告警。"""

    exit_code = ExitCode.INTERNAL


__all__ = [
    "CodingWikiError",
    "ConfigError",
    "ConfigFormatError",
    "ConfigNotFoundError",
    "ConfigValidationError",
    "EnvironmentIssue",
    "ExitCode",
    "InputError",
    "InternalError",
    "RetryableError",
]
