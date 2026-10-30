"""模型后端包：统一 Protocol + 各实现。"""

from codingwiki.backends.mock import MockBackend
from codingwiki.backends.protocol import (
    BackendResult,
    BackendStatus,
    ModelBackend,
    TaskPacket,
)

__all__ = [
    "BackendResult",
    "BackendStatus",
    "MockBackend",
    "ModelBackend",
    "TaskPacket",
]
