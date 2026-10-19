"""``indus pairing``: see and answer pairing requests from messaging platforms (P15-09).

``list`` (the default) shows pending requests — platform, request id, user, name, age; never a
code — and the users already let in. ``approve PLATFORM CODE|REQUEST-ID`` lets a user in: the code
the bot sent them, or the request id from ``list`` (an id is never refused by the wrong-code
lockout and never counts toward it). ``revoke PLATFORM USER`` takes access away again.
``clear-pending [PLATFORM]`` drops every open request. The store is the profile's
(``-p NAME`` picks another profile), the same one the gateway writes.

Exit codes: 0 done, 1 nothing matched (unknown code, user, or a locked-out platform).
"""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from indus.cli.args import CliState
    from indus.state.pairing import PairingStore

__all__ = ["pairing_group"]

_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 100}
_ALIASES = {"ls": "list"}


class _PairingGroup(click.Group):
    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        return super().get_command(ctx, _ALIASES.get(cmd_name, cmd_name))


def _state(ctx: click.Context) -> CliState:
    from indus.cli.args import CliState

    state = ctx.find_object(CliState)
    if state is None:
        raise click.UsageError("pairing was started without its process state")
    return state


def _store(ctx: click.Context) -> PairingStore:
    from indus.state.pairing import PairingStore

    return PairingStore(_state(ctx).scope)


def _age(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h"


def _show(store: PairingStore, as_json: bool) -> int:
    pending = store.list_pending()
    approved = store.list_approved()
    if as_json:
        payload = {
            "pending": [
                {
                    "platform": p.platform,
                    "request_id": p.request_id,
                    "user_id": p.user_id,
                    "user_name": p.user_name,
                    "age_seconds": int(p.age_seconds),
                }
                for p in pending
            ],
            "approved": [
                {"platform": a.platform, "user_id": a.user_id, "user_name": a.user_name}
                for a in approved
            ],
        }
        click.echo(json.dumps(payload, indent=2))
        return 0
    if not pending and not approved:
        click.echo("Nobody has asked to pair yet, and nobody is paired.")
        return 0
    if pending:
        click.echo(f"Waiting for approval ({len(pending)}):")
        click.echo(f"  {'PLATFORM':<12} {'REQUEST ID':<18} {'USER':<22} {'NAME':<20} AGE")
        for p in pending:
            click.echo(
                f"  {p.platform:<12} {p.request_id:<18} {p.user_id:<22} "
                f"{p.user_name[:20]:<20} {_age(p.age_seconds)}"
            )
        click.echo("Let one in with: indus pairing approve <platform> <request id or code>")
    else:
        click.echo("No requests are waiting.")
    if approved:
        click.echo(f"Paired users ({len(approved)}):")
        click.echo(f"  {'PLATFORM':<12} {'USER':<22} NAME")
        for a in approved:
            click.echo(f"  {a.platform:<12} {a.user_id:<22} {a.user_name}")
    return 0


@click.group(
    "pairing",
    cls=_PairingGroup,
    invoke_without_command=True,
    help="Approve or revoke people who asked to use the bot from a chat platform.",
    context_settings=_SETTINGS,
)
@click.pass_context
def pairing_group(ctx: click.Context) -> int:
    if ctx.invoked_subcommand is None:
        return _show(_store(ctx), as_json=False)
    return 0


@pairing_group.command("list", help="Show waiting requests and paired users (alias ls).")
@click.option("--json", "as_json", is_flag=True, help="Print JSON for scripts.")
@click.pass_context
def list_command(ctx: click.Context, as_json: bool) -> int:
    return _show(_store(ctx), as_json)


def _usage(message: str) -> click.UsageError:
    return click.UsageError(message)


@pairing_group.command("approve", help="Let a user in by the code they got or the request id.")
@click.argument("platform")
@click.argument("code_or_request_id", nargs=-1, required=True)
@click.pass_context
def approve_command(ctx: click.Context, platform: str, code_or_request_id: tuple[str, ...]) -> int:
    value = " ".join(code_or_request_id)  # people paste codes with spaces in them
    store = _store(ctx)
    try:
        result = store.approve(platform, value)
    except ValueError as exc:
        raise _usage(str(exc)) from None
    name = platform.strip().lower()
    if result.approved:
        who = f"{result.user_name} ({result.user_id})" if result.user_name else result.user_id
        click.echo(f"Paired {who} on {name}. Their next message reaches the agent.")
        return 0
    if result.status == "locked_out":
        minutes = max(1, math.ceil(result.lockout_seconds / 60))
        click.echo(
            f"Approvals by code on {name} are paused for about {minutes} more minute(s) after "
            "too many wrong codes. Approve by request id instead (see `indus pairing list`).",
            err=True,
        )
        return 1
    click.echo(
        f"No waiting request on {name} matches that code or id (it may have expired after an "
        "hour). See `indus pairing list`.",
        err=True,
    )
    return 1


@pairing_group.command("revoke", help="Take a paired user's access away.")
@click.argument("platform")
@click.argument("user_id")
@click.pass_context
def revoke_command(ctx: click.Context, platform: str, user_id: str) -> int:
    try:
        removed = _store(ctx).revoke(platform, user_id)
    except ValueError as exc:
        raise _usage(str(exc)) from None
    name = platform.strip().lower()
    if removed:
        click.echo(f"Revoked {user_id} on {name}.")
        return 0
    click.echo(f"{user_id} is not paired on {name}.", err=True)
    return 1


@pairing_group.command("clear-pending", help="Drop every waiting request (or one platform's).")
@click.argument("platform", required=False)
@click.pass_context
def clear_pending_command(ctx: click.Context, platform: str | None) -> int:
    try:
        count = _store(ctx).clear_pending(platform)
    except ValueError as exc:
        raise _usage(str(exc)) from None
    click.echo(f"Dropped {count} waiting request(s)." if count else "No requests were waiting.")
    return 0
