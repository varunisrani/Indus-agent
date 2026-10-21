"""The task board tools (``kanban_*``, P24-13).

Fifteen tools over ``indus.state.board``. They run in the agent's own process, so they reach the
board file even when the terminal runs in a container or over SSH.

Two toolsets (R02.25: membership comes from the registry, so every tool registered into a set,
``kanban_schedule`` included, is reachable wherever that set is on):

- ``kanban-worker`` (13 tools): show, complete, block, schedule, request review, request changes,
  heartbeat, comment, attach, attach by URL, list attachments, create, link;
- ``kanban`` (orchestrators): ``kanban_list`` and ``kanban_unblock``, plus everything in
  ``kanban-worker``.

Both sets are opt-in (``Toolset.opt_in``): selecting "all" never brings them in. The dispatcher
starts workers with ``core,kanban-worker``; a person enables ``kanban`` for an orchestrating
session.

A worker session is one whose scope env carries the dispatcher's pins (``INDUS_BOARD_TASK``,
``INDUS_BOARD_RUN``, ``INDUS_BOARD_CLAIM``, ``INDUS_BOARD_SLUG``, ``INDUS_BOARD_ROOT``). In a
worker: the task id defaults to the worker's own task; changes to any other task are refused
(comments and new tasks excepted: they are the hand-off channel); the run-ending tools pass the
pinned run id, so a stale worker cannot end the run of a successor; the orchestrator pair refuses
even if a stale tool list still offers it. Comment authors are the session's profile, never an
argument.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from indus.foundation import profiles
from indus.foundation.scope import Scope, scope_or_none
from indus.safety.url_policy import Blocked, check_url_async
from indus.state.board import (
    ATTACHMENT_MAX_BYTES,
    BLOCK_KINDS,
    BOARD_ENV,
    STATUSES,
    BoardError,
    BoardStore,
    Task,
    current_board,
)
from indus.tools.registry import ToolRegistry, ToolTraits

__all__ = [
    "LIST_DEFAULT_LIMIT",
    "LIST_MAX_LIMIT",
    "ORCHESTRATOR_TOOLS",
    "ORCHESTRATOR_TOOLSET",
    "TOOL_NAMES",
    "WORKER_TOOLSET",
    "BoardSession",
    "board_session",
    "register_tools",
]

log = logging.getLogger(__name__)

WORKER_TOOLSET = "kanban-worker"
ORCHESTRATOR_TOOLSET = "kanban"
ORCHESTRATOR_TOOLS = frozenset({"kanban_list", "kanban_unblock"})
LIFECYCLE_TOOLS = frozenset(
    {
        "kanban_complete",
        "kanban_block",
        "kanban_schedule",
        "kanban_request_review",
        "kanban_request_changes",
    }
)
LIST_DEFAULT_LIMIT = 50
LIST_MAX_LIMIT = 200
MAX_REDIRECTS = 5

type Fetcher = Callable[[str, Scope, int], Any]


class Refusal(Exception):
    """A call the tool answers with an error and no change."""


# -- who is calling -----------------------------------------------------------------------------


@dataclass(frozen=True)
class BoardSession:
    """The board a call works on, and the worker pins when the session is a dispatched worker."""

    root: Path
    slug: str
    identity: str
    task_id: str | None = None
    run_id: int | None = None
    claim: str | None = None

    @property
    def worker(self) -> bool:
        return self.task_id is not None


def board_session(scope: Scope, board: object = None) -> BoardSession:
    env = scope.env
    task_id = env.get(BOARD_ENV["task"]) or None
    if task_id:
        run_text = env.get(BOARD_ENV["run"]) or ""
        slug = env.get(BOARD_ENV["slug"]) or "default"
        if board not in (None, "", slug):
            raise Refusal(f"this worker belongs to board '{slug}'; it cannot use another board")
        root_text = env.get(BOARD_ENV["root"]) or ""
        return BoardSession(
            root=Path(root_text) if root_text else scope.root,
            slug=slug,
            identity=scope.profile_id,
            task_id=task_id,
            run_id=int(run_text) if run_text.isdigit() else None,
            claim=env.get(BOARD_ENV["claim"]) or None,
        )
    slug = str(board).strip() if isinstance(board, str) and board.strip() else None
    return BoardSession(
        root=scope.root, slug=slug or current_board(scope.root), identity=scope.profile_id
    )


# -- schemas ------------------------------------------------------------------------------------


def _s(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


_BOARD = _s("Board name; leave out for the current board (a worker always uses its own).")
_TASK = _s("Task id (t_…). A dispatched worker may leave it out: it defaults to its own task.")
_META = {"type": "object", "description": "Structured details for the next reader (JSON object)."}


def _schema(
    name: str, description: str, props: Mapping[str, Any], required: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {**props, "board": _BOARD},
            "required": list(required),
        },
    }


SCHEMAS: dict[str, dict[str, Any]] = {
    "kanban_show": _schema(
        "kanban_show",
        "Everything about one task: the card, parents, children, comments, runs and recent events.",
        {"task_id": _TASK},
    ),
    "kanban_list": _schema(
        "kanban_list",
        "List tasks on the board (orchestrators). Filters are optional.",
        {
            "status": {"type": "string", "enum": list(STATUSES)},
            "assignee": _s("Only tasks of this profile."),
            "include_archived": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": LIST_MAX_LIMIT},
        },
    ),  # fmt: skip
    "kanban_complete": _schema(
        "kanban_complete",
        "Finish the task with a handoff: what was done (summary) and/or the result. Refused "
        "without one.",
        {
            "task_id": _TASK,
            "summary": _s("What you did and how you checked it."),
            "result": _s("The result itself, if it is short."),
            "metadata": _META,
        },
    ),  # fmt: skip
    "kanban_block": _schema(
        "kanban_block",
        "Stop and ask a person: say exactly what you need. kind: dependency (waits on an "
        "unfinished parent), needs_input, capability (a missing tool, key or permission), "
        "transient.",
        {
            "task_id": _TASK,
            "reason": _s("What you need, so a person can act."),
            "kind": {"type": "string", "enum": list(BLOCK_KINDS)},
        },
        ("reason",),
    ),  # fmt: skip
    "kanban_schedule": _schema(
        "kanban_schedule",
        "Park the task until later (it is not picked up again until the time comes or someone "
        "unblocks it).",
        {
            "task_id": _TASK,
            "reason": _s("Why it waits."),
            "wake_at": {"type": "integer", "description": "Unix time to pick it up again."},
        },
    ),  # fmt: skip
    "kanban_request_review": _schema(
        "kanban_request_review",
        "Hand the work to a reviewer instead of finishing it yourself.",
        {
            "task_id": _TASK,
            "summary": _s("What was done and how it was checked."),
            "reviewer": _s("Profile that reviews it; leave out to keep the current assignee."),
            "metadata": _META,
        },
        ("summary",),
    ),  # fmt: skip
    "kanban_request_changes": _schema(
        "kanban_request_changes",
        "As the reviewer: send the task back to its implementer with the changes needed.",
        {"task_id": _TASK, "reason": _s("The changes needed.")},
        ("reason",),
    ),  # fmt: skip
    "kanban_heartbeat": _schema(
        "kanban_heartbeat",
        "Say you are still working (keeps your claim during long steps).",
        {"task_id": _TASK, "note": _s("Optional progress note.")},
    ),  # fmt: skip
    "kanban_comment": _schema(
        "kanban_comment",
        "Add a comment to a task (any task: comments are how tasks pass notes).",
        {"task_id": _s("Task id (t_…)."), "body": _s("The comment.")},
        ("task_id", "body"),
    ),  # fmt: skip
    "kanban_attach": _schema(
        "kanban_attach",
        f"Attach a file given as base64 (at most {ATTACHMENT_MAX_BYTES // (1024 * 1024)} MiB).",
        {
            "task_id": _TASK,
            "filename": _s("File name."),
            "content_base64": _s("File bytes."),
            "content_type": _s("MIME type, if known."),
        },
        ("filename", "content_base64"),
    ),  # fmt: skip
    "kanban_attach_url": _schema(
        "kanban_attach_url",
        "Download an http(s) file and attach it (same size limit; private addresses refused).",
        {
            "task_id": _TASK,
            "url": _s("http or https URL."),
            "filename": _s("Optional name."),
            "content_type": _s("MIME type, if known."),
        },
        ("url",),
    ),  # fmt: skip
    "kanban_attachments": _schema(
        "kanban_attachments",
        "List a task's attached files with their paths.",
        {"task_id": _TASK},
    ),  # fmt: skip
    "kanban_create": _schema(
        "kanban_create",
        "Create a task for a profile (fan out work). It waits until its parents are done.",
        {
            "title": _s("Short title."),
            "body": _s("What to do, in full."),
            "assignee": _s("Profile that should do it."),
            "parents": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Task ids that must finish first.",
            },
            "priority": {"type": "integer"},
            "workspace_kind": {"type": "string", "enum": ["scratch", "worktree", "dir"]},
            "workspace_path": _s("Folder (dir) or repository (worktree)."),
            "idempotency_key": _s("Same key again returns the task already made."),
        },
        ("title", "assignee"),
    ),  # fmt: skip
    "kanban_unblock": _schema(
        "kanban_unblock",
        "Send a blocked task back to the queue (orchestrators). It waits if a parent is open.",
        {"task_id": _s("Task id (t_…).")},
        ("task_id",),
    ),  # fmt: skip
    "kanban_link": _schema(
        "kanban_link",
        "Make one task wait for another (parent must finish before child). Cycles are refused.",
        {
            "parent_id": _s("The task that must finish first."),
            "child_id": _s("The task that waits."),
        },
        ("parent_id", "child_id"),
    ),  # fmt: skip
}
TOOL_NAMES: tuple[str, ...] = tuple(SCHEMAS)


# -- helpers ------------------------------------------------------------------------------------


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields}, ensure_ascii=False, default=str)


def _error(text: str) -> str:
    return json.dumps({"error": text}, ensure_ascii=False)


def _text(args: Mapping[str, Any], name: str, *, required: bool = False) -> str | None:
    value = args.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise Refusal(f"{name} is required")
        return None
    if not isinstance(value, str):
        raise Refusal(f"{name} must be text")
    return value.strip()


def _meta(args: Mapping[str, Any]) -> dict[str, Any] | None:
    value = args.get("metadata")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise Refusal("metadata must be a JSON object")
    return {str(k): v for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]


def _task_id(session: BoardSession, args: Mapping[str, Any]) -> str:
    given = _text(args, "task_id")
    if given:
        return given
    if session.task_id:
        return session.task_id
    raise Refusal(
        "task_id is required: this session has no dispatched task. Use kanban_list to find ids."
    )


def _own(session: BoardSession, task_id: str) -> None:
    if session.worker and task_id != session.task_id:
        raise Refusal(
            f"this worker works on {session.task_id}; it may not change {task_id}. Use "
            "kanban_comment to pass a note, or kanban_create for follow-up work."
        )


def _run_proof(session: BoardSession, tool: str) -> int | None:
    if not session.worker:
        return None
    if session.run_id is None:
        raise Refusal(
            f"{tool} refused: this worker cannot name the run it belongs to, so it cannot prove "
            "it still owns the task"
        )
    return session.run_id


def _landed(task: Task, **extra: Any) -> str:
    return _ok(task_id=task.id, status=task.status, **extra)


def _scope_of(scope: Scope | None) -> Scope:
    found = scope if scope is not None else scope_or_none()
    if found is None:
        raise Refusal("no profile is active, so there is no board to use")
    return found


def _open(session: BoardSession, scope: Scope) -> BoardStore:
    return BoardStore(scope, session.slug, root=session.root)


# -- handlers -----------------------------------------------------------------------------------


def _show(session: BoardSession, store: BoardStore, args: Mapping[str, Any]) -> str:
    if not _text(args, "task_id") and not session.worker:
        return _ok(
            task=None,
            hint="no dispatched task in this session; call kanban_list for ids, then "
            "kanban_show(task_id=…)",
        )
    task_id = _task_id(session, args)
    task = store.require(task_id)
    return _ok(
        task=task.view(),
        parents=store.parents(task_id),
        unsatisfied_parents=[
            {"id": pid, "status": status} for pid, status in store.unsatisfied_parents(task_id)
        ],
        children=store.children(task_id),
        comments=[c.view() for c in store.comments(task_id)],
        runs=[r.view() for r in store.runs(task_id)],
        events=[e.view() for e in store.events(task_id, limit=50)],
        attachments=[a.view() for a in store.attachments(task_id)],
    )


def _list(session: BoardSession, store: BoardStore, args: Mapping[str, Any]) -> str:
    raw = args.get("limit")
    limit = LIST_DEFAULT_LIMIT if raw is None else raw
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= LIST_MAX_LIMIT:
        raise Refusal(f"limit must be a whole number from 1 to {LIST_MAX_LIMIT}")
    promoted = store.recompute_ready()
    rows = store.list(
        status=_text(args, "status"),
        assignee=_text(args, "assignee"),
        include_archived=args.get("include_archived") is True,
        limit=limit + 1,
    )
    shown = rows[:limit]
    return _ok(
        board=store.slug,
        tasks=[
            {k: t.view()[k] for k in ("id", "title", "status", "assignee", "priority")}
            for t in shown
        ],
        count=len(shown),
        truncated=len(rows) > limit,
        promoted=promoted,
    )


def _complete(session: BoardSession, store: BoardStore, args: Mapping[str, Any]) -> str:
    task_id = _task_id(session, args)
    _own(session, task_id)
    summary, result = _text(args, "summary"), _text(args, "result")
    if not (summary or result):
        raise Refusal(
            "kanban_complete needs a handoff: give a summary (preferred) or a result. Nothing "
            "changed; call it again with one."
        )
    task = store.complete(
        task_id,
        summary=summary,
        result=result,
        metadata=_meta(args),
        expected_run_id=_run_proof(session, "kanban_complete"),
    )
    return _landed(task, attachments=[a.view() for a in store.attachments(task_id)])


def _block(session: BoardSession, store: BoardStore, args: Mapping[str, Any]) -> str:
    task_id = _task_id(session, args)
    _own(session, task_id)
    kind = _text(args, "kind")
    if kind is not None and kind not in BLOCK_KINDS:
        raise Refusal(f"kind must be one of {', '.join(BLOCK_KINDS)} (or left out)")
    reason = _text(args, "reason", required=True)
    assert reason is not None
    task = store.block(
        task_id,
        reason=reason,
        kind=kind,
        expected_run_id=_run_proof(session, "kanban_block"),
        force=not session.worker,
    )
    extra: dict[str, Any] = {"block_kind": task.block_kind, "escalated": task.escalated}
    if kind == "dependency" and task.block_kind != kind:
        extra["note"] = (
            "no parent is open, so this was recorded as needs_input: it waits for a person "
            "instead of being restarted"
        )
    return _landed(task, **extra)


def _schedule(session: BoardSession, store: BoardStore, args: Mapping[str, Any]) -> str:
    task_id = _task_id(session, args)
    _own(session, task_id)
    wake = args.get("wake_at")
    if wake is not None and (not isinstance(wake, int) or isinstance(wake, bool)):
        raise Refusal("wake_at must be a Unix time (whole seconds)")
    task = store.schedule(
        task_id,
        reason=_text(args, "reason"),
        wake_at=wake,
        expected_run_id=_run_proof(session, "kanban_schedule"),
        force=not session.worker,
    )
    return _landed(task, scheduled=True, wake_at=task.wake_at)


def _request_review(session: BoardSession, store: BoardStore, args: Mapping[str, Any]) -> str:
    task_id = _task_id(session, args)
    _own(session, task_id)
    summary = _text(args, "summary", required=True)
    assert summary is not None
    reviewer = _text(args, "reviewer")
    if reviewer and not profiles.is_live(session.root, reviewer):
        names = ", ".join(profiles.list_profiles(session.root))
        raise Refusal(f"there is no profile '{reviewer}' to review it (profiles: {names})")
    task = store.request_review(
        task_id,
        summary=summary,
        reviewer=reviewer,
        metadata=_meta(args),
        expected_run_id=_run_proof(session, "kanban_request_review"),
        force=not session.worker,
    )
    return _landed(task, reviewer=task.assignee)


def _request_changes(session: BoardSession, store: BoardStore, args: Mapping[str, Any]) -> str:
    task_id = _task_id(session, args)
    _own(session, task_id)
    reason = _text(args, "reason", required=True)
    assert reason is not None
    task = store.request_changes(
        task_id, reason=reason, expected_run_id=_run_proof(session, "kanban_request_changes")
    )
    return _landed(task, implementer=task.assignee)


def _heartbeat(session: BoardSession, store: BoardStore, args: Mapping[str, Any]) -> str:
    task_id = _task_id(session, args)
    _own(session, task_id)
    ok = store.heartbeat(
        task_id, claimer=session.claim, run_id=session.run_id, note=_text(args, "note")
    )
    if not ok:
        raise Refusal(f"{task_id} is not running under this session's claim any more")
    return _ok(task_id=task_id)


def _comment(session: BoardSession, store: BoardStore, args: Mapping[str, Any]) -> str:
    task_id = _text(args, "task_id", required=True)
    body = _text(args, "body", required=True)
    assert task_id is not None and body is not None
    comment_id = store.comment(task_id, session.identity, body)
    return _ok(task_id=task_id, comment_id=comment_id)


def _attach(session: BoardSession, store: BoardStore, args: Mapping[str, Any]) -> str:
    task_id = _task_id(session, args)
    _own(session, task_id)
    filename = _text(args, "filename", required=True)
    encoded = _text(args, "content_base64", required=True)
    assert filename is not None and encoded is not None
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Refusal(f"content_base64 is not valid base64: {exc}") from exc
    saved = store.attach_bytes(
        task_id,
        filename,
        data,
        content_type=_text(args, "content_type"),
        uploaded_by=session.identity,
    )
    return _ok(task_id=task_id, attachment=saved.view())


def _attachments(session: BoardSession, store: BoardStore, args: Mapping[str, Any]) -> str:
    task_id = _task_id(session, args)
    store.require(task_id)
    return _ok(task_id=task_id, attachments=[a.view() for a in store.attachments(task_id)])


def _create(
    session: BoardSession,
    store: BoardStore,
    args: Mapping[str, Any],
    *,
    session_id: str | None = None,
    origin: Mapping[str, Any] | None = None,
) -> str:
    title = _text(args, "title", required=True)
    assignee = _text(args, "assignee", required=True)
    assert title is not None and assignee is not None
    parents_raw = args.get("parents") or []
    if not isinstance(parents_raw, list) or not all(isinstance(p, str) for p in parents_raw):  # pyright: ignore[reportUnknownVariableType]
        raise Refusal("parents must be a list of task ids")
    parents: list[str] = [str(p) for p in parents_raw]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    priority = args.get("priority", 0)
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise Refusal("priority must be a whole number")
    task = store.create(
        title,
        body=_text(args, "body"),
        assignee=assignee,
        created_by=session.identity,
        parents=parents,
        priority=priority,
        workspace_kind=_text(args, "workspace_kind") or "scratch",
        workspace_path=_text(args, "workspace_path"),
        idempotency_key=_text(args, "idempotency_key"),
        session_id=session_id,
    )
    subscribed = False
    if origin:
        platform = str(origin.get("platform") or "")
        chat = str(origin.get("chat_id") or "")
        if platform and chat:
            subscribed = store.subscribe(
                task.id,
                platform=platform,
                chat_id=chat,
                thread_id=str(origin.get("thread_id") or ""),
                profile=session.identity,
            )
    waiting = store.unsatisfied_parents(task.id)
    return _landed(
        task,
        gated=bool(waiting),
        waits_on=[pid for pid, _ in waiting],
        subscribed=subscribed,
    )


def _unblock(session: BoardSession, store: BoardStore, args: Mapping[str, Any]) -> str:
    task_id = _text(args, "task_id", required=True)
    assert task_id is not None
    return _landed(store.unblock(task_id))


def _link(session: BoardSession, store: BoardStore, args: Mapping[str, Any]) -> str:
    parent = _text(args, "parent_id", required=True)
    child = _text(args, "child_id", required=True)
    assert parent is not None and child is not None
    gated = store.link(parent, child)
    return _ok(parent_id=parent, child_id=child, gated=gated)


_Handler = Callable[[BoardSession, BoardStore, Mapping[str, Any]], str]

_SYNC: dict[str, _Handler] = {
    "kanban_show": _show,
    "kanban_list": _list,
    "kanban_complete": _complete,
    "kanban_block": _block,
    "kanban_schedule": _schedule,
    "kanban_request_review": _request_review,
    "kanban_request_changes": _request_changes,
    "kanban_heartbeat": _heartbeat,
    "kanban_comment": _comment,
    "kanban_attach": _attach,
    "kanban_attachments": _attachments,
    "kanban_unblock": _unblock,
    "kanban_link": _link,
}


def _run(
    name: str,
    args: Mapping[str, Any],
    scope: Scope | None,
    body: Callable[[BoardSession, BoardStore], str],
) -> str:
    try:
        current = _scope_of(scope)
        session = board_session(current, args.get("board"))
        if name in ORCHESTRATOR_TOOLS and session.worker:
            raise Refusal(
                f"{name} is for orchestrators; a dispatched worker uses kanban_complete, "
                "kanban_block, kanban_request_review or kanban_comment on its own task"
            )
        store = _open(session, current)
        try:
            return body(session, store)
        finally:
            store.close()
    except Refusal as exc:
        return _error(str(exc))
    except BoardError as exc:
        return _error(f"{name}: {exc}")


def _sync_handler(name: str, fn: _Handler) -> Callable[..., str]:
    def handler(args: Mapping[str, Any], *, scope: Scope | None = None) -> str:
        return _run(name, args, scope, lambda session, store: fn(session, store, args))

    handler.__name__ = name
    handler.__qualname__ = name
    return handler


def kanban_create(
    args: Mapping[str, Any],
    *,
    scope: Scope | None = None,
    session_id: str | None = None,
    delivery_origin: Mapping[str, Any] | None = None,
) -> str:
    return _run(
        "kanban_create",
        args,
        scope,
        lambda session, store: _create(
            session, store, args, session_id=session_id, origin=delivery_origin
        ),
    )


# -- attach by URL ------------------------------------------------------------------------------


async def fetch_capped(url: str, scope: Scope, max_bytes: int) -> tuple[bytes, str | None]:
    """GET ``url`` through the URL policy (every hop) with the size checked while streaming."""
    from indus.safety.safe_transport import safe_client

    async with (
        safe_client(scope, max_redirects=MAX_REDIRECTS, timeout=30.0) as client,
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise Refusal(
                    f"the file is larger than {max_bytes // (1024 * 1024)} MiB; nothing attached"
                )
            chunks.append(chunk)
        return b"".join(chunks), content_type or None


async def kanban_attach_url(
    args: Mapping[str, Any],
    *,
    scope: Scope | None = None,
    fetch: Callable[[str, Scope, int], Any] | None = None,
) -> str:
    try:
        current = _scope_of(scope)
        session = board_session(current, args.get("board"))
        with_store = _open(session, current)
        try:
            task_id = _task_id(session, args)
            _own(session, task_id)
            with_store.require(task_id)
        finally:
            with_store.close()
        url = _text(args, "url", required=True)
        assert url is not None
        if urlsplit(url).scheme.lower() not in ("http", "https"):
            raise Refusal("only http and https URLs can be attached")
        verdict = await check_url_async(url, current)
        if isinstance(verdict, Blocked):
            raise Refusal(f"the URL was refused: {verdict.reason}")
        try:
            data, fetched_type = await (fetch or fetch_capped)(url, current, ATTACHMENT_MAX_BYTES)
        except httpx.HTTPError as exc:
            raise Refusal(f"could not download {url}: {type(exc).__name__}") from exc
        name = (
            _text(args, "filename") or unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "download"
        )
        store = _open(session, current)
        try:
            saved = store.attach_bytes(
                task_id,
                name,
                data,
                content_type=_text(args, "content_type") or fetched_type,
                uploaded_by=session.identity,
            )
        finally:
            store.close()
        return _ok(task_id=task_id, attachment=saved.view())
    except Refusal as exc:
        return _error(str(exc))
    except BoardError as exc:
        return _error(f"kanban_attach_url: {exc}")


# -- registration -------------------------------------------------------------------------------

_READ_ONLY = frozenset({"kanban_show", "kanban_list", "kanban_attachments"})


def register_tools(registry: ToolRegistry) -> None:
    for name in TOOL_NAMES:
        toolset = ORCHESTRATOR_TOOLSET if name in ORCHESTRATOR_TOOLS else WORKER_TOOLSET
        traits = ToolTraits(read_only=True) if name in _READ_ONLY else ToolTraits(mutating=True)
        if name == "kanban_create":
            handler: Callable[..., Any] = kanban_create
        elif name == "kanban_attach_url":
            handler = kanban_attach_url
        else:
            handler = _sync_handler(name, _SYNC[name])
        registry.register(name, toolset, SCHEMAS[name], handler, traits=traits)
