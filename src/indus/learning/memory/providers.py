# pyright: strict
"""One external memory provider beside the built-in stores (P13-24).

The two built-in stores (P09) are always on. In addition, a session may run one external provider,
named by ``memory.provider`` (empty, ``builtin`` or ``none`` = built-in only). A provider is a
plugin-supplied object that stores and recalls memories its own way; it is additive, never a
replacement.

``MemoryProvider`` is the protocol a provider implements; ``BaseMemoryProvider`` gives no-op
defaults so a provider overrides only what it uses. ``ProviderManager`` owns the one provider for a
session and drives its lifecycle:

- ``start`` checks ``is_available()`` (configured and credentialed, no network); unavailable means
  one warning and an inert manager;
- the provider's static ``system_prompt_block()`` is rendered once and frozen for the session
  (``provider_block``), so a write cannot change the prompt bytes mid-session (R07.1);
- before each turn ``on_turn_start`` then ``prefetch`` run off the loop with a bounded wait
  (``PREFETCH_WAIT_S`` = 8 s); a slow prefetch is abandoned and later turns skip it while it is
  still running. Recall comes back whole up to ``RECALL_CEILING``; with the opt-in spill flag
  on, recall over the plain spill threshold is replaced by a head/tail preview and a saved path;
- after the response ``sync_turn`` and ``queue_prefetch`` run on one serial background worker;
- every committed built-in memory write is mirrored with ``on_memory_write``;
- the provider's tools are offered only when the ``memory`` toolset is selected;
- ``on_session_end`` / ``on_session_switch`` / ``on_pre_compress`` / ``on_delegation`` fire at those
  points; ``shutdown`` drains the worker for at most ``DRAIN_WAIT_S`` (5 s) then shuts the provider.

Everything handed to a provider (turn text, transcript slices with tool output, queries, mirrored
writes, delegation results, the provider's own tool arguments) passes the force-mode secret redactor
first, even when ``security.redact_secrets`` is off — a provider archives whatever it is given.

Every provider call is guarded: a failing provider is logged and the turn goes on.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast, runtime_checkable

from indus.foundation.blocking import BlockingPool, DeadlineMissed, PoolFull
from indus.foundation.scope import Scope, bind_scope
from indus.safety import redaction
from indus.tools import spill
from indus.tools.budget import RESULT_CHARS

__all__ = [
    "CORE_PROVIDER_NAMES",
    "DRAIN_WAIT_S",
    "PREFETCH_WAIT_S",
    "RECALL_CEILING",
    "BaseMemoryProvider",
    "ConfigField",
    "DrainReport",
    "MemoryProvider",
    "MemoryWrite",
    "ProviderManager",
    "ProviderScope",
    "RecallStatus",
    "clear_registry",
    "installed_providers",
    "mirror_writes_from",
    "register_provider",
    "registered_provider",
    "resolve_provider",
]

log = logging.getLogger(__name__)

PREFETCH_WAIT_S = 8.0
DRAIN_WAIT_S = 5.0
# Recall is returned whole up to here; past it the opt-in spill replaces it with a preview + path.
RECALL_CEILING = max(RESULT_CHARS * 10, 100_000)

CORE_PROVIDER_NAMES = frozenset({"", "builtin", "built-in", "default", "none"})


def is_core_provider(name: str | None) -> bool:
    """True when ``memory.provider`` means the built-in store, not an external plugin."""
    return (name or "").strip().lower() in CORE_PROVIDER_NAMES


# -- data the manager hands around ----------------------------------------------------------------


@dataclass(frozen=True)
class ProviderScope:
    """What a provider needs to bind itself to one profile at ``initialize``."""

    home: str
    profile_id: str
    platform: str = ""
    agent_context: str = "primary"
    parent_session_id: str = ""
    user_id: str = ""


@dataclass(frozen=True)
class RecallStatus:
    """What the last prefetch injected, for the one-line recall indicator."""

    label: str
    count: int = 0
    glyph: str = "*"


@dataclass(frozen=True)
class MemoryWrite:
    """A committed built-in memory write mirrored to the provider.

    ``action`` is ``add`` | ``patch`` | ``remove``; ``target`` is ``memory`` | ``user``;
    ``content`` is the entry after the change (the whole entry for ``patch``); ``previous`` is the
    entry before, for ``patch`` / ``remove``.
    """

    action: str
    target: str
    content: str
    previous: str | None = None
    meta: Mapping[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class ConfigField:
    """One setup field for ``indus memory setup`` (shown by the provider's ``config_schema``)."""

    key: str
    description: str
    secret: bool = False
    required: bool = False
    default: Any = None
    choices: Sequence[str] = ()


@dataclass(frozen=True)
class DrainReport:
    """What ``shutdown`` drained. ``timed_out`` means work was abandoned at ``DRAIN_WAIT_S``."""

    timed_out: bool = False
    abandoned_writes: int = 0
    abandoned_prefetches: int = 0


# -- the protocol ---------------------------------------------------------------------------------


@runtime_checkable
class MemoryProvider(Protocol):
    """What a plugin memory provider implements (see ``BaseMemoryProvider`` for defaults)."""

    @property
    def name(self) -> str:
        """Short identifier, matched against ``memory.provider``."""
        ...

    def is_available(self) -> bool:
        """Configured and credentialed (config and deps only, no network)."""
        ...

    def unavailable_reason(self) -> str:
        """Why ``is_available`` is False, for the one warning the manager prints."""
        ...

    def initialize(self, session_id: str, scope: ProviderScope) -> None:
        """Bind to one profile and open resources (once, at session start)."""
        ...

    def system_prompt_block(self) -> str:
        """Static prompt text (frozen for the session); "" to add nothing."""
        ...

    def prefetch(self, query: str) -> str:
        """Recall text for the upcoming turn ("" when none); must be fast."""
        ...

    def queue_prefetch(self, query: str) -> None:
        """Queue a background recall for the next turn."""
        ...

    def recall_status(self) -> RecallStatus | None:
        """What the most recent ``prefetch`` injected (None = no indicator)."""
        ...

    def sync_turn(self, turn: Mapping[str, Any]) -> None:
        """Persist one finished turn (``user``, ``assistant``, ``messages``)."""
        ...

    def tool_schemas(self) -> Sequence[Mapping[str, Any]]:
        """Function-tool schemas the provider adds ({name, description, parameters})."""
        ...

    def handle_tool_call(self, name: str, args: Mapping[str, Any]) -> str:
        """Run one of the provider's tools; returns a JSON string."""
        ...

    def on_turn_start(self, turn_number: int, message: str) -> None:
        """Per-turn tick before prefetch."""
        ...

    def on_session_end(self, messages: Sequence[Mapping[str, Any]]) -> None:
        """Extract at a real session boundary."""
        ...

    def on_session_switch(self, new_session_id: str, parent_session_id: str = "") -> None:
        """Rebind per-session state when the id rotated without teardown."""
        ...

    def on_pre_compress(self, messages: Sequence[Mapping[str, Any]]) -> str:
        """Text fed into the compression summary ("" when none)."""
        ...

    def on_delegation(self, task: str, result: str, child_session_id: str = "") -> None:
        """Observe a finished delegation from the parent side."""
        ...

    def on_memory_write(self, write: MemoryWrite) -> None:
        """Mirror a committed built-in memory write."""
        ...

    def config_schema(self) -> Sequence[ConfigField]:
        """Setup fields for ``indus memory setup`` ([] when none)."""
        ...

    def save_config(self, values: Mapping[str, Any]) -> None:
        """Persist non-secret setup values to the provider's own config."""
        ...

    def identity_signature(self) -> Mapping[str, Any]:
        """Identity values that must bust a cached agent when they change."""
        ...

    def backup_paths(self) -> Sequence[str]:
        """Absolute paths of provider state for backup/import."""
        ...

    def shutdown(self) -> None:
        """Flush and close."""
        ...


class BaseMemoryProvider:
    """No-op defaults for every optional method; a provider overrides ``name`` and what it uses."""

    @property
    def name(self) -> str:
        raise NotImplementedError

    def is_available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    def initialize(self, session_id: str, scope: ProviderScope) -> None:
        return None

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query: str) -> str:
        return ""

    def queue_prefetch(self, query: str) -> None:
        return None

    def recall_status(self) -> RecallStatus | None:
        return None

    def sync_turn(self, turn: Mapping[str, Any]) -> None:
        return None

    def tool_schemas(self) -> Sequence[Mapping[str, Any]]:
        return ()

    def handle_tool_call(self, name: str, args: Mapping[str, Any]) -> str:
        raise NotImplementedError(f"{self.name} does not handle the tool {name!r}")

    def on_turn_start(self, turn_number: int, message: str) -> None:
        return None

    def on_session_end(self, messages: Sequence[Mapping[str, Any]]) -> None:
        return None

    def on_session_switch(self, new_session_id: str, parent_session_id: str = "") -> None:
        return None

    def on_pre_compress(self, messages: Sequence[Mapping[str, Any]]) -> str:
        return ""

    def on_delegation(self, task: str, result: str, child_session_id: str = "") -> None:
        return None

    def on_memory_write(self, write: MemoryWrite) -> None:
        return None

    def config_schema(self) -> Sequence[ConfigField]:
        return ()

    def save_config(self, values: Mapping[str, Any]) -> None:
        return None

    def identity_signature(self) -> Mapping[str, Any]:
        return {}

    def backup_paths(self) -> Sequence[str]:
        return ()

    def shutdown(self) -> None:
        return None


# -- the catalog ----------------------------------------------------------------------------------

type ProviderFactory = Callable[[], MemoryProvider]
_REGISTRY: dict[str, ProviderFactory] = {}


def register_provider(name: str, factory: ProviderFactory) -> None:
    """Register a provider factory. First registration of a name wins (bundled before user before
    project), so a plugin cannot shadow a built-in one."""
    key = name.strip().lower()
    if not key or is_core_provider(key):
        raise ValueError(f"{name!r} is a reserved memory provider name")
    _REGISTRY.setdefault(key, factory)


def registered_provider(name: str) -> ProviderFactory | None:
    return _REGISTRY.get(name.strip().lower())


def installed_providers() -> tuple[str, ...]:
    """The names of every registered external provider."""
    return tuple(_REGISTRY)


def clear_registry() -> None:
    """Drop every registered factory (tests, and a full reload)."""
    _REGISTRY.clear()


def resolve_provider(name: str | None) -> MemoryProvider | None:
    """A fresh provider instance for ``memory.provider``; None for built-in only or an unknown name.

    An unknown name logs one warning (per call) and returns None, so the session runs built-in only.
    """
    if is_core_provider(name):
        return None
    factory = registered_provider(name or "")
    if factory is None:
        log.warning(
            "memory.provider %r is not installed; using the built-in memory only "
            "(install it, or clear memory.provider)",
            name,
        )
        return None
    return factory()


# -- egress scrub ---------------------------------------------------------------------------------


def _scrub_text(text: str, scope: Scope) -> str:
    return redaction.redact_output(text, force=True, scope=scope)


def _scrub(value: Any, scope: Scope) -> Any:
    """A copy of ``value`` with every string masked; ``data:`` media URLs pass through."""
    if isinstance(value, str):
        return value if value.startswith("data:") else _scrub_text(value, scope)
    if isinstance(value, Mapping):
        return {k: _scrub(v, scope) for k, v in value.items()}  # type: ignore[misc]
    if isinstance(value, list | tuple):
        scrubbed = [_scrub(v, scope) for v in value]  # type: ignore[misc]
        return tuple(scrubbed) if isinstance(value, tuple) else scrubbed
    return value


def _scrub_messages(
    messages: Sequence[Mapping[str, Any]], scope: Scope
) -> tuple[Mapping[str, Any], ...]:
    return tuple(_scrub(dict(m), scope) for m in messages)


# -- the manager ----------------------------------------------------------------------------------


def _trivial(query: str) -> bool:
    """A query not worth a recall round-trip (empty, a slash command)."""
    stripped = query.strip()
    return not stripped or stripped.startswith("/")


@dataclass
class ProviderManager:
    """Owns the one external provider for a session (built from ``scope``'s config)."""

    scope: Scope
    provider: MemoryProvider | None = None
    toolset_selected: bool = True
    _started: bool = False
    _provider_block: str | None = None
    _last_recall: RecallStatus | None = None
    _prefetch_running: bool = False
    _tool_names: frozenset[str] = frozenset()
    _worker: BlockingPool | None = None
    _blocking: BlockingPool | None = None
    _tasks: dict[asyncio.Task[Any], str] = field(default_factory=dict[asyncio.Task[Any], str])
    _closed: bool = False

    # -- construction -----------------------------------------------------------------------------

    @classmethod
    def for_scope(
        cls,
        scope: Scope,
        *,
        toolset_selected: bool = True,
        provider: MemoryProvider | None = None,
    ) -> ProviderManager:
        """Build the manager for ``scope``: the configured provider, or one passed in (tests)."""
        chosen = (
            provider
            if provider is not None
            else resolve_provider(_config_str(scope, "memory.provider"))
        )
        return cls(scope=scope, provider=chosen, toolset_selected=toolset_selected)

    @property
    def active(self) -> bool:
        return self._started and self.provider is not None

    def _pool(self, attr: str, name: str, *, workers: int, queue_limit: int) -> BlockingPool:
        pool = getattr(self, attr)
        if pool is None:
            pool = BlockingPool(name, workers, queue_limit=queue_limit)
            setattr(self, attr, pool)
        return pool

    def _serial(self) -> BlockingPool:
        return self._pool("_worker", "memory-provider-sync", workers=1, queue_limit=256)

    def _fast(self) -> BlockingPool:
        return self._pool("_blocking", "memory-provider", workers=2, queue_limit=0)

    async def _run(self, fn: Callable[[], Any], *, label: str) -> Any:
        return await self._fast().run(bind_scope(fn, scope=self.scope), label=label)

    # -- lifecycle --------------------------------------------------------------------------------

    def start(self, session_id: str, provider_scope: ProviderScope | None = None) -> bool:
        """Initialize the provider; False (and inert) when there is none or it is unavailable."""
        if self.provider is None or self._started or self._closed:
            return self.active
        try:
            available = self.provider.is_available()
        except Exception:
            log.warning(
                "memory provider %r is_available() failed", self.provider.name, exc_info=True
            )
            available = False
        if not available:
            reason = ""
            try:
                reason = self.provider.unavailable_reason()
            except Exception:
                log.debug("unavailable_reason() failed", exc_info=True)
            log.warning(
                "memory provider %r is configured but unavailable%s; built-in memory still works",
                self.provider.name,
                f": {reason}" if reason else "",
            )
            self.provider = None
            return False
        bound = provider_scope or ProviderScope(
            home=str(self.scope.home), profile_id=self.scope.profile_id
        )
        try:
            self.provider.initialize(session_id, bound)
            self._tool_names = frozenset(
                str(s.get("name")) for s in self.provider.tool_schemas() if s.get("name")
            )
        except Exception:
            log.warning("memory provider %r initialize() failed", self.provider.name, exc_info=True)
            self.provider = None
            return False
        self._started = True
        return True

    # -- prompt -----------------------------------------------------------------------------------

    def prompt_block(self) -> str:
        """The provider's static block, rendered once and then frozen for the session."""
        if self._provider_block is None:
            self._provider_block = self._render_block()
        return self._provider_block

    def freeze_block(self, text: str) -> None:
        """Adopt a block cut from a stored prompt (resume / compaction rebuild)."""
        self._provider_block = text

    def _render_block(self) -> str:
        if not self.active or self.provider is None:
            return ""
        try:
            return (self.provider.system_prompt_block() or "").strip()
        except Exception:
            log.warning("memory provider %r system_prompt_block() failed", self.provider.name)
            return ""

    # -- tools ------------------------------------------------------------------------------------

    def tool_schemas(self) -> tuple[Mapping[str, Any], ...]:
        """The provider's tools, only when the memory toolset is on and names do not collide."""
        if not self.active or self.provider is None or not self.toolset_selected:
            return ()
        try:
            raw = list(self.provider.tool_schemas())
        except Exception:
            log.warning("memory provider %r tool_schemas() failed", self.provider.name)
            return ()
        out: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        for schema in raw:
            name = schema.get("name")
            if isinstance(name, str) and name and name not in seen:
                out.append(schema)
                seen.add(name)
        return tuple(out)

    def handles_tool(self, name: str) -> bool:
        return self.active and self.toolset_selected and name in self._tool_names

    async def handle_tool_call(self, name: str, args: Mapping[str, Any]) -> str:
        if not self.handles_tool(name) or self.provider is None:
            raise KeyError(name)
        clean = _scrub(dict(args), self.scope)
        provider = self.provider

        def call() -> str:
            return provider.handle_tool_call(name, clean)

        try:
            return await self._run(call, label=f"provider tool {name}")
        except Exception:
            log.warning("memory provider %r tool %r failed", provider.name, name, exc_info=True)
            raise

    # -- per turn ---------------------------------------------------------------------------------

    async def before_turn(self, turn_number: int, message: str) -> str:
        """The recall block for this turn ("" when none); bounded by ``PREFETCH_WAIT_S``."""
        if not self.active or self.provider is None:
            return ""
        provider = self.provider
        clean = _scrub_text(message, self.scope)

        def tick() -> None:
            provider.on_turn_start(turn_number, clean)

        try:
            await self._run(tick, label="provider on_turn_start")
        except Exception:
            log.debug("memory provider on_turn_start failed", exc_info=True)
        if _trivial(message) or self._prefetch_running:
            return ""
        return await self._prefetch(clean)

    async def _prefetch(self, query: str) -> str:
        provider = self.provider
        assert provider is not None
        self._last_recall = None
        self._prefetch_running = True

        def run() -> str:
            return provider.prefetch(query) or ""

        try:
            recall = await self._fast().run(
                bind_scope(run, scope=self.scope),
                deadline=PREFETCH_WAIT_S,
                clock=self.scope.clock,
                label="provider prefetch",
            )
        except DeadlineMissed:
            log.warning(
                "memory provider %r prefetch exceeded %.0fs; skipping until it returns",
                provider.name,
                PREFETCH_WAIT_S,
            )
            return ""
        except (PoolFull, Exception):
            log.debug("memory provider prefetch failed", exc_info=True)
            self._prefetch_running = False
            return ""
        else:
            self._prefetch_running = False
            try:
                self._last_recall = provider.recall_status()
            except Exception:
                self._last_recall = None
            return self._bounded(recall)

    def _bounded(self, recall: str) -> str:
        """Cap recall at ``RECALL_CEILING``; the opt-in flag spills over the plain threshold."""
        if not recall:
            return ""
        if _config_bool(self.scope, "memory.provider_recall_spill"):
            sink = spill.host_spill_for(self.scope, lambda: None)
            return spill.spill_if_needed(
                recall,
                "memory_provider_recall",
                "memory-provider-recall",
                sink=sink,
                threshold=RESULT_CHARS,
            )
        if len(recall) > RECALL_CEILING:
            log.warning(
                "memory provider recall of %d chars capped at %d", len(recall), RECALL_CEILING
            )
            return recall[:RECALL_CEILING]
        return recall

    def recall_indicator(self) -> str:
        """A one-line note that recall ran ("" when none)."""
        status = self._last_recall
        if status is None:
            return ""
        if status.count == 1:
            detail = "recalled 1 memory"
        elif status.count > 1:
            detail = f"recalled {status.count} memories"
        else:
            detail = "recalled relevant memory"
        return f"{status.glyph} {status.label} — {detail}"

    def after_turn(self, turn: Mapping[str, Any], *, next_query: str | None = None) -> None:
        """Queue sync and the next prefetch on the serial worker (after the response)."""
        if not self.active or self.provider is None:
            return
        provider = self.provider
        clean = _scrub(dict(turn), self.scope)
        query = None if next_query is None else _scrub_text(next_query, self.scope)

        def work() -> None:
            try:
                provider.sync_turn(clean)
            except Exception:
                log.warning("memory provider %r sync_turn failed", provider.name, exc_info=True)
            if query and not _trivial(query):
                try:
                    provider.queue_prefetch(query)
                except Exception:
                    log.debug("memory provider queue_prefetch failed", exc_info=True)

        self._submit(work, prefetch=False)

    # -- other hooks ------------------------------------------------------------------------------

    def mirror_write(self, write: MemoryWrite) -> None:
        """Mirror a committed built-in memory write (scrubbed) on the serial worker."""
        if not self.active or self.provider is None:
            return
        provider = self.provider
        clean = MemoryWrite(
            action=write.action,
            target=write.target,
            content=_scrub_text(write.content, self.scope),
            previous=None if write.previous is None else _scrub_text(write.previous, self.scope),
            meta=_scrub(dict(write.meta), self.scope),
        )

        def work() -> None:
            try:
                provider.on_memory_write(clean)
            except Exception:
                log.warning(
                    "memory provider %r on_memory_write failed", provider.name, exc_info=True
                )

        self._submit(work, prefetch=False)

    def notify_tool_write(
        self, tool_result: Mapping[str, Any] | str, tool_args: Mapping[str, Any]
    ) -> None:
        """Mirror a built-in ``memory`` tool call, given its result and arguments.

        Called by the host after the memory tool runs (the reference does this in the loop). Only a
        committed write is mirrored (not a staged one); a single op and a batch are both expanded,
        and a ``patch`` becomes a ``replace`` carrying the whole entry after the edit. ``patch`` is
        mirrored as ``patch`` here (the provider sees the changed entry as ``content``).
        """
        for write in mirror_writes_from(tool_result, tool_args):
            self.mirror_write(write)

    def session_end(self, messages: Sequence[Mapping[str, Any]]) -> None:
        if not self.active or self.provider is None:
            return
        provider = self.provider
        clean = _scrub_messages(messages, self.scope)

        def work() -> None:
            try:
                provider.on_session_end(clean)
            except Exception:
                log.warning(
                    "memory provider %r on_session_end failed", provider.name, exc_info=True
                )

        self._submit(work, prefetch=False)

    def session_switch(self, new_session_id: str, parent_session_id: str = "") -> None:
        if not self.active or self.provider is None or not new_session_id:
            return
        try:
            self.provider.on_session_switch(new_session_id, parent_session_id)
        except Exception:
            log.warning("memory provider on_session_switch failed", exc_info=True)

    async def pre_compress(self, messages: Sequence[Mapping[str, Any]]) -> str:
        if not self.active or self.provider is None:
            return ""
        provider = self.provider
        clean = _scrub_messages(messages, self.scope)

        def run() -> str:
            return provider.on_pre_compress(clean) or ""

        try:
            return (await self._run(run, label="provider on_pre_compress")).strip()
        except Exception:
            log.debug("memory provider on_pre_compress failed", exc_info=True)
            return ""

    def delegation(self, task: str, result: str, child_session_id: str = "") -> None:
        if not self.active or self.provider is None:
            return
        provider = self.provider
        clean_task = _scrub_text(task, self.scope)
        clean_result = _scrub_text(result, self.scope)

        def work() -> None:
            try:
                provider.on_delegation(clean_task, clean_result, child_session_id)
            except Exception:
                log.warning("memory provider %r on_delegation failed", provider.name, exc_info=True)

        self._submit(work, prefetch=False)

    # -- background worker ------------------------------------------------------------------------

    def _submit(self, work: Callable[[], None], *, prefetch: bool) -> None:
        if self._closed:
            log.warning("memory provider manager is closed; dropping a background task")
            return
        kind = "prefetch" if prefetch else "write"
        try:
            task = asyncio.ensure_future(self._serial().run(work, label="provider background"))
        except Exception:
            log.debug("could not queue a memory provider background task", exc_info=True)
            return
        self._tasks[task] = kind
        task.add_done_callback(self._tasks.pop)

    # -- shutdown ---------------------------------------------------------------------------------

    async def shutdown(self) -> DrainReport:
        """Drain the worker (bounded by ``DRAIN_WAIT_S``), shut the provider, report leftovers."""
        if self._closed:
            return DrainReport()
        self._closed = True
        report = await self._drain()
        if self.provider is not None and self._started:
            try:
                await self._run(self.provider.shutdown, label="provider shutdown")
            except Exception:
                log.debug("memory provider shutdown failed", exc_info=True)
        for pool in (self._worker, self._blocking):
            if pool is not None:
                pool.shutdown()
        return report

    async def _drain(self) -> DrainReport:
        tasks = set(self._tasks)
        if not tasks:
            return DrainReport()
        timer = asyncio.ensure_future(self.scope.clock.sleep(DRAIN_WAIT_S))
        try:
            while not timer.done():
                left = {t for t in tasks if not t.done()}
                if not left:
                    break
                await asyncio.wait({*left, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
        left_kinds = [self._tasks[t] for t in tasks if not t.done() and t in self._tasks]
        if not left_kinds:
            return DrainReport()
        prefetch = left_kinds.count("prefetch")
        writes = len(left_kinds) - prefetch
        log.warning(
            "memory provider drain timed out after %.0fs; abandoning %d write(s), %d prefetch(es)",
            DRAIN_WAIT_S,
            writes,
            prefetch,
        )
        return DrainReport(True, writes, prefetch)


# -- translating a built-in memory-tool result ----------------------------------------------------

_MIRRORED_ACTIONS = frozenset({"add", "patch", "remove"})


def _committed(result: Mapping[str, Any] | str) -> Mapping[str, Any] | None:
    """The result dict only when it marks a committed (not staged) write; else None."""
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except ValueError:
            return None
        result = cast(Mapping[str, Any], parsed) if isinstance(parsed, Mapping) else {}
    if result.get("success") is True and result.get("staged") is not True:
        return result
    return None


def mirror_writes_from(
    result: Mapping[str, Any] | str, args: Mapping[str, Any]
) -> list[MemoryWrite]:
    """The mirror events a committed ``memory`` tool call produces (empty when none)."""
    body = _committed(result)
    if body is None:
        return []
    target = str(args.get("target") or "memory")
    raw_ops = args.get("operations")
    batch = isinstance(raw_ops, list) and bool(cast(list[Any], raw_ops))
    ops: list[Any] = list(cast(list[Any], raw_ops)) if batch else [args]
    changed: dict[int, Mapping[str, Any]] = {}
    for item in cast(list[Any], body.get("changed_entries") or []):
        if isinstance(item, Mapping):
            entry = cast(Mapping[str, Any], item)
            changed[int(entry.get("operation", 0) or 0)] = entry
    out: list[MemoryWrite] = []
    for index, op in enumerate(ops, start=1):
        if not isinstance(op, Mapping):
            continue
        op_map = cast(Mapping[str, Any], op)
        action = str(op_map.get("action") or "")
        if action not in _MIRRORED_ACTIONS:
            continue
        entry = changed.get(index) if batch else next(iter(changed.values()), None)
        before = entry.get("before") if entry is not None else None
        content = str(op_map.get("content") or op_map.get("new_text") or "")
        if action == "patch" and entry is not None:
            content = str(entry.get("after") or content)
        out.append(
            MemoryWrite(
                action,
                target,
                content,
                previous=before if isinstance(before, str) else None,
                meta={"old_text": str(op_map.get("old_text"))} if op_map.get("old_text") else {},
            )
        )
    return out


# -- config helpers -------------------------------------------------------------------------------


def _config_str(scope: Scope, key: str) -> str:
    if scope.config is None:
        return ""
    try:
        value = scope.config.get(key)
    except Exception:
        return ""
    return value.strip() if isinstance(value, str) else ""


def _config_bool(scope: Scope, key: str) -> bool:
    if scope.config is None:
        return False
    try:
        value = scope.config.get(key)
    except Exception:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "on", "1")
    return False
