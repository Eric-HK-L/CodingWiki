"""真实重排后端：bge-reranker-v2-m3（架构文档 v0.6 15.2）。

cross-encoder：把 (query, passage) 成对送入 XLMRobertaForSequenceClassification，
`sigmoid(logits)` 作为相关性分。只对融合后的候选池重排，成本可控。
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Protocol

from codingwiki.errors import EnvironmentIssue, InputError


class RerankerBackend(Protocol):
    """把候选资料按与查询的相关性重新打分。分数越高越相关，范围 [0, 1]。"""

    @property
    def name(self) -> str: ...

    def score(self, query: str, passages: list[str]) -> list[float]: ...


class BgeReranker:
    """bge-reranker-v2-m3。首次 score 才加载权重。"""

    def __init__(
        self,
        model_path: str | Path,
        *,
        device: str = "auto",
        batch_size: int = 16,
        max_length: int = 512,
    ) -> None:
        if batch_size <= 0:
            raise InputError(f"batch_size 必须为正，得到 {batch_size}")
        self._model_path = Path(model_path).expanduser()
        self._device_pref = device
        self._batch_size = batch_size
        self._max_length = max_length
        self._torch: Any = None
        self._tok: Any = None
        self._model: Any = None
        self._device = "cpu"

    @property
    def name(self) -> str:
        return "bge-reranker-v2-m3"

    def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        self._ensure_loaded()
        scores: list[float] = []
        for start in range(0, len(passages), self._batch_size):
            batch = passages[start : start + self._batch_size]
            encoded = self._tok(
                [query] * len(batch),
                batch,
                padding=True,
                truncation=True,
                max_length=self._max_length,
                return_tensors="pt",
            )
            encoded = {key: value.to(self._device) for key, value in encoded.items()}
            with self._torch.no_grad():
                logits = self._model(**encoded).logits.view(-1)
                probs = self._torch.sigmoid(logits)
            scores.extend(float(value) for value in probs.float().cpu().tolist())
        return scores

    # ---------------------------------------------------------------- 内部

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        try:
            torch = importlib.import_module("torch")
            transformers = importlib.import_module("transformers")
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise EnvironmentIssue(
                "缺少语义依赖 torch/transformers；请执行: uv pip install -e '.[semantic]'"
            ) from exc
        if not self._model_path.is_dir():
            raise InputError(
                f"重排模型路径不存在: {self._model_path}；"
                "可用 --rerank-model-path 或 CODINGWIKI_MODEL_DIR 指定"
            )
        from codingwiki.retrieval.embed_bge import _resolve_device

        self._device = _resolve_device(torch, self._device_pref)
        self._tok = transformers.AutoTokenizer.from_pretrained(str(self._model_path))
        try:
            model = transformers.AutoModelForSequenceClassification.from_pretrained(
                str(self._model_path), dtype=torch.float32
            )
        except TypeError:  # 老版本 transformers 用 torch_dtype
            model = transformers.AutoModelForSequenceClassification.from_pretrained(
                str(self._model_path), torch_dtype=torch.float32
            )
        self._model = model.eval().to(self._device)
        self._torch = torch


__all__ = ["BgeReranker", "RerankerBackend"]
