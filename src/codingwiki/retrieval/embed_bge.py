"""真实嵌入后端：bge-m3（架构文档 v0.6 15.2）。

用 transformers 直接加载 XLM-R，CLS pooling + L2 归一（对齐 bge-m3 的
`1_Pooling/config.json`）。不依赖 sentence-transformers，因为本机模型目录里
`modules.json` 引用的 `2_Normalize` 子目录缺失，ST 会加载失败。

torch / transformers 用 importlib 惰性导入：
- 未安装时抛环境错误并给出安装命令，`hash` 后端不受影响；
- mypy 与 CI 在无 torch 环境下仍可检查本模块。
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from codingwiki.errors import EnvironmentIssue, InputError

ProgressFn = Callable[[int, int], None]


class BgeM3Embedding:
    """bge-m3 嵌入：CLS pooling + L2 归一，1024 维。首次 embed 才加载权重。"""

    def __init__(
        self,
        model_path: str | Path,
        *,
        device: str = "auto",
        batch_size: int = 32,
        max_length: int = 512,
        dim: int = 1024,
        progress: ProgressFn | None = None,
    ) -> None:
        if batch_size <= 0:
            raise InputError(f"batch_size 必须为正，得到 {batch_size}")
        if max_length <= 0:
            raise InputError(f"max_length 必须为正，得到 {max_length}")
        self._model_path = Path(model_path).expanduser()
        self._device_pref = device
        self._batch_size = batch_size
        self._max_length = max_length
        self._dim = dim
        self._progress = progress
        self._torch: Any = None
        self._tok: Any = None
        self._model: Any = None
        self._device = "cpu"

    @property
    def name(self) -> str:
        return f"bge-m3-d{self._dim}"

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def device(self) -> str:
        """实际使用的设备（加载后才确定）。"""
        return self._device

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self._ensure_loaded()
        vectors: list[list[float] | None] = [None] * len(texts)
        pending = [(i, t) for i, t in enumerate(texts) if t.strip()]
        done = 0
        for start in range(0, len(pending), self._batch_size):
            batch = pending[start : start + self._batch_size]
            encoded = self._tok(
                [text for _, text in batch],
                padding=True,
                truncation=True,
                max_length=self._max_length,
                return_tensors="pt",
            )
            encoded = {key: value.to(self._device) for key, value in encoded.items()}
            with self._torch.no_grad():
                hidden = self._model(**encoded).last_hidden_state[:, 0]
                hidden = self._torch.nn.functional.normalize(hidden, p=2, dim=1)
            rows = hidden.float().cpu().tolist()
            for (index, _), row in zip(batch, rows, strict=True):
                vectors[index] = [float(value) for value in row]
            done = min(done + len(batch), len(pending))
            if self._progress is not None:
                self._progress(done, len(pending))
        zero = [0.0] * self._dim
        return [vector if vector is not None else zero for vector in vectors]

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
                f"嵌入模型路径不存在: {self._model_path}；"
                "可用 --model-path 或 CODINGWIKI_MODEL_DIR 指定"
            )
        self._device = _resolve_device(torch, self._device_pref)
        self._tok = transformers.AutoTokenizer.from_pretrained(str(self._model_path))
        try:
            model = transformers.AutoModel.from_pretrained(
                str(self._model_path), dtype=torch.float32
            )
        except TypeError:  # 老版本 transformers 用 torch_dtype
            model = transformers.AutoModel.from_pretrained(
                str(self._model_path), torch_dtype=torch.float32
            )
        hidden_size = int(model.config.hidden_size)
        if hidden_size != self._dim:
            raise InputError(
                f"模型维度 {hidden_size} 与配置 dimension={self._dim} 不一致；"
                f"请把 dimension 设为 {hidden_size}"
            )
        self._model = model.eval().to(self._device)
        self._torch = torch


def _resolve_device(torch: Any, preference: str) -> str:
    """auto 时优先 MPS，其次 CUDA，最后 CPU；显式指定但不可用则报错。"""
    if preference == "auto":
        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
        return "cpu"
    if preference == "mps" and not torch.backends.mps.is_available():
        raise EnvironmentIssue("本机 MPS 不可用；请改用 --device cpu 或 auto")
    if preference == "cuda" and not torch.cuda.is_available():
        raise EnvironmentIssue("本机 CUDA 不可用；请改用 --device cpu 或 auto")
    return preference


__all__ = ["BgeM3Embedding"]
