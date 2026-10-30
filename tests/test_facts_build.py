"""构建/测试发现单元测试：CMake 文本、compile_commands、CMake File API。"""

from __future__ import annotations

import json
from pathlib import Path

from codingwiki.domain.facts import Confidence
from codingwiki.facts.build_system import (
    ctest_facts,
    find_cmake_file_api_dirs,
    parse_cmake_files,
    read_cmake_file_api,
    read_compile_commands,
)


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_cmake_parses_targets_variables_and_interfaces(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "CMakeLists.txt",
        """
cmake_minimum_required(VERSION 3.20)
project(demo)
set(SRC
    src/a.c
    src/b.cpp)
add_library(core STATIC ${SRC})
target_compile_definitions(core PUBLIC CORE_VERSION=1)
target_include_directories(core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(core PUBLIC extra)
add_test(NAME unit COMMAND core_tests)
""",
    )
    targets = {t.name: t for t in parse_cmake_files(tmp_path, ["CMakeLists.txt"])}

    core = targets["core"]
    assert core.kind == "library"
    assert core.sources == ["src/a.c", "src/b.cpp"]
    assert core.compile_definitions == ["CORE_VERSION=1"]
    assert core.include_dirs == ["include"]
    assert core.deps == ["extra"]
    assert core.confidence is Confidence.MEDIUM
    assert core.line == 7

    assert targets["unit"].kind == "test"
    assert ctest_facts(list(targets.values()))[0].name == "unit"


def test_cmake_ignores_comments_and_generator_expressions(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "CMakeLists.txt",
        """
# add_library(fake src/fake.c)
add_executable(real $<TARGET_OBJECTS:obj> src/main.cpp)
""",
    )
    targets = parse_cmake_files(tmp_path, ["CMakeLists.txt"])
    assert [t.name for t in targets] == ["real"]
    assert targets[0].sources == ["src/main.cpp"]


def test_cmake_handles_unknown_variables_without_crashing(tmp_path: Path) -> None:
    _write(tmp_path, "CMakeLists.txt", "add_library(lib ${UNKNOWN_VAR})\n")
    targets = parse_cmake_files(tmp_path, ["CMakeLists.txt"])
    assert targets[0].sources == []


def test_compile_commands_reads_root_and_filters_paths(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "compile_commands.json",
        json.dumps(
            [
                {"file": "src/a.c"},
                {"file": str(tmp_path / "src" / "b.c")},
                {"file": "/outside/c.c"},
            ]
        ),
    )
    covered, found, notes = read_compile_commands(tmp_path)
    assert found is True
    assert covered == {"src/a.c", "src/b.c", "/outside/c.c"}
    assert any("compile_commands" in note for note in notes)


def test_compile_commands_falls_back_to_build_dir(tmp_path: Path) -> None:
    _write(tmp_path, "build/compile_commands.json", json.dumps([{"file": "src/a.c"}]))
    covered, found, _ = read_compile_commands(tmp_path)
    assert found is True
    assert covered == {"src/a.c"}


def test_compile_commands_corrupt_json_returns_note(tmp_path: Path) -> None:
    _write(tmp_path, "compile_commands.json", "{not json")
    covered, found, notes = read_compile_commands(tmp_path)
    # 文件存在但不可解析，视为"没有可用编译数据库"，覆盖率保持未知（None）
    assert found is False
    assert covered == set()
    assert any("读取失败" in note for note in notes)


def test_file_api_targets_are_high_confidence(tmp_path: Path) -> None:
    reply = tmp_path / "build" / ".cmake" / "api" / "v1" / "reply"
    reply.mkdir(parents=True)
    _write(
        reply,
        "target-phy.json",
        json.dumps(
            {
                "name": "phy",
                "type": "STATIC_LIBRARY",
                "sources": [{"path": "src/a.c"}],
                "compileGroups": [{"defines": ["PHY=1"], "includes": [{"path": "include"}]}],
            }
        ),
    )
    _write(reply, "target-broken.json", "{oops")

    targets, notes = read_cmake_file_api(reply)
    assert [t.name for t in targets] == ["phy"]
    target = targets[0]
    assert target.kind == "library"
    assert target.build_system == "cmake-file-api"
    assert target.sources == ["src/a.c"]
    assert target.compile_definitions == ["PHY=1"]
    assert target.include_dirs == ["include"]
    assert target.confidence is Confidence.HIGH
    assert any("target-broken.json" in note for note in notes)

    dirs = find_cmake_file_api_dirs(tmp_path)
    assert reply.resolve() in [d.resolve() for d in dirs]
