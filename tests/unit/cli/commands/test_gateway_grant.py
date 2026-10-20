"""P17-19: ``indus gateway grant`` — the attach grant the TUI renderer runs.

The daemon's half is real: a ``ControlServer`` with the P17-11 attach verbs on a ticket book, in
the scope's profile home. Most tests replace P17-15's ensure (it would start a daemon process) with
``Discover``, a recording double that only asks ``describe``; the real ``ensure_endpoint`` runs in
discover mode against the same control socket, and its verdicts are mapped with ``ensure`` faked.
Tickets are minted by the real ``grant_for`` unless a test refuses them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from indus.cli.commands import gateway_grant
from indus.cli.commands.gateway_grant import (
    EXIT_OK,
    EXIT_RETRY,
    EXIT_TERMINAL,
    Grant,
    GrantRefused,
    obtain_grant,
    scrub_launch_env,
)
from indus.foundation.scope import Scope
from indus.gateway.control_socket import ControlServer
from indus.gateway.daemon.attach_verbs import AttachVerbs
from indus.service import gateway_control
from indus.service.gateway_ensure import Ensured
from indus.service.remote.channel import DialRefused
from indus.service.ws_auth import (
    SUBPROTOCOL,
    TICKET_PROTOCOL_PREFIX,
    WS_INTERACTIVE,
    TicketBook,
    stable_profile_id,
)
from tests.unit.cli.commands.cmdrun import Ran, run_cli

WS_URL = "ws://127.0.0.1:47019/ws"


class EnsureFailed(Exception):
    """An ensure failure carrying a code and whether a retry can help."""

    def __init__(self, code: str, message: str, *, terminal: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.terminal = terminal


@dataclass
class Discover:
    """TEST DOUBLE of ``gateway_grant.ensure_endpoint``: discovers through ``describe`` only (never
    starts a daemon) and records its calls; ``fail`` makes every call raise it."""

    fail: Exception | None = None
    override: dict[str, Any] | None = None
    calls: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def __call__(self, scope: Scope, *, mode: str, timeout: float) -> dict[str, Any]:
        self.calls.append({"env": dict(scope.env.to_dict()), "mode": mode, "timeout": timeout})
        if self.fail is not None:
            raise self.fail
        if self.override is not None:
            return self.override
        found = gateway_control.describe(scope.home, timeout=timeout)
        if found is None:
            raise EnsureFailed("not_running", "no daemon answers")
        return {key: found[key] for key in ("ws_url", "profile_id", "instance_id", "code_version")}


class Daemon:
    """A control socket with the attach verbs, served on its own loop in a thread."""

    def __init__(self, home: Path, *, url: str | None = WS_URL) -> None:
        self.home = home
        self.book = TicketBook.for_home(home)
        self.verbs = AttachVerbs(home, self.book, ws_url=lambda: url, version="9.9.9")
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        server = ControlServer(self.home, status=lambda: {"state": "running"}, env={})
        self.verbs.install(server)
        self._stop = asyncio.Event()
        await server.start()
        self._ready.set()
        try:
            await self._stop.wait()
        finally:
            await server.stop()
            self.verbs.close()

    def __enter__(self) -> Daemon:
        self._thread.start()
        assert self._ready.wait(10), "control socket did not start"
        return self

    def __exit__(self, *exc: object) -> None:
        stop = self._stop
        assert stop is not None
        self._loop.call_soon_threadsafe(stop.set)
        self._thread.join(10)
        self._loop.close()


@pytest.fixture
def ensure(monkeypatch: pytest.MonkeyPatch) -> Discover:
    double = Discover()
    monkeypatch.setattr(gateway_grant, "ensure_endpoint", double)
    return double


def grant(capsys: pytest.CaptureFixture[str], scope: Scope, *args: str) -> Ran:
    return run_cli(capsys, scope, "gateway", "grant", *args)


def ticket_of(answer: dict[str, Any]) -> str:
    protocols = answer["protocols"]
    assert protocols[0] == SUBPROTOCOL and protocols[1].startswith(TICKET_PROTOCOL_PREFIX)
    return protocols[1][len(TICKET_PROTOCOL_PREFIX) :]


# -- success ---------------------------------------------------------------------------------------


def test_the_grant_carries_the_ticket_only_inside_protocols(
    scope: Scope,
    ensure: Discover,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG), Daemon(scope.home) as daemon:
        ran = grant(capsys, scope, "--json")
        assert ran.code == EXIT_OK, ran
        answer = json.loads(ran.out)
        # The ticket is real and single use.
        ticket = ticket_of(answer)
        assert daemon.book.redeem(ticket, purpose=WS_INTERACTIVE).purpose == WS_INTERACTIVE
        with pytest.raises(Exception, match="used|unknown"):
            daemon.book.redeem(ticket, purpose=WS_INTERACTIVE)
    assert set(answer) == {"url", "protocols", "profile_id", "instance_id"}
    assert answer["url"] == WS_URL
    assert answer["profile_id"] == stable_profile_id(scope.home)
    assert answer["instance_id"] == daemon.book.instance_id
    assert len(answer["protocols"]) == 2
    rest = json.dumps({k: v for k, v in answer.items() if k != "protocols"})
    assert ticket not in rest and ticket not in answer["url"]
    assert ran.out.count(ticket) == 1 and ticket not in ran.err
    assert ticket not in caplog.text
    assert ensure.calls and ensure.calls[0]["mode"] == "start"
    assert ensure.calls[0]["timeout"] == 30.0
    assert not list(scope.home.rglob("*grant*"))


def test_a_grant_repr_never_shows_the_ticket() -> None:
    secret = "t" + "k" * 42
    shown = repr(Grant(WS_URL, secret, "p", "i"))
    assert secret not in shown and WS_URL in shown


def test_launch_options_are_scrubbed_before_ensure(
    scope: Scope, ensure: Discover, capsys: pytest.CaptureFixture[str]
) -> None:
    launch = {
        "INDUS_TUI_YOLO": "1",
        "INDUS_TUI_QUERY": "hello",
        "INDUS_YOLO_MODE": "1",
        "INDUS_MODEL": "some-model",
        "INDUS_SAFE_MODE": "1",
    }
    loaded = scope.with_env(scope.env.with_overlay(launch))
    with Daemon(scope.home):
        ran = grant(capsys, loaded, "--json")
    assert ran.code == EXIT_OK, ran
    seen = ensure.calls[0]["env"]
    assert not set(launch) & set(seen)
    assert seen["INDUS_HOME"] == str(scope.home)


def test_stray_prints_during_ensure_never_reach_stdout(
    scope: Scope,
    ensure: Discover,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def noisy(scope: Scope, *, mode: str, timeout: float) -> Any:
        print("starting the daemon…")  # noqa: T201 - a chatty start must not corrupt the grant
        return ensure(scope, mode=mode, timeout=timeout)

    monkeypatch.setattr(gateway_grant, "ensure_endpoint", noisy)
    with Daemon(scope.home):
        ran = grant(capsys, scope, "--json")
    assert ran.code == EXIT_OK, ran
    assert set(json.loads(ran.out)) == {"url", "protocols", "profile_id", "instance_id"}
    assert "starting the daemon" in ran.err


def test_scrub_keeps_everything_but_launch_options() -> None:
    env = {"INDUS_HOME": "/h", "PATH": "/bin", "INDUS_TUI_CWD": "/w", "INDUS_ACCEPT_HOOKS": "1"}
    assert scrub_launch_env(env) == {"INDUS_HOME": "/h", "PATH": "/bin"}


def test_discover_mode_never_starts_and_waits_briefly(
    scope: Scope, ensure: Discover, capsys: pytest.CaptureFixture[str]
) -> None:
    with Daemon(scope.home):
        ran = grant(capsys, scope, "--json", "--mode", "discover")
    assert ran.code == EXIT_OK, ran
    assert ensure.calls == [ensure.calls[0]]
    assert ensure.calls[0]["mode"] == "discover" and ensure.calls[0]["timeout"] == 5.0
    with Daemon(scope.home):
        assert grant(capsys, scope, "--json", "--mode", "recover").code == EXIT_OK
    assert ensure.calls[1]["mode"] == "recover"


# -- verdicts --------------------------------------------------------------------------------------


def error_of(ran: Ran) -> dict[str, Any]:
    body = json.loads(ran.out)
    assert set(body) == {"error"}
    return body["error"]


def test_a_daemon_without_an_attach_endpoint_is_terminal_with_a_hint(
    scope: Scope, ensure: Discover, capsys: pytest.CaptureFixture[str]
) -> None:
    with Daemon(scope.home, url=None) as daemon:
        ran = grant(capsys, scope, "--json")
    assert ran.code == EXIT_TERMINAL
    error = error_of(ran)
    assert error["verdict"] == "gateway_unavailable" and error["code"] == "ws_unavailable"
    assert "--local" in error["hint"]
    assert daemon.book.instance_id not in ran.err


@pytest.mark.parametrize(
    ("failure", "code", "verdict", "exit_code"),
    [
        (EnsureFailed("start_failed", "the daemon exited"), "start_failed", "retry", EXIT_RETRY),
        (EnsureFailed("budget", "too many restarts", terminal=False), "budget", "retry", 1),
        (
            EnsureFailed("broken_install", "cannot run", terminal=True),
            "broken_install",
            "gateway_unavailable",
            EXIT_TERMINAL,
        ),
        (RuntimeError("boom"), "ensure_failed", "retry", EXIT_RETRY),
    ],
)
def test_ensure_failures_keep_their_verdict(
    scope: Scope,
    ensure: Discover,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
    code: str,
    verdict: str,
    exit_code: int,
) -> None:
    ensure.fail = failure
    ran = grant(capsys, scope, "--json")
    assert ran.code == exit_code
    error = error_of(ran)
    assert (error["code"], error["verdict"]) == (code, verdict)


def test_another_profiles_daemon_is_refused(
    scope: Scope, ensure: Discover, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure.override = {
        "ws_url": WS_URL,
        "profile_id": "0" * 16,
        "instance_id": "inst",
        "code_version": "1",
    }
    ran = grant(capsys, scope, "--json")
    assert ran.code == EXIT_TERMINAL
    assert error_of(ran)["code"] == "profile_mismatch"


def test_an_unsafe_home_is_terminal(
    scope: Scope, ensure: Discover, capsys: pytest.CaptureFixture[str]
) -> None:
    mode = scope.home.stat().st_mode
    os.chmod(scope.home, 0o777)
    try:
        ran = grant(capsys, scope, "--json")
    finally:
        os.chmod(scope.home, mode)
    assert ran.code == EXIT_TERMINAL
    assert error_of(ran)["code"] == "unsafe_home"
    assert ensure.calls == []


def test_windows_has_no_control_socket(scope: Scope) -> None:
    with pytest.raises(GrantRefused) as caught:
        obtain_grant(scope, ensure=Discover(), platform="win32")
    assert caught.value.code == "unsupported" and caught.value.terminal


def refuse_with(error: str) -> Callable[[Path], Any]:
    """``grant_for`` refusing as the real one does (its own terminal set)."""

    def fake(_home: Path) -> Any:
        terminal = error in {"unsafe_home", "profile_mismatch", "forbidden"}
        raise DialRefused(error, "refused", terminal=terminal)

    return fake


@pytest.mark.parametrize(
    ("error", "terminal"),
    [
        ("capacity", False),
        ("stale_instance", False),
        ("timeout", False),
        ("no_socket", False),
        ("bad_response", False),
        ("forbidden", True),
        ("ws_unavailable", True),
        ("profile_mismatch", True),
        ("unsafe_home", True),
    ],
)
def test_ticket_refusals_map_to_verdicts(scope: Scope, error: str, terminal: bool) -> None:
    descriptor = {
        "ws_url": WS_URL,
        "profile_id": stable_profile_id(scope.home),
        "instance_id": "inst-1",
        "code_version": "1",
    }
    with pytest.raises(GrantRefused) as caught:
        obtain_grant(scope, ensure=Discover(override=descriptor), mint=refuse_with(error))
    assert caught.value.code == error and caught.value.terminal is terminal
    assert caught.value.verdict == ("gateway_unavailable" if terminal else "retry")


# -- the real ensure ---------------------------------------------------------------------------


def test_the_real_ensure_discovers_a_running_daemon(
    scope: Scope, capsys: pytest.CaptureFixture[str]
) -> None:
    with Daemon(scope.home) as daemon:
        ran = grant(capsys, scope, "--json", "--mode", "discover")
    assert ran.code == EXIT_OK, ran
    answer = json.loads(ran.out)
    assert answer["url"] == WS_URL and answer["instance_id"] == daemon.book.instance_id


def test_the_real_ensure_without_a_daemon_in_discover_mode_is_retry(
    scope: Scope, capsys: pytest.CaptureFixture[str]
) -> None:
    ran = grant(capsys, scope, "--json", "--mode", "discover")
    assert ran.code == EXIT_RETRY
    error = error_of(ran)
    assert (error["verdict"], error["code"]) == ("retry", "not_running")


@pytest.mark.parametrize(
    ("verdict", "code", "terminal"),
    [
        (
            Ensured("stopped", "planned_stop", "the gateway was stopped (stop)"),
            "planned_stop",
            True,
        ),
        (
            Ensured("refused", "recover_budget", "restarted 3 times in 60 s"),
            "recover_budget",
            False,
        ),
        (Ensured("refused", "unsafe_home", "writable by others"), "unsafe_home", True),
        (Ensured("exited", "exited", "the gateway exited\n  a log tail"), "exited", False),
        (Ensured("starting", "timeout", "did not answer in time"), "timeout", False),
    ],
)
def test_ensure_verdicts_keep_their_terminal_flag(
    scope: Scope, monkeypatch: pytest.MonkeyPatch, verdict: Ensured, code: str, terminal: bool
) -> None:
    from indus.service import gateway_ensure

    seen: list[str] = []

    def fake(_home: Path, _env: Any, *, mode: str, timeout: float) -> Ensured:
        seen.append(mode)
        return verdict

    monkeypatch.setattr(gateway_ensure, "ensure", fake)
    with pytest.raises(GrantRefused) as caught:
        obtain_grant(scope, mode="recover")
    assert (caught.value.code, caught.value.terminal) == (code, terminal)
    assert "\n" not in caught.value.message
    assert seen == ["recover"]
    if code == "planned_stop":
        assert caught.value.hint is not None and "--local" in caught.value.hint


def test_a_daemon_restarted_between_ensure_and_ticket_is_retry(scope: Scope) -> None:
    descriptor = {
        "ws_url": WS_URL,
        "profile_id": stable_profile_id(scope.home),
        "instance_id": "inst-old",
        "code_version": "1",
    }
    with Daemon(scope.home), pytest.raises(GrantRefused) as caught:
        obtain_grant(scope, ensure=Discover(override=descriptor))
    assert caught.value.code == "stale_instance" and not caught.value.terminal


# -- without --json --------------------------------------------------------------------------------


def test_without_json_nothing_is_minted(
    scope: Scope, ensure: Discover, capsys: pytest.CaptureFixture[str]
) -> None:
    with Daemon(scope.home) as daemon:
        ran = grant(capsys, scope)
        minted_before = daemon.book.mint(WS_INTERACTIVE)
    assert ran.code == EXIT_OK
    assert WS_URL in ran.out and TICKET_PROTOCOL_PREFIX not in ran.out
    assert ensure.calls == []
    assert minted_before.ticket not in ran.out
    idle = grant(capsys, scope)
    assert idle.code == EXIT_RETRY and "--json" in idle.err
