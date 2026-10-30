"""事实提取集成测试：真实快照 → 事实 → 缓存复用 → 增量。"""

from __future__ import annotations

import shutil
from collections.abc import Generator
from pathlib import Path

import pytest

from codingwiki.domain.models import RepoConfig, RepoType
from codingwiki.errors import InternalError
from codingwiki.facts import EXTRACTOR_VERSION, FilesystemFactExtractor, FilesystemFactStore
from codingwiki.facts import extractor as extractor_module
from codingwiki.facts.store import FilesystemFactStore as StoreClass
from codingwiki.snapshot import FilesystemSnapshotStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "facts" / "sample"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    base = tmp_path / ".codingwiki"
    monkeypatch.setenv("CODINGWIKI_HOME", str(base))
    yield base


@pytest.fixture
def work(tmp_path: Path) -> Path:
    target = tmp_path / "work"
    shutil.copytree(FIXTURES, target)
    return target


def _snapshot(store: FilesystemSnapshotStore, work: Path, revision: str = "rev1", **kwargs: object):
    store.create("sample", revision, work, RepoType.GIT, **kwargs)  # type: ignore[arg-type]
    return store.snapshot(f"sample@{revision}")


def test_extract_sample_repository(home: Path, work: Path) -> None:
    snapshots = FilesystemSnapshotStore()
    store = StoreClass()
    snapshot = _snapshot(snapshots, work)

    facts = FilesystemFactExtractor(store).extract(snapshot, work)

    paths = {file.path for file in facts.files}
    assert paths == {"src/main.cu", "src/engine.h", "src/helper.c", "tests/test_sample.py"}
    assert facts.modules == {
        "src": ["src/engine.h", "src/helper.c", "src/main.cu"],
        "tests": ["tests/test_sample.py"],
    }
    assert {target.name for target in facts.build_targets} == {"phy", "sample_app", "sample_smoke"}
    assert {test.framework for test in facts.tests} == {"ctest", "pytest"}
    assert {config.key for config in facts.config_facts} >= {
        "engine.name",
        "engine.features",
        "logging.level",
        "PHY_LOG_LEVEL",
        "SAMPLE_FEATURE",
    }
    assert facts.quality.parse_success_rate == 1.0
    assert facts.quality.compdb_coverage is None
    assert facts.quality.build_target_source_coverage == pytest.approx(2 / 3)

    kernel = next(
        symbol
        for file in facts.files
        for symbol in file.symbols
        if symbol.qualified_name == "add_kernel"
    )
    assert kernel.kind.value == "kernel"


def test_facts_are_persisted_and_loadable(home: Path, work: Path) -> None:
    snapshots = FilesystemSnapshotStore()
    store = StoreClass()
    snapshot = _snapshot(snapshots, work)
    facts = FilesystemFactExtractor(store).extract(snapshot, work)

    path = store.save(facts)
    assert path.is_file()
    assert store.load("sample", "rev1") == facts
    assert store.latest("sample") == facts
    assert store.load("sample", "missing") is None


def test_cache_allows_second_run_without_reparsing(
    home: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshots = FilesystemSnapshotStore()
    store = StoreClass()
    snapshot = _snapshot(snapshots, work)
    first = FilesystemFactExtractor(store).extract(snapshot, work)

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("第二次不应重新解析")

    monkeypatch.setattr(extractor_module, "extract_file_facts", explode)
    second = FilesystemFactExtractor(store).extract(snapshot, work)

    assert second.quality.parse_success_rate == first.quality.parse_success_rate
    assert [f.symbols for f in second.files] == [f.symbols for f in first.files]


def test_incremental_extract_only_reparses_changed_file(
    home: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshots = FilesystemSnapshotStore()
    store = StoreClass()
    first_snapshot = _snapshot(snapshots, work)
    FilesystemFactExtractor(store).extract(first_snapshot, work)

    (work / "src/helper.c").write_text(
        "int helper(int value) { return value + 2; }\nint extra(int x) { return x; }\n",
        encoding="utf-8",
    )
    second_snapshot = _snapshot(snapshots, work, "rev2")

    real = extractor_module.extract_file_facts
    repaired: list[str] = []

    def counting(*args: object, **kwargs: object) -> object:
        repaired.append(str(args[1]))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(extractor_module, "extract_file_facts", counting)
    second = FilesystemFactExtractor(store).extract(second_snapshot, work)

    assert repaired == ["src/helper.c"]
    helper = next(f for f in second.files if f.path == "src/helper.c")
    assert {symbol.name for symbol in helper.symbols} == {"helper", "extra"}


def test_force_reparses_everything(home: Path, work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshots = FilesystemSnapshotStore()
    store = StoreClass()
    snapshot = _snapshot(snapshots, work)
    FilesystemFactExtractor(store).extract(snapshot, work)

    real = extractor_module.extract_file_facts
    repaired: list[str] = []

    def counting(*args: object, **kwargs: object) -> object:
        repaired.append(str(args[1]))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(extractor_module, "extract_file_facts", counting)
    FilesystemFactExtractor(store, force=True).extract(snapshot, work)

    assert sorted(repaired) == [
        "src/engine.h",
        "src/helper.c",
        "src/main.cu",
        "tests/test_sample.py",
    ]


def test_excluded_binary_and_truncated_files_are_skipped(home: Path, work: Path) -> None:
    (work / "vendor").mkdir()
    (work / "vendor" / "blob.bin").write_bytes(b"\x00\x01binary")
    (work / "vendor" / "lib.c").write_text("int vendored(void) { return 0; }\n", encoding="utf-8")
    config = RepoConfig(
        repo_id="sample",
        provider=RepoType.GIT,
        url=str(work),
        excludes=["vendor/**"],
        max_text_file_size_kb=1,
    )
    (work / "src" / "huge.c").write_text("x" * 2048, encoding="utf-8")

    snapshots = FilesystemSnapshotStore()
    snapshot = _snapshot(snapshots, work, config=config)
    facts = FilesystemFactExtractor(StoreClass()).extract(snapshot, work)

    assert "vendor/lib.c" not in {file.path for file in facts.files}
    assert "vendor/blob.bin" not in {file.path for file in facts.files}
    assert "src/huge.c" not in {file.path for file in facts.files}


def test_missing_manifest_raises_internal_error(home: Path, work: Path) -> None:
    snapshots = FilesystemSnapshotStore()
    snapshot = _snapshot(snapshots, work)
    broken = snapshot.model_copy(
        update={"manifest_path": str(Path(snapshot.manifest_path).with_suffix(".gone"))}
    )

    with pytest.raises(InternalError, match="manifest 不存在"):
        FilesystemFactExtractor(StoreClass()).extract(broken, work)


def test_corrupted_cache_is_treated_as_miss(home: Path) -> None:
    store = StoreClass()
    cache = home / "facts" / "sample" / "cache" / EXTRACTOR_VERSION / "ab" / "abc.json"
    cache.parent.mkdir(parents=True)
    cache.write_text("{broken", encoding="utf-8")

    assert store.cached_file("sample", "abc", EXTRACTOR_VERSION) is None


def test_latest_is_none_for_unknown_repository(home: Path) -> None:
    assert StoreClass().latest("nope") is None


def test_store_defaults_to_environment_home(home: Path) -> None:
    assert FilesystemFactStore().load("sample", "rev1") is None
