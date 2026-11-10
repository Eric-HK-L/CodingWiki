"""模块映射（S37，优化架构 v1.0 §3.1/§14.1）。

ModuleMap 是 Wiki v2 的模块权威来源：由 `generate/cluster.py` 从事实层确定性构建，
落盘 `wiki/<repo>/module_map.json` 供校验与复算。纯数据模型，无 I/O。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _ModuleMapModel(BaseModel):
    """与领域层其余模型一致：拒绝未知字段、不可变、去首尾空白。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ModuleEntry(_ModuleMapModel):
    """一个模块：文件集合 + 来源信号 + 分层归属。"""

    module_key: str = Field(min_length=1)
    layer: str = ""  # "L<depth>"，由模块依赖图最长路径深度给出
    files: list[str] = Field(default_factory=list)
    build_targets: list[str] = Field(default_factory=list)
    cohesion_score: float = Field(default=0.0, ge=0.0)
    source_signals: list[str] = Field(default_factory=list)  # dir / build / call 的子集


class ModulePair(_ModuleMapModel):
    """一对协作模块（跨模块调用密度超阈值的互链）。"""

    a: str = Field(min_length=1)
    b: str = Field(min_length=1)
    calls: int = Field(default=0, ge=0)
    call_density: float = Field(default=0.0, ge=0.0)


class ModuleMap(_ModuleMapModel):
    """一次快照的模块划分结果。不变量：entries.files 是在范围文件集的划分。"""

    repo_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    cluster_version: str = Field(min_length=1)
    seed: int = 0
    entries: list[ModuleEntry] = Field(default_factory=list)
    collaborations: list[ModulePair] = Field(default_factory=list)

    @property
    def module_keys(self) -> list[str]:
        return [entry.module_key for entry in self.entries]

    def entry(self, module_key: str) -> ModuleEntry | None:
        for item in self.entries:
            if item.module_key == module_key:
                return item
        return None
