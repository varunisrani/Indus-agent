"""The default ACP backend: a viewer of the profile's daemon (P19-04, D-16.29).

``AttachedHost`` connects on first use. The P17-16 ``Channel`` dials the way every surface does:
the owner-only control socket's ``describe`` must name this process's profile id
(``stable_profile_id(home)``) before a single-use ticket is asked for, and ``gateway.ready`` must
name it again (else ``profile_mismatch``; no daemon is ``daemon_unavailable``). Once connected,
the daemon's ``runtime.describe`` (asked again on every reconnection) says what it offers, and its
profile id is checked once more. The channel (surface ``acp``) carries the P17-17
``RemoteSessionService``, which does the hard parts: idempotent
create, the submit journal, "outcome unknown" for every waiter of a lost connection (R09.11),
redial with backoff, resubscribe from each session's last seq, re-offers deduplicated by request
id. The first dial never starts a daemon (D-16.83); redials may recover one that died (P17-15
``recover`` budget).

This host adds the editor's part on every (re)connection, in a channel ``on_ready`` hook that runs
after the remote service has resubscribed and before callers' requests flow again: the daemon is
described again, the editor's MCP specs are attached again, and turns the daemon lost in a restart
are announced to the session's sink once per admission. Live reasoning needs no request: the
daemon's event stream carries ``reasoning.delta`` to every viewer (D-16.603).

Creating a session needs the daemon to list ``acp`` among its creatable sources and advertise
``ACP_EDITOR_POLICY`` (else ``acp_policy_unavailable``). Launch options the wire cannot carry yet
(anything but cwd, model, toolsets) are refused with a pointer to ``--local`` (D-16.490).

``session.usage`` reads the session's counters and ``admission.cancel`` drops a queued input (a
started one is stopped through its generation, ``session.interrupt``). ``session.mutate`` carries
the editor's MCP servers, forks, folder moves and the other session changes; a daemon that does not
serve it (``-32601``) answers ``mutation_unavailable`` and does not advertise ``ACP_EDITOR_POLICY``.
Reads of the transcript, the session row and the runtime ledger go through read-only store
connections (``store_view``).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

from indus.acp import store_view
from indus.acp.backend import (
    Admission,
    BackendError,
    CancelOutcome,
    CommandReply,
    CreateRequest,
    EditorPolicy,
    EventSink,
    HostMode,
    ListedSession,
    LostTurn,
    MutationResult,
    PendingPrompt,
    SessionSnapshot,
    Settled,
    UsageInfo,
)
from indus.foundation.blocking import run_blocking
from indus.foundation.scope import Scope
from indus.protocol.attach import ACP_EDITOR_POLICY, AdmissionCancelResult, RuntimeDescribeResult
from indus.protocol.errors import METHOD_NOT_FOUND, SESSION_BUSY, SESSION_NOT_FOUND
from indus.protocol.events import ExitReason
from indus.protocol.methods import ProtocolError, SessionCreateParams, SubmitStatus
from indus.service.remote.channel import Channel, DialRefused, NotSent, RpcError, TransportLost
from indus.service.remote.host import OUTCOME_UNKNOWN, OutcomeUnknown, RemoteSessionService
from indus.service.ws_auth import stable_profile_id

__all__ = [
    "ACP_SOURCE",
    "CARRIED_LAUNCH",
    "AttachedHost",
    "Connect",
    "default_connect",
]

log = logging.getLogger(__name__)

ACP_SOURCE: Final = "acp"
CARRIED_LAUNCH: Final = frozenset({"cwd", "model", "toolsets"})
"""Launch fields ``session.create`` carries today (the rest is refused, D-16.490)."""
LAUNCH_UNAVAILABLE: Final = (
    "This launch option is not carried to the gateway daemon yet; run `indus acp --local` to use "
    "it."
)
_REQUEST_ID: Final = re.compile(r"^[0-9a-f]{32}$")

type Connect = Callable[[], Awaitable[RemoteSessionService]]


def default_connect(scope: Scope) -> Connect:
    """The channel + remote service for this profile: the first dial discovers only, later
    dials may recover a daemon that died (see the module notes)."""

    async def connect() -> RemoteSessionService:
        from indus.service.gateway_ensure import RecoverBudget
        from indus.service.remote.channel import home_dialer
        from indus.service.remote.journal import Journal

        discover = home_dialer(scope.home)
        recover = home_dialer(scope.home, env=scope.env.to_dict(), budget=RecoverBudget())
        connected = False

        async def dial() -> Any:
            nonlocal connected
            dialed = await (recover if connected else discover)()
            connected = True
            return dialed

        channel = Channel(dial, surface=ACP_SOURCE, features=("server_requests",))
        return RemoteSessionService(channel, Journal(scope.home), surface=ACP_SOURCE)

    return connect


def _profile_route(scope: Scope) -> tuple[str | None, str | None]:
    """The profile's default model and provider as this process resolves them (the daemon serves
    the same profile); ``(None, None)`` when nothing resolves here."""
    try:
        from indus.providers.route import resolve

        route = resolve(scope)
    except Exception:
        return None, None
    return route.model or None, route.provider or None


def _request_id(input_id: str) -> str:
    """The journal/admission request id for an editor ``input_id`` (same input → same id)."""
    text = input_id.strip().lower()
    if _REQUEST_ID.match(text):
        return "sub-" + text
    return "sub-" + hashlib.sha256(input_id.encode("utf-8")).hexdigest()[:32]


def _outcome(reason: ExitReason) -> str:
    if reason is ExitReason.COMPLETED:
        return "completed"
    if reason in (ExitReason.INTERRUPTED_BY_USER, ExitReason.INTERRUPTED_BY_SYSTEM):
        return "cancelled"
    return "failed"


def _from_protocol(error: ProtocolError) -> BackendError:
    if error.error.code == SESSION_NOT_FOUND.code:
        return BackendError("not_found", error.message or "no such session")
    if error.error.code == SESSION_BUSY.code:
        return BackendError("session_busy", error.message or "the session is busy")
    return BackendError("backend_failed", error.message or error.error.name)


def _from_channel(error: BaseException) -> BackendError:
    if isinstance(error, OutcomeUnknown | TransportLost):
        return BackendError("outcome_unknown", OUTCOME_UNKNOWN)
    if isinstance(error, NotSent):
        return BackendError("daemon_unavailable", str(error))
    if isinstance(error, ProtocolError):
        return _from_protocol(error)
    if isinstance(error, RpcError):
        return BackendError("backend_failed", error.message)
    if isinstance(error, DialRefused):
        return _refused(error)
    return BackendError("backend_failed", f"{type(error).__name__}: the gateway call failed")


def _refused(error: DialRefused) -> BackendError:
    if error.reason == "profile_mismatch":
        return BackendError("profile_mismatch", "the gateway daemon serves another profile")
    detail = f" ({error.reason})" if error.reason else ""
    return BackendError("daemon_unavailable", "the gateway daemon could not be reached" + detail)


class AttachedHost:
    """``AcpBackend`` over the profile's daemon (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        *,
        connect: Connect | None = None,
        route: Callable[[], tuple[str | None, str | None]] | None = None,
    ) -> None:
        self.scope = scope
        self._connect = connect or default_connect(scope)
        self._route = route or (lambda: _profile_route(scope))
        """The profile's default ``(model, provider)`` (blocking; the daemon serves the same
        profile, so a new session runs on it unless the launch named a model)."""
        self._service: RemoteSessionService | None = None
        self._lock = asyncio.Lock()
        self.described: Mapping[str, Any] = {}
        self._editors: dict[str, EditorPolicy] = {}
        self._sinks: dict[str, EventSink] = {}
        self._undo: dict[str, Callable[[], None]] = {}
        self._announced: set[str] = set()
        self._held: set[str] = set()

    @property
    def mode(self) -> HostMode:
        return "attached"

    # -- connection -------------------------------------------------------------------------------

    async def _described(self, channel: Channel, *, before_open: bool = False) -> dict[str, Any]:
        """``runtime.describe`` over the connection, its profile id checked against ours."""
        answer = await channel.request("runtime.describe", {}, before_open=before_open)
        described = RuntimeDescribeResult.model_validate(answer)
        if described.profile_id != stable_profile_id(self.scope.home):
            raise DialRefused(
                "profile_mismatch", "the daemon serves another profile", terminal=True
            )
        return described.model_dump(mode="json")

    async def start(self) -> RemoteSessionService:
        """Connect now (the selector calls it so a missing daemon shows at once)."""
        return await self._client()

    async def _client(self) -> RemoteSessionService:
        async with self._lock:
            service = self._service
            if service is not None and service.channel.state not in ("failed", "closed"):
                return service
            if service is not None:
                self._service = None
                with contextlib.suppress(Exception):
                    await service.aclose()
            fresh = await self._connect()
            try:
                await fresh.start()
                self.described = await self._described(fresh.channel)
            except DialRefused as refused:
                with contextlib.suppress(Exception):
                    await fresh.aclose()
                raise _refused(refused) from None
            except BaseException as exc:
                with contextlib.suppress(Exception):
                    await fresh.aclose()
                raise _from_channel(exc) from None
            self._service = fresh
            # After the remote service's own hook: resubscribe first, then the editor's part.
            fresh.channel.on_ready(self._on_ready)
            if service is not None:
                await self._rehold(fresh)
            return fresh

    async def _rehold(self, service: RemoteSessionService) -> None:
        """A rebuilt connection: attach every session this editor held again."""
        for key in sorted(self._held):
            try:
                await service.resume(key)
            except Exception:
                log.info("ACP session %s could not be attached again", key, exc_info=True)
                continue
            sink = self._sinks.get(key)
            if sink is not None:
                self._undo[key] = self._wire(service, key, sink)

    async def _on_ready(self, channel: Channel) -> None:
        """Every (re)connection, after the remote service resubscribed (module notes)."""
        service = self._service
        if service is None:
            return
        with contextlib.suppress(RpcError, TransportLost, NotSent):
            self.described = await self._described(channel, before_open=True)
        for key in sorted(self._held):
            try:
                current = service.current_id(key)
            except ProtocolError:
                continue  # gone after the reconnect (the remote service dropped it)
            editor = self._editors.get(key)
            if editor is not None and editor.mcp_servers:
                with contextlib.suppress(RpcError, TransportLost, NotSent):
                    await channel.request(
                        "session.mutate",
                        self._mutation(current, "editor_mcp", editor.wire()),
                        before_open=True,
                    )
            await self._announce_lost(key, current)

    async def _announce_lost(self, key: str, current: str) -> list[LostTurn]:
        lost = await run_blocking(
            lambda: store_view.lost_turns(self.scope, {key, current}), label="acp ledger"
        )
        fresh = [turn for turn in lost if turn.admission_id not in self._announced]
        self._announced.update(turn.admission_id for turn in fresh)
        sink = self._sinks.get(key)
        if sink is not None:
            for turn in fresh:
                try:
                    sink.on_lost(key, turn)
                except Exception:
                    log.exception("an ACP lost-turn listener failed")
        return lost

    @staticmethod
    def _mutation(session_id: str, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "operation": operation,
            "payload": dict(payload),
            "request_id": secrets.token_hex(16),
        }

    # -- sessions ---------------------------------------------------------------------------------

    def _require_policy(self) -> None:
        sources = self.described.get("creatable_sources") or []
        capabilities = self.described.get("capabilities") or []
        if ACP_SOURCE not in sources or ACP_EDITOR_POLICY not in capabilities:
            raise BackendError(
                "acp_policy_unavailable",
                "the gateway daemon cannot host editor sessions yet (it does not offer the ACP "
                "editor policy); update it, or run `indus acp --local`",
            )

    async def create(self, request: CreateRequest) -> SessionSnapshot:
        service = await self._client()
        self._require_policy()
        extra = sorted(k for k, v in request.launch.fields.items() if v and k not in CARRIED_LAUNCH)
        if extra:
            raise BackendError("launch_unavailable", f"{LAUNCH_UNAVAILABLE} ({', '.join(extra)})")
        model = request.launch.fields.get("model")
        toolsets = request.launch.fields.get("toolsets")
        params = SessionCreateParams(
            cwd=request.cwd or None,
            model=model if isinstance(model, str) and model else None,
            toolsets=[str(t) for t in toolsets] if isinstance(toolsets, list | tuple) else None,  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            idempotency_key=request.request_id,
        )
        try:
            created = await service.create(params)
        except BaseException as exc:
            raise _from_channel(exc) from None
        sid = created.session_id
        self._held.add(sid)
        self._editors[sid] = request.editor
        if request.editor.mcp_servers:
            await self.mutate(sid, "editor_mcp", request.editor.wire())
        epoch = service.channel.ready_params.get("replay_epoch")
        route_model, route_provider = await run_blocking(self._route, label="acp route")
        return SessionSnapshot(
            session_id=sid,
            cwd=request.cwd or None,
            model=params.model or route_model,
            provider=route_provider,
            replay_epoch=epoch if isinstance(epoch, str) else None,
            created=created.created,
        )

    async def resume(
        self, session_id: str, *, editor: EditorPolicy | None = None
    ) -> SessionSnapshot:
        service = await self._client()
        try:
            target = service.current_id(session_id)
        except ProtocolError:
            _root, target = await run_blocking(
                store_view.resolve, self.scope, session_id, label="acp lineage"
            )
        try:
            resumed = await service.resume(target)
        except BaseException as exc:
            raise _from_channel(exc) from None
        key = service.key_of(resumed.session_id)
        self._held.add(key)
        if editor is not None and editor.mcp_servers:
            self._editors[key] = editor
            await self.mutate(key, "editor_mcp", editor.wire())
        else:
            self._editors.setdefault(key, EditorPolicy())
        sid = resumed.session_id
        rows = await run_blocking(store_view.transcript, self.scope, sid, label="acp transcript")
        row = await run_blocking(store_view.session_row, self.scope, sid, label="acp session row")
        lost = await run_blocking(
            lambda: store_view.lost_turns(self.scope, {key, sid}), label="acp ledger"
        )
        self._announced.update(turn.admission_id for turn in lost)
        cwd = row.get("cwd") if row else None
        title = resumed.title or (row.get("title") if row else None)
        model = row.get("model") if row else None
        model = model if isinstance(model, str) and model else None
        provider = await self._provider_of(row, model)
        epoch = service.channel.ready_params.get("replay_epoch")
        return SessionSnapshot(
            session_id=key,
            cwd=cwd if isinstance(cwd, str) and cwd else None,
            title=title if isinstance(title, str) else None,
            model=model,
            provider=provider,
            history=tuple(rows),
            pending=tuple(PendingPrompt(r) for r in resumed.pending_requests),
            lost=tuple(lost),
            replay_epoch=epoch if isinstance(epoch, str) else None,
            last_seq=resumed.last_seq,
        )

    async def _provider_of(self, row: Mapping[str, Any] | None, model: str | None) -> str | None:
        """The provider of a stored session: the row's, else the profile's when the row runs the
        profile's model (so the model picker shows ``provider:model`` as on the local host)."""
        stored = row.get("billing_provider") if row else None
        if isinstance(stored, str) and stored:
            return stored
        if model is None:
            return None
        route_model, route_provider = await run_blocking(self._route, label="acp route")
        return route_provider if route_model == model else None

    def editor_policy(self, session_id: str) -> EditorPolicy | None:
        return self._editors.get(self._key(session_id))

    def _key(self, session_id: str) -> str:
        service = self._service
        if service is None:
            return session_id
        try:
            return service.key_of(session_id)
        except ProtocolError:
            return session_id

    async def fork(self, session_id: str, *, editor: EditorPolicy | None = None) -> str:
        parent = self._key(session_id)
        payload: dict[str, Any] = {}
        if editor is not None and editor.mcp_servers:
            payload = editor.wire()
        result = await self.mutate(parent, "branch", payload)
        child = result.data.get("session_id")
        if result.status != "applied" or not isinstance(child, str) or not child:
            reason = result.data.get("reason")
            raise BackendError(
                "backend_failed", f"the fork was refused ({reason or result.status})"
            )
        chosen = editor if editor is not None and editor.mcp_servers else None
        self._editors[child] = chosen or self._editors.get(parent, EditorPolicy())
        return child

    async def mutate(
        self, session_id: str, operation: str, payload: Mapping[str, Any]
    ) -> MutationResult:
        service = await self._client()
        try:
            current = service.current_id(session_id)
        except ProtocolError:
            current = session_id
        try:
            answer = await service.channel.request(
                "session.mutate", self._mutation(current, operation, payload)
            )
        except RpcError as error:
            if error.code == METHOD_NOT_FOUND.code:
                raise BackendError(
                    "mutation_unavailable",
                    f"the gateway daemon cannot apply the {operation} change yet",
                ) from None
            if error.code == SESSION_BUSY.code:
                raise BackendError("session_busy", error.message) from None
            if error.code == SESSION_NOT_FOUND.code:
                raise BackendError("not_found", error.message) from None
            raise _from_channel(error) from None
        except BaseException as exc:
            raise _from_channel(exc) from None
        status = answer.get("status", "applied")
        if status not in ("applied", "deferred", "cancelled", "refused", "confirm"):
            status = "refused"
        if operation == "editor_mcp" and status == "applied":
            servers = payload.get("mcp_servers")
            specs = tuple(dict(s) for s in servers) if isinstance(servers, list) else ()  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            self._editors[self._key(session_id)] = EditorPolicy(mcp_servers=specs)  # pyright: ignore[reportUnknownArgumentType]
        if operation == "workspace" and status == "applied":
            with contextlib.suppress(ProtocolError):
                await service.resume(current)  # the daemon reloaded the agent: refresh our view
        return MutationResult(status, {k: v for k, v in answer.items() if k != "status"})

    async def list_sessions(self, *, limit: int = 1000) -> list[ListedSession]:
        return await run_blocking(
            lambda: store_view.listed(self.scope, limit=limit), label="acp session list"
        )

    # -- turns ------------------------------------------------------------------------------------

    async def submit(self, session_id: str, text: str, *, input_id: str) -> Admission:
        service = await self._client()
        try:
            current = service.current_id(session_id)
        except ProtocolError as exc:
            raise _from_protocol(exc) from None
        request_id = _request_id(input_id)
        known = await run_blocking(service.journal.get, request_id, label="journal")
        if known is None:
            await run_blocking(
                lambda: service.journal.prepare(current, text, request_id=request_id),
                label="journal",
            )
        try:
            result = await service.retry(request_id)
        except BaseException as exc:
            raise _from_channel(exc) from None
        row = await run_blocking(store_view.admission, self.scope, result.turn_id, label="ledger")
        if row is not None and row.state in ("terminal", "unknown"):
            return Admission(
                result.turn_id,
                row.generation or result.generation,
                row.state,
                outcome=row.outcome,
                final_text=row.final_text,
            )
        state = "started" if result.status is SubmitStatus.STREAMING else "queued"
        return Admission(result.turn_id, result.generation, state)

    async def cancel(self, session_id: str, admission_id: str) -> CancelOutcome:
        service = await self._client()
        row = await run_blocking(store_view.admission, self.scope, admission_id, label="ledger")
        if row is None or row.session_id not in {self._key(session_id), session_id}:
            return "not_found"
        if row.state == "terminal":
            return "settled"
        if row.state == "unknown":
            return "unknown"
        if row.state == "started" and row.generation is not None:
            try:
                done = await service.interrupt(session_id, row.generation)
            except BaseException as exc:
                raise _from_channel(exc) from None
            return "interrupted" if done.interrupted else "settled"
        try:
            answer = await service.channel.request(
                "admission.cancel",
                {"session_id": self.current_id(session_id), "admission_id": admission_id},
            )
        except RpcError as error:
            if error.code == METHOD_NOT_FOUND.code:
                raise BackendError(
                    "mutation_unavailable", "the gateway daemon cannot cancel queued input yet"
                ) from None
            raise _from_channel(error) from None
        except BaseException as exc:
            raise _from_channel(exc) from None
        done = AdmissionCancelResult.model_validate(answer)
        if done.cancelled:
            return "cancelled"
        if done.state == "unknown":
            return "unknown"
        if done.state == "started" and done.generation is not None:
            # Claimed since the ledger read: stop that turn through its generation.
            try:
                stopped = await service.interrupt(session_id, done.generation)
            except BaseException as exc:
                raise _from_channel(exc) from None
            return "interrupted" if stopped.interrupted else "settled"
        return "settled"

    async def receipt(self, session_id: str, admission_id: str) -> Admission:
        row = await run_blocking(store_view.admission, self.scope, admission_id, label="ledger")
        if row is None:
            raise BackendError("not_found", "no such admission")
        return Admission(
            row.admission_id,
            row.generation,
            row.state,
            outcome=row.outcome,
            final_text=row.final_text,
        )

    async def wait(self, session_id: str, admission_id: str) -> Settled:
        service = await self._client()
        try:
            turn = await service.wait(session_id, admission_id)
        except BaseException as exc:
            raise _from_channel(exc) from None
        failure = turn.failure.reason if turn.failure is not None else None
        return Settled(turn.turn_id, _outcome(turn.reason), failure)

    async def lost(self, session_id: str) -> list[LostTurn]:
        ids = {self._key(session_id), self.current_id(session_id), session_id}
        return await run_blocking(
            lambda: store_view.lost_turns(self.scope, ids), label="acp ledger"
        )

    async def steer(self, session_id: str, text: str, *, admission_id: str | None) -> bool:
        if not admission_id:
            return False
        service = await self._client()
        row = await run_blocking(store_view.admission, self.scope, admission_id, label="ledger")
        if row is None or row.state != "started" or row.generation is None:
            return False  # only this editor's own running turn is steered
        try:
            done = await service.steer(session_id, text, generation=row.generation)
        except (ProtocolError, RpcError, TransportLost, NotSent):
            return False
        return done.queued

    async def run_command(self, session_id: str, name: str, arg: str) -> CommandReply:
        done = await self.mutate(session_id, "command", {"name": name, "arg": arg})
        if done.status != "applied":
            message = done.data.get("message") or f"/{name} is not available in this session"
            raise BackendError("unsupported_command", str(message))
        return CommandReply(str(done.data.get("text") or ""))

    async def answer(
        self,
        session_id: str,
        request_id: str,
        generation: int | None,
        answer: Mapping[str, Any],
    ) -> bool:
        service = await self._client()
        try:
            result = await service.answer_server_request(session_id, request_id, answer)
        except (ProtocolError, RpcError, TransportLost, NotSent):
            return False
        return result.accepted

    async def usage(self, session_id: str) -> UsageInfo | None:
        service = await self._client()
        try:
            current = service.current_id(session_id)
            answer = await service.channel.request("session.usage", {"session_id": current})
        except (ProtocolError, RpcError, TransportLost, NotSent):
            return None
        numbers = answer.get("usage")
        if not isinstance(numbers, Mapping):
            return None
        size = numbers.get("context_window")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        used = numbers.get("context_tokens")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            return None
        return UsageInfo(size=size, used=used if isinstance(used, int) and used > 0 else 0)

    async def show_reasoning(self, session_id: str) -> None:
        return None  # the daemon streams ``reasoning.delta`` to every viewer (D-16.603)

    def _wire(self, service: RemoteSessionService, key: str, sink: EventSink) -> Callable[[], None]:
        def on_request(request: Any) -> None:
            generation = service.generation(key) if key in service.attached else None
            request_id = str(getattr(request, "request_id", ""))
            try:
                found = service._session(key)  # pyright: ignore[reportPrivateUsage]
                generation = found.offers.get(request_id, generation)
            except ProtocolError:
                pass
            sink.on_request(key, request, generation)

        off_events = service.subscribe(key, lambda e: sink.on_event(key, e))
        off_requests = service.on_request(key, on_request)
        off_gaps = service.on_gap(key, lambda info: sink.on_gap(key, info))

        def off() -> None:
            off_events()
            off_requests()
            off_gaps()

        return off

    def subscribe(self, session_id: str, sink: EventSink) -> Callable[[], None]:
        service = self._service
        if service is None:
            raise BackendError("not_found", "no session is attached")
        try:
            key = service.key_of(session_id)
        except ProtocolError as exc:
            raise _from_protocol(exc) from None
        previous = self._undo.pop(key, None)
        if previous is not None:
            previous()
        self._sinks[key] = sink
        self._undo[key] = self._wire(service, key, sink)

        def off() -> None:
            undo = self._undo.pop(key, None)
            self._sinks.pop(key, None)
            if undo is not None:
                undo()

        return off

    def current_id(self, session_id: str) -> str:
        service = self._service
        if service is None:
            return session_id
        try:
            return service.current_id(session_id)
        except ProtocolError:
            return session_id

    async def close_session(self, session_id: str) -> None:
        service = self._service
        if service is None:
            return
        key = self._key(session_id)
        self._held.discard(key)
        undo = self._undo.pop(key, None)
        self._sinks.pop(key, None)
        if undo is not None:
            undo()
        await service.close(key)

    async def aclose(self) -> None:
        service, self._service = self._service, None
        for undo in self._undo.values():
            undo()
        self._undo.clear()
        self._sinks.clear()
        if service is not None:
            await service.aclose()
