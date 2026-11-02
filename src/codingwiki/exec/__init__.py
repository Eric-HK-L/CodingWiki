"""命令执行器（S7 接口缝 §16.3-3）：协议 + 本地实现。"""

from codingwiki.exec.local import LocalCommandRunner
from codingwiki.exec.protocol import CommandResult, CommandRunner, CommandSpec, CommandStatus

__all__ = [
    "CommandResult",
    "CommandRunner",
    "CommandSpec",
    "CommandStatus",
    "LocalCommandRunner",
]
