"""``indus send``: send a message to a chat from a script, a CI job or a terminal (P15-20).

``indus send TARGET [TEXT] [--file PATH] [--subject LINE]`` parses TARGET with the cron target
grammar (P14: ``telegram`` for the home channel, ``telegram:<chat>[:<thread>]``,
``discord:#name``, ``all``) and sends through the delivery router, with adapters built for this
send from the platform registry and the profile's gateway settings; no running gateway and no
model are involved. A platform a plugin offers (P13 provider slot ``platform``) is found by loading
the profile's plugins for the send, only when TARGET names no built-in platform; adapters get the
same ``AdapterConfig`` the daemon gives them, marked ``send_only`` (no listener, no update
consumption, so a running gateway loses nothing). The body is TEXT, else whatever is piped on stdin.

Files are attached only with ``--file``, and only when the gateway's media path policy (P15-18)
allows the file: credential and system locations are refused, symlinks are resolved first, and
the file has to sit in a folder media may be sent from (the gateway caches or
``gateway.media_allow_dirs``). ``MEDIA:`` tags in the body are sent as text, never followed: the
body may come from a log or another program's output.

``--list [PLATFORM]`` prints the channel directory the gateway saved (chats that names resolve
to). Exit codes: 0 sent, 1 the platform refused or failed, 2 a usage problem (no target, an
unknown or disabled platform, a refused file, no text).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, cast

import click

if TYPE_CHECKING:
    from indus.cron.delivery import DeliveryReceipt
    from indus.cron.targets import DeliveryTarget
    from indus.foundation.scope import Scope
    from indus.gateway.adapters.base import Adapter
    from indus.gateway.config import GatewaySettings, PlatformSettings
    from indus.gateway.outbound.media_policy import MediaItem
    from indus.gateway.registry import PlatformRegistry

__all__ = ["ADAPTER_BUILDER", "build_adapter", "send_command"]

_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 100}
_NOT_TARGETS = frozenset({"api_server", "webhook"})
_LABEL = "a message sent with `indus send`"

OK, FAILED, USAGE = 0, 1, 2


def build_adapter(
    scope: Scope,
    name: str,
    settings: PlatformSettings,
    registry: PlatformRegistry | None = None,
) -> Adapter | None:
    """The platform's adapter from the registry (None and a log line when it cannot be built)."""
    from indus.gateway.adapters.base import Adapter as _Adapter
    from indus.gateway.config import AdapterConfig, gateway_section
    from indus.gateway.registry import PlatformRegistry, proxy_for

    proxy = proxy_for(
        env=scope.env,
        trust_env=bool(gateway_section(scope).trust_env),
        platform_proxy=str(settings.extra.get("proxy") or "") or None,
    )
    config = AdapterConfig(name, settings, scope, scope.clock, proxy, send_only=True)
    built = (registry or PlatformRegistry()).create_adapter(name, config)
    return built if isinstance(built, _Adapter) else None


ADAPTER_BUILDER: Callable[..., Adapter | None] = build_adapter
"""How ``indus send`` gets an adapter: ``(scope, name, settings, registry)`` (tests put a fake
platform here)."""


async def _plugin_platforms(scope: Scope) -> dict[str, Any]:
    """The platform entries the profile's plugins offer (loaded for this send, then closed)."""
    from indus.service.plugin_host import PluginUpkeep
    from indus.tools.registry import ToolRegistry

    upkeep = PluginUpkeep(scope, ToolRegistry(clock=scope.clock), surface="cli")
    try:
        await upkeep.start()
        host = upkeep.host
        return dict(host.providers.get("platform") or {}) if host is not None else {}
    finally:
        await upkeep.shutdown()


def _with_plugins(scope: Scope, raw: str) -> PlatformRegistry | None:
    """A registry with the plugin platforms when ``raw`` names a platform no built-in serves
    (their names become known to the target grammar); None when nothing is to be added."""
    from indus.cron import targets
    from indus.gateway.registry import PlatformRegistry

    registry = PlatformRegistry()
    head = raw.strip().split(":", 1)[0].strip().lower()
    if not head or head in targets.RESERVED or registry.get(head) is not None:
        return None
    try:
        offered = asyncio.run(_plugin_platforms(scope))
    except Exception as exc:
        click.echo(f"indus send: plugins could not be loaded ({type(exc).__name__})", err=True)
        return None
    if not registry.load_plugin_entries({"platform": offered}):
        return None
    for name in registry.names():
        with contextlib.suppress(ValueError):
            targets.register_platform(name)
    return registry


class _ProcessState(Protocol):
    """What ``indus send`` reads from the CLI's process state (``ctx.obj``); the CLI package is
    not imported here (layer rule D-16.246)."""

    @property
    def scope(self) -> Scope: ...

    def stdin_isatty(self) -> bool: ...


def _state(ctx: click.Context) -> _ProcessState:
    from indus.foundation.scope import Scope as _Scope

    state: Any = ctx.obj
    if not isinstance(getattr(state, "scope", None), _Scope):
        raise click.UsageError("send was started without its process state")
    return cast(_ProcessState, state)


def _fail(message: str, code: int = USAGE) -> int:
    click.echo(f"indus send: {message}", err=True)
    return code


def _body(text: str | None, state: _ProcessState) -> str | None:
    if text:
        return text
    if state.stdin_isatty():
        return None
    try:
        piped = sys.stdin.read()
    except (OSError, ValueError):
        return None
    return piped.rstrip() or None  # a pipe's final newline is not part of the message


def _one_target(raw: str) -> DeliveryTarget | str:
    from indus.cron import targets

    parsed = targets.parse_targets(raw)
    if parsed.problems:
        return "; ".join(p.message for p in parsed.problems)
    if len(parsed.targets) != 1:
        return "give exactly one target"
    target = parsed.targets[0]
    if target.is_local or target.is_origin:
        return f"{target.render()} is not a chat: use a platform, platform:chat or all"
    return target


def _platforms(target: DeliveryTarget, settings: GatewaySettings) -> list[str] | str:
    if target.is_broadcast:
        names = [n for n in settings.enabled() if n not in _NOT_TARGETS]
        return names or "no messaging platform is set up for this profile"
    entry = settings.get(target.platform)
    if entry is None or not entry.enabled:
        why = entry.reason if entry is not None else "not configured"
        return f"{target.platform} is not enabled for this profile ({why})"
    if target.platform in _NOT_TARGETS:
        return f"{target.platform} cannot receive messages"
    return [target.platform]


def _attachment(scope: Scope, raw: str) -> MediaItem | str:
    from indus.gateway.outbound.media_policy import MediaItem, MediaPolicy, MediaRef

    path = os.path.abspath(os.path.expanduser(raw))
    verdict = MediaPolicy.for_scope(scope).check(path)
    if verdict.path is None:
        hint = ""
        if verdict.reason.startswith("not in a folder"):
            hint = " (add its folder to gateway.media_allow_dirs to allow it)"
        return f"cannot attach {raw}: {verdict.reason}{hint}"
    ref = MediaRef(verdict.path)
    return MediaItem(verdict.path, ref.kind, ref.name)


async def _deliver(
    scope: Scope,
    settings: GatewaySettings,
    names: list[str],
    target: DeliveryTarget,
    text: str,
    files: tuple[MediaItem, ...],
    platforms: PlatformRegistry | None = None,
) -> DeliveryReceipt | str:
    from indus.cron.delivery import DeliveryReceipt
    from indus.gateway.adapters.base import AdapterConnectError
    from indus.gateway.delivery.directory import ChannelDirectory
    from indus.gateway.delivery.mirror import Mirror
    from indus.gateway.delivery.router import DeliveryRouter
    from indus.gateway.outbound.dead_targets import DeadTargets
    from indus.gateway.outbound.sender import OutboundSender
    from indus.state import registry

    def home(name: str) -> Any:
        entry = settings.get(name)
        return entry.home_channel if entry is not None else None

    adapters: dict[str, Adapter] = {}
    with registry.shared(scope) as store:
        router = DeliveryRouter(
            scope, homes=home, directory=ChannelDirectory(scope).load(), mirror=Mirror(store)
        )
        dead = DeadTargets.for_scope(scope)
        try:
            for name in names:
                entry = settings.get(name)
                assert entry is not None
                adapter = ADAPTER_BUILDER(scope, name, entry, platforms)
                if adapter is None:
                    hint = ""
                    from indus.gateway.registry import PlatformRegistry

                    missing = (platforms or PlatformRegistry()).missing_deps(name)
                    if missing:
                        hint = f": {missing}"
                    return f"the {name} adapter could not be built{hint}"
                try:
                    await adapter.connect()
                except AdapterConnectError as exc:
                    return f"could not connect to {name}: {exc}"
                adapters[name] = adapter
                sender = OutboundSender(
                    adapter,
                    scope=scope,
                    platform=name,
                    dead_targets=dead,
                    reply_to_mode=entry.reply_to_mode,  # pyright: ignore[reportArgumentType]
                )
                router.attach(name, sender)
            receipt = await router.send(target, text, files=files, label=_LABEL)
        finally:
            router.close()
            for adapter in adapters.values():
                await adapter.disconnect()
    return receipt if isinstance(receipt, DeliveryReceipt) else DeliveryReceipt(ok=False)


def _list(scope: Scope, platform: str | None, as_json: bool) -> int:
    from indus.gateway.config import load_gateway_settings
    from indus.gateway.delivery.directory import ChannelDirectory

    directory = ChannelDirectory(scope).load()
    names = set(directory.platforms())
    with contextlib.suppress(Exception):  # the saved directory alone is still worth showing
        names |= {n for n in load_gateway_settings(scope).enabled() if n not in _NOT_TARGETS}
    if platform:
        wanted = platform.strip().lower()
        if wanted not in names:
            known = ", ".join(sorted(names)) or "none"
            return _fail(f"nothing known for {platform} (known: {known})", FAILED)
        names = {wanted}
    if as_json:
        data = directory.as_json()
        data["platforms"] = {n: data["platforms"].get(n, []) for n in sorted(names)}
        click.echo(json.dumps(data, indent=2))
        return OK
    click.echo(directory.render(sorted(names)))
    return OK


@click.command(
    "send",
    context_settings=_SETTINGS,
    help="Send a message to a chat (home channel, platform:chat, #name or all).",
)
@click.argument("target", required=False)
@click.argument("text", required=False)
@click.option("-f", "--file", "file_path", metavar="PATH", help="Attach this file.")
@click.option("-s", "--subject", metavar="LINE", help="Put this line above the message.")
@click.option("-l", "--list", "list_targets", is_flag=True, help="List known chats instead.")
@click.option("--json", "as_json", is_flag=True, help="Print the result as JSON.")
@click.option("-q", "--quiet", is_flag=True, help="Print nothing when it worked.")
@click.pass_context
def send_command(
    ctx: click.Context,
    target: str | None,
    text: str | None,
    file_path: str | None,
    subject: str | None,
    list_targets: bool,
    as_json: bool,
    quiet: bool,
) -> int:
    state = _state(ctx)
    scope = state.scope
    if list_targets:
        return _list(scope, target, as_json)
    if not target:
        return _fail(
            'a target is required, e.g. `indus send telegram "deploy done"`, '
            "`indus send discord:#ops --file report.pdf`, or `indus send --list`"
        )
    registry = _with_plugins(scope, target)
    parsed = _one_target(target)
    if isinstance(parsed, str):
        return _fail(parsed)
    from indus.gateway.config import load_gateway_settings

    settings = load_gateway_settings(scope)
    names = _platforms(parsed, settings)
    if isinstance(names, str):
        return _fail(names)
    files: tuple[MediaItem, ...] = ()
    if file_path:
        item = _attachment(scope, file_path)
        if isinstance(item, str):
            return _fail(item)
        files = (item,)
    body = _body(text, state) or ""
    if subject:
        body = f"{subject.strip()}\n\n{body.lstrip()}" if body.strip() else subject.strip()
    if not body.strip() and not files:
        return _fail("no message: give the text as an argument or pipe it on stdin")
    outcome = asyncio.run(_deliver(scope, settings, names, parsed, body, files, registry))
    if isinstance(outcome, str):
        return _fail(outcome, FAILED)
    where = parsed.render()
    if as_json:
        click.echo(
            json.dumps(
                {
                    "ok": outcome.ok,
                    "target": where,
                    "message_id": outcome.message_id,
                    "error": outcome.error,
                },
                indent=2,
            )
        )
        return OK if outcome.ok else FAILED
    if not outcome.ok:
        return _fail(f"not sent to {where}: {outcome.error or 'the platform refused it'}", FAILED)
    if not quiet:
        click.echo(f"Sent to {where}.")
    return OK
