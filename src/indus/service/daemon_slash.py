"""The daemon as the shared slash commands' host (W48-h): ``slash.exec`` / ``command.dispatch`` for
an attached TUI, with the in-process bridge's answers (``service.slash_commands``).

``DaemonSlashHost`` acts on one hosted session the connection attached (the transport checks
that first). Changes go where the daemon's other clients' changes go: ``/title``, ``/undo``,
``/retry`` and ``/branch`` through ``session.mutate`` (``rename``, ``rewind``, ``branch``: the
session's gate, so never under a starting turn), ``/compress`` under the same gate, ``/stop`` and
``/steer`` through the generation fences naming the turn that runs. A title change reaches every
viewer of the session as ``session.title``.

The session switches ``/yolo``, ``/reasoning``, ``/fast`` go through the daemon's ``config.set``
(``service.daemon_config``, W50-a), and ``/status`` counts the inputs waiting in the session's
durable queue. What a shared daemon does not run is refused with its reason
(``picker_reads.DAEMON_REFUSED``: ``/queue`` is one viewer's own queue in the bridge), and the
daemon's catalogue lists it ``unavailable``. Quick ``exec`` commands run here (W50-b) through
``service.host_shell`` with the checks ``--local`` applies (dangerous-command refusal, the built
child environment, the limit on the scope's clock, redaction before the cut), in the session's
folder.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from pydantic import JsonValue

from indus.foundation.blocking import run_blocking
from indus.foundation.scope import Scope
from indus.protocol.attach import SessionMutateParams, SessionMutateResult
from indus.protocol.bridge import ConfigKey, ConfigSetParams
from indus.protocol.errors import INVALID_PARAMS, SESSION_BUSY, ErrorCode
from indus.protocol.methods import ProtocolError
from indus.service import daemon_config, host_shell, picker_reads, slash_commands
from indus.service.commands.registry import Surface
from indus.service.daemon_host import DaemonSessionHost
from indus.service.fences import ControlApplied, ControlStatus, StaleGeneration
from indus.service.session_service import SessionService
from indus.state import titles

if TYPE_CHECKING:
    from indus.learning.skills.invoke import CommandTable

__all__ = ["DAEMON_SLASH_TIMEOUT_S", "DaemonSlashHost"]

DAEMON_SLASH_TIMEOUT_S: Final = 45.0
"""How long a command may take on the daemon (the bridge's default ``slash_timeout``)."""
COMPRESSED: Final[Mapping[str, str]] = {"nothing": "Nothing to compress yet."}


def _took(result: ControlApplied | StaleGeneration) -> bool:
    return isinstance(result, ControlApplied) and result.status is not ControlStatus.REJECTED


class DaemonSlashHost:
    """One attached session of the daemon as a ``SlashHost`` (see the module notes)."""

    def __init__(
        self,
        host: DaemonSessionHost,
        key: str,
        *,
        scope: Scope | None = None,
        surface: Surface = "tui",
    ) -> None:
        self.host = host
        self.key = key
        self._scope = scope if scope is not None else host.service.scope
        self._surface: Surface = surface

    @property
    def scope(self) -> Scope:
        """The connection's profile scope (settings, quick commands, skills)."""
        return self._scope

    @property
    def service(self) -> SessionService:
        return self.host.service

    @property
    def surface(self) -> Surface:
        return self._surface

    @property
    def timeout(self) -> float:
        return max(slash_commands.MIN_TIMEOUT_S, DAEMON_SLASH_TIMEOUT_S)

    @property
    def refused(self) -> Mapping[str, str]:
        return picker_reads.DAEMON_REFUSED

    @property
    def quick_exec_refusal(self) -> str | None:
        return None  # they run here (W50-b)

    def skills(self, session_id: str | None) -> CommandTable | None:
        return picker_reads.skill_table(self.scope, session_id)

    async def queued(self, session_id: str) -> int:
        """The inputs waiting in the session's durable queue (W50-a; every viewer's)."""
        return await self.host.queued_inputs(self.key)

    async def set_switch(self, session_id: str, key: ConfigKey, value: JsonValue) -> JsonValue:
        """The daemon's ``config.set`` of a session switch (``service.daemon_config``, W50-a)."""
        sid = self.service.current_id(self.key)
        params = ConfigSetParams(key=key, value=value, session_id=sid)
        done = await daemon_config.set_switch(self.host, self.key, params, scope=self.scope)
        return done.value

    async def _mutate(self, operation: str, **payload: object) -> SessionMutateResult:
        params = SessionMutateParams.model_validate(
            {
                "session_id": self.service.current_id(self.key),
                "operation": operation,
                "payload": payload,
                "request_id": f"slash-{secrets.token_hex(8)}",
            }
        )
        return await self.host.mutations.mutate(self.key, params)

    @staticmethod
    def _check(result: SessionMutateResult, path: str, code: ErrorCode = INVALID_PARAMS) -> None:
        if result.status == "refused":
            raise ProtocolError(code, result.message or result.reason or "refused", path=path)

    async def title(self, session_id: str, title: str | None) -> tuple[str | None, bool]:
        if title is None:
            sid = self.service.current_id(session_id)
            stored = await run_blocking(
                titles.get_title, self.host.store, sid, label="session title"
            )
            return stored, False
        done = await self._mutate("rename", title=title)
        self._check(done, "title")
        return done.title, False

    async def undo(self, session_id: str, count: int) -> tuple[int, str]:
        done = await self._mutate("rewind", count=count)
        return int(done.removed or 0), done.text or ""

    async def branch(self, session_id: str, title: str | None) -> tuple[str, str | None]:
        done = await self._mutate("branch", **({"title": title} if title else {}))
        child = str(done.session_id or "")
        named = await run_blocking(titles.get_title, self.host.store, child, label="session title")
        return child, named

    async def compress(self, session_id: str, focus: str | None) -> str:
        async with self.host.mutations.gate(self.key):
            if self.host.running_generation(self.key) is not None:
                raise ProtocolError(SESSION_BUSY, "stop the running turn first")
            outcome = await self.service.compress(self.service.current_id(self.key), focus)
        if outcome.reason:
            return outcome.reason
        if outcome.status == "compressed":
            return f"Compressed {outcome.before} messages into {outcome.after}."
        return COMPRESSED.get(outcome.status) or f"Compression: {outcome.status}."

    def stop(self, session_id: str) -> bool:
        generation = self.host.running_generation(self.key)
        if generation is None:
            return False
        return _took(self.host.interrupt(self.key, generation, hard=True))

    def steer(self, session_id: str, text: str) -> bool:
        generation = self.host.running_generation(self.key)
        if generation is None:
            return False
        return _took(self.host.steer(self.key, generation, text))

    async def run_quick(self, session_id: str, command: str) -> tuple[str, str, int]:
        ran = await host_shell.run_host_command(
            self.scope,
            command,
            session_id=self.service.current_id(self.key),
            service=self.service,
            timeout=host_shell.TIMEOUT_S,
            clock=self.scope.clock,
        )
        return ran.stdout, ran.stderr, ran.code
