"""仓库级并发锁（架构文档 v0.9 §18.4）。

锁粒度 = 仓库：`run` 全程持锁，同一仓库并发触发时第二个进程立即失败
（报持有者 pid 与启动时间），不等待、不排队——日更场景宁可失败重跑，
也不要两个任务并发写坏快照。

实现：`.codingwiki/locks/<repo_id>.lock`，`O_CREAT|O_EXCL` 原子创建，
内容为 `pid=<pid> started=<iso8601> host=<hostname>`。
陈旧锁接管：持有 pid 已死（os.kill(pid, 0) 报 ProcessLookupError），
或锁龄超过 24 小时（防 pid 复用假活）→ 视为陈旧锁，安全接管；
内容不可解析 → 保守视为存活，报错待人处理。
"""

from __future__ import annotations

import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

from codingwiki.errors import EnvironmentIssue

_STALE_AFTER = timedelta(hours=24)


def _locks_dir(base_dir: Path) -> Path:
    return base_dir / "locks"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _parse_holder(text: str) -> dict[str, str] | None:
    """解析锁文件内容；不可解析返回 None（保守视为存活）。"""
    fields: dict[str, str] = {}
    for part in text.split():
        if "=" in part:
            key, _, value = part.partition("=")
            fields[key] = value
    if "pid" not in fields:
        return None
    return fields


class RepoLock:
    """with 语义的仓库锁；锁文件落在 CODINGWIKI_HOME/locks/ 下。"""

    def __init__(self, repo_id: str, *, base_dir: Path | None = None) -> None:
        self._repo_id = repo_id
        self._base = (base_dir or Path(os.environ.get("CODINGWIKI_HOME", ".codingwiki"))).resolve()
        self._path = _locks_dir(self._base) / f"{repo_id}.lock"
        self._acquired = False

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            f"pid={os.getpid()} started={datetime.now(UTC).isoformat()} host={socket.gethostname()}"
        )
        while True:
            try:
                fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._stale():
                    # 陈旧锁：接管前记录一次告警信息（接管本身是唯一正确动作）
                    self._path.unlink(missing_ok=True)
                    continue
                holder = self._path.read_text(encoding="utf-8", errors="replace").strip()
                raise EnvironmentIssue(
                    f"仓库 {self._repo_id} 已有任务在运行，锁：{self._path}（{holder}）；"
                    "同一仓库不允许并发 run，请等它结束或人工确认后删除锁文件"
                ) from None
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload + "\n")
            self._acquired = True
            return

    def release(self) -> None:
        if self._acquired:
            self._path.unlink(missing_ok=True)
            self._acquired = False

    def _stale(self) -> bool:
        """陈旧锁判定：持有 pid 已死，或锁龄超过 24 小时。"""
        try:
            raw = self._path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return False
        holder = _parse_holder(raw)
        if holder is None:
            return False
        try:
            pid = int(holder["pid"])
        except ValueError:
            return False
        if not _alive(pid):
            return True
        started = holder.get("started", "")
        try:
            age = datetime.now(UTC) - datetime.fromisoformat(started)
        except ValueError:
            return False
        return age > _STALE_AFTER

    def __enter__(self) -> RepoLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


__all__ = ["RepoLock"]
