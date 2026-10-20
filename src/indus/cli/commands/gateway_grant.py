"""``indus gateway grant``: a single-use attach grant for a client of the daemon (P17-19).

The TUI renderer (Node) cannot use the owner-only control socket itself, so it runs this helper
and reads one JSON object from stdout:

- success (exit 0): ``{"url", "protocols", "profile_id", "instance_id"}`` where ``protocols`` is
  ``["indus-gateway-v1", "indus-gateway-ticket.<ticket>"]``. The ticket appears there and nowhere
  else: not in ``url``, not on stderr, not in any log line, never on disk;
- failure: ``{"error": {"verdict", "code", "message", "hint"?}}``. ``verdict`` is
  ``gateway_unavailable`` (exit 2: the daemon cannot be used from here at all — the client shows
  the hint and stops asking) or ``retry`` (exit 1: the client backs off and asks again).

``--mode`` says how hard to try: ``start`` (default; start the daemon when none answers, 30 s),
``recover`` (the same, for a client's budgeted re-ensure after it lost its socket) and
``discover`` (only a daemon that already answers, 5 s). Starting goes through P17-15's
``service.gateway_ensure.ensure`` in the same mode; its verdict keeps its terminal flag
(``Ensured.terminal``: a stopped-on-purpose daemon met by ``recover``, an unsafe home, no control
socket). Launch-option env vars (``INDUS_TUI_*``, ``INDUS_MODEL``, ``INDUS_YOLO_MODE``, …) are
dropped before anything that may start a daemon, because they belong to a session, not to the
daemon. Then the control socket mints the ticket for the daemon's profile and instance
(``remote.channel.grant_for``: ``describe`` then ``session-ticket``), and the answer is checked
(loopback URL, same ids as the ensured endpoint).

Without ``--json`` nothing is minted: the command says whether a daemon endpoint is ready.
This module never imports ``indus.gateway`` (layer rule D-16.246).
"""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

import click

from indus.cli.commands._support import state_of
from indus.foundation.scope import EnvView, Scope
from indus.service import gateway_control
from indus.service.creation_policy import LAUNCH_ENV, LAUNCH_ONLY_ENV
from indus.service.creation_policy import scrub_launch_env as scrub_policy_env
from indus.service.remote.channel import DialRefused, grant_for
from indus.service.remote.channel import Grant as ChannelGrant
from indus.service.ws_auth import (
    SUBPROTOCOL,
    TICKET_PROTOCOL_PREFIX,
    WS_INTERACTIVE,
    stable_profile_id,
)

__all__ = [
    "EXIT_OK",
    "EXIT_RETRY",
    "EXIT_TERMINAL",
    "LAUNCH_ENV_NAMES",
    "MODE_TIMEOUTS",
    "TERMINAL_CODES",
    "EnsureFn",
    "Grant",
    "MintFn",
    "GrantRefused",
    "grant_command",
    "obtain_grant",
    "scrub_launch_env",
]

type Mode = Literal["start", "discover", "recover"]
type EnsureFn = Callable[..., Mapping[str, Any]]
"""``ensure(scope, *, mode: Mode, timeout: float) -> descriptor`` (``ws_url``, ``profile_id``,
``instance_id``, ``code_version``); raises ``GrantRefused`` (or anything, see
``_from_ensure_error``) on failure."""
type MintFn = Callable[[Path], ChannelGrant]
"""``grant_for(home)``: a fresh ticket from the control socket; raises ``DialRefused``."""

EXIT_OK: Final = 0
EXIT_RETRY: Final = 1
EXIT_TERMINAL: Final = 2

MODE_TIMEOUTS: Final[Mapping[str, float]] = {"start": 30.0, "recover": 30.0, "discover": 5.0}
"""Seconds to wait for a daemon per mode (research 05 §3.30: ensure 30 s, discover 5 s)."""

TERMINAL_CODES: Final = frozenset(
    {
        "ws_unavailable",  # the daemon runs without a WebSocket endpoint
        "unsafe_home",  # others can write to the profile home: no socket is trusted there
        "forbidden",  # the OS cannot confirm the peer is this user
        "profile_mismatch",  # another profile answered
        "bad_purpose",  # the daemon does not mint this kind of ticket
        "unsupported",  # no control socket on this OS
    }
)
"""Refusals no retry can fix (verdict ``gateway_unavailable``); every other code is ``retry``."""

LAUNCH_ENV_NAMES: Final = frozenset(LAUNCH_ENV) | LAUNCH_ONLY_ENV
"""Launch options of one run (``creation_policy``'s lists, plus every ``INDUS_TUI_*``); a daemon
started on its behalf must not inherit them."""

_HINTS: Final[Mapping[str, str]] = {
    "ws_unavailable": "The daemon has no attach endpoint; restart it with `indus gateway "
    "restart`, or run `indus --tui --local`.",
    "unsafe_home": "The profile folder is writable by other users; fix its permissions "
    "(chmod go-w), then try again.",
    "forbidden": "This system cannot confirm who is asking the daemon; run `indus --tui --local`.",
    "profile_mismatch": "Another profile's daemon answered; check INDUS_HOME.",
    "unsupported": "Attaching needs the daemon's control socket, which this system lacks; run "
    "`indus --tui --local`.",
    "planned_stop": "The daemon was stopped on purpose; start it with `indus gateway start`, or "
    "run `indus --tui --local`.",
}


@dataclass(frozen=True)
class Grant:
    """What a client dials with. ``ticket`` is the secret: hidden from ``repr``."""

    url: str
    ticket: str = field(repr=False)
    profile_id: str
    instance_id: str

    @property
    def protocols(self) -> list[str]:
        return [SUBPROTOCOL, TICKET_PROTOCOL_PREFIX + self.ticket]

    def to_wire(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "protocols": self.protocols,
            "profile_id": self.profile_id,
            "instance_id": self.instance_id,
        }


class GrantRefused(Exception):
    """No grant; ``code`` is a short code, ``terminal`` says whether asking again can help."""

    def __init__(self, code: str, message: str, *, terminal: bool | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.terminal = code in TERMINAL_CODES if terminal is None else terminal

    @property
    def verdict(self) -> str:
        return "gateway_unavailable" if self.terminal else "retry"

    @property
    def hint(self) -> str | None:
        return _HINTS.get(self.code)

    def to_wire(self) -> dict[str, Any]:
        body: dict[str, Any] = {"verdict": self.verdict, "code": self.code, "message": self.message}
        if self.hint:
            body["hint"] = self.hint
        return {"error": body}


def scrub_launch_env(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` without the launch options of one run (``creation_policy.scrub_launch_env``, the
    same scrub ``ensure`` applies to a daemon it starts)."""
    return scrub_policy_env(env)


# -- reaching the daemon --------------------------------------------------------------------------


def ensure_endpoint(scope: Scope, *, mode: Mode, timeout: float) -> Mapping[str, Any]:
    """P17-15's ensure in ``mode``: the endpoint descriptor, or ``GrantRefused`` carrying the
    verdict's reason and its terminal flag (``Ensured.terminal``)."""
    from indus.service.gateway_ensure import ensure

    found = ensure(scope.home, scope.env.to_dict(), mode=mode, timeout=timeout)
    if not found.attachable or found.endpoint is None:
        message = (found.detail or "").splitlines()[0] if found.detail else ""
        raise GrantRefused(
            found.reason or found.state,
            message or "the daemon could not be reached",
            terminal=found.terminal,
        )
    return found.endpoint.to_json()


def _from_ensure_error(exc: Exception) -> GrantRefused:
    """Any failure ensure raises: its ``code`` / ``terminal`` when it has them, else retry."""
    code = getattr(exc, "code", None)
    terminal = getattr(exc, "terminal", None)
    message = str(exc) or "the daemon could not be reached"
    return GrantRefused(
        code if isinstance(code, str) and code else "ensure_failed",
        message,
        terminal=terminal if isinstance(terminal, bool) else None,
    )


def _descriptor(scope: Scope, mode: Mode, timeout: float, ensure: EnsureFn) -> dict[str, Any]:
    try:
        found = ensure(scope, mode=mode, timeout=timeout)
    except GrantRefused:
        raise
    except Exception as exc:
        raise _from_ensure_error(exc) from None
    return dict(found)


def _mint(home: Path, timeout: float) -> ChannelGrant:
    return grant_for(home, purpose=WS_INTERACTIVE, timeout=timeout)


def obtain_grant(
    scope: Scope,
    *,
    mode: Mode = "start",
    timeout: float | None = None,
    ensure: EnsureFn | None = None,
    mint: MintFn | None = None,
    platform: str = sys.platform,
) -> Grant:
    """A grant for the daemon serving ``scope.home`` (see the module notes); raises
    ``GrantRefused``. ``ensure`` defaults to P17-15's (``ensure_endpoint``), ``mint`` to
    ``grant_for``; ``scope`` should already be scrubbed."""
    if platform == "win32":
        raise GrantRefused("unsupported", "the daemon's control socket is POSIX only")
    if not gateway_control.home_is_safe(scope.home):
        raise GrantRefused("unsafe_home", "the profile home is writable by other users")
    wait = MODE_TIMEOUTS[mode] if timeout is None else timeout
    found = _descriptor(scope, mode, wait, ensure if ensure is not None else ensure_endpoint)
    profile_id, instance_id = found.get("profile_id"), found.get("instance_id")
    if not isinstance(profile_id, str) or not isinstance(instance_id, str):
        raise GrantRefused("bad_response", "the daemon did not name its profile and instance")
    if profile_id != stable_profile_id(scope.home):
        raise GrantRefused("profile_mismatch", "the daemon that answered serves another profile")
    if not found.get("ws_url"):
        raise GrantRefused("ws_unavailable", "the daemon has no attach endpoint")
    try:
        minted = (mint or (lambda home: _mint(home, min(wait, 5.0))))(scope.home)
    except DialRefused as refused:
        raise GrantRefused(
            refused.reason,
            refused.detail or "no ticket was minted",
            terminal=True if refused.terminal else None,
        ) from None
    if (minted.profile_id, minted.instance_id) != (profile_id, instance_id):
        # The daemon restarted between ensure and the ticket: ask again.
        raise GrantRefused("stale_instance", "the daemon restarted while the grant was made")
    return Grant(minted.ws_url, minted.ticket, minted.profile_id, minted.instance_id)


# -- the command ----------------------------------------------------------------------------------


@click.command(
    "grant",
    help="Print a single-use attach grant for a client of the daemon (used by the TUI).",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
)
@click.option("--json", "as_json", is_flag=True, help="Mint a grant and print it as JSON.")
@click.option(
    "--mode",
    type=click.Choice(["start", "discover", "recover"]),
    default="start",
    show_default=True,
    help="start: start the daemon when none answers; discover: only a running one; "
    "recover: re-ensure after a lost connection.",
)
@click.option("--timeout", "timeout_s", type=float, default=None, help="Seconds to wait.")
@click.pass_context
def grant_command(ctx: click.Context, as_json: bool, mode: Mode, timeout_s: float | None) -> int:
    base = state_of(ctx).scope
    scope = base.with_env(EnvView(scrub_launch_env(base.env.to_dict())))
    if not as_json:
        return _describe_only(scope)
    try:
        # Only the one JSON line may reach stdout: anything the daemon start prints goes to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            grant = obtain_grant(scope, mode=mode, timeout=timeout_s)
    except GrantRefused as refused:
        click.echo(json.dumps(refused.to_wire()))
        return EXIT_TERMINAL if refused.terminal else EXIT_RETRY
    click.echo(json.dumps(grant.to_wire()))
    return EXIT_OK


def _describe_only(scope: Scope) -> int:
    described = gateway_control.describe(scope.home)
    if described is None or not described.get("ws_url"):
        click.echo(
            "No daemon attach endpoint is ready for this profile. "
            "`indus gateway grant --json` starts one and prints a grant.",
            err=True,
        )
        return EXIT_RETRY
    click.echo(f"The daemon's attach endpoint is ready: {described['ws_url']}")
    click.echo("`indus gateway grant --json` prints a single-use grant for a client.")
    return EXIT_OK
