"""第三阶段编码任务包与后端协议（S19）。"""

from __future__ import annotations

from codingwiki.coding.backend import load_backend_config, run_backend
from codingwiki.coding.packet import build_packet
from codingwiki.coding.run import load_approved_spec, run_task
from codingwiki.coding.tasks import decompose

__all__ = [
    "build_packet",
    "decompose",
    "load_approved_spec",
    "load_backend_config",
    "run_backend",
    "run_task",
]
