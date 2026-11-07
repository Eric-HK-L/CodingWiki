"""S10 并发锁测试：互斥、陈旧锁接管、释放语义。"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codingwiki.errors import EnvironmentIssue
from codingwiki.pipeline.lock import RepoLock


def _write_lock(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_acquire_and_release_creates_and_removes_lock(tmp_path: Path) -> None:
    lock = RepoLock("sample", base_dir=tmp_path)
    with lock as acquired:
        assert acquired is lock
        assert lock.path.is_file()
        content = lock.path.read_text(encoding="utf-8")
        assert f"pid={os.getpid()}" in content
    assert not lock.path.exists()


def test_second_acquire_fails_with_holder_info(tmp_path: Path) -> None:
    started = datetime.now(UTC).isoformat()
    _write_lock(tmp_path / "locks" / "sample.lock", f"pid={os.getpid()} started={started}\n")
    lock = RepoLock("sample", base_dir=tmp_path)
    with pytest.raises(EnvironmentIssue, match="已有任务在运行"):
        lock.acquire()
    # 失败后锁文件保持原样（持有者不受影响）
    assert lock.path.is_file()


def test_stale_lock_dead_pid_is_taken_over(tmp_path: Path) -> None:
    _write_lock(
        tmp_path / "locks" / "sample.lock", "pid=999999999 started=2026-01-01T00:00:00+00:00\n"
    )
    lock = RepoLock("sample", base_dir=tmp_path)
    with lock:
        assert lock.path.is_file()
        assert f"pid={os.getpid()}" in lock.path.read_text(encoding="utf-8")


def test_stale_lock_expired_age_is_taken_over(tmp_path: Path) -> None:
    # pid 写一个活着的进程（比如当前进程），但锁龄超过 24 小时 → 接管
    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    _write_lock(tmp_path / "locks" / "sample.lock", f"pid={os.getpid()} started={old}\n")
    lock = RepoLock("sample", base_dir=tmp_path)
    with lock:
        assert f"pid={os.getpid()}" in lock.path.read_text(encoding="utf-8")


def test_unparseable_lock_is_considered_alive(tmp_path: Path) -> None:
    # 内容不可解析 → 保守视为存活，报错待人处理，绝不吞掉别人的锁
    _write_lock(tmp_path / "locks" / "sample.lock", "不是合法的锁内容")
    lock = RepoLock("sample", base_dir=tmp_path)
    with pytest.raises(EnvironmentIssue, match="已有任务在运行"):
        lock.acquire()
