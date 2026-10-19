"""Delivery mirror notes: tell a chat's agent what was sent into its chat from elsewhere (P15-20).

When cron output or ``indus send`` lands in a chat that has a conversation, the agent of that
conversation should know on its next turn (otherwise "what did you mean by that report?" finds
an agent that never saw it). The note never becomes a fake assistant message: it rides the next
user message as a must-deliver note (the P04 note channel), which is persisted with that message.

Where the note waits:

- the session runs in this process (the gateway's ``SessionService`` has it attached) → staged on
  its note channel at once;
- otherwise (the session is idle, or this is the ``indus send`` process) → kept in the session's
  metadata (namespace ``gateway.mirror``) and staged by the gateway just before that session's
  next turn (``stage_pending``).

The conversation is found through the routes index: the route whose stored origin is that chat
(and thread). When a chat holds several conversations (a group with one per person) and nothing
says which, no note is written: guessing would tell the wrong agent.
"""

from __future__ import annotations

import functools
import logging
import secrets
from typing import TYPE_CHECKING, Any, Final, cast

from indus.foundation.blocking import run_blocking
from indus.state import gateway_routes as routes
from indus.state import session_meta
from indus.state.registry import StoreHandle

if TYPE_CHECKING:
    from indus.service.session_service import SessionService

__all__ = [
    "MAX_NOTES",
    "NAMESPACE",
    "NOTE_CHARS",
    "Mirror",
    "find_session",
    "note_text",
    "stage_pending",
    "store_note",
    "take_notes",
]

log = logging.getLogger(__name__)

NAMESPACE: Final = "gateway.mirror"
MAX_NOTES: Final = 20
NOTE_CHARS: Final = 2000


def note_text(label: str, text: str) -> str:
    """Our wording for one mirror note; long texts are cut."""
    body = text.strip()
    if len(body) > NOTE_CHARS:
        body = body[:NOTE_CHARS].rstrip() + " […]"
    return (
        f"[Delivery note: {label} sent this into the chat outside our conversation. "
        f"You did not write it, but the person has seen it.]\n{body}"
    )


def find_session(
    store: StoreHandle, platform: str, chat_id: str, thread_id: str | None = None
) -> str | None:
    """The one session whose route lives in that chat (and thread); None when none or several."""
    matches: list[routes.RouteEntry] = []
    for entry in routes.list_routes(store, platform=platform.strip().lower()):
        origin = entry.origin
        if str(origin.get("chat_id", "")) != str(chat_id):
            continue
        own_thread = origin.get("thread_id") or None
        if thread_id is not None and str(own_thread or "") != str(thread_id):
            continue
        matches.append(entry)
    if thread_id is None:
        unthreaded = [e for e in matches if not e.origin.get("thread_id")]
        matches = unthreaded or matches
    sessions = {e.session_id for e in matches}
    return sessions.pop() if len(sessions) == 1 else None


def store_note(store: StoreHandle, session_id: str, note: str) -> bool:
    """Keep ``note`` for the session's next turn (the oldest go beyond ``MAX_NOTES``)."""
    now = store.scope.clock.now().timestamp()
    key = f"{now:017.6f}-{secrets.token_hex(3)}"
    session_meta.put(store, session_id, NAMESPACE, key, {"text": note})
    kept = session_meta.list_entries(store, session_id, NAMESPACE)
    for old in sorted(kept)[: max(0, len(kept) - MAX_NOTES)]:
        session_meta.delete(store, session_id, NAMESPACE, old)
    return True


def take_notes(store: StoreHandle, session_id: str) -> list[str]:
    """The kept notes, oldest first, removed as they are taken."""
    kept = session_meta.list_entries(store, session_id, NAMESPACE)
    notes: list[str] = []
    for key in sorted(kept):
        value = kept[key]
        if session_meta.delete(store, session_id, NAMESPACE, key) and isinstance(value, dict):
            text = cast(dict[str, Any], value).get("text")
            if isinstance(text, str) and text.strip():
                notes.append(text)
    return notes


def _stage(service: SessionService, session_id: str, notes: list[str]) -> int:
    channel = service.agent(session_id).runtime.prompts.notes
    for note in notes:
        channel.stage_must_deliver(note)
    return len(notes)


async def stage_pending(store: StoreHandle, service: SessionService, session_id: str) -> int:
    """Stage the session's kept notes on its note channel (before a turn); how many."""
    if session_id not in service.attached():
        return 0
    notes = await run_blocking(take_notes, store, session_id, label="mirror notes")
    if not notes:
        return 0
    return _stage(service, session_id, notes)


class Mirror:
    """Writes mirror notes for one profile's store (``service``: the gateway's, when present)."""

    def __init__(self, store: StoreHandle, service: SessionService | None = None) -> None:
        self.store = store
        self.service = service

    async def record(
        self,
        platform: str,
        chat_id: str,
        thread_id: str | None,
        text: str,
        label: str,
    ) -> bool:
        """Leave a note for the conversation in that chat; False when there is none (or several,
        or the store failed). Never raises."""
        if not text.strip():
            return False
        try:
            session_id = await run_blocking(
                functools.partial(find_session, self.store, platform, chat_id, thread_id),
                label="mirror lookup",
            )
            if session_id is None:
                log.info("no single conversation in %s:%s; no delivery note", platform, chat_id)
                return False
            note = note_text(label, text)
            service = self.service
            if service is not None and session_id in service.attached():
                _stage(service, session_id, [note])
                return True
            await run_blocking(
                functools.partial(store_note, self.store, session_id, note), label="mirror note"
            )
            return True
        except Exception:
            log.warning(
                "could not leave a delivery note in %s:%s", platform, chat_id, exc_info=True
            )
            return False
