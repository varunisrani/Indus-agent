"""``indus peer``: register other Indus gateways and message their bots (P24-07).

``add NAME --url URL [--lan] [--note TEXT] [--key-stdin]`` registers (or updates) a peer: its API
server's base URL. The key (the peer's API server key) is never an argument: ``--key-stdin`` reads
it from stdin, a terminal is asked for it (hidden), and otherwise it can be stored later or given
headless as ``INDUS_PEER_<NAME>_KEY`` in the profile's environment. It lives in the profile's
secret store. ``--lan`` marks a peer on your own network: its private or loopback address is then
allowed (without it, the SSRF policy applies to every request).

``list`` (the default) shows the peers and whether each has a key; ``remove NAME`` forgets a peer
and its stored key. ``dm TARGET MESSAGE`` runs one turn in the target bot's canonical chat on the
peer and prints the reply (``TARGET`` is ``peer`` or ``peer/profile``; ``MESSAGE`` ``-`` reads
stdin). ``run TARGET MESSAGE`` starts a long turn and prints its run id; ``status`` / ``stop TARGET
RUN_ID`` follow or stop it.

Exit codes: 0 done, 1 a peer or delivery problem (the message says what to do), 2 usage.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:
    from indus.foundation.scope import Scope

__all__ = ["peer_group"]

_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 100}
_ALIASES = {"ls": "list", "rm": "remove", "set": "add"}


class _PeerGroup(click.Group):
    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        return super().get_command(ctx, _ALIASES.get(cmd_name, cmd_name))


def _scope() -> Scope:
    from indus.cli.cmdkit import cli_state

    return cli_state().scope


def _interactive() -> bool:
    from indus.cli.cmdkit import cli_state

    state = cli_state()
    return state.stdin_isatty() and state.stdout_isatty()


def _fail(message: str, code: int = 1) -> None:
    click.echo(f"peer: {message}", err=True)
    raise click.exceptions.Exit(code)


def _call[T](make: Callable[[], Coroutine[Any, Any, T]]) -> T:
    from indus.service.peers import PeerError

    try:
        return asyncio.run(make())
    except PeerError as exc:
        hint = " The message is already there: do not resend." if exc.accepted else ""
        _fail(f"{exc.message} [reason: {exc.reason}]{hint}")
        raise AssertionError("unreachable") from None


def _message(text: str) -> str:
    body = sys.stdin.read() if text == "-" else text
    if not body.strip():
        _fail("the message is empty", 2)
    return body


@click.group("peer", cls=_PeerGroup, context_settings=_SETTINGS, invoke_without_command=True)
@click.pass_context
def peer_group(ctx: click.Context) -> None:
    """Register peer gateways and message their bots."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(list_peers)


@peer_group.command("add", context_settings=_SETTINGS)
@click.argument("name")
@click.option("--url", required=True, help="The peer's API server base URL.")
@click.option("--lan", is_flag=True, help="The peer is on your own network (private address).")
@click.option("--note", default="", help="A note shown by `list`.")
@click.option("--key-stdin", is_flag=True, help="Read the peer's API key from stdin.")
def add(name: str, url: str, lan: bool, note: str, key_stdin: bool) -> None:
    """Register (or update) a peer gateway."""
    from indus.cli.commands._support import secret_store
    from indus.service.peers import Peer, PeerBook, PeerError, key_name

    scope = _scope()
    peer_name = name.strip().lower()
    try:
        new = PeerBook(scope).put(Peer(peer_name, url, lan, note))
    except PeerError as exc:
        _fail(exc.message, 2)
        return
    key = ""
    if key_stdin:
        key = sys.stdin.readline().strip()
        if not key:
            _fail("--key-stdin was given but stdin held no key", 2)
    elif _interactive():
        key = click.prompt(
            "API key of the peer (empty: set it later)",
            default="",
            hide_input=True,
            show_default=False,
        ).strip()
    verb = "Added" if new else "Updated"
    if key:
        secret_store(scope).set(key_name(peer_name), key)
        click.echo(f"{verb} peer {peer_name!r}; its key is in the secret store.")
        return
    click.echo(
        f"{verb} peer {peer_name!r}. It has no key yet: run `indus peer add {peer_name} --url "
        f"URL --key-stdin < keyfile`, or set {key_name(peer_name)} for headless use."
    )


@peer_group.command("list", context_settings=_SETTINGS)
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
def list_peers(as_json: bool) -> None:
    """List the registered peers."""
    from indus.service.peers import PeerBook, key_name, peer_key

    scope = _scope()
    peers = PeerBook(scope).load()
    rows = [
        {**peer.to_dict(), "name": name, "has_key": peer_key(scope, name) is not None}
        for name, peer in sorted(peers.items())
    ]
    if as_json:
        click.echo(json.dumps({"peers": rows}, indent=2))
        return
    if not rows:
        click.echo("No peers yet. Add one: indus peer add NAME --url http://host:8642 --key-stdin")
        return
    for row in rows:
        key = "key set" if row["has_key"] else f"no key ({key_name(str(row['name']))})"
        lan = ", lan" if row["lan"] else ""
        note = f" — {row['note']}" if row.get("note") else ""
        click.echo(f"{row['name']}\t{row['url']}\t[{key}{lan}]{note}")


@peer_group.command("remove", context_settings=_SETTINGS)
@click.argument("name")
def remove(name: str) -> None:
    """Forget a peer and its stored key."""
    from indus.cli.commands._support import secret_store
    from indus.service.peers import PeerBook, key_name

    scope = _scope()
    peer_name = name.strip().lower()
    if not PeerBook(scope).remove(peer_name):
        _fail(f"no peer is named {peer_name!r}")
    secret_store(scope).delete(key_name(peer_name))
    click.echo(f"Removed peer {peer_name!r} and its stored key.")


def _client_and_target(target: str) -> tuple[Any, Any]:
    from indus.service.peers import PeerClient, PeerError, parse_target

    try:
        parsed = parse_target(target)
    except PeerError as exc:
        _fail(exc.message, 2)
        raise AssertionError("unreachable") from None
    return PeerClient(_scope()), parsed


@peer_group.command("dm", context_settings=_SETTINGS)
@click.argument("target")
@click.argument("message")
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
def dm(target: str, message: str, as_json: bool) -> None:
    """Message the bot TARGET (peer or peer/profile) and print its reply ('-' reads stdin)."""
    text = _message(message)
    client, parsed = _client_and_target(target)
    sender = _scope().profile_id
    reply = _call(lambda: client.dm(parsed, text, author=sender))
    if as_json:
        click.echo(json.dumps({"target": parsed.render(), "session_id": reply.session_id,
                               "reply": reply.reply}))  # fmt: skip
        return
    click.echo(reply.reply or "(no reply)")


@peer_group.command("run", context_settings=_SETTINGS)
@click.argument("target")
@click.argument("message")
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
def run(target: str, message: str, as_json: bool) -> None:
    """Start a long turn for the bot TARGET; prints its run id at once."""
    text = _message(message)
    client, parsed = _client_and_target(target)
    started = _call(lambda: client.run(parsed, text))
    if as_json:
        click.echo(json.dumps({"target": parsed.render(), **started}))
        return
    click.echo(f"{started['run_id']}: {started['status']} (session {started['session_id']})")


def _control(action: str, target: str, run_id: str, as_json: bool) -> None:
    client, parsed = _client_and_target(target)
    call = client.stop if action == "stop" else client.status
    result: dict[str, Any] = _call(lambda: call(parsed, run_id))
    if as_json:
        click.echo(json.dumps({"target": parsed.render(), **result}))
        return
    click.echo(f"{run_id}: {result.get('status', 'unknown')}")
    output = result.get("output")
    if action == "status" and isinstance(output, str) and output:
        click.echo(output)


@peer_group.command("status", context_settings=_SETTINGS)
@click.argument("target")
@click.argument("run_id")
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
def status(target: str, run_id: str, as_json: bool) -> None:
    """Show a peer run's status (and its output once it finished)."""
    _control("status", target, run_id, as_json)


@peer_group.command("stop", context_settings=_SETTINGS)
@click.argument("target")
@click.argument("run_id")
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
def stop(target: str, run_id: str, as_json: bool) -> None:
    """Stop one peer run (no other turn is touched)."""
    _control("stop", target, run_id, as_json)
