"""命令审计与执行护栏（开发计划 14.9）。

护栏是编排器的职责：模型只改文件，所有进程由编排器启动。
这里做两件事：
1. 启动前拒绝明显破坏性的命令（删除/重写历史/丢弃工作区/强删工作树/提权/裸写磁盘等）；
2. 把每条实际执行的命令追加到 `<run>/audit/commands.jsonl`，供人工复盘。
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from pathlib import Path

from codingwiki.domain.coding import CommandAuditEntry
from codingwiki.errors import InputError
from codingwiki.exec.protocol import CommandResult, CommandRunner, CommandSpec

_FORBIDDEN: tuple[tuple[re.Pattern[str], str], ...] = (
    # 删除仓库/文件
    (
        re.compile(
            r"(^|\s)rm\s+(?:-{1,2}[a-z-]+\s+)*-[a-z]*r[a-z]*f\b"
            r"|(^|\s)rm\s+(?:-{1,2}[a-z-]+\s+)*-[a-z]*f[a-z]*r\b"
        ),
        "递归强制删除",
    ),
    (re.compile(r"(^|\s)rm\s+-[rR]\s+-[a-z]*f\b|(^|\s)rm\s+-[a-z]*f\s+-[rR]\b"), "递归强制删除"),
    (
        re.compile(
            r"(^|\s)rm\s+--recursive\b[^\n]*\s--force\b|(^|\s)rm\s+--force\b[^\n]*--recursive\b"
        ),
        "递归强制删除",
    ),
    (re.compile(r"\bgit\s+rm\s+-r\b"), "递归删除版本文件"),
    (re.compile(r"\bfind\s+[^\n]*\s-delete\b"), "批量删除"),
    (re.compile(r"\bshred\b"), "粉碎文件"),
    (re.compile(r"\btruncate\s+-s\s*0\b"), "清空文件"),
    # 重写历史/破坏恢复点
    (re.compile(r"\bgit\s+(?:filter-branch|filter-repo)\b"), "重写历史"),
    (re.compile(r"\bgit\s+reflog\s+expire\b"), "清理 reflog"),
    (re.compile(r"\bgit\s+gc\b[^\n]*--prune\b"), "激进回收对象"),
    (re.compile(r"\bgit\s+replace\b"), "替换历史对象"),
    (re.compile(r"\bgit\s+update-ref\s+-d\b"), "删除引用"),
    (re.compile(r"\bgit\s+tag\s+-d\b"), "删除版本标签"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "硬回滚"),
    (re.compile(r"\bgit\s+clean\s+-[a-z]*f\b"), "强制清理未跟踪文件"),
    # 分支/工作区
    (re.compile(r"\bgit\s+push\b"), "推送远端"),
    (re.compile(r"\bgit\s+branch\s+-D\b"), "强删分支"),
    (re.compile(r"\bgit\s+worktree\s+remove\b[^\n]*--force\b"), "强删工作树"),
    (re.compile(r"\bgit\s+checkout\b[^\n]*\s--\s"), "丢弃工作区改动"),
    (re.compile(r"\bgit\s+restore\b(?![^\n]*--staged)"), "丢弃工作区改动"),
    # 系统级破坏
    (re.compile(r"(^|\s)(sudo|doas|su)\s"), "提权"),
    (re.compile(r"\bmkfs(\.|\s)"), "格式化磁盘"),
    (re.compile(r"\bdd\s+[^\n]*\bof="), "裸写磁盘"),
    (re.compile(r"\b(shutdown|reboot|halt)\b"), "关机/重启"),
    (re.compile(r"\bchmod\s+(?:-[Rr]\s+)?777\s+/(?:\s|$)"), "全盘可写权限"),
    (re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;"), "fork bomb"),
)


def assert_command_allowed(argv: list[str], *, source: str) -> None:
    """按 argv 拼接文本匹配高危模式；命中即拒绝，不做“先跑再回滚”。"""
    text = " ".join(argv)
    for pattern, reason in _FORBIDDEN:
        if pattern.search(text):
            raise InputError(f"拒绝执行危险命令（{reason}）：{source}: {_clip(text)}")


class AuditSink:
    """把命令执行追加成 JSONL；失败不阻断编排（审计是旁路）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._seq = 0

    def record(
        self,
        *,
        kind: str,
        argv: list[str],
        status: str,
        exit_code: int,
        duration_ms: int,
        cwd: str = "",
        started_at: datetime | None = None,
        error: str = "",
    ) -> None:
        self._seq += 1
        entry = CommandAuditEntry(
            seq=self._seq,
            kind=kind,
            argv=argv,
            cwd=cwd,
            status=status,
            exit_code=exit_code,
            duration_ms=max(duration_ms, 0),
            started_at=started_at or datetime.now(UTC),
            error=error,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json() + "\n")


class AuditRunner:
    """包装 CommandRunner：命令照常执行，同时落一条审计。"""

    def __init__(self, inner: CommandRunner, sink: AuditSink) -> None:
        self._inner = inner
        self._sink = sink

    def run(self, spec: CommandSpec) -> CommandResult:
        started_at = datetime.now(UTC)
        started = time.monotonic()
        result = self._inner.run(spec)
        duration_ms = result.duration_ms or int((time.monotonic() - started) * 1000)
        kind = f"stage:{spec.label}" if spec.label else "stage"
        self._sink.record(
            kind=kind,
            argv=list(spec.argv),
            cwd=spec.cwd,
            status=result.status.value,
            exit_code=result.exit_code,
            duration_ms=duration_ms,
            started_at=started_at,
        )
        return result


def audit_count(path: str | Path) -> int:
    """统计审计行数；文件不存在返回 0。"""
    audit_path = Path(path)
    if not audit_path.is_file():
        return 0
    return sum(1 for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip())


def _clip(value: str, limit: int = 200) -> str:
    return value if len(value) <= limit else f"{value[:limit]}…"


__all__ = ["AuditRunner", "AuditSink", "assert_command_allowed", "audit_count"]
