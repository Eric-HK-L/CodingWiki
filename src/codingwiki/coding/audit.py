"""命令审计与执行护栏（开发计划 14.9）。

护栏是编排器的职责：模型只改文件，所有进程由编排器启动。
这里做两件事：
1. 启动前拒绝明显破坏性的命令（删除/硬回滚/提权/裸写磁盘等）；
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
    (re.compile(r"(^|\s)rm\s+-[a-z]*r[a-z]*f\b|(^|\s)rm\s+-[a-z]*f[a-z]*r\b"), "递归强制删除"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "硬回滚"),
    (re.compile(r"\bgit\s+clean\s+-[a-z]*f\b"), "强制清理未跟踪文件"),
    (re.compile(r"\bgit\s+push\b"), "推送远端"),
    (re.compile(r"\bgit\s+branch\s+-D\b"), "强删分支"),
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
