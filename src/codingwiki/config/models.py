"""配置数据模型。

规则（对应开发计划 3.8 节）：
- 配置文件只接受 YAML；模型层统一校验，未知字段一律拒绝。
- 敏感信息不落配置：只写环境变量名。
- 公司内 Gauss API 每分钟最多 30 次，模型里做成硬上限，避免配置写错。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)


class ModelBackendName(StrEnum):
    """模型调用后端的名字。公司内默认走 Cline headless。"""

    CLINE = "cline"
    GAUSS = "gauss"
    API = "api"
    MOCK = "mock"


class _StrictModel(BaseModel):
    """所有配置模型的基类：拒绝未知字段、不可变、去掉字符串首尾空白。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ClineSettings(_StrictModel):
    """Cline headless 的启动方式。参数不写死在代码里，全部可配。"""

    command: list[str] = Field(default_factory=lambda: ["cline"], min_length=1)
    args: list[str] = Field(
        default_factory=lambda: [
            "--headless",
            "--workspace",
            "{workspace}",
            "--task-file",
            "{task_file}",
        ]
    )
    timeout_s: int = Field(default=1800, ge=1, le=86_400)
    model_name: str = ""
    telemetry: StrictBool = False

    @field_validator("command")
    @classmethod
    def _command_not_blank(cls, value: list[str]) -> list[str]:
        if not value[0].strip():
            raise ValueError("command 第一项不能为空")
        return value


class GaussSettings(_StrictModel):
    """公司内 Gauss API。默认关闭；启用时必须给出地址、模型名和密钥环境变量。"""

    enabled: StrictBool = False
    base_url: str = ""
    api_key_env: str = "CODINGWIKI_GAUSS_KEY"
    model: str = ""
    max_requests_per_minute: int = Field(default=30, ge=1, le=30)
    max_concurrent: int = Field(default=1, ge=1, le=4)
    daily_budget: int = Field(default=200, ge=0)

    @model_validator(mode="after")
    def _require_fields_when_enabled(self) -> GaussSettings:
        if not self.enabled:
            return self
        missing = [
            name
            for name, value in (
                ("base_url", self.base_url),
                ("model", self.model),
                ("api_key_env", self.api_key_env),
            )
            if not value
        ]
        if missing:
            raise ValueError("gauss 已启用但缺少字段: " + ", ".join(missing))
        return self


class ApiSettings(_StrictModel):
    """通用 API 模型后端（公司外使用；公司内改走 Cline）。

    密钥来源二选一，优先 api_key_file：
    - api_key_file：本机密钥文件路径（只写路径，不写内容）。**不要写进仓库内的
      configs/default.yaml**，只写进已 gitignore 的 configs/*.local.yaml；
    - api_key_env：环境变量名（默认 CODINGWIKI_API_KEY）。

    max_tokens 下限是 16384：实测 8192 会在页面 JSON 未闭合时被截断
    （finish_reason=length），Output 被丢弃，必须留足余量。
    """

    enabled: StrictBool = False
    base_url: str = ""
    api_key_env: str = "CODINGWIKI_API_KEY"
    api_key_file: str = ""
    model: str = ""
    timeout_s: int = Field(default=300, ge=1, le=86_400)
    max_retries: int = Field(default=3, ge=0, le=10)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=16_384, ge=16_384, le=131_072)


class ModelSettings(_StrictModel):
    """模型后端组合。同一时刻只有一个后端生效。"""

    backend: ModelBackendName = ModelBackendName.CLINE
    cline: ClineSettings = Field(default_factory=ClineSettings)
    gauss: GaussSettings = Field(default_factory=GaussSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)

    @model_validator(mode="after")
    def _backend_must_be_configured(self) -> ModelSettings:
        if self.backend is ModelBackendName.GAUSS and not self.gauss.enabled:
            raise ValueError("model.backend 为 gauss 时，必须同时设置 model.gauss.enabled: true")
        if self.backend is ModelBackendName.API:
            if not self.api.enabled:
                raise ValueError("model.backend 为 api 时，必须同时设置 model.api.enabled: true")
            missing = [
                name
                for name, value in (("base_url", self.api.base_url), ("model", self.api.model))
                if not value
            ]
            if missing:
                raise ValueError("model.api 缺少字段: " + ", ".join(missing))
            if not (self.api.api_key_env or self.api.api_key_file):
                raise ValueError("model.api 需要 api_key_env 或 api_key_file 之一")
        return self


class EmbeddingSettings(_StrictModel):
    """向量化模型。local=真实模型（bge-m3），hash=词面占位（离线/CI 默认）。

    model_path 为空表示「由运行时自动解析」：依次尝试 CODINGWIKI_MODEL_DIR、
    ~/models/BAAI/<name>、工作区 .cache/models/<name>；解析失败时才报错。
    """

    backend: Literal["local", "api", "hash"] = "local"
    model_path: str = ""
    device: Literal["cpu", "cuda", "mps", "auto"] = "auto"
    batch_size: int = Field(default=32, ge=1, le=512)
    max_length: int = Field(default=512, ge=32, le=8192)
    dimension: int = Field(default=1024, ge=1, le=8192)


class RerankerSettings(_StrictModel):
    """重排模型。默认关闭。model_path 为空时同 EmbeddingSettings 自动解析。"""

    enabled: StrictBool = False
    model_path: str = ""
    device: Literal["cpu", "cuda", "mps", "auto"] = "auto"
    batch_size: int = Field(default=16, ge=1, le=256)
    top_n: int = Field(default=8, ge=1, le=100)


class RetrievalSettings(_StrictModel):
    """检索参数：关键词与向量两路融合，可选重排。"""

    top_k: int = Field(default=20, ge=1, le=100)
    keyword_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    vector_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    # 真实语义模型下的拒答门槛（哈希后端改用规模自适应公式，不受此值影响）
    min_vector_score: float = Field(default=0.35, ge=0.0, le=1.0)
    rerank_pool: int = Field(default=20, ge=1, le=200)

    @model_validator(mode="after")
    def _weights_not_both_zero(self) -> RetrievalSettings:
        if self.keyword_weight + self.vector_weight <= 0:
            raise ValueError("keyword_weight 与 vector_weight 不能同时为 0")
        return self


class AppConfig(_StrictModel):
    """一份完整的主配置。"""

    config_version: str = Field(pattern=r"^\d+\.\d+$")
    id: str = Field(default="default", min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    model: ModelSettings = Field(default_factory=ModelSettings)
    embedding: EmbeddingSettings
    reranker: RerankerSettings = Field(default_factory=RerankerSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)


__all__ = [
    "ApiSettings",
    "AppConfig",
    "ClineSettings",
    "EmbeddingSettings",
    "GaussSettings",
    "ModelBackendName",
    "ModelSettings",
    "RerankerSettings",
    "RetrievalSettings",
]
