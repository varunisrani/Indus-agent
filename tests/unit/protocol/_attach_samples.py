"""One valid instance of every attach model (``indus.protocol.attach``) and of the attach events."""

from __future__ import annotations

from indus.protocol.attach import (
    AdmissionCancelParams,
    AdmissionCancelResult,
    AdmissionDiscardParams,
    AdmissionDiscardResult,
    AdmissionReceiptParams,
    AdmissionReceiptResult,
    EventsSinceParams,
    EventsSinceResult,
    MaintenanceRunParams,
    MaintenanceRunResult,
    ReplayGapInfo,
    RuntimeDescribeParams,
    RuntimeDescribeResult,
    SessionMutateParams,
    SessionMutateResult,
    SessionReplayGap,
    SessionsResync,
    SessionSubscribeParams,
    SessionSubscribeResult,
    SessionsWatchParams,
    SessionsWatchResult,
    SessionUnsubscribeParams,
    SessionUnsubscribeResult,
    SessionUpdated,
)
from indus.protocol.base import ProtocolModel
from indus.protocol.events import (
    AdmissionStateChanged,
    RequestSettled,
    SessionViewers,
    ViewerCount,
)

SID = "s-1"
EPOCH = "e" * 16

ATTACH_SAMPLES: list[ProtocolModel] = [
    ReplayGapInfo(reason="evicted", replay_epoch=EPOCH, last_seq=9),
    SessionSubscribeParams(session_id=SID, epoch=EPOCH, cursor=3),
    SessionSubscribeResult(session_id=SID, replay_epoch=EPOCH, last_seq=9, replayed=6),
    SessionUnsubscribeParams(session_id=SID),
    SessionUnsubscribeResult(unsubscribed=True),
    SessionsWatchParams(enabled=False),
    SessionsWatchResult(watching=False),
    EventsSinceParams(session_id=SID, epoch=EPOCH, cursor=3),
    EventsSinceResult(
        session_id=SID,
        replay_epoch=EPOCH,
        last_seq=9,
        gap=ReplayGapInfo(reason="ahead", replay_epoch=EPOCH, last_seq=9),
    ),
    AdmissionDiscardParams(session_id=SID, admission_id="adm-1", generation=4),
    AdmissionDiscardResult(discarded=False, reason="stale_generation"),
    AdmissionReceiptParams(session_id=SID, admission_id="adm-1"),
    AdmissionReceiptResult(
        admission_id="adm-1", session_id=SID, state="queued", position=2, blocked_by="adm-0"
    ),
    AdmissionCancelParams(session_id=SID, admission_id="adm-1"),
    AdmissionCancelResult(cancelled=True, state="terminal", outcome="cancelled"),
    AdmissionCancelResult(cancelled=False, state="started", generation=3),
    SessionMutateParams(
        session_id=SID,
        operation="editor_mcp",
        payload={"mcp_servers": [{"name": "docs", "command": "docs-server", "args": []}]},
        request_id="r-1",
        generation=2,
    ),
    SessionMutateResult(status="applied", servers=["docs"], changed=[]),
    SessionMutateResult(status="refused", reason="editor_mcp_changed", refused_servers=["docs"]),
    SessionMutateResult(status="confirm", message="the window is small", model="m", provider="p"),
    RuntimeDescribeParams(),
    RuntimeDescribeResult(
        profile_id="0" * 16,
        instance_id="1" * 16,
        code_version="0.1.0",
        protocol_version=1,
        replay_epoch=EPOCH,
        ws_url="ws://127.0.0.1:4100/ws",
        creatable_sources=["cli", "tui"],
        capabilities=["events.since"],
    ),
    MaintenanceRunParams(op="prune", ids=["a", "b"]),
    MaintenanceRunResult(op="delete", refused="guarded", detail="a turn holds the session"),
    SessionReplayGap(session_id=SID, replay_epoch=EPOCH, last_seq=9, reason="overflow"),
    SessionUpdated(session_id=SID, last_activity=1.5, running=True, title="t"),
    SessionsResync(reason="overflow"),
    ViewerCount(surface="tui", count=2),
    SessionViewers(
        session_id=SID,
        seq=20,
        viewers=[ViewerCount(surface="cli", count=1), ViewerCount(surface="tui", count=1)],
        count=2,
    ),
    RequestSettled(
        session_id=SID,
        seq=21,
        request_id="srq-0123456789ab",
        kind="approval",
        outcome="withdrawn",
        reason="turn_ended",
    ),
    AdmissionStateChanged(
        session_id=SID, seq=22, admission_id="adm-1", state="terminal", outcome="completed"
    ),
]
