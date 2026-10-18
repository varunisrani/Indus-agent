"""Prompt lifecycle: build once, restore verbatim on resume, rebuild only after compaction (R01.1).

``PromptLifecycle.restore_or_build`` gives the prompt for a turn:

1. a prompt this lifecycle already holds for the session is returned untouched;
2. with history, the session row's prompt is read; its state (``missing``, ``null``, ``empty``,
   ``present``) is logged, and a store error is a WARNING that counts as ``missing``;
3. a present prompt is reused when it still matches the runtime: the ``Model:`` and
   ``Provider:`` lines of the trailer paragraph, the ``Session ID:`` line when ids are shown, and
   the ``Working directory:`` line of the runtime block. ``Platform:`` is deliberately not
   checked: a surface switch keeps the bytes and stages a note on the note channel instead;
4. anything else builds a new prompt and persists it (a failed write is a WARNING).

``invalidate("compaction")`` is the one full-rebuild trigger (P08); ``rebind_identity`` is the
route-binding path (P04-23) and rewrites only the ``Model:``/``Provider:`` lines.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast

from indus.core.notes import NoteChannel
from indus.core.prompt import texts
from indus.core.prompt.builder import BuildContext, PromptParts, configured_hints, platform_hint
from indus.state import sessions
from indus.state.registry import StoreHandle

__all__ = [
    "IDENTITY_LABELS",
    "RowState",
    "Builder",
    "PromptLifecycle",
    "PromptStore",
    "SessionPrompts",
    "StoredPrompt",
    "identity_value",
    "last_announced_surface",
    "runtime_value",
    "split_runtime",
    "with_identity",
]

log = logging.getLogger(__name__)

RowState = Literal["missing", "null", "empty", "present"]

# Trailer labels in the order the builder writes them.
IDENTITY_LABELS = (
    texts.LABEL_SESSION_ID,
    texts.LABEL_MODEL,
    texts.LABEL_PROVIDER,
    texts.LABEL_PLATFORM,
)
# Only the newest surface note matters; a bounded look-back keeps long sessions cheap.
NOTE_LOOKBACK = 200

_RUNTIME_SEP = f"\n\n{texts.RUNTIME_HEADING}\n\n"


# -- reading a stored prompt ----------------------------------------------------------------------


def _runtime_start(prompt: str) -> int:
    """Index of the separator before the renderer's runtime block, or -1 for another layout."""
    if not prompt.endswith(texts.RUNTIME_END):
        return -1
    return prompt.rfind(_RUNTIME_SEP)


def split_runtime(prompt: str) -> tuple[str, str]:
    """``(everything before the runtime block, the runtime body)``; body "" for other layouts."""
    start = _runtime_start(prompt)
    if start < 0:
        return prompt, ""
    body = prompt[start + len(_RUNTIME_SEP) : -len(texts.RUNTIME_END)]
    return prompt[:start], body.strip("\n")


def _labelled(lines: Sequence[str], label: str) -> str:
    prefix = f"{label}:"
    values = [line[len(prefix) :].strip() for line in lines if line.startswith(prefix)]
    return values[-1] if values else ""


def identity_value(prompt: str, label: str) -> str:
    """``label``'s value from the trailer paragraph that closes the prompt before the runtime
    block. ``Model:`` lines inside project files or memory never count."""
    before, _ = split_runtime(prompt)
    trailer = before.rsplit("\n\n", 1)[-1]
    return _labelled(trailer.splitlines(), label)


def runtime_value(prompt: str, label: str) -> str:
    """``label``'s value from the first paragraph of the runtime block ("" when absent)."""
    _, body = split_runtime(prompt)
    if not body:
        return ""
    return _labelled(body.split("\n\n", 1)[0].splitlines(), label)


def with_identity(prompt: str, *, model: str, provider: str) -> str:
    """``prompt`` with only its trailer ``Model:``/``Provider:`` lines replaced (or added in
    their place, or dropped when empty); every other byte is kept."""
    start = _runtime_start(prompt)
    head, tail = (prompt[:start], prompt[start:]) if start >= 0 else (prompt, "")
    cut = head.rfind("\n\n")
    lead, trailer = (head[: cut + 2], head[cut + 2 :]) if cut >= 0 else ("", head)
    lines = trailer.split("\n")
    known = tuple(f"{label}:" for label in IDENTITY_LABELS)
    values = {label: _labelled(lines, label) for label in IDENTITY_LABELS}
    values[texts.LABEL_MODEL] = model.strip()
    values[texts.LABEL_PROVIDER] = provider.strip()
    kept = [line for line in lines if not line.startswith(known)]
    kept.extend(f"{label}: {values[label]}" for label in IDENTITY_LABELS if values[label])
    return lead + "\n".join(kept) + tail


def _message_text(message: Mapping[str, Any]) -> str:
    sidecar = message.get("api_content")
    pieces = [sidecar if isinstance(sidecar, str) else ""]
    content = message.get("content")
    if isinstance(content, str):
        pieces.append(content)
    elif isinstance(content, list):
        for part in cast(list[object], content):
            if isinstance(part, Mapping):
                text = cast(Mapping[str, object], part).get("text")
                if isinstance(text, str):
                    pieces.append(text)
    return "\n".join(pieces)


def last_announced_surface(history: Sequence[Mapping[str, Any]]) -> str:
    """The surface named by the newest surface-switch note in recent user rows ("" when none)."""
    for message in reversed(history[-NOTE_LOOKBACK:]):
        if message.get("role") != "user":
            continue
        text = _message_text(message)
        if texts.SURFACE_SWITCH_PREFIX in text:
            tail = text.rsplit(texts.SURFACE_SWITCH_PREFIX, 1)[1]
            return tail.split(texts.SURFACE_SWITCH_NAME_END, 1)[0].strip()
    return ""


# -- storage --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredPrompt:
    state: RowState
    text: str = ""
    started_at: float | None = None


class PromptStore(Protocol):
    """Where a session's prompt is kept. ``load`` may raise; the lifecycle logs and rebuilds."""

    async def load(self, session_id: str) -> StoredPrompt: ...

    async def save(self, session_id: str, text: str) -> None: ...


class SessionPrompts:
    """``PromptStore`` over the P02 session store: reads off the loop, writes through the writer."""

    def __init__(self, store: StoreHandle) -> None:
        self._store = store

    async def load(self, session_id: str) -> StoredPrompt:
        row = await asyncio.to_thread(sessions.get, self._store, session_id)
        if row is None:
            return StoredPrompt("missing")
        started = row.get("started_at")
        started_at = float(started) if isinstance(started, (int, float)) else None
        raw = row.get("system_prompt")
        if raw is None:
            return StoredPrompt("null", started_at=started_at)
        if raw == "":
            return StoredPrompt("empty", started_at=started_at)
        return StoredPrompt("present", str(raw), started_at)

    async def save(self, session_id: str, text: str) -> None:
        updated = await self._store.awrite(
            lambda conn: sessions.set_system_prompt_in(conn, session_id, text)
        )
        if not updated:
            log.debug("no session row %s yet; its prompt was not stored", session_id)


class Builder(Protocol):
    async def build(self, ctx: BuildContext) -> PromptParts: ...


# -- the lifecycle --------------------------------------------------------------------------------


class PromptLifecycle:
    """One conversation's system prompt. Not shared between sessions."""

    def __init__(
        self,
        builder: Builder,
        *,
        store: PromptStore | None = None,
        notes: NoteChannel | None = None,
    ) -> None:
        self._builder = builder
        self._store = store
        self.notes = notes if notes is not None else NoteChannel()
        self._prompt: str | None = None
        self._session_id = ""
        self._force_rebuild = False
        self.last_parts: PromptParts | None = None

    @property
    def prompt(self) -> str | None:
        return self._prompt

    async def restore_or_build(
        self, ctx: BuildContext, *, history: Sequence[Mapping[str, Any]] = ()
    ) -> str:
        """The prompt for this turn of ``ctx.session_id`` (see the module docstring)."""
        if self._prompt is not None and self._session_id == ctx.session_id:
            return self._prompt
        stored = StoredPrompt("missing")
        if history and self._store is not None:
            try:
                stored = await self._store.load(ctx.session_id)
            except Exception as exc:
                log.warning(
                    "could not read the stored system prompt of session %s (%s); building a new "
                    "one, so the provider's prompt cache misses this turn",
                    ctx.session_id,
                    exc,
                )
        log.debug(
            "system prompt restore for session %s: stored row %s", ctx.session_id, stored.state
        )
        if ctx.started_at is None and stored.started_at is not None:
            ctx = replace(ctx, started_at=datetime.fromtimestamp(stored.started_at, UTC))

        forced = self._force_rebuild
        if stored.state == "present" and not forced:
            if self._matches(stored.text, ctx):
                self._keep(ctx, stored.text, history)
                return stored.text
            log.info(
                "stored system prompt of session %s no longer matches the runtime (model %s, "
                "provider %s); rebuilding it",
                ctx.session_id,
                ctx.model,
                ctx.provider,
            )
        elif history and stored.state in ("null", "empty") and not forced:
            log.warning(
                "stored system prompt of session %s is %s; rebuilding it, and the provider's "
                "prompt cache misses until the rebuilt prompt is saved",
                ctx.session_id,
                stored.state,
            )

        parts = await self._builder.build(ctx)
        self.last_parts = parts
        self._force_rebuild = False
        self._keep(ctx, parts.text, history)
        await self._save(ctx.session_id, parts.text)
        return parts.text

    def invalidate(self, reason: str) -> None:
        """Drop the held prompt so the next turn rebuilds it. Only compaction may do this."""
        if reason != "compaction":
            raise ValueError(
                f"the system prompt is rebuilt only after compaction, not for {reason!r}; route "
                "changes go through rebind_identity"
            )
        self._prompt = None
        self._force_rebuild = True

    async def rebind_identity(self, *, model: str, provider: str) -> str | None:
        """Route binding (P04-23): rewrite only the ``Model:``/``Provider:`` lines and persist."""
        if self._prompt is None:
            return None
        updated = with_identity(self._prompt, model=model, provider=provider)
        if updated != self._prompt:
            self._prompt = updated
            await self._save(self._session_id, updated)
        return updated

    # -- helpers ----------------------------------------------------------------------------------

    @staticmethod
    def _matches(prompt: str, ctx: BuildContext) -> bool:
        live = {texts.LABEL_MODEL: ctx.model.strip(), texts.LABEL_PROVIDER: ctx.provider.strip()}
        for label, current in live.items():
            stored = identity_value(prompt, label)
            if stored and stored != current:
                return False
        stored_id = identity_value(prompt, texts.LABEL_SESSION_ID)
        if ctx.show_session_id and stored_id and stored_id != ctx.session_id:
            return False
        stored_cwd = runtime_value(prompt, texts.LABEL_CWD)
        live_cwd = ctx.runtime.cwd if ctx.runtime is not None else str(ctx.working_dir())
        return not (stored_cwd and live_cwd and stored_cwd != live_cwd)

    def _keep(self, ctx: BuildContext, prompt: str, history: Sequence[Mapping[str, Any]]) -> None:
        self._prompt = prompt
        self._session_id = ctx.session_id
        self._stage_surface_note(ctx, prompt, history)

    def _stage_surface_note(
        self, ctx: BuildContext, prompt: str, history: Sequence[Mapping[str, Any]]
    ) -> None:
        current = ctx.platform.strip()
        if not current:
            return
        described = identity_value(prompt, texts.LABEL_PLATFORM)
        told = last_announced_surface(history) or described
        if not told or told == current:
            return
        hint = platform_hint(current, configured_hints(ctx.scope)) if described != current else ""
        where = texts.SURFACE_SWITCH_WHERE_HINT if hint else texts.SURFACE_SWITCH_WHERE_PROMPT
        note = texts.SURFACE_SWITCH_NOTE.format(platform=current, where=where)
        self.notes.stage_surface_switch(f"{note}\n{hint}" if hint else note)
        log.info(
            "session %s moved from %s to %s; keeping the stored prompt and sending a note",
            ctx.session_id,
            told,
            current,
        )

    async def _save(self, session_id: str, text: str) -> None:
        if self._store is None or not session_id:
            return
        try:
            await self._store.save(session_id, text)
        except Exception as exc:
            log.warning(
                "could not save the system prompt of session %s (%s); later turns will rebuild "
                "it and miss the provider's prompt cache",
                session_id,
                exc,
            )
