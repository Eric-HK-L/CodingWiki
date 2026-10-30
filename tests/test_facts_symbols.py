"""tree-sitter 符号提取单元测试：C / C++ / CUDA / Python 正例与反例。"""

from __future__ import annotations

from pathlib import Path

import pytest

from codingwiki.domain.facts import SymbolKind
from codingwiki.facts.symbols import extract_file_facts, scan_env_reads

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "facts" / "sample"


def _extract(tmp_path: Path, rel: str, body: str, language: str) -> object:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return extract_file_facts(path, rel, language, "sha-test", "src")


def _names(facts: object) -> dict[str, str]:
    from codingwiki.domain.facts import FileFacts

    assert isinstance(facts, FileFacts)
    return {symbol.qualified_name: symbol.kind.value for symbol in facts.symbols}


def test_c_symbols_includes_macros_structs_enums_and_functions(tmp_path: Path) -> None:
    facts = _extract(
        tmp_path,
        "src/demo.c",
        """
#include <stdio.h>
#define MAX_LEN 32
typedef struct point { int x; int y; } point_t;
enum color { RED, GREEN };
int add(int a, int b) { return a + b; }
int main(void) { add(1, 2); printf("ok"); return 0; }
""",
        "c",
    )
    assert isinstance(facts, object)
    names = _names(facts)
    assert names["MAX_LEN"] == SymbolKind.MACRO.value
    assert names["point"] == SymbolKind.STRUCT.value
    assert names["point_t"] == SymbolKind.TYPE_ALIAS.value
    assert names["color"] == SymbolKind.ENUM.value
    assert names["add"] == SymbolKind.FUNCTION.value
    assert names["main"] == SymbolKind.FUNCTION.value

    from codingwiki.domain.facts import FileFacts

    assert isinstance(facts, FileFacts)
    assert facts.parse_ok is True
    assert [entry.kind for entry in facts.entries] == ["main"]
    assert {include.target for include in facts.includes} == {"stdio.h"}
    assert {call.callee for call in facts.calls} == {"add", "printf"}


def test_cpp_namespaces_classes_and_out_of_class_methods(tmp_path: Path) -> None:
    facts = _extract(
        tmp_path,
        "src/widget.cpp",
        """
namespace ns {
class Widget {
public:
    void draw();
    int size;
};
void Widget::draw() { render(); }
void helper() {}
using IntList = int;
}
""",
        "cpp",
    )
    names = _names(facts)
    assert names["ns"] == SymbolKind.NAMESPACE.value
    assert names["ns::Widget"] == SymbolKind.CLASS.value
    assert names["ns::Widget::draw"] == SymbolKind.METHOD.value
    assert names["ns::helper"] == SymbolKind.FUNCTION.value
    assert names["ns::IntList"] == SymbolKind.TYPE_ALIAS.value

    from codingwiki.domain.facts import FileFacts

    assert isinstance(facts, FileFacts)
    draw = next(symbol for symbol in facts.symbols if symbol.name == "draw")
    assert draw.parent == "ns::Widget"
    assert draw.signature.startswith("void Widget::draw()")


def test_cuda_kernel_device_function_and_launch(tmp_path: Path) -> None:
    source = (FIXTURES / "src" / "main.cu").read_text(encoding="utf-8")
    facts = _extract(tmp_path, "src/main.cu", source, "cuda")
    names = _names(facts)
    assert names["add_kernel"] == SymbolKind.KERNEL.value
    assert names["scale"] == SymbolKind.DEVICE_FUNCTION.value
    assert names["phy::Engine::run"] == SymbolKind.METHOD.value
    assert names["main"] == SymbolKind.FUNCTION.value

    from codingwiki.domain.facts import FileFacts

    assert isinstance(facts, FileFacts)
    # CUDA 限定符和启动语法已挖空，因此仍应按正常 C++ 解析
    assert facts.parse_ok is True
    launches = [call for call in facts.calls if call.kind == "kernel_launch"]
    assert [(call.callee, call.line) for call in launches] == [("add_kernel", 25)]
    assert [entry.kind for entry in facts.entries].count("kernel_launch") == 1


def test_python_symbols_imports_and_main(tmp_path: Path) -> None:
    source = (FIXTURES / "tests" / "test_sample.py").read_text(encoding="utf-8")
    facts = _extract(tmp_path, "tests/test_sample.py", source, "python")
    names = _names(facts)
    assert names["TestEngine"] == SymbolKind.CLASS.value
    assert names["TestEngine.test_run_returns_zero"] == SymbolKind.METHOD.value
    assert names["test_helper_increments"] == SymbolKind.FUNCTION.value
    assert names["helper"] == SymbolKind.FUNCTION.value

    from codingwiki.domain.facts import FileFacts

    assert isinstance(facts, FileFacts)
    targets = {include.target for include in facts.includes}
    assert "__future__" in targets
    assert "os" in targets
    assert [entry.kind for entry in facts.entries] == ["python_main"]
    assert {call.callee for call in facts.calls} == {"helper", "os.getenv"}


def test_parse_failure_is_reported_not_hidden(tmp_path: Path) -> None:
    facts = _extract(tmp_path, "src/broken.c", "int broken( { return 1;\n", "c")

    from codingwiki.domain.facts import FileFacts

    assert isinstance(facts, FileFacts)
    assert facts.parse_ok is False
    assert facts.parse_errors >= 1


def test_unsupported_language_returns_no_facts(tmp_path: Path) -> None:
    facts = _extract(tmp_path, "docs/readme.md", "# title\n", "text")

    from codingwiki.domain.facts import FileFacts

    assert isinstance(facts, FileFacts)
    assert facts.symbols == []
    assert facts.line_count == 1


def test_scan_env_reads_covers_getenv_environ_and_argparse() -> None:
    text = """
const char* a = std::getenv("CW_A");
x = os.getenv("CW_B", "0")
y = os.environ["CW_C"]
parser.add_argument("--cw-d")
"""
    found = scan_env_reads(text, "src/x.py")
    assert {key for key, _, _ in found} == {"CW_A", "CW_B", "CW_C", "--cw-d"}
    assert found[0][1] == 2
    assert found[-1][2] == "argparse"


@pytest.mark.parametrize("language", ["c", "cpp", "cuda", "python"])
def test_empty_file_is_parsed_without_error(tmp_path: Path, language: str) -> None:
    facts = _extract(tmp_path, "src/empty.txt", "", language)

    from codingwiki.domain.facts import FileFacts

    assert isinstance(facts, FileFacts)
    assert facts.parse_ok is True
