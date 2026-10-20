"""The ACP agent: one method dispatcher over the editor connection (P19-02, wiring for P19-03..07).

``AcpAgent.dispatch(method, params, is_notification)`` is the handler the SDK's JSON-RPC
``Connection`` calls for every incoming frame; it answers plain JSON values. Method names, field
names and error codes are the vendor's (agent-client-protocol 0.12.1, schema v1.19.0); everything
behind them is ours.

- Handlers live in a table (``HANDLERS``): ``initialize`` and ``authenticate`` here, the session
  methods in ``sessions`` / ``history`` / ``listing`` (later tasks register prompt, cancel, modes
  and the rest the same way). An unknown method is ``-32601``; liveness probes (``ping``,
  ``health``, ``healthcheck``) get the same answer and only a DEBUG log line (R09.14).
- The raw ``initialize`` params are read before anything else: whether the client can show form
  elicitations is taken from ``clientCapabilities.elicitation.form`` there (P19-11 uses it).
- Every session the editor holds is in ``sessions`` (``SessionTable``): the backend key, the id the
  editor uses (an old id after compression is an alias, R09.9), and the subscription.
- ``after_reply`` runs a notice once the current response frame is queued (one loop turn later),
  so an editor never gets an update for a session id it has not been told yet (R09.9).
- Errors: ``AcpError`` carries a JSON-RPC code; a ``BackendError`` becomes an error whose message
  is the backend's text and whose ``data.reason`` is its code; bad params are ``-32602``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from pydantic import ValidationError

from indus import __version__
from indus.acp import auth
from indus.acp.backend import AcpBackend, BackendError, HostSelector, LostTurn
from indus.foundation.scope import Scope
from indus.protocol.events import EventBase
from indus.protocol.server_requests import ServerRequest

__all__ = [
    "AGENT_NAME",
    "AUTH_REQUIRED",
    "HANDLERS",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "METHOD_NOT_FOUND",
    "PROBE_METHODS",
    "RESOURCE_NOT_FOUND",
    "AcpAgent",
    "AcpError",
    "ClientPeer",
    "EditorSink",
    "HeldSession",
    "SessionTable",
    "error_for",
    "handler",
    "wire",
]

log = logging.getLogger(__name__)

AGENT_NAME: Final = "indus-agent"
PROBE_METHODS: Final = frozenset({"ping", "health", "healthcheck"})

# JSON-RPC 2.0 codes and the ACP ones the SDK names (``acp.RequestError``).
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603
AUTH_REQUIRED: Final = -32000
RESOURCE_NOT_FOUND: Final = -32002


class AcpError(Exception):
    """A JSON-RPC error answer."""

    def __init__(self, code: int, message: str, data: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = dict(data) if data is not None else None


def error_for(exc: BackendError) -> AcpError:
    code = RESOURCE_NOT_FOUND if exc.code == "not_found" else INTERNAL_ERROR
    return AcpError(code, exc.message, {"reason": exc.code})


class ClientPeer(Protocol):
    """The editor's side of the connection (requests and notifications we send it)."""

    async def notify(self, method: str, params: Mapping[str, Any]) -> None: ...

    async def request(self, method: str, params: Mapping[str, Any]) -> Any: ...


type Handler = Callable[["AcpAgent", Mapping[str, Any]], Awaitable[Any]]

HANDLERS: dict[str, Handler] = {}
"""Method name → handler; filled by ``handler`` in this module and the session modules."""


def handler(method: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        if method in HANDLERS and HANDLERS[method] is not fn:
            raise ValueError(f"ACP method {method} registered twice")
        HANDLERS[method] = fn
        return fn

    return register


# -- held sessions --------------------------------------------------------------------------------


@dataclass
class HeldSession:
    """A session this editor connection holds."""

    key: str
    editor_id: str
    cwd: str | None = None
    unsubscribe: Callable[[], None] | None = None
    title: str | None = None
    offered: list[tuple[ServerRequest, int | None]] = field(
        default_factory=list[tuple[ServerRequest, int | None]]
    )
    lost: list[LostTurn] = field(default_factory=list[LostTurn])


class SessionTable:
    """Editor ids ↔ backend keys. The id an editor used on its latest load is the one updates
    are addressed to; any other id it used for the same session still resolves."""

    def __init__(self) -> None:
        self._held: dict[str, HeldSession] = {}
        self._alias: dict[str, str] = {}

    def hold(self, key: str, editor_id: str) -> HeldSession:
        found = self._held.get(key)
        if found is None:
            found = HeldSession(key, editor_id)
            self._held[key] = found
        found.editor_id = editor_id
        self._alias[editor_id] = key
        self._alias[key] = key
        return found

    def key_for(self, editor_id: str) -> str | None:
        return self._alias.get(editor_id)

    def get(self, key: str) -> HeldSession | None:
        return self._held.get(key)

    def by_editor(self, editor_id: str) -> HeldSession | None:
        key = self._alias.get(editor_id)
        return self._held.get(key) if key is not None else None

    def drop(self, key: str) -> HeldSession | None:
        found = self._held.pop(key, None)
        for alias in [a for a, k in self._alias.items() if k == key]:
            del self._alias[alias]
        return found

    def __iter__(self) -> Iterator[HeldSession]:
        return iter(list(self._held.values()))


# -- the sink every subscribed session reports to --------------------------------------------------


class EditorSink:
    """Backend traffic of held sessions, handed to the projection (P19-10) and the permission
    bridge (P19-11) once those are wired; until then requests and lost turns are kept on the
    held session and events go to the optional ``on_event`` hook."""

    def __init__(self, agent: AcpAgent) -> None:
        self.agent = agent

    def on_event(self, session_id: str, event: EventBase) -> None:
        hook = self.agent.on_backend_event
        if hook is not None:
            hook(session_id, event)

    def on_request(self, session_id: str, request: ServerRequest, generation: int | None) -> None:
        held = self.agent.sessions.get(session_id)
        if held is not None:
            ident = str(request.request_id)
            if any(str(seen.request_id) == ident for seen, _ in held.offered):
                return  # already offered (a load's snapshot and the subscription both carry it)
            held.offered.append((request, generation))
        hook = self.agent.on_server_request
        if hook is not None:
            hook(session_id, request, generation)

    def on_gap(self, session_id: str, info: Mapping[str, Any]) -> None:
        log.info("session %s skipped events after a reconnect (%s)", session_id, info.get("reason"))

    def on_lost(self, session_id: str, lost: LostTurn) -> None:
        held = self.agent.sessions.get(session_id)
        if held is None:
            return
        held.lost.append(lost)
        from indus.acp.history import announce_lost

        self.agent.spawn(announce_lost(self.agent, held, [lost]))


# -- the agent ------------------------------------------------------------------------------------


class AcpAgent:
    """See the module notes."""

    def __init__(
        self,
        scope: Scope,
        selector: HostSelector,
        *,
        peer: ClientPeer | None = None,
        env: Mapping[str, str] | None = None,
        wsl: bool | None = None,
    ) -> None:
        self.scope = scope
        self.selector = selector
        self.peer = peer
        self.env: Mapping[str, str] = env if env is not None else scope.env.to_dict()
        self.wsl = wsl
        self.form_elicitation = False
        self.client_info: Mapping[str, Any] = {}
        self.sessions = SessionTable()
        self.sink = EditorSink(self)
        self.on_backend_event: Callable[[str, EventBase], None] | None = None
        self.on_server_request: Callable[[str, ServerRequest, int | None], None] | None = None
        self._handlers: dict[str, Handler] = default_handlers()
        self._background: set[asyncio.Task[Any]] = set()

    # -- plumbing ---------------------------------------------------------------------------------

    async def backend(self) -> AcpBackend:
        return await self.selector.get()

    async def dispatch(self, method: str, params: Any, is_notification: bool) -> Any:
        """The SDK connection's handler (see the module notes)."""
        found = self._handlers.get(method)
        if found is None:
            if method in PROBE_METHODS:
                log.debug("liveness probe %s answered method-not-found", method)
            else:
                log.info("the editor called %s, which this agent does not offer", method)
            if is_notification:
                return None
            raise _rpc(AcpError(METHOD_NOT_FOUND, "Method not found", {"method": method}))
        body: Mapping[str, Any] = params if isinstance(params, Mapping) else {}
        try:
            return await found(self, body)
        except AcpError as exc:
            raise _rpc(exc) from None
        except BackendError as exc:
            raise _rpc(error_for(exc)) from None
        except ValidationError as exc:
            errors = [
                {"loc": [str(p) for p in e.get("loc", ())], "msg": e.get("msg")}
                for e in exc.errors()
            ]
            raise _rpc(AcpError(INVALID_PARAMS, "Invalid params", {"errors": errors})) from None

    async def update(self, editor_id: str, update: Mapping[str, Any]) -> None:
        """One ``session/update`` notification to the editor."""
        if self.peer is None:
            return
        await self.peer.notify("session/update", {"sessionId": editor_id, "update": dict(update)})

    def spawn(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        task: asyncio.Task[Any] = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._background.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.debug("an ACP background notice failed", exc_info=task.exception())

    def after_reply(self, work: Callable[[], Awaitable[Any]]) -> None:
        """Run ``work`` once the response being returned now is queued (module notes)."""

        async def later() -> None:
            await _next_turn()
            await work()

        self.spawn(later())

    async def aclose(self) -> None:
        """Cancel pending notices, detach held sessions, close the backend (loop still alive)."""
        tasks = list(self._background)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for held in self.sessions:
            if held.unsubscribe is not None:
                held.unsubscribe()
        await self.selector.aclose()


async def _next_turn() -> None:
    loop = asyncio.get_running_loop()
    waiter: asyncio.Future[None] = loop.create_future()
    loop.call_soon(waiter.set_result, None)
    await waiter


def wire(model: Any) -> dict[str, Any]:
    """A vendor model as the JSON the editor gets: aliases, only the fields we set."""
    return model.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_unset=True)


def _rpc(error: AcpError) -> Exception:
    """The SDK's error type for ``error`` (what its connection turns into an error frame)."""
    from acp import RequestError

    return RequestError(error.code, error.message, error.data)


def default_handlers() -> dict[str, Handler]:
    from indus.acp import history, listing, sessions  # noqa: F401  (they register handlers)

    return dict(HANDLERS)


# -- initialize and authenticate ------------------------------------------------------------------


def _form_elicitation(params: Mapping[str, Any]) -> bool:
    capabilities = params.get("clientCapabilities")
    if not isinstance(capabilities, Mapping):
        return False
    elicitation = capabilities.get("elicitation")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return isinstance(elicitation, Mapping) and isinstance(elicitation.get("form"), Mapping)  # pyright: ignore[reportUnknownMemberType]


@handler("initialize")
async def initialize(agent: AcpAgent, params: Mapping[str, Any]) -> dict[str, Any]:
    from acp import PROTOCOL_VERSION
    from acp.schema import InitializeRequest, InitializeResponse

    agent.form_elicitation = _form_elicitation(params)
    request = InitializeRequest.model_validate(params)
    if request.client_info is not None:
        agent.client_info = {
            "name": request.client_info.name,
            "version": request.client_info.version,
        }
    log.info(
        "editor %s connected (protocol %s)",
        agent.client_info.get("name", "unknown"),
        request.protocol_version,
    )
    response = InitializeResponse.model_validate(
        {
            "protocolVersion": PROTOCOL_VERSION,
            "agentInfo": {"name": AGENT_NAME, "version": __version__},
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {"image": True},
                "sessionCapabilities": {"fork": {}, "list": {}, "resume": {}},
            },
            "authMethods": auth.auth_methods(agent.scope),
        }
    )
    return wire(response)


@handler("authenticate")
async def authenticate(agent: AcpAgent, params: Mapping[str, Any]) -> dict[str, Any]:
    from acp.schema import AuthenticateRequest

    request = AuthenticateRequest.model_validate(params)
    if not auth.accepts(agent.scope, request.method_id):
        if request.method_id.strip().lower() == auth.SETUP_METHOD_ID:
            message = "Indus Agent has no usable provider yet: finish `indus acp --setup` first"
        else:
            message = "that sign-in method is not available for this Indus Agent profile"
        raise AcpError(AUTH_REQUIRED, message, {"methodId": request.method_id})
    return {}
