"""分词测试（架构文档 v0.5 14.2）。"""

from __future__ import annotations

from codingwiki.retrieval import identifiers, tokenize


def test_camel_case_splits_and_keeps_original() -> None:
    tokens = tokenize("getHTTPResponse")
    assert "gethttpresponse" in tokens  # 原词小写保留
    for part in ("get", "http", "response"):
        assert part in tokens


def test_snake_case_splits_and_keeps_original() -> None:
    tokens = tokenize("cuda_launch_kernel")
    assert "cuda_launch_kernel" in tokens
    for part in ("cuda", "launch", "kernel"):
        assert part in tokens


def test_plain_english_lowercased() -> None:
    assert tokenize("Threads") == ["threads"]


def test_cjk_bigram() -> None:
    assert tokenize("流水线") == ["流水", "水线"]
    assert tokenize("核") == ["核"]  # 单字直接成词


def test_mixed_code_and_cjk() -> None:
    tokens = tokenize("cuphyCreateTensorDescriptor 创建张量")
    assert "cuphycreatetensordescriptor" in tokens
    assert "tensor" in tokens
    assert "创建" in tokens
    assert "张" not in tokens  # CJK 段只出 bigram，不出单字（段长 > 1）


def test_numbers_kept() -> None:
    tokens = tokenize("版本 v2 耗时 3.14")
    assert "v2" in tokens
    assert "3.14" in tokens


def test_identifiers_extracts_code_like_words() -> None:
    found = identifiers("call cudaLaunchKernel and plain words, plus snake_case_id")
    assert "cudaLaunchKernel" in found
    assert "snake_case_id" in found
    assert "plain" not in found
    assert "words" not in found


def test_identifiers_empty_on_plain_text() -> None:
    assert identifiers("hello world") == set()
