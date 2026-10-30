"""事实提取编排（架构文档 v0.3 第 12 节）。

流程：逐文件缓存/解析 → 构建发现 → 测试发现 → 配置发现 → 调用解析 → 质量报告。
事实层不做任何模型调用，同一输入必须产出同一输出。
"""

from __future__ import annotations

from pathlib import Path

from codingwiki.domain.facts import (
    BuildTarget,
    CallFact,
    ConfigFact,
    FileFacts,
    QualityReport,
    RepoFacts,
)
from codingwiki.domain.models import Manifest, Snapshot
from codingwiki.errors import InternalError
from codingwiki.facts.build_system import (
    ctest_facts,
    find_cmake_file_api_dirs,
    parse_cmake_files,
    read_cmake_file_api,
    read_compile_commands,
)
from codingwiki.facts.config_scan import scan_config
from codingwiki.facts.protocol import FactStore
from codingwiki.facts.symbols import SUPPORTED_LANGUAGES, extract_file_facts
from codingwiki.facts.tests_scan import scan_gtest, scan_pytest

EXTRACTOR_VERSION = "facts-v1"

_C_LIKE = frozenset({"c", "cpp", "cuda"})
_CONFIG_LANGUAGES = frozenset({"yaml", "json", "toml"})
_CODE_LANGUAGES = frozenset({"c", "cpp", "cuda", "python", "shell"})


class FilesystemFactExtractor:
    def __init__(self, store: FactStore, *, force: bool = False) -> None:
        self._store = store
        self._force = force

    def extract(self, snapshot: Snapshot, workdir: Path) -> RepoFacts:
        manifest = _load_manifest(snapshot)
        workdir = Path(workdir).resolve()
        if not workdir.is_dir():
            raise InternalError(f"workdir 不存在或不是目录: {workdir}")

        entries = [f for f in manifest.files if not f.excluded and not f.binary and not f.truncated]

        # 1) 逐文件符号解析（命中缓存则不重解析）
        file_facts: list[FileFacts] = []
        texts: dict[str, tuple[str, str]] = {}  # path -> (language, text)
        scan_languages = _C_LIKE | _CONFIG_LANGUAGES | _CODE_LANGUAGES
        for entry in entries:
            if entry.language in SUPPORTED_LANGUAGES:
                facts = (
                    None
                    if self._force
                    else self._store.cached_file(
                        snapshot.repo_id, entry.blob_sha256, EXTRACTOR_VERSION
                    )
                )
                if facts is None:
                    facts = extract_file_facts(
                        workdir / entry.path,
                        entry.path,
                        entry.language,
                        entry.blob_sha256,
                        _module_for(entry.path),
                    )
                    self._store.cache_file(
                        snapshot.repo_id, entry.blob_sha256, EXTRACTOR_VERSION, facts
                    )
                file_facts.append(facts)
            if entry.language in scan_languages:
                texts[entry.path] = (entry.language, _read_text(workdir / entry.path))

        # 2) 构建目标：File API 优先，CMake 文本解析补齐
        cmake_paths = [e.path for e in entries if e.path.rsplit("/", 1)[-1] == "CMakeLists.txt"] + [
            e.path for e in entries if e.path.endswith(".cmake")
        ]
        notes: list[str] = []
        text_targets = parse_cmake_files(workdir, cmake_paths)
        api_targets: list[BuildTarget] = []
        for reply_dir in find_cmake_file_api_dirs(workdir):
            targets, api_notes = read_cmake_file_api(reply_dir)
            api_targets.extend(targets)
            notes.extend(api_notes)
        merged = {t.name: t for t in api_targets}
        for target in text_targets:
            merged.setdefault(target.name, target)
        build_targets = sorted(merged.values(), key=lambda t: t.name)

        # 3) 测试发现：gtest / pytest / ctest
        gtest_texts = [
            (path, text) for path, (language, text) in texts.items() if language in _C_LIKE
        ]
        gtest, _ = scan_gtest(gtest_texts)
        tests = sorted(
            [*gtest, *scan_pytest(file_facts), *ctest_facts(build_targets)],
            key=lambda t: (t.framework, t.path, t.line, t.name),
        )

        # 4) 配置发现：配置文件键 + 环境变量 / CLI 参数读取
        config_texts = [
            (path, language, text)
            for path, (language, text) in texts.items()
            if language in _CONFIG_LANGUAGES or language in _CODE_LANGUAGES
        ]
        config_facts, config_skipped = scan_config(config_texts)
        if config_skipped:
            notes.append(f"{config_skipped} 个配置文件解析失败，已跳过")

        # 5) 调用解析（同文件优先，全库唯一名次之）
        file_facts = _resolve_all_calls(file_facts)

        # 6) 质量报告
        covered, compdb_found, compdb_notes = read_compile_commands(workdir)
        notes.extend(compdb_notes)
        quality = _quality(
            file_facts=file_facts,
            build_targets=build_targets,
            config_facts=config_facts,
            tests_count=len(tests),
            compdb_covered=covered,
            compdb_found=compdb_found,
            notes=notes,
        )

        modules: dict[str, list[str]] = {}
        for facts in file_facts:
            modules.setdefault(facts.module, []).append(facts.path)
        modules = {k: sorted(v) for k, v in sorted(modules.items())}

        return RepoFacts(
            snapshot_id=snapshot.snapshot_id,
            repo_id=snapshot.repo_id,
            revision=snapshot.revision,
            extractor_version=EXTRACTOR_VERSION,
            files=sorted(file_facts, key=lambda f: f.path),
            modules=modules,
            build_targets=build_targets,
            config_facts=sorted(config_facts, key=lambda c: (c.path, c.line, c.key)),
            tests=tests,
            quality=quality,
        )


# ---------------------------------------------------------------- 辅助


def _load_manifest(snapshot: Snapshot) -> Manifest:
    path = Path(snapshot.manifest_path)
    if not path.is_file():
        raise InternalError(f"快照 manifest 不存在: {path}（请先执行 repo sync）")
    return Manifest.model_validate_json(path.read_text(encoding="utf-8"))


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _module_for(path: str) -> str:
    parts = path.split("/")
    if len(parts) >= 3:
        return f"{parts[0]}/{parts[1]}"
    if len(parts) == 2:
        return parts[0]
    return "(root)"


def _resolve_all_calls(file_facts: list[FileFacts]) -> list[FileFacts]:
    by_name: dict[str, list[str]] = {}
    for facts in file_facts:
        for symbol in facts.symbols:
            by_name.setdefault(symbol.name, []).append(symbol.symbol_id)
    resolved: list[FileFacts] = []
    for facts in file_facts:
        local: dict[str, list[str]] = {}
        for symbol in facts.symbols:
            local.setdefault(symbol.name, []).append(symbol.symbol_id)
        calls = [_resolve_call(call, local, by_name) for call in facts.calls]
        resolved.append(facts.model_copy(update={"calls": calls}))
    return resolved


def _resolve_call(
    call: CallFact, local: dict[str, list[str]], by_name: dict[str, list[str]]
) -> CallFact:
    name = call.callee.split("::")[-1].split(".")[-1].split("->")[-1].strip()
    name = name.rstrip("(< ")
    same_file = local.get(name, [])
    if len(same_file) == 1:
        return call.model_copy(update={"targets": same_file})
    global_matches = by_name.get(name, [])
    if len(global_matches) == 1:
        return call.model_copy(update={"targets": global_matches})
    return call


def _quality(
    *,
    file_facts: list[FileFacts],
    build_targets: list[BuildTarget],
    config_facts: list[ConfigFact],
    tests_count: int,
    compdb_covered: set[str],
    compdb_found: bool,
    notes: list[str],
) -> QualityReport:
    total = len(file_facts)
    parsed = sum(1 for f in file_facts if f.parse_ok)
    all_calls = [c for f in file_facts for c in f.calls]
    unresolved = sum(1 for c in all_calls if not c.targets)
    c_like_paths = {f.path for f in file_facts if f.language in _C_LIKE}
    env_keys = [c for c in config_facts if c.kind == "env"]

    return QualityReport(
        files_total=total,
        files_parsed=parsed,
        parse_success_rate=parsed / total if total else None,
        unresolved_call_ratio=unresolved / len(all_calls) if all_calls else None,
        compdb_coverage=(
            len(c_like_paths & compdb_covered) / len(c_like_paths)
            if c_like_paths and compdb_found
            else None
        ),
        build_target_source_coverage=(
            sum(1 for t in build_targets if t.sources) / len(build_targets)
            if build_targets
            else None
        ),
        env_read_coverage=(
            sum(1 for c in env_keys if c.path and c.line > 0) / len(env_keys) if env_keys else None
        ),
        tests_found=tests_count,
        parse_failures=sorted(f.path for f in file_facts if not f.parse_ok)[:200],
        notes=sorted(set(notes)),
    )


__all__ = ["EXTRACTOR_VERSION", "FilesystemFactExtractor"]
