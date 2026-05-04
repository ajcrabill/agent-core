"""agent_core.work — the work-management layer.

Three submodules in this sprint:

  inbound.py    — capture pipeline: email/chat/peer-message → Obligation
                  (the L20 "every inbound spawns an obligation" half)

  pipeline_monitor.py
                — stalled-task detection (obligations that haven't moved in
                  N hours become incidents that surface in the agent's
                  context — so they can't slip)

  incidents.py  — helper for recording incidents from anywhere (tool calls,
                  cron failures, quality audits)

Cron watchdog lands when scheduling lands (later sprint).
"""

from agent_core.work.email_fetch import (
    EmailFetchError,
    EmailFetcher,
    FetchedEmail,
    FetchReport,
    fetch_and_capture,
)
from agent_core.work.email_send import (
    ComposeReport,
    EmailSender,
    EmailSendError,
    SendReport,
    compose_drafts,
    send_draft,
)
from agent_core.work.inbound import InboundCapture
from agent_core.work.incidents import IncidentRecorder
from agent_core.work.pipeline_monitor import (
    PipelineMonitor,
    StalledObligation,
    StalledScanResult,
)

__all__ = [
    "ComposeReport",
    "EmailFetchError",
    "EmailFetcher",
    "EmailSendError",
    "EmailSender",
    "FetchReport",
    "FetchedEmail",
    "InboundCapture",
    "IncidentRecorder",
    "PipelineMonitor",
    "SendReport",
    "StalledObligation",
    "StalledScanResult",
    "compose_drafts",
    "fetch_and_capture",
    "send_draft",
]
