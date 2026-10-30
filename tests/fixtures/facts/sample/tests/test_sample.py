"""示例 Python 测试，用于事实层的 pytest 发现。"""

from __future__ import annotations

import os

FEATURE_FLAG = os.getenv("SAMPLE_FEATURE", "off")


def test_helper_increments() -> None:
    assert helper(1) == 2


class TestEngine:
    def test_run_returns_zero(self) -> None:
        assert True


def helper(value: int) -> int:
    return value + 1


if __name__ == "__main__":
    helper(1)
