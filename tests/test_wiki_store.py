"""FilesystemPageStore 与发布门禁单元测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from codingwiki.domain.wiki import (
    PageDeps,
    PageResult,
    PageType,
    TaskMeta,
    VerifyReport,
)
from codingwiki.errors import InputError
from codingwiki.publish import FilesystemPageStore, check_gate


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FilesystemPageStore:
    monkeypatch.setenv("CODINGWIKI_HOME", str(tmp_path / ".codingwiki"))
    return FilesystemPageStore("r")


def _meta(page_id: str = "module/src") -> TaskMeta:
    return TaskMeta(
        task_id="t",
        page_id=page_id,
        page_type=PageType.MODULE,
        title_hint="模块 src",
        repo_id="r",
        snapshot_id="r@1",
        prompt_version="module_page@1",
    )


def _result_text(page_id: str = "module/src") -> str:
    return PageResult(page_id=page_id, snapshot="r@1").model_dump_json(indent=2)


def _report(
    page_id: str = "module/src",
    status: str = "passed",
    page_text: str = "",
    result_text: str = "",
) -> VerifyReport:
    return VerifyReport(
        page_id=page_id,
        status=status,
        verifier_version="verify-v1",
        page_sha256=hashlib.sha256(page_text.encode("utf-8")).hexdigest() if page_text else "",
        result_sha256=(
            hashlib.sha256(result_text.encode("utf-8")).hexdigest() if result_text else ""
        ),
    )


def _stage(store: FilesystemPageStore, page_text: str, page_id: str = "module/src") -> None:
    result_text = _result_text(page_id)
    store.stage(
        page_text,
        result_text,
        _report(page_id, page_text=page_text, result_text=result_text),
        _meta(page_id),
        _deps(page_id),
    )


def _deps(page_id: str = "module/src") -> PageDeps:
    return PageDeps(page_id=page_id, snapshot_id="r@1", files={"src/a.c": "sha"})


PAGE = "---\nid: module/src\ntype: module\ntitle: t\nsnapshot: r@1\n---\n正文。[E1]\n"


def test_stage_and_load(store: FilesystemPageStore) -> None:
    _stage(store, PAGE)
    assert store.list_drafts() == ["module/src"]
    loaded = store.load_draft("module/src")
    assert loaded is not None and "正文" in loaded[0]
    assert store.draft_meta("module/src").page_id == "module/src"
    deps = store.load_deps()
    assert deps["module/src"].files == {"src/a.c": "sha"}


def test_stage_rejects_failed_report(store: FilesystemPageStore) -> None:
    with pytest.raises(InputError, match="校验未通过"):
        store.stage(PAGE, _result_text(), _report(status="failed"), _meta(), _deps())


def test_stage_rejects_illegal_page_id(store: FilesystemPageStore) -> None:
    with pytest.raises(InputError, match="非法页面 ID"):
        store.stage(PAGE, _result_text("../x"), _report("../x"), _meta("../x"), _deps("../x"))


def test_publish_creates_version_and_current(store: FilesystemPageStore) -> None:
    _stage(store, PAGE)
    index = store.publish("r@1")
    assert index.version_id.startswith("v")
    assert index.snapshot_id == "r@1"
    assert len(index.pages) == 1
    assert store.current_version() == index.version_id
    assert store.version_index(index.version_id).pages[0].page_id == "module/src"
    assert store.list_versions() == [index.version_id]


def test_publish_empty_drafts_rejected(store: FilesystemPageStore) -> None:
    with pytest.raises(InputError, match="草稿区为空"):
        store.publish("r@1")


def test_gate_blocks_broken_link() -> None:
    payload = [
        ("module/a", "---\n---\n参见 [x](module/ghost)。\n", "{}"),
        ("module/b", "---\n---\n正常。\n", "{}"),
    ]
    errors = check_gate(payload)
    assert len(errors) == 1 and "module/ghost" in errors[0]


def test_gate_passes_internal_links(store: FilesystemPageStore) -> None:
    page_a = PAGE.replace("正文。[E1]", "正文 [链接](module/src-b)。")
    _stage(store, page_a)
    _stage(store, PAGE.replace("module/src", "module/src-b"), "module/src-b")
    index = store.publish("r@1")
    assert len(index.pages) == 2


def test_publish_rejects_tampered_draft(store: FilesystemPageStore, tmp_path: Path) -> None:
    _stage(store, PAGE)
    # 通过校验后篡改草稿正文 → 发布门禁复算哈希不一致，拒绝发布
    page_path = tmp_path / ".codingwiki" / "wiki" / "r" / "drafts" / "pages" / "module" / "src.md"
    page_path.write_text(PAGE.replace("正文", "被篡改的正文"), encoding="utf-8")
    with pytest.raises(InputError, match="通过校验后被修改"):
        store.publish("r@1")
