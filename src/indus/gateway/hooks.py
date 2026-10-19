"""Gateway hook folders (P15-29, G12).

A person drops a folder into ``<home>/hooks/<name>/`` with two files:

- ``HOOK.yaml``: ``name`` (defaults to the folder name), ``description``, ``events`` (a list);
- ``handler.py``: ``handle(event_type, context)``, a plain or ``async`` function.

Event names are ``<kind>:<what>``. Kinds: ``agent`` (``start`` when a chat message begins a
turn, ``step`` after each tool call of it, ``end`` with the reply), ``session`` (``start``,
``end``, ``reset``), ``command`` (the command's name, every one through ``command:*``) and
``gateway`` (``startup`` once the adapters are up). ``<kind>:*`` listens to a whole kind; a
handler listening for ``agent`` alone gets nothing (names match exactly or through ``:*``).
Context: ``platform``, ``user_id``, ``chat_id``, ``thread_id`` ("" outside a thread),
``chat_type``, ``session_id``, ``message`` (first 500 characters); ``agent:end`` adds
``response``, ``model`` and ``provider``. Each handler gets its own copy of the context.

The folder is looked up when an event fires, from the profile in scope then (``home_of``), never
at import, and loaded once per home. A plain handler runs on the ``hooks`` blocking pool with a
10 s deadline (D-04.1), an ``async`` one gets 10 s on the loop; a handler that raises or runs late
is logged and skipped — hooks never block or break a reply. Non-None answers are collected in
order: ``command:<name>`` answers carry the P15-13 decisions (deny / handled / rewrite).

``bridge`` feeds the folders from the daemon's P13 hooks bus (``post_gateway_admission`` →
``agent:start``, ``post_tool_call`` → ``agent:step``, ``on_session_start`` / ``end`` / ``reset`` →
``session:*``) and from the service's turn-end listener (``agent:end``).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import inspect
import logging
import sys
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, cast

from indus.foundation import yamlio
from indus.foundation.blocking import BlockingError, run_blocking

if TYPE_CHECKING:
    from indus.core.session_scheduler import TurnEnd
    from indus.gateway.core.events import InboundEvent
    from indus.plugins.hooks import HookBus

__all__ = [
    "EVENTS",
    "HANDLER_TIMEOUT_S",
    "MESSAGE_CHARS",
    "GatewayHooks",
    "LoadedHook",
    "bridge",
    "hook_context",
    "load_hook_folder",
]

log = logging.getLogger(__name__)

EVENTS = (
    "gateway:startup",
    "session:start",
    "session:end",
    "session:reset",
    "agent:start",
    "agent:step",
    "agent:end",
    "command:*",
)
HANDLER_TIMEOUT_S = 10.0
MESSAGE_CHARS = 500
HOOKS_DIR = "hooks"
_REMEMBERED = 1024

type HandleFn = Callable[[str, dict[str, Any]], Any]


@dataclass(frozen=True)
class LoadedHook:
    name: str
    description: str
    events: tuple[str, ...]
    path: Path
    handle: HandleFn


def _skip(folder: Path, why: str) -> None:
    log.warning("gateway hook %s skipped: %s", folder.name, why)


def _import_handler(path: Path) -> ModuleType:
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:12]
    module_name = f"indus_gateway_hook_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    loader = spec.loader
    fresh = importlib.util.module_from_spec(spec)
    # Visible in sys.modules while it runs, so dataclasses / pydantic models in it resolve; a
    # handler that fails to load leaves nothing behind.
    with _listed(module_name, fresh):
        loader.exec_module(fresh)
    return fresh


@contextlib.contextmanager
def _listed(name: str, module: ModuleType) -> Iterator[None]:
    sys.modules[name] = module
    ok = False
    try:
        yield
        ok = True
    finally:
        if not ok:
            sys.modules.pop(name, None)


def load_hook_folder(folder: Path) -> LoadedHook | None:
    """The hook in ``folder``, or None (with the reason logged) when it is not usable."""
    manifest_path, handler_path = folder / "HOOK.yaml", folder / "handler.py"
    if not manifest_path.is_file() or not handler_path.is_file():
        return None
    try:
        manifest: object = yamlio.load_safe(manifest_path)
    except (ValueError, OSError) as exc:
        _skip(folder, f"HOOK.yaml cannot be read ({exc})")
        return None
    if not isinstance(manifest, dict):
        _skip(folder, "HOOK.yaml is not a mapping")
        return None
    data = cast(dict[str, Any], manifest)
    raw_events: object = data.get("events")
    if isinstance(raw_events, str):
        raw_events = [raw_events]
    events = (
        tuple(str(e).strip() for e in cast(list[object], raw_events) if str(e).strip())
        if isinstance(raw_events, list)
        else ()
    )
    if not events:
        _skip(folder, "HOOK.yaml lists no events")
        return None
    name = str(data.get("name") or folder.name)
    try:
        module = _import_handler(handler_path)
    except Exception as exc:
        _skip(folder, f"handler.py failed to load ({type(exc).__name__}: {exc})")
        return None
    handle = getattr(module, "handle", None)
    if not callable(handle):
        _skip(folder, "handler.py has no handle(event_type, context) function")
        return None
    return LoadedHook(
        name,
        str(data.get("description") or ""),
        events,
        folder,
        cast(HandleFn, handle),
    )


def hook_context(
    *,
    platform: str = "",
    user_id: str | None = "",
    chat_id: str = "",
    thread_id: str | None = "",
    chat_type: str = "",
    session_id: str = "",
    message: str = "",
    **extra: Any,
) -> dict[str, Any]:
    """The context every event carries (``message`` cut to its first 500 characters)."""
    return {
        "platform": platform,
        "user_id": user_id or "",
        "chat_id": chat_id,
        "thread_id": thread_id or "",
        "chat_type": chat_type,
        "session_id": session_id,
        "message": message[:MESSAGE_CHARS],
        **extra,
    }


class GatewayHooks:
    """See the module notes."""

    def __init__(
        self, home_of: Callable[[], Path], *, timeout_s: float = HANDLER_TIMEOUT_S
    ) -> None:
        self.home_of = home_of
        self.timeout_s = timeout_s
        self._loaded: dict[Path, list[LoadedHook]] = {}
        self._lock = threading.Lock()

    def hooks_for(self, home: Path) -> list[LoadedHook]:
        """The hooks of ``home`` (loaded on first use)."""
        key = home.resolve()
        with self._lock:
            found = self._loaded.get(key)
            if found is None:
                found = self._discover(key)
                self._loaded[key] = found
            return found

    def reload(self, home: Path | None = None) -> list[LoadedHook]:
        """Forget what was loaded for ``home`` (default: the current one) and load it again."""
        target = (home or self.home_of()).resolve()
        with self._lock:
            self._loaded.pop(target, None)
        return self.hooks_for(target)

    @staticmethod
    def _discover(home: Path) -> list[LoadedHook]:
        root = home / HOOKS_DIR
        if not root.is_dir():
            return []
        found: list[LoadedHook] = []
        for folder in sorted(root.iterdir()):
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            hook = load_hook_folder(folder)
            if hook is not None:
                log.info("gateway hook %s loaded for %s", hook.name, ", ".join(hook.events))
                found.append(hook)
        return found

    def _matching(self, event: str) -> list[LoadedHook]:
        try:
            home = self.home_of()
        except Exception:
            log.warning("gateway hooks: no profile home for %s", event, exc_info=True)
            return []
        hooks = self.hooks_for(home)
        exact = [h for h in hooks if event in h.events]
        wildcard: list[LoadedHook] = []
        if ":" in event:
            pattern = f"{event.split(':', 1)[0]}:*"
            wildcard = [h for h in hooks if pattern in h.events and h not in exact]
        return [*exact, *wildcard]

    async def emit(self, event: str, context: Mapping[str, Any] | None = None) -> list[Any]:
        """Run every hook for ``event``; their non-None answers in order. Never raises."""
        answers: list[Any] = []
        for hook in self._matching(event):
            payload = dict(context or {})
            try:
                answer = await self._run(hook, event, payload)
            except (TimeoutError, BlockingError):
                log.warning(
                    "gateway hook %s did not finish %s within %.0f s; skipped",
                    hook.name,
                    event,
                    self.timeout_s,
                )
                continue
            except Exception as exc:
                log.warning(
                    "gateway hook %s failed on %s: %s: %s",
                    hook.name,
                    event,
                    type(exc).__name__,
                    exc,
                    exc_info=exc,
                )
                continue
            if answer is not None:
                answers.append(answer)
        return answers

    async def _run(self, hook: LoadedHook, event: str, payload: dict[str, Any]) -> Any:
        if inspect.iscoroutinefunction(hook.handle):
            async with asyncio.timeout(self.timeout_s):
                return await cast(Awaitable[Any], hook.handle(event, payload))
        answer = await run_blocking(
            hook.handle,
            event,
            payload,
            pool="hooks",
            deadline=self.timeout_s,
            label=f"gateway hook {hook.name}",
        )
        if inspect.isawaitable(answer):
            async with asyncio.timeout(self.timeout_s):
                return await cast(Awaitable[Any], answer)
        return answer

    async def command_hooks(self, event: str, context: Mapping[str, Any]) -> list[Any]:
        """The P15-13 ``CommandHooks`` seam (``command:<name>`` with its context)."""
        return await self.emit(event, context)


# -- feeding the folders from the daemon ----------------------------------------------------------


def bridge(
    hooks: GatewayHooks,
    bus: HookBus,
    *,
    session_of: Callable[[str], str | None],
    on_turn_end: Callable[[Callable[[TurnEnd], Awaitable[None]]], Callable[[], None]] | None = None,
    model_of: Callable[[str], tuple[str, str]] | None = None,
) -> Callable[[], None]:
    """Fire the hook folders from the P13 bus and the service's turn ends; returns the undo.

    ``session_of(session_key)`` names the session a key routes to; ``model_of(session_id)`` gives
    ``(model, provider)`` for ``agent:end``.
    """
    # Chats seen through the intake, by session key. A new chat's session exists only once its
    # turn starts, so the session id is looked up (and filled in) when it is first needed.
    by_key: OrderedDict[str, dict[str, Any]] = OrderedDict()
    owner = "gateway-hooks"

    def find(session_id: str) -> dict[str, Any] | None:
        if not session_id:
            return None
        for key, context in reversed(by_key.items()):
            if not context["session_id"]:
                context["session_id"] = session_of(key) or ""
            if context["session_id"] == session_id:
                return context
        return None

    def known(session_id: str, **extra: Any) -> dict[str, Any]:
        base = find(session_id) or hook_context(session_id=session_id)
        return {**base, **extra}

    async def admitted(
        event: InboundEvent, session_key: str = "", session_id: str = "", **_: Any
    ) -> None:
        source = event.source
        context = hook_context(
            platform=str(source.platform),
            user_id=source.user_id,
            chat_id=source.chat_id,
            thread_id=source.thread_id,
            chat_type=str(source.chat_type),
            session_id=session_id or session_of(session_key) or "",
            message=event.text,
        )
        if session_key:
            by_key[session_key] = context
            by_key.move_to_end(session_key)
            while len(by_key) > _REMEMBERED:
                by_key.popitem(last=False)
        await hooks.emit("agent:start", dict(context))

    async def tool_done(session_id: str = "", tool_name: str = "", **_: Any) -> None:
        if find(session_id) is not None:
            await hooks.emit("agent:step", known(session_id, tool=tool_name))

    def session_event(kind: str) -> Callable[..., Awaitable[None]]:
        async def fire(session_id: str = "", platform: str = "", **_: Any) -> None:
            context = known(session_id)
            if not context.get("platform"):
                context["platform"] = platform
            await hooks.emit(f"session:{kind}", context)

        return fire

    async def turn_ended(ended: TurnEnd) -> None:
        if find(ended.session_id) is None:
            return
        model, provider = "", ""
        if model_of is not None:
            try:
                model, provider = model_of(ended.session_id)
            except Exception:
                log.debug("no model for %s", ended.session_id, exc_info=True)
        await hooks.emit(
            "agent:end",
            known(ended.session_id, response=ended.final_text, model=model, provider=provider),
        )

    registrations = [
        bus.register("post_gateway_admission", admitted, owner=owner),
        bus.register("post_tool_call", tool_done, owner=owner),
        bus.register("on_session_start", session_event("start"), owner=owner),
        bus.register("on_session_end", session_event("end"), owner=owner),
        bus.register("on_session_reset", session_event("reset"), owner=owner),
    ]
    undo_turns = on_turn_end(turn_ended) if on_turn_end is not None else None

    def undo() -> None:
        for registration in registrations:
            registration.dispose()
        if undo_turns is not None:
            undo_turns()

    return undo
