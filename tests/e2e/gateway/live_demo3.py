"""Demo 3 with real bots (P15-36): the owner's run, never part of default CI.

Run it by hand, against a profile whose gateway is set up (credentials in that profile's secret
store or the environment, under the vendors' names: ``TELEGRAM_BOT_TOKEN``, ``DISCORD_BOT_TOKEN``,
``SLACK_BOT_TOKEN`` + ``SLACK_APP_TOKEN``; allowlists and the Telegram home channel configured)::

    INDUS_LIVE=1 INDUS_HOME=~/.indus/profiles/demo \\
    INDUS_LIVE_TELEGRAM_CHAT=<chat id> INDUS_LIVE_DISCORD_CHAT=<channel id> \\
    INDUS_LIVE_SLACK_CHAT=<channel id> \\
    uv run pytest tests/e2e/gateway/live_demo3.py -m network -s

The file name keeps it out of normal collection; the ``network`` marker and ``INDUS_LIVE=1`` keep
it out of every default run. Credentials are only ever read through the profile's settings (secret
store first, then the environment); they are never printed, never put on a command line, and a
child's output is checked for them without echoing it.

Steps (``-s`` shows the prompts for the person at the keyboard):

1. ``indus send`` reaches each configured chat (send-only: no polling, no socket).
2. The gateway runs with a crash point armed: the first reply it sends is killed (SIGKILL) after
   the ledger marks it ``attempting``. The person DMs the Telegram bot; the process dies; the
   gateway is started again and the ledger row must reach ``delivered`` (the chat shows the reply
   once, labelled as possibly a duplicate). While it runs, the person also chats on Discord and
   Slack and approves a command with the Telegram button (checked by eye).
3. A cron job "every 30m" delivering to the Telegram home channel is created; with
   ``INDUS_LIVE_CRON_WAIT=1`` the run waits for its first delivery (about 30 minutes).
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope, make_scope
from indus.foundation.secrets import open_secret_store
from indus.gateway.config import load_gateway_settings

pytestmark = [pytest.mark.network]

LIVE = os.environ.get("INDUS_LIVE") == "1"
PLATFORMS = ("telegram", "discord", "slack")
WAIT_S = float(os.environ.get("INDUS_LIVE_WAIT_SECONDS", "600"))
CLOCK = SystemClock()

# Arms the ledger's crash point, then runs ``indus gateway run`` (argv is fixed, no secrets).
_CRASHING_GATEWAY = """\
import os, runpy, signal, sys
from indus.gateway.delivery import ledger
def die(point):
    if point == "attempting":
        os.kill(os.getpid(), signal.SIGKILL)
ledger.FAULT_HOOK = die
sys.argv = ["indus", "gateway", "run"]
runpy.run_module("indus", run_name="__main__")
"""


def _say(text: str) -> None:
    print(f"\n[demo 3] {text}", flush=True)


@pytest.fixture(scope="module")
def live_scope() -> Iterator[Scope]:
    if not LIVE:
        pytest.skip("set INDUS_LIVE=1 (and INDUS_HOME) to run the live Demo 3")
    home = os.environ.get("INDUS_HOME")
    if not home:
        pytest.skip("set INDUS_HOME to the demo profile's home")
    scope = make_scope(Path(home).expanduser())
    yield scope.with_secrets(open_secret_store(scope.home, scope.profile_id))


@pytest.fixture(scope="module")
def secrets(live_scope: Scope) -> list[str]:
    """The credential values, only to check that no output contains them."""
    settings = load_gateway_settings(live_scope)
    found: list[str] = []
    missing: list[str] = []
    for name in PLATFORMS:
        platform = settings.get(name)
        if platform is None or not platform.enabled:
            missing.append(name)
            continue
        found.extend(v for v in platform.credentials.values() if v)
    if missing:
        pytest.skip("not configured for the live demo: " + ", ".join(missing))
    return found


def _clean(output: str, secrets: list[str]) -> None:
    if any(value and value in output for value in secrets):
        pytest.fail("a credential appeared in a child's output (not shown)")


def _indus(scope: Scope, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "INDUS_HOME": str(scope.home)}
    return subprocess.run(
        [sys.executable, "-m", "indus", *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )


def _ledger(scope: Scope) -> list[str]:
    db = scope.home / "state.db"
    if not db.exists():
        return []
    with sqlite3.connect(db) as conn:
        return [row[0] for row in conn.execute("SELECT state FROM delivery_ledger")]


def _wait(check: object, what: str, limit: float = WAIT_S) -> None:
    end = CLOCK.monotonic() + limit
    while not check():  # type: ignore[operator]
        if CLOCK.monotonic() > end:
            pytest.fail(f"timed out waiting for {what}")
        CLOCK.sleep_sync(2.0)


def test_1_indus_send_reaches_each_chat(live_scope: Scope, secrets: list[str]) -> None:
    for name in PLATFORMS:
        chat = os.environ.get(f"INDUS_LIVE_{name.upper()}_CHAT")
        if not chat:
            _say(f"{name}: INDUS_LIVE_{name.upper()}_CHAT not set, send step skipped")
            continue
        ran = _indus(live_scope, "send", f"{name}:{chat}", "Indus Agent Demo 3: send check")
        _clean(ran.stdout + ran.stderr, secrets)
        assert ran.returncode == 0, f"indus send {name} failed (exit {ran.returncode})"


def test_2_kill_mid_send_restart_and_chat_on_three_platforms(
    live_scope: Scope, secrets: list[str]
) -> None:
    before = len(_ledger(live_scope))
    env = {**os.environ, "INDUS_HOME": str(live_scope.home)}
    crashing = subprocess.Popen(
        [sys.executable, "-c", _CRASHING_GATEWAY],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    _say("DM the Telegram bot now (any question). The gateway dies while sending the answer.")
    try:
        crashing.wait(timeout=WAIT_S)
    finally:
        if crashing.poll() is None:
            crashing.kill()
            crashing.wait()
    output = crashing.stdout.read() if crashing.stdout else ""
    _clean(output, secrets)
    assert crashing.returncode == -signal.SIGKILL, "the gateway did not die at the crash point"
    assert "attempting" in _ledger(live_scope)[before:]

    gateway = subprocess.Popen(
        [sys.executable, "-m", "indus", "gateway", "run"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait(lambda: "attempting" not in _ledger(live_scope)[before:], "the recovered reply")
        assert "delivered" in _ledger(live_scope)[before:]
        _say(
            "Telegram should show the answer once, labelled as possibly a duplicate. Now: chat "
            "with the bot on Discord and Slack, then ask it on Telegram to delete a scratch "
            "folder and approve with the button. Press Enter here when done."
        )
        if sys.stdin.isatty():
            input()
        else:
            CLOCK.sleep_sync(min(WAIT_S, 300.0))
    finally:
        gateway.send_signal(signal.SIGTERM)
        try:
            gateway.wait(timeout=90)
        except subprocess.TimeoutExpired:
            gateway.kill()
            gateway.wait()
    _clean(gateway.stdout.read() if gateway.stdout else "", secrets)
    assert gateway.returncode == 0


def test_3_cron_every_30m_to_the_telegram_home(live_scope: Scope, secrets: list[str]) -> None:
    created = _indus(
        live_scope,
        "cron",
        "create",
        "every 30m",
        "Say which minute of the hour it is.",
        "--deliver",
        "telegram",
    )
    _clean(created.stdout + created.stderr, secrets)
    assert created.returncode == 0, "indus cron create failed"
    if os.environ.get("INDUS_LIVE_CRON_WAIT") != "1":
        _say("cron job created; run `indus gateway run` and watch the Telegram home channel")
        return
    gateway = subprocess.Popen(
        [sys.executable, "-m", "indus", "gateway", "run"],
        env={**os.environ, "INDUS_HOME": str(live_scope.home)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _say("waiting about 30 minutes for the first cron delivery on Telegram")
        CLOCK.sleep_sync(31 * 60)
    finally:
        gateway.send_signal(signal.SIGTERM)
        gateway.wait(timeout=90)
    _clean(gateway.stdout.read() if gateway.stdout else "", secrets)
