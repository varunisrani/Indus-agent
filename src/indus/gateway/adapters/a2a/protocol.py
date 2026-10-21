# pyright: strict
"""A2A (Agent2Agent) protocol v1.0 shapes and the per-peer task store (P24-10).

Pinned to the A2A specification tag 1.0.x: JSON-RPC 2.0 over HTTP, the agent card at
``/.well-known/agent-card.json``, PascalCase methods (the 0.3 slash names are accepted as
aliases), task states and roles in SCREAMING_SNAKE_CASE, parts told apart by which member they
carry (``text``, ``url``, ``raw``, ``data``). Error codes: the JSON-RPC ones plus the A2A block
(−32001 task not found, −32002 not cancelable, −32003 push notifications not supported, −32004
unsupported operation); in the implementation range, −32050 answers a call without valid
credentials and −32051 one over the per-peer rate.

Everything here is plain data: the adapter (``adapter.py``) serves it and the ``a2a_call`` tool
(``tools.py``) speaks it to other agents.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final, cast

__all__ = [
    "CARD_PATH",
    "LEGACY_CARD_PATH",
    "METHODS",
    "PROTOCOL_VERSION",
    "A2AError",
    "SendRequest",
    "TaskRecord",
    "TaskStore",
    "agent_card",
    "artifact_update",
    "make_message",
    "message_text",
    "read_send_params",
    "rpc_error",
    "rpc_result",
    "sse_frame",
    "status_update",
    "task_view",
    "text_part",
]

PROTOCOL_VERSION: Final = "1.0"
CARD_PATH: Final = "/.well-known/agent-card.json"
LEGACY_CARD_PATH: Final = "/.well-known/agent.json"

SUBMITTED: Final = "TASK_STATE_SUBMITTED"
WORKING: Final = "TASK_STATE_WORKING"
INPUT_REQUIRED: Final = "TASK_STATE_INPUT_REQUIRED"
COMPLETED: Final = "TASK_STATE_COMPLETED"
FAILED: Final = "TASK_STATE_FAILED"
CANCELED: Final = "TASK_STATE_CANCELED"
REJECTED: Final = "TASK_STATE_REJECTED"
STATES: Final = frozenset(
    {SUBMITTED, WORKING, INPUT_REQUIRED, COMPLETED, FAILED, CANCELED, REJECTED}
)
# A task in one of these is finished for this request (input-required ends the call too: the peer
# answers with a new message in the same context).
TERMINAL: Final = frozenset({INPUT_REQUIRED, COMPLETED, FAILED, CANCELED, REJECTED})

ROLE_USER: Final = "ROLE_USER"
ROLE_AGENT: Final = "ROLE_AGENT"

PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603
TASK_NOT_FOUND: Final = -32001
TASK_NOT_CANCELABLE: Final = -32002
PUSH_NOT_SUPPORTED: Final = -32003
UNSUPPORTED_OPERATION: Final = -32004
UNAUTHORIZED: Final = -32050
RATE_LIMITED: Final = -32051

# JSON-RPC method → what it does here.
METHODS: Final[Mapping[str, str]] = {
    "SendMessage": "send",
    "message/send": "send",
    "SendStreamingMessage": "stream",
    "message/stream": "stream",
    "GetTask": "get",
    "tasks/get": "get",
    "CancelTask": "cancel",
    "tasks/cancel": "cancel",
    "ListTasks": "list",
    "tasks/list": "list",
    "SubscribeToTask": "unsupported",
    "tasks/resubscribe": "unsupported",
    "GetExtendedAgentCard": "unsupported",
    "CreateTaskPushNotificationConfig": "push",
    "GetTaskPushNotificationConfig": "push",
    "ListTaskPushNotificationConfigs": "push",
    "DeleteTaskPushNotificationConfig": "push",
    "tasks/pushNotificationConfig/set": "push",
    "tasks/pushNotificationConfig/get": "push",
    "tasks/pushNotificationConfig/list": "push",
    "tasks/pushNotificationConfig/delete": "push",
}
# The 1.0 method names answer ``{"task": ...}`` for a send; the 0.3 aliases a bare task.
WRAPPED_SEND: Final = frozenset({"SendMessage", "SendStreamingMessage"})

CONTEXT_ID: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
MAX_TEXT: Final = 256 * 1024
TERMINAL_KEPT: Final = 500
PAGE_MAX: Final = 100


class A2AError(Exception):
    """A JSON-RPC error to answer with."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# -- JSON-RPC ------------------------------------------------------------------------------------


def rpc_result(request_id: object, result: object) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def rpc_error(request_id: object, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def sse_frame(request_id: object, payload: Mapping[str, Any]) -> str:
    """One stream response as a server-sent event (a full JSON-RPC envelope per event)."""
    return f"data: {json.dumps(rpc_result(request_id, dict(payload)), ensure_ascii=False)}\n\n"


# -- messages and parts --------------------------------------------------------------------------


def text_part(text: str) -> dict[str, Any]:
    return {"text": text}


def make_message(
    role: str,
    text: str,
    *,
    context_id: str | None = None,
    task_id: str | None = None,
    message_id: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "messageId": message_id or uuid.uuid4().hex,
        "role": role,
        "parts": [text_part(text)],
    }
    if context_id:
        message["contextId"] = context_id
    if task_id:
        message["taskId"] = task_id
    return message


def _part_text(part: Mapping[str, Any]) -> str | None:
    text = part.get("text")
    if isinstance(text, str):
        return text
    name = part.get("filename") or part.get("name") or ""
    url = part.get("url")
    if isinstance(url, str) and url:
        return f"[file {name}: {url}]" if name else f"[file: {url}]"
    raw = part.get("raw")
    if isinstance(raw, str):
        return (
            f"[file {name}: {len(raw)} base64 characters, not opened]"
            if name
            else (f"[file: {len(raw)} base64 characters, not opened]")
        )
    if "data" in part:
        try:
            return "[data]\n" + json.dumps(part.get("data"), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return "[data]"
    return None


def message_text(message: object) -> str:
    """The readable text of a message (or of anything with ``parts``)."""
    if not isinstance(message, Mapping):
        return ""
    parts = cast(Mapping[str, Any], message).get("parts")
    if not isinstance(parts, list):
        return ""
    chunks: list[str] = []
    for part in cast(list[object], parts):
        if isinstance(part, Mapping):
            found = _part_text(cast(Mapping[str, Any], part))
            if found:
                chunks.append(found)
    return "\n".join(chunks).strip()


@dataclass(frozen=True)
class SendRequest:
    text: str
    context_id: str | None
    message_id: str | None


def read_send_params(params: object) -> SendRequest:
    """The message of a ``SendMessage`` call (``A2AError`` −32602 when it cannot be read)."""
    if not isinstance(params, Mapping):
        raise A2AError(INVALID_PARAMS, "params must be an object")
    message = cast(Mapping[str, Any], params).get("message")
    if not isinstance(message, Mapping):
        raise A2AError(INVALID_PARAMS, "params.message must be an object")
    body = cast(Mapping[str, Any], message)
    if not isinstance(body.get("parts"), list):
        raise A2AError(INVALID_PARAMS, "params.message.parts must be a list")
    role = body.get("role")
    if role is not None and role not in (ROLE_USER, "user"):
        raise A2AError(INVALID_PARAMS, "params.message.role must be ROLE_USER")
    context = body.get("contextId")
    if context is not None and (not isinstance(context, str) or not CONTEXT_ID.match(context)):
        raise A2AError(INVALID_PARAMS, "contextId must be 1-128 characters of [A-Za-z0-9_.:-]")
    message_id = body.get("messageId")
    text = message_text(body)
    if len(text) > MAX_TEXT:
        raise A2AError(INVALID_PARAMS, f"the message is longer than {MAX_TEXT} characters")
    return SendRequest(
        text=text,
        context_id=context if isinstance(context, str) else None,
        message_id=message_id if isinstance(message_id, str) and message_id else None,
    )


# -- the card ------------------------------------------------------------------------------------


def agent_card(
    *,
    name: str,
    description: str,
    url: str,
    skills: Iterable[Mapping[str, Any]] = (),
    version: str = "1.0.0",
) -> dict[str, Any]:
    """Our agent card: one JSON-RPC interface, bearer auth, streaming, no push notifications."""
    listed = [dict(s) for s in skills] or [
        {
            "id": "general",
            "name": "General assistance",
            "description": "Answers requests and does tasks with its tools.",
            "tags": ["general"],
        }
    ]
    return {
        "name": name,
        "description": description,
        "version": version,
        "supportedInterfaces": [
            {"url": url, "protocolBinding": "JSONRPC", "protocolVersion": PROTOCOL_VERSION}
        ],
        "capabilities": {"streaming": True, "pushNotifications": False},
        "securitySchemes": {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}},
        "securityRequirements": [{"schemes": {"bearer": {"list": []}}}],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": listed,
    }


# -- tasks ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    context_id: str
    peer: str
    state: str
    reply: str = ""
    updated: str = ""


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def task_view(record: TaskRecord) -> dict[str, Any]:
    """A stored task as the A2A ``Task`` object."""
    status: dict[str, Any] = {"state": record.state}
    if record.updated:
        status["timestamp"] = record.updated
    view: dict[str, Any] = {"id": record.task_id, "contextId": record.context_id, "status": status}
    if record.reply and record.state == COMPLETED:
        view["artifacts"] = [
            {"artifactId": f"{record.task_id}-reply", "parts": [text_part(record.reply)]}
        ]
    elif record.reply:
        status["message"] = make_message(
            ROLE_AGENT,
            record.reply,
            context_id=record.context_id,
            task_id=record.task_id,
            message_id=f"{record.task_id}-status",
        )
    return view


def status_update(record: TaskRecord, *, with_message: bool) -> dict[str, Any]:
    status: dict[str, Any] = {"state": record.state}
    if record.updated:
        status["timestamp"] = record.updated
    if with_message and record.reply:
        status["message"] = make_message(
            ROLE_AGENT, record.reply, context_id=record.context_id, task_id=record.task_id
        )
    return {
        "statusUpdate": {"taskId": record.task_id, "contextId": record.context_id, "status": status}
    }


def artifact_update(record: TaskRecord) -> dict[str, Any]:
    artifact = {"artifactId": f"{record.task_id}-reply", "parts": [text_part(record.reply)]}
    return {
        "artifactUpdate": {
            "taskId": record.task_id,
            "contextId": record.context_id,
            "artifact": artifact,
        }
    }


class TaskStore:
    """In-memory tasks, each owned by the peer that sent it; another peer never sees it."""

    def __init__(self, *, kept: int = TERMINAL_KEPT) -> None:
        self._tasks: OrderedDict[str, TaskRecord] = OrderedDict()
        self._kept = kept

    def create(self, peer: str, context_id: str, now: datetime) -> TaskRecord:
        record = TaskRecord(
            f"task-{uuid.uuid4().hex[:20]}", context_id, peer, SUBMITTED, "", _stamp(now)
        )
        self._tasks[record.task_id] = record
        return record

    def get(self, peer: str, task_id: str) -> TaskRecord | None:
        record = self._tasks.get(task_id)
        return record if record is not None and record.peer == peer else None

    def move(self, task_id: str, state: str, now: datetime, reply: str = "") -> TaskRecord | None:
        """Set a task's state; a finished task never changes again (None)."""
        record = self._tasks.get(task_id)
        if record is None or record.state in TERMINAL:
            return None
        moved = replace(record, state=state, reply=reply, updated=_stamp(now))
        self._tasks[task_id] = moved
        if state in TERMINAL:
            self._trim()
        return moved

    def list(
        self, peer: str, *, context_id: str = "", state: str = "", size: int = 50, offset: int = 0
    ) -> tuple[list[TaskRecord], int, int]:
        """The peer's tasks, newest first: ``(page, next offset or 0, total)``."""
        size = max(1, min(size, PAGE_MAX))
        found = [
            r
            for r in reversed(self._tasks.values())
            if r.peer == peer
            and (not context_id or r.context_id == context_id)
            and (not state or r.state == state)
        ]
        page = found[offset : offset + size]
        following = offset + size if offset + size < len(found) else 0
        return page, following, len(found)

    def _trim(self) -> None:
        finished = [k for k, r in self._tasks.items() if r.state in TERMINAL]
        for key in finished[: max(0, len(finished) - self._kept)]:
            del self._tasks[key]
