"""Multiplexing inside ``gateway run``: the daemon side of P24-01..04 (wave 42 wiring).

With ``gateway.multiplex_profiles: true`` the ``GatewayDaemon`` of the launch profile, before it
takes its per-home instance lock, asks ``decide_second_run`` whether a host gateway already serves
this user (``open_host``): attach (exit 0, or 75 under a supervisor), refuse (75 transient, 78 / 1
permanent) or start. Starting claims the host lock and publishes the rendezvous record
(``MultiplexHost``); the record and the lock go when the daemon ends.

``MultiplexServing`` is what the running host keeps:

- **one embedded daemon per served secondary profile.** ``serve`` builds the profile's scope with
  ``ProfileScopeCache`` (its own home, ``.env``, config and secret store; the launch env reduced
  to OS names, so a secret or allowlist of the launch profile is absent: R06.26 fails closed) and
  runs a ``GatewayDaemon`` for it on this loop, with its own store, session service, authorizer,
  adapters, control socket and attach endpoint. It takes that profile's instance lock, so a
  separate ``gateway run`` for it sees a live gateway. Signals, watchdogs, idle exit and systemd
  belong to the launch daemon only; the root-level parts (task boards, group rooms) run once, in
  the launch daemon. ``unserve`` (or the host's shutdown) stops it the normal way (drain, then
  stop). A secondary whose daemon ends on its own is unserved.
- **the adapter table and the router.** Every connected adapter of every served profile is in the
  ``AdapterTable``; each adapter's inbound events go through ``ProfileRouter.identify`` (the
  receiving bot and the profile that runs the message, ``gateway.profile_routes`` of the launch
  profile) to that profile's admission pipeline; inbound files are moved into its cache first. A
  message that cannot be placed is dropped with one warning (and one "please resend" notice).
  Replies of every served profile go through ``delivery_adapter``: the bot that received the chat
  answers, or none (never another bot by guess); the receiving bot is persisted beside the session
  key (``ReceivingBots``) so a restart keeps each chat with its own bot.
- **shared ingress.** The launch profile's API server and webhook receiver serve a
  ``SharedIngress`` around their own app (``ingress_front``); a secondary's HTTP surfaces do not
  bind a port: they are mounted at ``/p/<profile>/`` on those listeners (``mount``), checked with
  that profile's own keys and secrets.

The control verbs ``serve-profile`` / ``unserve-profile`` / ``rescan-profiles`` and the served set
in ``identify`` come from ``MultiplexHost``; the daemon's ticket book is limited to the served set.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from indus.foundation.blocking import run_blocking
from indus.foundation.config import loader as config_loader
from indus.foundation.scope import Scope, use_scope
from indus.gateway.core.events import InboundEvent, Source
from indus.gateway.multiplex.host import (
    MultiplexHost,
    RunVerdict,
    decide_second_run,
    host_state_dir,
)
from indus.gateway.multiplex.ingress import SharedIngress
from indus.gateway.multiplex.routes import (
    AdapterTable,
    ProfileRouter,
    ReceivingBots,
    RoutingIdentity,
    Unresolved,
    parse_routes,
)
from indus.gateway.multiplex.scope import ProfileScopeCache, ScopeBinder, adopt_inbound_media
from indus.gateway.registry import PlatformRegistry
from indus.state.gateway_runtime import EXIT_RESTART

if TYPE_CHECKING:
    from indus.gateway.adapters.base import Adapter, InboundSink
    from indus.gateway.daemon.parts import DaemonPart
    from indus.gateway.daemon.process import GatewayDaemon
    from indus.gateway.http.serving import Asgi
    from indus.gateway.outbound.sender import OutboundSender

__all__ = [
    "CONFIG_KEY",
    "SERVE_START_S",
    "MultiplexServing",
    "multiplex_enabled",
    "open_host",
]

log = logging.getLogger(__name__)

CONFIG_KEY: Final = "gateway.multiplex_profiles"
SERVE_START_S: Final = 120.0
"""How long a served profile's daemon may take to start before the serve counts as failed."""
STOP_S: Final = 90.0
"""How long a served profile's daemon may take to drain and stop before it is cancelled."""
_IDENTITIES_KEPT: Final = 4096

type SecondaryParts = Callable[[GatewayDaemon], Sequence[DaemonPart]]


def multiplex_enabled(scope: Scope) -> bool:
    """``gateway.multiplex_profiles`` of ``scope`` (off unless explicitly true; D-16.17)."""
    try:
        view: Any = scope.config if scope.config is not None else config_loader.load(scope)
        return view.get(CONFIG_KEY) is True
    except Exception:
        return False


def _routes(scope: Scope) -> list[Any]:
    try:
        view: Any = scope.config if scope.config is not None else config_loader.load(scope)
        raw = view.get("gateway.profile_routes")
    except Exception:
        return []
    routes, problems = parse_routes(raw or [])
    for problem in problems:
        log.warning("gateway.profile_routes: %s", problem)
    return routes


def _target(source: Source) -> tuple[str, str, str]:
    return (
        str(source.platform).lower(),
        str(source.chat_id or ""),
        str(source.thread_id or ""),
    )


async def open_host(
    daemon: GatewayDaemon, *, record_dir: Path | None = None
) -> MultiplexServing | RunVerdict:
    """Before the instance lock: attach to / refuse beside a live host (the verdict), or claim
    the host and return what the running host keeps."""
    scope = daemon.scope
    where = record_dir if record_dir is not None else host_state_dir(scope.env)
    verdict = await run_blocking(
        lambda: decide_second_run(
            scope.profile_id, root=scope.root, record_dir=where, env=scope.env
        ),
        label="host gateway check",
    )
    if verdict.outcome != "start":
        return verdict
    serving = MultiplexServing(daemon, record_dir=where)
    claimed = await run_blocking(serving.host.claim, label="host gateway lock")
    if not claimed:
        return RunVerdict(
            "refuse",
            EXIT_RESTART,
            "another gateway took this user's host lock a moment ago; nothing was started "
            "(a supervisor retries)",
            transient=True,
        )
    return serving


class MultiplexServing:
    """See the module notes."""

    def __init__(
        self,
        launch: GatewayDaemon,
        *,
        record_dir: Path,
        secondary_parts: SecondaryParts | None = None,
    ) -> None:
        self.launch = launch
        self.profile = launch.scope.profile_id
        self.host = MultiplexHost(
            launch.scope,
            record_dir=record_dir,
            clock=launch.clock,
            on_serve=self.serve,
            on_unserve=self.unserve,
        )
        self.scopes = ProfileScopeCache(launch.scope)
        self.table = AdapterTable()
        self.router = ProfileRouter(
            _routes(launch.scope),
            self.table,
            served=self.host.served_profiles,
            bots=ReceivingBots(launch.scope.home),
        )
        self.secondary_parts = secondary_parts
        self.daemons: dict[str, GatewayDaemon] = {self.profile: launch}
        self.ingress: dict[str, SharedIngress] = {}
        self._runs: dict[str, asyncio.Task[int]] = {}
        self._identities: OrderedDict[tuple[str, tuple[str, str, str]], RoutingIdentity] = (
            OrderedDict()
        )
        self._stopping = False
        self.track(launch)

    # -- daemons ---------------------------------------------------------------------------------

    def track(self, daemon: GatewayDaemon) -> None:
        """Keep the adapter table in step with ``daemon``'s platforms."""
        profile = daemon.scope.profile_id

        def up(name: str, adapter: Adapter) -> None:
            self.table.register(profile, name, adapter)

        def down(name: str, _fatal: bool, _reason: str) -> None:
            self.table.unregister(profile, name)

        daemon.on_platform_up(up)
        daemon.on_platform_down(down)

    def daemon_for(self, profile: str) -> GatewayDaemon | None:
        return self.daemons.get(profile)

    def served(self) -> list[str]:
        return self.host.served_profiles()

    def _secondary_options(self) -> Any:
        launch = self.launch
        return replace(
            launch.options,
            replace=False,
            install_signals=False,
            watchdogs=False,
            export_guard_env=False,
            idle_exit_seconds=0.0,
            # D-16.542 (amended, wave 42): a served profile's daemon serves its own attach
            # endpoint, bound to that profile's routed scope (``WsEndpoint(served=True)``).
            supervised=launch.is_supervised,
        )

    async def serve(self, profile: str, home: Path) -> None:
        """The host's serve hook: run ``profile``'s daemon on this loop until it is up."""
        from indus.gateway.daemon.process import GatewayDaemon

        if self._stopping:
            raise RuntimeError("the host gateway is stopping")
        scope = await run_blocking(self.scopes.get, profile, label="profile scope")
        launch = self.launch
        registry = launch.registry if launch.registry_given else PlatformRegistry()
        sub = GatewayDaemon(
            scope,
            options=self._secondary_options(),
            registry=registry,
            service_factory=launch.service_factory,
            parts=list(self.secondary_parts(launch)) if self.secondary_parts else None,
            surface_factory=launch.surface_factory,
            parent=launch,
        )
        self.track(sub)
        ready = asyncio.Event()
        sub.on_ready(ready.set)
        # Everything the served profile's daemon starts runs under its scope (P24-02).
        task = ScopeBinder(scope).run_turn(sub.run(), name=f"profile {profile} gateway")
        waiter = asyncio.ensure_future(ready.wait())
        try:
            done, _ = await asyncio.wait(
                {task, waiter}, timeout=SERVE_START_S, return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:  # the serve itself was cancelled (the host is stopping)
            sub.request_stop("serve_cancelled")
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(task), STOP_S)
            self.table.unregister(profile, release=True)
            raise
        finally:
            waiter.cancel()
        if task in done or not done:
            if not task.done():
                sub.request_stop("serve_timeout")
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(task, STOP_S)
            self.table.unregister(profile, release=True)
            code = task.result() if task.done() and not task.cancelled() else None
            raise RuntimeError(f"profile {profile}'s gateway did not start (exit {code})")
        self.daemons[profile] = sub
        self._runs[profile] = task
        task.add_done_callback(lambda finished: self._ended(profile, finished))
        log.info("profile %s served by this gateway", profile)

    def _ended(self, profile: str, task: asyncio.Task[int]) -> None:
        """A served profile's daemon ended on its own (its own ``stop`` verb, a fatal start)."""
        if self._runs.get(profile) is not task:
            return
        self._runs.pop(profile, None)
        self.daemons.pop(profile, None)
        self.table.unregister(profile, release=True)
        self.scopes.drop(profile)
        if self._stopping or not self.host.serves(profile):
            return
        log.warning("profile %s's gateway ended on its own; it is no longer served", profile)
        self.launch.spawn(self._forget(profile), name=f"unserve {profile}")

    async def _forget(self, profile: str) -> None:
        with contextlib.suppress(Exception):
            await self.host.unserve(profile)

    async def unserve(self, profile: str, _home: Path) -> None:
        """The host's unserve hook: stop ``profile``'s daemon (drain, then stop)."""
        sub = self.daemons.pop(profile, None)
        task = self._runs.pop(profile, None)
        for key in [k for k in self._identities if k[0] == profile]:
            self._identities.pop(key, None)
        if sub is not None and task is not None and not task.done():
            sub.request_stop("unserved")
            try:
                await asyncio.wait_for(asyncio.shield(task), STOP_S)
            except TimeoutError:
                log.warning("profile %s's gateway did not stop in time; cancelled", profile)
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        self.table.unregister(profile, release=True)
        self.scopes.drop(profile)
        for ingress in self.ingress.values():
            ingress.unmount(profile)

    async def serve_all(self) -> None:
        """Serve every live profile of the root (the host's start; ``rescan-profiles`` again)."""
        try:
            answer = await self.host.rescan()
        except Exception:
            log.exception("the profiles of this root could not be served")
            return
        if answer.get("failed"):
            log.warning("profiles not served: %s", ", ".join(answer["failed"]))

    async def stop_served(self) -> None:
        """Stop every served secondary (the host's shutdown; ``aclose`` drops the record and the
        lock afterwards)."""
        self._stopping = True
        for profile in [p for p in self.served() if p != self.profile]:
            with contextlib.suppress(Exception):
                await self.unserve(profile, Path())

    async def aclose(self) -> None:
        self._stopping = True
        await self.host.aclose()

    # -- intake and delivery ---------------------------------------------------------------------

    def intake(self, receiving: GatewayDaemon, adapter: Adapter) -> InboundSink:
        """The sink of one adapter of ``receiving``: identity first, then the routed profile's
        admission pipeline."""

        async def sink(event: InboundEvent) -> None:
            identity = self.router.identify(event.source, adapter)
            if isinstance(identity, Unresolved):
                self.router.drop_unresolved(
                    event.source,
                    identity,
                    acknowledged=True,
                    notify=lambda source, text: self._notice(receiving, source, text),
                )
                return
            target = self.daemons.get(identity.runtime_profile)
            pipeline = getattr(target, "pipeline", None) if target is not None else None
            if target is None or pipeline is None:
                self.router.drop_unresolved(
                    event.source,
                    Unresolved("unserved_profile", identity.runtime_profile),
                    acknowledged=True,
                )
                return
            self._remember(identity, event.source)
            if target is not receiving:
                event = await run_blocking(
                    lambda: adopt_inbound_media(
                        event, from_home=receiving.scope.home, scope=target.scope
                    ),
                    label="inbound media",
                )
            try:
                key = pipeline.session_key(event.source)
                await run_blocking(self.router.remember, key, identity, label="receiving bot")
            except Exception:
                log.debug("the receiving bot could not be recorded", exc_info=True)
            with use_scope(target.scope):  # the turn it starts runs under the routed profile
                await pipeline.accept(event)

        return sink

    def _notice(self, receiving: GatewayDaemon, source: Any, text: str) -> None:
        replies = getattr(receiving, "replies", None)
        if replies is None:
            return
        receiving.spawn(replies.reply(source, text, kind="notice"), name="multiplex resend notice")

    def _remember(self, identity: RoutingIdentity, source: Source) -> None:
        key = (identity.runtime_profile, _target(source))
        self._identities[key] = identity
        self._identities.move_to_end(key)
        while len(self._identities) > _IDENTITIES_KEPT:
            self._identities.popitem(last=False)

    def reply_sender(
        self, daemon: GatewayDaemon
    ) -> Callable[[Source, str | None], OutboundSender | None]:
        """How ``daemon``'s replies pick their sender: the bot that received the chat
        (``delivery_adapter``), never another one by guess."""
        profile = daemon.scope.profile_id

        def pick(source: Source, session_key: str | None) -> OutboundSender | None:
            platform = str(source.platform).lower()
            identity = self._identities.get((profile, _target(source)))
            if identity is None and session_key:
                identity = self.router.restored_identity(session_key, platform)
            if identity is None:
                return daemon.senders.get(platform)
            adapter = self.router.delivery_adapter(identity)
            if adapter is None:
                return None
            owner = self.table.owner_of(adapter, platform)
            serving = self.daemons.get(owner) if owner is not None else None
            return serving.senders.get(platform) if serving is not None else None

        return pick

    # -- shared ingress --------------------------------------------------------------------------

    def ingress_front(self, name: str, app: Asgi) -> Asgi:
        """The launch profile's listener ``name`` (``api_server`` / ``webhook``) serves this."""
        front = SharedIngress(app)
        self.ingress[name] = front
        return front

    def mount(self, daemon: GatewayDaemon, name: str, app: Asgi, paths: Iterable[str]) -> str:
        """Mount a secondary's surface on the launch listener; its URL prefix. Raises
        ``LookupError`` when the launch profile has no such listener."""
        front = self.ingress.get(name)
        if front is None:
            raise LookupError(
                f"the launch profile ({self.profile}) has no {name} listener to share: enable "
                f"gateway.{name}.enabled there"
            )
        profile = daemon.scope.profile_id
        front.mount(profile, name, app, paths=paths, scope=daemon.scope)
        return f"/p/{profile}/"

    def unmount(self, daemon: GatewayDaemon, name: str | None = None) -> None:
        profile = daemon.scope.profile_id
        for key, front in self.ingress.items():
            if name is None or key == name:
                front.unmount(profile, name)

    # -- idle ------------------------------------------------------------------------------------

    def busy_reason(self) -> str | None:
        """Why a served secondary keeps the host up (its own idle probe), or None."""
        from indus.gateway.daemon.idle_exit import DaemonProbe

        for profile, daemon in list(self.daemons.items()):
            if daemon is self.launch:
                continue
            try:
                why = DaemonProbe(daemon).quick()
            except Exception as exc:
                return f"profile {profile} could not be probed ({type(exc).__name__})"
            if why is not None:
                return f"profile {profile}: {why}"
        return None
