"""GitProvider：用 subprocess 调 git 命令。

materialize 策略（架构文档 v0.2 11.1）：
- 第一次：`git clone --bare <url> <bare_dir>`；
- 每次新 revision：`git fetch --prune origin <branch>` → `git worktree add <workdir> <sha>`；
- 这样一个 bare 仓库 + 多个 worktree 天然隔离不同版本，省磁盘。
"""

from __future__ import annotations

import os
import subprocess
from contextlib import suppress
from pathlib import Path

from codingwiki.domain.models import (
    ChangeSet,
    RepoConfig,
    RepoMetadata,
    RepoType,
)
from codingwiki.errors import EnvironmentIssue, InternalError, RetryableError


class GitProvider:
    name = "git"

    def __init__(self, git_bin: str | None = None) -> None:
        self._git = git_bin or _find_git()

    # ------------------------------------------------------------------ 公共方法

    def latest_revision(self, repo: RepoConfig) -> str:
        bare = self._ensure_bare(repo)
        ref = repo.branch
        out = self._run(
            ["rev-parse", f"origin/{ref}"],
            cwd=bare,
            retryable=True,
        )
        sha = out.strip()
        if not sha:
            raise InternalError(f"git rev-parse 返回空：{repo.url} origin/{ref}")
        return sha

    def materialize(self, repo: RepoConfig, revision: str) -> Path:
        bare = self._ensure_bare(repo)
        workdir = Path(repo.workdir) if repo.workdir else self._default_workdir(repo.repo_id)
        workdir = workdir.resolve()

        # 如果 workdir 已指向目标 SHA，直接复用
        if workdir.is_dir() and (workdir / ".git").exists():
            current = self._try_workdir_rev(workdir)
            if current == revision:
                return self._apply_root(workdir, repo.root)
            # 指向其他 SHA → 移除这个 worktree（git worktree remove）
            self._remove_worktree_if_any(bare, workdir)

        # 确保 bare 仓库里有这个 SHA
        self._run(
            ["fetch", "--prune", "origin", repo.branch],
            cwd=bare,
            retryable=True,
            extra_args=self._auth_args(repo),
            env=self._command_env(),
        )

        # 用 worktree 固定到该 SHA；--force 以防 dir 残留
        self._run(
            ["worktree", "add", "--force", str(workdir), revision],
            cwd=bare,
        )
        return self._apply_root(workdir, repo.root)

    def changes(self, repo: RepoConfig, old: str, new: str) -> ChangeSet:
        bare = self._ensure_bare(repo)
        # -M 启用重命名检测（默认相似度 50%），--name-status 只输出状态行
        out = self._run(
            ["diff", "--name-status", "-M", "-z", old, new],
            cwd=bare,
        )
        return _parse_name_status(out, old, new)

    def read_file(self, repo: RepoConfig, revision: str, path: str) -> bytes:
        bare = self._ensure_bare(repo)
        try:
            return self._run_bytes(["show", f"{revision}:{path}"], cwd=bare)
        except RetryableError as exc:
            # git show 失败常见于路径不存在，包装成更明确的错误
            raise InternalError(f"无法读取 {repo.repo_id}@{revision}:{path}: {exc}") from exc

    def metadata(self, repo: RepoConfig) -> RepoMetadata:
        bare = self._ensure_bare(repo)
        try:
            out = self._run(["remote", "get-url", "origin"], cwd=bare, retryable=False)
            url = out.strip() or repo.url
        except Exception:
            url = repo.url
        return RepoMetadata(
            repo_id=repo.repo_id,
            provider=RepoType.GIT,
            url=url,
            default_branch=repo.branch,
        )

    # ------------------------------------------------------------------ 可覆盖钩子

    def _effective_url(self, repo: RepoConfig) -> str:
        """实际用于 clone 的 URL；子类可做规范化（如 GitHub 简写补全）。"""
        return repo.url

    def _auth_args(self, repo: RepoConfig) -> list[str]:
        """鉴权相关的 `git -c ...` 前置参数；默认无。"""
        return []

    def _command_env(self) -> dict[str, str] | None:
        """额外环境变量；默认无。"""
        return None

    # ------------------------------------------------------------------ 内部辅助

    def _ensure_bare(self, repo: RepoConfig) -> Path:
        import shutil

        # 所有路径一律 resolve 成绝对路径——subprocess.run 的 cwd 和
        # git 命令里的路径参数如果同时用相对路径，git 会把目标路径
        # 再相对 cwd 解析一遍，导致双重嵌套。
        bare = self._default_bare(repo.repo_id).resolve()
        headers = self._auth_args(repo)
        env = self._command_env()
        if bare.is_dir() and (bare / "HEAD").is_file():
            # 已有 bare 仓库：确保 remote-tracking refs 已建好，这样
            # `git rev-parse origin/<branch>` 才能工作。
            # clone --bare 默认不会创建 refs/remotes/origin/*。
            if not (bare / "refs" / "remotes" / "origin").is_dir():
                self._run(
                    ["fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*"],
                    cwd=bare,
                    retryable=True,
                    extra_args=headers,
                    env=env,
                )
            return bare
        # 残留半空目录：清掉再 clone
        if bare.exists():
            shutil.rmtree(bare)
        bare.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            ["clone", "--bare", "--no-tags", self._effective_url(repo), str(bare)],
            cwd=bare.parent,
            retryable=True,
            extra_args=headers,
            env=env,
        )
        # clone --bare 后还需要建立 remote-tracking refs，
        # 否则后续 `git rev-parse origin/<branch>` 会找不到。
        self._run(
            ["fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*"],
            cwd=bare,
            retryable=True,
            extra_args=headers,
            env=env,
        )
        return bare

    def _run(
        self,
        args: list[str],
        *,
        cwd: Path,
        retryable: bool = False,
        extra_args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        return self._run_bytes(
            args, cwd=cwd, retryable=retryable, extra_args=extra_args, env=env
        ).decode("utf-8", errors="replace")

    def _run_bytes(
        self,
        args: list[str],
        *,
        cwd: Path,
        retryable: bool = False,
        extra_args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> bytes:
        cmd = [self._git, *(extra_args or []), *args]
        run_env: dict[str, str] | None = None
        if env is not None:
            run_env = {**os.environ, **env}
        try:
            result = subprocess.run(
                cmd,
                cwd=str(cwd),
                check=False,
                capture_output=True,
                timeout=10 * 60,
                env=run_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise (
                RetryableError(f"命令超时：{' '.join(cmd)}")
                if retryable
                else EnvironmentIssue(f"命令超时：{' '.join(cmd)}")
            ) from exc
        except FileNotFoundError as exc:
            raise EnvironmentIssue(f"git 命令不存在: {self._git}") from exc

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            msg = f"{' '.join(cmd)} 失败（rc={result.returncode}）: {stderr}"
            raise RetryableError(msg) if retryable else InternalError(msg)
        return result.stdout

    def _try_workdir_rev(self, workdir: Path) -> str | None:
        try:
            out = self._run(["rev-parse", "HEAD"], cwd=workdir, retryable=False)
        except Exception:
            return None
        return out.strip()

    def _remove_worktree_if_any(self, bare: Path, workdir: Path) -> None:
        # 只负责把 git worktree 关系解除；workdir 可能还被其他进程用，
        # 所以用 --force 并容忍残留。
        with suppress(InternalError):
            self._run(["worktree", "remove", "--force", str(workdir)], cwd=bare)

    @staticmethod
    def _default_bare(repo_id: str) -> Path:
        base = os.environ.get("CODINGWIKI_HOME", ".codingwiki")
        return Path(base) / "repos" / repo_id / "repo.git"

    @staticmethod
    def _default_workdir(repo_id: str) -> Path:
        base = os.environ.get("CODINGWIKI_HOME", ".codingwiki")
        return Path(base) / "repos" / repo_id / "work"

    @staticmethod
    def _apply_root(workdir: Path, root: str) -> Path:
        if not root:
            return workdir
        p = (workdir / root).resolve()
        return p if str(p).startswith(str(workdir.resolve())) else workdir


def _find_git() -> str:
    # 优先 PATH 里的 git
    try:
        result = subprocess.run(
            ["git", "--version"],
            check=False,
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            return "git"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    raise EnvironmentIssue("未找到 git 可执行文件（需要 PATH 里有 git）")


def _parse_name_status(raw: str, old: str, new: str) -> ChangeSet:
    """解析 `git diff --name-status -M -z` 的输出。

    -z 用 NUL 分隔每个字段和每个条目；
    R100	old	new → 重命名相似度 100%；
    R50	old	new → 相似度 50%，按架构约定：相似度低于 100% 的重命名按 "del + add" 记录；
    C	copy_of	new → 复制（按新增处理）；
    其他大写状态 (A/M/D/T/U/X) 各归其位。
    """
    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    renamed: list[tuple[str, str]] = []
    binary_changed: list[str] = []

    if not raw:
        return ChangeSet(
            old_revision=old,
            new_revision=new,
            added=added,
            modified=modified,
            deleted=deleted,
            renamed=renamed,
            binary_changed=binary_changed,
        )

    # NUL 分隔
    tokens = [t for t in raw.split("\x00") if t]
    i = 0
    while i < len(tokens):
        status = tokens[i]
        code = status[0]
        # 大小写一律当大写，简化处理
        if code == "A":
            added.append(tokens[i + 1])
            i += 2
        elif code in {"M", "T"}:  # modified / type change
            modified.append(tokens[i + 1])
            i += 2
        elif code == "D":
            deleted.append(tokens[i + 1])
            i += 2
        elif code in {"R", "C"}:
            similarity = 100
            # R50 / C100 等；解析后面的数字
            if len(status) > 1 and status[1:].isdigit():
                similarity = int(status[1:])
            old_path = tokens[i + 1]
            new_path = tokens[i + 2]
            if code == "R" and similarity >= 100:
                renamed.append((old_path, new_path))
            else:
                # 复制或低相似度重命名：按 add + del 处理
                deleted.append(old_path)
                added.append(new_path)
            i += 3
        elif code == "U":  # unmerged
            # 按 modified 记，让上层决定怎么处理
            modified.append(tokens[i + 1])
            i += 2
        elif code == "X":  # unknown
            i += 2  # 跳过，不记
        else:
            # 未知状态，保险起见跳过
            i += 2

    return ChangeSet(
        old_revision=old,
        new_revision=new,
        added=added,
        modified=modified,
        deleted=deleted,
        renamed=renamed,
        binary_changed=binary_changed,
    )


__all__ = ["GitProvider"]
