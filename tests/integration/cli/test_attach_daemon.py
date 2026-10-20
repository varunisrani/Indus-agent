"""P17-15..22 end to end: the CLI, a second CLI client and the TS client on one real daemon.

A real ``indus gateway run`` process serves a temp profile home (``SpoolHome``: the spool platform
and the loopback fake model; ``HOME`` and the config folder are temp folders too). Its session
host runs every turn (P17-22) and its WebSocket endpoint is mounted. Against it:

- ``indus chat -q`` (no ``--local``) ensures the daemon, attaches and prints the answer; ``-z``
  does the same; nothing runs in the CLI process;
- a second client — the CLI's own attach stack (``DaemonBackend`` → ``AttachedHost.feed``, what
  the classic REPL renders from) — attaches to that session and sees another CLI's turn live;
- ``indus gateway grant --json`` prints a single-use grant, and ``@indus/client`` (Node) attaches
  through it, runs a turn and reads the reply;
- bulk maintenance is served by the daemon while it owns the profile, and a restore is refused;
- with no daemon, ``indus chat -q`` starts one (detached) and attaches; ``--local`` still runs in
  this process and starts nothing.

Every daemon is stopped at teardown (SIGTERM, then SIGKILL), and no process naming the home is
left behind.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.cli import attach
from indus.cli.attach_remote import DaemonBackend
from indus.foundation.clock import SystemClock
from indus.foundation.scope import EnvView, make_scope
from indus.protocol.events import MessageDelta, TurnDone, TurnStarted
from indus.service import gateway_control
from indus.service.ws_auth import TICKET_PROTOCOL_PREFIX
from indus.state import gateway_runtime
from tests.fakes.model import script_of, text_turn
from tests.fakes.model.http_serve import serving
from tests.integration.gateway.spool import ROOT, Daemon, SpoolHome

pytestmark = [
    pytest.mark.skipif(os.name != "posix", reason="POSIX sockets and signals"),
    pytest.mark.slow,
]

TIMEOUT = 90.0
TS_SCRIPT = ROOT / "tests" / "contract" / "client" / "ts_grant_script.ts"
CLIENT_DEPS = ROOT / "packages" / "client" / "node_modules" / "@indus" / "protocol"


def _node() -> str | None:
    node = shutil.which("node")
    if node is None:
        return None
    try:
        version = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.match(r"v(\d+)\.(\d+)", version)
    if found is None:
        return None
    # TypeScript runs without a build from Node 22.18 (type stripping on by default).
    return node if (int(found[1]), int(found[2])) >= (22, 18) else None


_TICK = threading.Event()  # never set: a wall-clock pause between polls


def _pause(seconds: float) -> None:
    _TICK.wait(seconds)


def profile(tmp_path: Path, port: int) -> SpoolHome:
    """A profile home whose ``HOME`` and config folder are temp folders as well."""
    user_home = tmp_path / "user-home"
    user_home.mkdir(parents=True, exist_ok=True)
    extra = {
        "HOME": str(user_home),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
    }
    home = SpoolHome(tmp_path, port, env_extra=extra)
    home.home.chmod(0o700)
    return home


def indus(home: SpoolHome, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "indus", *args],
        capture_output=True,
        text=True,
        input=stdin,
        timeout=TIMEOUT,
        cwd=home.work,
        env=home.env(),
        check=False,
    )


def attachable(home: SpoolHome, daemon: Daemon | None = None) -> dict[str, Any]:
    """Wait until the daemon answers ``describe`` with a mounted WebSocket endpoint."""
    end = time.monotonic() + TIMEOUT
    while True:
        found = gateway_control.describe(home.home, timeout=2.0)
        if found is not None and found.get("ws_url"):
            return found
        if daemon is not None and daemon.proc.poll() is not None:
            raise AssertionError(f"the daemon exited first:\n{daemon.log()}")
        assert time.monotonic() < end, "the daemon never mounted its attach endpoint"
        _pause(0.1)


def stop_every_daemon(home: SpoolHome) -> None:
    """Stop whatever serves the home (a child or a detached one), then any straggler."""
    pids: set[int] = set()
    found = gateway_control.identify(home.home, timeout=1.0)
    if found is not None:
        pids.add(int(found["pid"]))
    view = gateway_runtime.runtime_view(home.home)
    if view.pid is not None and view.liveness == "running":
        pids.add(view.pid)
    pids |= set(home.stray_processes())
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    end = time.monotonic() + 30
    while pids and time.monotonic() < end:
        pids = {pid for pid in pids if gateway_runtime.pid_alive(pid)}
        for pid in pids:
            with contextlib.suppress(ChildProcessError, OSError):
                os.waitpid(pid, os.WNOHANG)
        _pause(0.1)
    for pid in pids | set(home.stray_processes()):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def session_of(err: str) -> str:
    found = re.search(r"session_id: (\S+)", err)
    assert found is not None, err
    return found[1]


def admissions(home: SpoolHome) -> list[tuple[str, str, str]]:
    import sqlite3

    uri = f"file:{home.home / 'state.db'}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        rows = conn.execute(
            "SELECT principal_id, state, coalesce(outcome, '') FROM runtime_admissions ORDER BY seq"
        ).fetchall()
    return [(str(a), str(b), str(c)) for a, b, c in rows]


def session_rows(home: SpoolHome, session_id: str) -> int:
    import sqlite3

    uri = f"file:{home.home / 'state.db'}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        (count,) = conn.execute(
            "SELECT count(*) FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    return int(count)


def logs_of(home: SpoolHome) -> str:
    folder = home.home / "logs"
    if not folder.exists():
        return ""
    return "\n".join(p.read_text(errors="replace") for p in sorted(folder.glob("*.log")))


@pytest.fixture
def stopped() -> Iterator[list[SpoolHome]]:
    homes: list[SpoolHome] = []
    try:
        yield homes
    finally:
        for home in homes:
            stop_every_daemon(home)
            assert home.stray_processes() == [], "a process naming the home is still running"


# -- one daemon, three clients --------------------------------------------------------------------


def test_cli_second_client_ts_client_and_maintenance_share_one_daemon(
    tmp_path: Path, stopped: list[SpoolHome]
) -> None:
    script = script_of(
        text_turn("First answer"),
        text_turn("Second ", "answer"),
        text_turn("Third answer"),
        text_turn("Fourth answer"),
    )
    with serving(script) as model:
        home = profile(tmp_path, model.port)
        stopped.append(home)
        daemon = home.start()
        daemon.running()
        described = attachable(home, daemon)

        # (b) indus chat -q attaches by default and prints the daemon's answer.
        first = indus(home, "chat", "-q", "say hello")
        assert first.returncode == 0, first.stderr + daemon.log()
        assert first.stdout == "First answer\n"
        sid = session_of(first.stderr)
        assert session_rows(home, sid) == 1
        answering = gateway_control.identify(home.home)
        assert answering is not None and answering["pid"] == daemon.proc.pid  # no second daemon

        # (c) a second client attaches to the session and sees another CLI's turn live.
        scope = make_scope(home.home, env=EnvView(home.env()), clock=SystemClock())
        backend = DaemonBackend()
        endpoint = backend.ensure(scope, timeout=30.0)
        assert endpoint.instance_id == described["instance_id"]
        watcher = attach.AttachedHost(backend, scope, endpoint).start()
        try:
            watcher.invoke("resume", sid, policy=None)
            feed, detach = watcher.feed(sid)
            second = indus(home, "-r", sid, "chat", "-q", "and again")
            seen: list[Any] = []
            end = time.monotonic() + TIMEOUT
            while not any(isinstance(item, TurnDone) for item in seen):
                assert time.monotonic() < end, f"the watcher saw only {seen!r}"
                with contextlib.suppress(queue.Empty):
                    seen.append(feed.get(timeout=1.0))
            detach()
        finally:
            watcher.stop()
        assert second.returncode == 0, second.stderr + daemon.log()
        assert second.stdout == "Second answer\n"
        assert session_of(second.stderr) == sid
        started = [item for item in seen if isinstance(item, TurnStarted)]
        assert len(started) == 1
        turn = started[0].turn_id
        live = "".join(i.text for i in seen if isinstance(i, MessageDelta) and i.turn_id == turn)
        assert live == "Second answer"

        # -z attaches as well (an unattended finite turn, the reply alone on stdout).
        oneshot = indus(home, "-z", "third")
        assert oneshot.returncode == 0, oneshot.stderr + daemon.log()
        assert (oneshot.stdout, oneshot.stderr) == ("Third answer\n", "")

        # (d) indus gateway grant --json, then the TS client attaches through it.
        node = _node()
        if node is not None and CLIENT_DEPS.exists():
            granted = indus(home, "gateway", "grant", "--json")
            assert granted.returncode == 0, granted.stdout + granted.stderr
            grant = json.loads(granted.stdout)
            assert set(grant) == {"url", "protocols", "profile_id", "instance_id"}
            ticket = grant["protocols"][1].removeprefix(TICKET_PROTOCOL_PREFIX)
            assert ticket and ticket not in grant["url"] and ticket not in granted.stderr
            ran = subprocess.run(
                [node, str(TS_SCRIPT)],
                input=json.dumps({"grant": grant, "text": "from the tui"}) + "\n",
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                cwd=ROOT,
                env={**home.env(), "NODE_NO_WARNINGS": "1"},
                check=False,
            )
            assert ran.returncode == 0, ran.stderr + daemon.log()
            done = json.loads(ran.stdout.strip().splitlines()[-1])
            assert done["reply"] == "Fourth answer"
            assert done["profile_id"] == grant["profile_id"]
            assert done["instance_id"] == grant["instance_id"] == described["instance_id"]
            assert ticket not in ran.stdout and ticket not in "\n".join(done["logs"])
            assert ticket not in logs_of(home)
            # The grant was single use: dialling with it again is refused by the daemon.
            again = indus(home, "gateway", "grant", "--json")
            assert (
                again.returncode == 0
                and json.loads(again.stdout)["protocols"] != grant["protocols"]
            )

        # Every turn ran in the daemon, each admitted once.
        rows = admissions(home)
        turns = 4 if node is not None and CLIENT_DEPS.exists() else 3
        assert [r[1:] for r in rows] == [("terminal", "completed")] * turns
        assert [r[0] for r in rows[:3]] == ["attach:cli"] * 3
        assert all(r[0].startswith("tui:") for r in rows[3:])
        assert model.model_turns == turns

        # (e) bulk maintenance is served by the daemon while it owns the profile ...
        deleted = indus(home, "sessions", "delete", sid, "--yes")
        assert deleted.returncode == 0, deleted.stdout + deleted.stderr + daemon.log()
        assert f"Deleted {sid}" in deleted.stdout
        assert "maintenance delete runs in the daemon" in logs_of(home)
        assert session_rows(home, sid) == 0
        # ... and restoring a backup over its files is refused.
        backup = tmp_path / "backup.zip"
        with zipfile.ZipFile(backup, "w") as zf:
            zf.writestr("config.yaml", "model: {}\n")
        restored = indus(home, "import", str(backup), "--force")
        assert restored.returncode == 1
        assert "gateway daemon" in restored.stderr and "Not done" in restored.stderr

        daemon.signal(signal.SIGTERM)
        assert daemon.wait() == 0


# -- no daemon yet --------------------------------------------------------------------------------


def test_chat_starts_a_daemon_when_none_runs_and_local_starts_nothing(
    tmp_path: Path, stopped: list[SpoolHome]
) -> None:
    script = script_of(text_turn("from the local agent"), text_turn("from the daemon"))
    with serving(script) as model:
        home = profile(tmp_path, model.port)
        stopped.append(home)
        assert gateway_control.identify(home.home) is None

        # --local keeps today's in-process route: it answers and starts no daemon.
        local = indus(home, "--local", "chat", "-q", "hello")
        assert local.returncode == 0, local.stderr
        assert local.stdout == "from the local agent\n"
        assert gateway_control.identify(home.home) is None
        assert home.stray_processes() == []
        assert admissions(home) == []  # nothing went through the daemon's ledger

        # Without --local the CLI ensures the daemon: none runs, so one is started (detached).
        attached = indus(home, "chat", "-q", "hello again")
        assert attached.returncode == 0, attached.stderr + logs_of(home)
        assert attached.stdout == "from the daemon\n"
        found = gateway_control.identify(home.home)
        assert found is not None
        pid = int(found["pid"])
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)  # not our child: double-forked away
        assert [r[1:] for r in admissions(home)] == [("terminal", "completed")]
        assert model.model_turns == 2
