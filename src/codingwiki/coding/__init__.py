"""第三阶段编码任务包、后端协议与独立验证（S19–S21）。"""

from __future__ import annotations

from codingwiki.coding.backend import load_backend_config, run_backend
from codingwiki.coding.packet import build_packet
from codingwiki.coding.run import load_approved_spec, run_task
from codingwiki.coding.tasks import decompose
from codingwiki.coding.verify import mechanical_report, verify_patch

__all__ = [
    "build_packet",
    "decompose",
    "load_approved_spec",
    "load_backend_config",
    "mechanical_report",
    "run_backend",
    "run_task",
    "verify_patch",
]
