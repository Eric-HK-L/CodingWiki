"""第三阶段编码任务包、后端协议与独立验证（S19–S21）。"""

from __future__ import annotations

from codingwiki.coding.audit import AuditRunner, AuditSink, assert_command_allowed, audit_count
from codingwiki.coding.backend import load_backend_config, run_backend
from codingwiki.coding.delivery import build_delivery, render_delivery, render_pull_request
from codingwiki.coding.escalate import build_escalation, escalation_reason, render_escalation
from codingwiki.coding.packet import build_packet
from codingwiki.coding.redgate import check_red_gate
from codingwiki.coding.replay import replay_run
from codingwiki.coding.run import load_approved_spec, run_task
from codingwiki.coding.tasks import decompose
from codingwiki.coding.verify import mechanical_report, verify_patch

__all__ = [
    "AuditRunner",
    "AuditSink",
    "assert_command_allowed",
    "audit_count",
    "build_escalation",
    "build_delivery",
    "build_packet",
    "check_red_gate",
    "decompose",
    "escalation_reason",
    "load_approved_spec",
    "load_backend_config",
    "mechanical_report",
    "render_delivery",
    "render_escalation",
    "render_pull_request",
    "replay_run",
    "run_backend",
    "run_task",
    "verify_patch",
]
