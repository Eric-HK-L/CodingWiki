"""仓库命令 → CommandSpec（开发计划 14.7.1）。

仓库配置里的命令是 shell 字符串；这里只做 argv 拆分与参数归一化，
真正执行由 exec.CommandRunner 负责（不在本模块起进程）。
"""

from __future__ import annotations

import shlex
from pathlib import Path

from codingwiki.domain.coding import StageKind
from codingwiki.domain.models import RepoConfig
from codingwiki.errors import InputError
from codingwiki.exec.protocol import CommandSpec


def stage_specs(repo: RepoConfig, *, workspace_root: Path) -> list[tuple[StageKind, CommandSpec]]:
    """按 configure → lint → compile → test-fast → test-full 生成非空阶段。"""
    build = repo.commands.build
    test = repo.commands.test
    lint = repo.commands.lint
    entries = [
        (StageKind.CONFIGURE, build.configure, build.timeout_s),
        (StageKind.LINT, lint.command, lint.timeout_s),
        (StageKind.COMPILE, build.compile, build.timeout_s),
        (StageKind.TEST_FAST, test.fast, test.timeout_s),
        (StageKind.TEST_FULL, test.full, test.timeout_s),
    ]
    specs: list[tuple[StageKind, CommandSpec]] = []
    for kind, text, timeout_s in entries:
        if not text.strip():
            continue
        specs.append(
            (
                kind,
                CommandSpec(
                    argv=split_command(text),
                    cwd=str(workspace_root),
                    timeout_s=timeout_s,
                    label=kind.value,
                ),
            )
        )
    return specs


def split_command(text: str) -> list[str]:
    """把配置字符串拆成 argv；空值或引号不平衡直接拒绝。"""
    try:
        argv = shlex.split(text)
    except ValueError as exc:
        raise InputError(f"命令引号不平衡：{text!r}: {exc}") from exc
    if not argv:
        raise InputError(f"命令为空：{text!r}")
    return argv


__all__ = ["split_command", "stage_specs"]
