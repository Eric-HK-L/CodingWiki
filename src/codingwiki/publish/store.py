"""页面存储与发布（计划文档 9.12，架构文档 v0.4 13.5）。

布局：
- drafts/pages/<page_id>.md + .result.json/.verify.json/.meta.json/.deps.json
- versions/<version_id>/pages/... + index.json
- current 文件指向当前发布版本

发布走门禁：不通过则中止，旧版本不动（9.12.2/9.12.3）。
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from codingwiki.domain.wiki import (
    PageDeps,
    PageStatus,
    TaskMeta,
    VerifyReport,
    VersionIndex,
    VersionPageEntry,
)
from codingwiki.errors import InputError

_INTERNAL_LINK = re.compile(
    r"\[[^\]]*\]\(((?:module|interface|flow|config|build|test|overview|home|architecture|glossary|howto|adr|file)/[^)\s]+)\)"
)
_PAGE_TYPE_CAPS = {"module": 60, "interface": 30, "flow": 15}
_MAX_PAGES = 120


class FilesystemPageStore:
    def __init__(self, repo_id: str, base_dir: str | Path | None = None) -> None:
        base = Path(base_dir or os.environ.get("CODINGWIKI_HOME", ".codingwiki")).resolve()
        self._repo_id = repo_id
        self._root = base / "wiki" / repo_id

    # ---------------------------------------------------------------- 草稿区

    def stage(
        self,
        page_text: str,
        result_text: str,
        report: VerifyReport,
        meta: TaskMeta,
        deps: PageDeps,
    ) -> None:
        if report.status != "passed":
            raise InputError(f"页面 {meta.page_id} 校验未通过，不能入草稿区")
        draft = self._drafts / "pages"
        draft.mkdir(parents=True, exist_ok=True)
        stem = self._stem(meta.page_id)
        stem.parent.mkdir(parents=True, exist_ok=True)
        stem.with_suffix(".md").write_text(page_text, encoding="utf-8")
        # result.json 按原文落盘，保证发布门禁复算的哈希与校验器看到的一致
        stem.with_suffix(".result.json").write_text(result_text, encoding="utf-8")
        _write_json(stem.with_suffix(".verify.json"), report.model_dump_json(indent=2))
        _write_json(stem.with_suffix(".meta.json"), meta.model_dump_json(indent=2))
        _write_json(stem.with_suffix(".deps.json"), deps.model_dump_json(indent=2))

    def list_drafts(self) -> list[str]:
        pages = self._drafts / "pages"
        if not pages.is_dir():
            return []
        return sorted(str(p.relative_to(pages))[: -len(".md")] for p in pages.rglob("*.md"))

    def load_draft(self, page_id: str) -> tuple[str, str] | None:
        stem = self._stem(page_id)
        page_path = stem.with_suffix(".md")
        result_path = stem.with_suffix(".result.json")
        if not page_path.is_file() or not result_path.is_file():
            return None
        return (
            page_path.read_text(encoding="utf-8"),
            result_path.read_text(encoding="utf-8"),
        )

    def draft_meta(self, page_id: str) -> TaskMeta | None:
        path = self._stem(page_id).with_suffix(".meta.json")
        if not path.is_file():
            return None
        return TaskMeta.model_validate_json(path.read_text(encoding="utf-8"))

    def load_deps(self) -> dict[str, PageDeps]:
        pages = self._drafts / "pages"
        deps: dict[str, PageDeps] = {}
        if not pages.is_dir():
            return deps
        for path in pages.rglob("*.deps.json"):
            try:
                item = PageDeps.model_validate_json(path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            deps[item.page_id] = item
        return deps

    # ---------------------------------------------------------------- 发布

    def version_dir(self, version_id: str) -> Path:
        return self._root / "versions" / version_id

    def publish(
        self,
        snapshot_id: str,
        *,
        workdir: Path | None = None,
        site_profile: Mapping[str, object] | None = None,
    ) -> VersionIndex:
        """发布草稿区。传 workdir 时同时生成离线站点，站点成功后才切 current。"""
        page_ids = self.list_drafts()
        if not page_ids:
            raise InputError(f"仓库 {self._repo_id} 草稿区为空，没有可发布的页面")

        entries: list[VersionPageEntry] = []
        payload: list[tuple[str, str, str]] = []  # (page_id, page_text, result_text)
        prompt_versions: dict[str, str] = {}
        for page_id in page_ids:
            loaded = self.load_draft(page_id)
            meta = self.draft_meta(page_id)
            report = self._draft_report(page_id)
            if loaded is None or meta is None or report is None:
                raise InputError(f"草稿 {page_id} 文件不完整（缺 page/result/meta/verify）")
            page_text, result_text = loaded
            if report.status != "passed":
                raise InputError(
                    f"草稿 {page_id} 的校验状态为 {report.status}，发布门禁拒绝；"
                    "请先 wiki generate 修复或 wiki verify 复验"
                )
            if (
                not report.page_sha256
                or report.page_sha256 != _sha256(page_text)
                or report.result_sha256 != _sha256(result_text)
            ):
                raise InputError(
                    f"草稿 {page_id} 在通过校验后被修改，发布门禁拒绝；"
                    "请重新 wiki generate 生成该校验通过的页面"
                )
            entries.append(
                VersionPageEntry(
                    page_id=page_id,
                    type=meta.page_type,
                    status=PageStatus.VERIFIED,
                    title=meta.title_hint or page_id,
                    evidence_count=len(meta.evidence),
                )
            )
            payload.append((page_id, page_text, result_text))
            prompt_versions[page_id] = meta.prompt_version

        gate_errors = check_gate(payload)
        if gate_errors:
            raise InputError("发布门禁未通过：\n" + "\n".join(f"- {e}" for e in gate_errors))

        revision_part = snapshot_id.split("@", 1)[-1][:8] or "unknown"
        version_id = f"v{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{revision_part}"
        version_dir = self._root / "versions" / version_id
        pages_dir = version_dir / "pages"
        for page_id, page_text, result_text in payload:
            stem = pages_dir / page_id
            stem.parent.mkdir(parents=True, exist_ok=True)
            stem.with_suffix(".md").write_text(page_text, encoding="utf-8")
            _write_json(stem.with_suffix(".result.json"), result_text)

        index = VersionIndex(
            version_id=version_id,
            repo_id=self._repo_id,
            snapshot_id=snapshot_id,
            pages=entries,
            prompt_versions=prompt_versions,
        )
        _write_json(version_dir / "index.json", index.model_dump_json(indent=2))

        if workdir is not None:
            from codingwiki.publish.site import build_site

            build_site(
                version_dir,
                index,
                payload,
                workdir,
                site_profile or {},
            )

        (self._root / "current").write_text(version_id, encoding="utf-8")
        return index

    def current_version(self) -> str | None:
        pointer = self._root / "current"
        if not pointer.is_file():
            return None
        version = pointer.read_text(encoding="utf-8").strip()
        return version or None

    def version_index(self, version_id: str) -> VersionIndex | None:
        path = self._root / "versions" / version_id / "index.json"
        if not path.is_file():
            return None
        return VersionIndex.model_validate_json(path.read_text(encoding="utf-8"))

    def list_versions(self) -> list[str]:
        root = self._root / "versions"
        if not root.is_dir():
            return []
        return sorted(p.name for p in root.iterdir() if p.is_dir())

    def load_published_pages(self, version_id: str | None = None) -> list[tuple[str, str, str]]:
        """读取某发布版本（默认当前版本）的全部页面：(page_id, title, page_text)。供索引切块用。"""
        version = version_id or self.current_version()
        if version is None:
            return []
        index = self.version_index(version)
        if index is None:
            return []
        pages_dir = self._root / "versions" / version / "pages"
        pages: list[tuple[str, str, str]] = []
        for entry in index.pages:
            path = (pages_dir / entry.page_id).with_suffix(".md")
            if not path.is_file():
                continue
            pages.append((entry.page_id, entry.title, path.read_text(encoding="utf-8")))
        return pages

    # ---------------------------------------------------------------- 内部

    @property
    def _drafts(self) -> Path:
        return self._root / "drafts"

    def _stem(self, page_id: str) -> Path:
        if page_id.startswith("/") or ".." in page_id.split("/"):
            raise InputError(f"非法页面 ID: {page_id}")
        return self._drafts / "pages" / page_id

    def _draft_report(self, page_id: str) -> VerifyReport | None:
        path = self._stem(page_id).with_suffix(".verify.json")
        if not path.is_file():
            return None
        try:
            return VerifyReport.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            return None


def check_gate(payload: list[tuple[str, str, str]]) -> list[str]:
    """发布门禁（9.12.3）。payload = [(page_id, page_text, result_text)]。"""
    errors: list[str] = []
    known = {page_id for page_id, _, _ in payload}
    for page_id, page_text, _ in payload:
        for link in sorted(set(_INTERNAL_LINK.findall(page_text))):
            if link not in known:
                errors.append(f"{page_id}: 内部链接 {link} 指向不存在的页面")

    if len(payload) > _MAX_PAGES:
        errors.append(f"页面总数 {len(payload)} 超过上限 {_MAX_PAGES}（先调粒度，不要靠删页降数）")
    for page_type, cap in _PAGE_TYPE_CAPS.items():
        count = sum(1 for page_id, _, _ in payload if page_id.startswith(f"{page_type}/"))
        if count > cap:
            errors.append(f"{page_type} 页数 {count} 超过上限 {cap}")
    return errors


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_json(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


__all__ = ["FilesystemPageStore", "check_gate"]
