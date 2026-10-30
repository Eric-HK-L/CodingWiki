"""S6 语义后端测试：构造校验、依赖缺失提示、设备解析。不加载真实权重。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codingwiki.errors import EnvironmentIssue, InputError
from codingwiki.retrieval.embed_bge import BgeM3Embedding, _resolve_device
from codingwiki.retrieval.rerank import BgeReranker


def test_embedder_rejects_non_positive_batch_size(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="batch_size"):
        BgeM3Embedding(tmp_path, batch_size=0)


def test_embedder_rejects_non_positive_max_length(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="max_length"):
        BgeM3Embedding(tmp_path, max_length=0)


def test_reranker_rejects_non_positive_batch_size(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="batch_size"):
        BgeReranker(tmp_path, batch_size=0)


def test_embedder_empty_input_does_not_load_model(tmp_path: Path) -> None:
    # 空列表直接返回，避免无意义地加载数 GB 权重
    assert BgeM3Embedding(tmp_path).embed([]) == []


def test_reranker_empty_input_does_not_load_model(tmp_path: Path) -> None:
    assert BgeReranker(tmp_path).score("q", []) == []


def test_embedder_missing_dependency_reports_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import codingwiki.retrieval.embed_bge as module

    def _missing(name: str, *_args: Any, **_kwargs: Any) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(module.importlib, "import_module", _missing)
    with pytest.raises(EnvironmentIssue, match="semantic"):
        BgeM3Embedding(tmp_path).embed(["x"])


def test_reranker_missing_dependency_reports_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import codingwiki.retrieval.rerank as module

    def _missing(name: str, *_args: Any, **_kwargs: Any) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(module.importlib, "import_module", _missing)
    with pytest.raises(EnvironmentIssue, match="semantic"):
        BgeReranker(tmp_path).score("q", ["passage"])


class _FakeBackends:
    class _MPS:
        def __init__(self, available: bool) -> None:
            self._available = available

        def is_available(self) -> bool:
            return self._available

    class _CUDA:
        def __init__(self, available: bool) -> None:
            self._available = available

        def is_available(self) -> bool:
            return self._available


class _FakeTorch:
    def __init__(self, *, mps: bool = False, cuda: bool = False) -> None:
        self.backends = type("B", (), {})()
        self.backends.mps = _FakeBackends._MPS(mps)
        self.backends.cuda = _FakeBackends._CUDA(cuda)
        self.cuda = _FakeBackends._CUDA(cuda)


def test_resolve_device_auto_prefers_mps() -> None:
    assert _resolve_device(_FakeTorch(mps=True), "auto") == "mps"


def test_resolve_device_auto_falls_back_to_cuda_then_cpu() -> None:
    assert _resolve_device(_FakeTorch(cuda=True), "auto") == "cuda"
    assert _resolve_device(_FakeTorch(), "auto") == "cpu"


def test_resolve_device_explicit_unavailable_raises() -> None:
    with pytest.raises(EnvironmentIssue, match="MPS"):
        _resolve_device(_FakeTorch(), "mps")
    with pytest.raises(EnvironmentIssue, match="CUDA"):
        _resolve_device(_FakeTorch(), "cuda")
