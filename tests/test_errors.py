"""异常分类与退出码测试。

重点：四类错误的退出码必须稳定，因为脚本和 CI 会据此判断结果；
一旦发布，数值不能变（架构文档第 6 节）。
"""

from __future__ import annotations

import pytest

from codingwiki.errors import (
    CodingWikiError,
    ConfigError,
    ConfigFormatError,
    ConfigNotFoundError,
    ConfigValidationError,
    EnvironmentIssue,
    ExitCode,
    InputError,
    InternalError,
    RetryableError,
)


def test_exit_code_values_are_stable() -> None:
    assert ExitCode.OK == 0
    assert ExitCode.INTERNAL == 1
    assert ExitCode.INPUT == 2
    assert ExitCode.ENVIRONMENT == 3
    assert ExitCode.RETRYABLE == 4


@pytest.mark.parametrize(
    ("error_class", "expected"),
    [
        (InputError, ExitCode.INPUT),
        (ConfigError, ExitCode.INPUT),
        (ConfigNotFoundError, ExitCode.INPUT),
        (ConfigFormatError, ExitCode.INPUT),
        (ConfigValidationError, ExitCode.INPUT),
        (EnvironmentIssue, ExitCode.ENVIRONMENT),
        (RetryableError, ExitCode.RETRYABLE),
        (InternalError, ExitCode.INTERNAL),
    ],
)
def test_error_class_maps_to_expected_exit_code(
    error_class: type[CodingWikiError], expected: ExitCode
) -> None:
    assert error_class("示例错误").exit_code == expected


def test_base_error_defaults_to_internal() -> None:
    assert CodingWikiError("未分类错误").exit_code == ExitCode.INTERNAL


def test_message_is_preserved() -> None:
    exc = ConfigValidationError("配置校验失败: id 不合法")

    assert exc.message == "配置校验失败: id 不合法"
    assert str(exc) == "配置校验失败: id 不合法"


def test_config_errors_are_input_errors() -> None:
    # 配置问题属于用户输入问题，必须是同一类，方便入口层统一处理。
    assert issubclass(ConfigError, InputError)
    assert issubclass(ConfigNotFoundError, ConfigError)
    assert issubclass(ConfigFormatError, ConfigError)
    assert issubclass(ConfigValidationError, ConfigError)


def test_retryable_and_environment_are_not_input_errors() -> None:
    # 反例：环境问题和可重试问题不能被当成用户输入去提示“改配置”。
    assert not issubclass(EnvironmentIssue, InputError)
    assert not issubclass(RetryableError, InputError)
