"""S9 评测基线测试：金标准加载、命中判定、指标计算、拒答统计、报告落盘与对比。"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from codingwiki.domain.eval import GoldenSet
from codingwiki.domain.rag import Chunk
from codingwiki.errors import ConfigFormatError, ConfigNotFoundError, ConfigValidationError
from codingwiki.eval import EvalHarness, load_golden, load_previous_report, save_report
from codingwiki.eval.harness import _matches_expectation, _ndcg_binary
from codingwiki.retrieval import HashEmbedding, HybridSearcher

# ---------------------------------------------------------------- golden 加载

VALID = """
set_id: demo-v1
repo_id: demo
questions:
  - id: Q01
    question: "LDPC 在哪？"
    expect_any: ["module/src/cuphy/ldpc", "src/cuphy/ldpc/"]
  - id: R01
    question: "怎么部署 K8s？"
    expect_refused: true
"""


def test_load_golden_valid(tmp_path: Path) -> None:
    path = tmp_path / "demo-v1.yaml"
    path.write_text(VALID, encoding="utf-8")
    golden = load_golden(path)
    assert golden.set_id == "demo-v1"
    assert len(golden.questions) == 2
    assert golden.questions[1].expect_refused is True


def test_load_golden_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigNotFoundError):
        load_golden(tmp_path / "nope.yaml")


def test_load_golden_rejects_dup_question_id(tmp_path: Path) -> None:
    path = tmp_path / "dup.yaml"
    path.write_text(
        VALID.replace("id: R01", "id: Q01"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigValidationError, match="重复"):
        load_golden(path)


def test_load_golden_rejects_dup_yaml_key(tmp_path: Path) -> None:
    path = tmp_path / "dupkey.yaml"
    # 顶层 set_id 重复：StrictLoader 的重复键检查必须报错
    path.write_text(VALID + "\nset_id: other\n", encoding="utf-8")
    with pytest.raises(ConfigFormatError, match="重复"):
        load_golden(path)


def test_golden_question_expectations_consistent() -> None:
    with pytest.raises(ValueError, match="expect_any"):
        GoldenSet.model_validate(
            {
                "set_id": "s",
                "repo_id": "r",
                "questions": [
                    {"id": "Q1", "question": "x", "expect_refused": True, "expect_any": ["a"]}
                ],
            }
        )
    with pytest.raises(ValueError, match="expect_any"):
        GoldenSet.model_validate(
            {"set_id": "s", "repo_id": "r", "questions": [{"id": "Q1", "question": "x"}]}
        )


# ---------------------------------------------------------------- 命中判定


def test_matches_expectation_page_and_file() -> None:
    assert _matches_expectation("", "module/src/cuphy/ldpc", ("module/src/cuphy/ldpc",))
    assert _matches_expectation("src/cuphy/ldpc/decode.cu", "", ("src/cuphy/ldpc/",))
    assert _matches_expectation("src/cuphy/ldpc", "", ("src/cuphy/ldpc",))
    assert not _matches_expectation("src/cuphy/ldpcx/a.c", "", ("src/cuphy/ldpc/",))
    assert not _matches_expectation("other.c", "", ("src/cuphy/ldpc/",))


# ---------------------------------------------------------------- 指标计算


def test_ndcg_binary_known_values() -> None:
    # 命中第 1 名，且全库只有 1 个相关块 → 完美排序
    assert _ndcg_binary([1], 1, 10) == pytest.approx(1.0)
    # 命中第 2 名，1 个相关块：DCG = 1/log2(3) ≈ 0.631
    assert _ndcg_binary([2], 1, 10) == pytest.approx(1.0 / math.log2(3), rel=1e-6)
    # 无命中
    assert _ndcg_binary([], 3, 10) == 0.0
    # 两个相关块都命中前两名
    assert _ndcg_binary([1, 2], 2, 10) == pytest.approx(1.0)


def _chunk(
    chunk_id: str, text: str, *, module: str = "", file: str = "", page_id: str = ""
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        source_type="wiki" if page_id else "code",
        repo_id="r",
        snapshot_id="r@1",
        file=file,
        module=module,
        title=chunk_id,
        text=text,
        trust_tier="wiki" if page_id else "code",
        page_id=page_id,
    )


def _harness(tmp_path: Path, chunks: list[Chunk], monkeypatch: pytest.MonkeyPatch) -> EvalHarness:
    from codingwiki.retrieval.answer import AnswerPipeline

    monkeypatch.setenv("CODINGWIKI_HOME", str(tmp_path / ".cw"))
    embedder = HashEmbedding()
    texts = [f"{c.title}\n{c.text}" for c in chunks]
    searcher = HybridSearcher(chunks, embedder.embed(texts), embedder)
    from codingwiki.backends import MockBackend

    pipeline = AnswerPipeline(MockBackend(), searcher, base_dir=tmp_path / ".cw")
    return EvalHarness(
        searcher,
        chunks,
        pipeline,
        known_pages={"module/ldpc", "build"},
        known_paths={"src/cuphy/ldpc/decode.cu"},
    )


def _golden() -> GoldenSet:
    return GoldenSet.model_validate(
        {
            "set_id": "t-v1",
            "repo_id": "r",
            "questions": [
                {
                    "id": "Q1",
                    "question": "ldpc 译码模块",
                    "expect_any": ["module/ldpc", "src/cuphy/ldpc/"],
                },
                {"id": "Q2", "question": "构建目标 build target 页面", "expect_any": ["build"]},
                {"id": "R1", "question": "kubernetes 部署集群教程", "expect_refused": True},
            ],
        }
    )


def test_harness_runs_and_computes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [
        _chunk("c1", "ldpc 译码模块 LDPC decoder", page_id="module/ldpc"),
        _chunk("c2", "ldpc decode kernel 实现", file="src/cuphy/ldpc/decode.cu"),
        _chunk("c3", "build target cuphy 库构建", page_id="build"),
        _chunk("c4", "完全无关的内容 xyzzy", file="misc/readme.txt"),
    ]
    harness = _harness(tmp_path, chunks, monkeypatch)
    report = harness.run(_golden(), top_k=3)

    assert report.question_count == 3
    assert report.recall_at_k == 1.0  # Q1/Q2 都能命中
    assert report.mrr is not None and report.mrr > 0
    assert report.refusal_total == 1
    assert report.refusal_correct + report.false_refusal <= 3
    assert len(report.per_question) == 3


def test_harness_records_top1_features(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 每题必须记录 Top1 命中特征（拒答阈值标定依据，架构 §15.5 v0.8）
    chunks = [
        _chunk("c1", "ldpc 译码模块 LDPC decoder", page_id="module/ldpc"),
        _chunk("c2", "完全无关的内容 xyzzy", file="misc/readme.txt"),
    ]
    harness = _harness(tmp_path, chunks, monkeypatch)
    report = harness.run(_golden(), top_k=3)

    by_id = {q.question_id: q for q in report.per_question}
    # 拒答题 R1 也会被采集特征（哈希嵌入下 query 词面无命中 → 特征为默认零值）
    r1 = by_id["R1"]
    assert r1.top_vector_score >= 0.0
    assert r1.top_bm25_rank >= 0
    # 可答题 Q1：命中资料页，Top1 向量分与 BM25 rank 均非零
    q1 = by_id["Q1"]
    assert q1.top_vector_score > 0.0
    assert q1.top_bm25_rank >= 1


def test_harness_counts_misses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [
        _chunk("c1", "ldpc 译码模块 LDPC decoder", page_id="module/ldpc"),
        _chunk("c2", "无关内容", file="misc/x.txt"),
    ]
    harness = _harness(tmp_path, chunks, monkeypatch)
    golden = GoldenSet.model_validate(
        {
            "set_id": "t-v2",
            "repo_id": "r",
            "questions": [
                {"id": "Q1", "question": "ldpc", "expect_any": ["module/ldpc"]},
                {
                    "id": "Q2",
                    "question": "不存在的主题 quantum teleport",
                    "expect_any": ["module/ghost"],
                },
            ],
        }
    )
    report = harness.run(golden, top_k=3)
    assert report.recall_at_k == 0.5
    assert [q.question_id for q in report.misses] == ["Q2"]


# ---------------------------------------------------------------- 报告落盘与对比


def test_save_and_load_previous_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _harness(tmp_path, [_chunk("c1", "ldpc", page_id="module/ldpc")], monkeypatch)
    report = harness.run(_golden(), top_k=3)
    path = save_report(report, "r")
    assert path.is_file()

    previous = load_previous_report("r", "t-v1")
    assert previous is not None
    assert previous.set_id == "t-v1"
    assert previous.recall_at_k == report.recall_at_k

    assert load_previous_report("r", "other-v9") is None
