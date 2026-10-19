"""P15 waves 2–3 end to end: the real ``indus gateway run`` process with every daemon part.

One profile home with the spool plugin platform and the loopback fake model:

- a DM is answered, and the reply went through the ledgered sender (its ledger row delivered);
- ``indus send spool`` (the plugin platform, found by ``indus send``) reaches the home channel;
- ``indus gateway status`` answers over the control socket;
- SIGTERM drains and stops (exit 0, status ``stopped``);
- a crash in the middle of sending a reply, then a restart: the stored reply goes out once, with
  the "may be a duplicate" label, and is never regenerated or sent twice.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from indus.gateway.delivery.ledger import DUPLICATE_PREFIX
from indus.state.gateway_runtime import EXIT_OK, pid_path
from tests.fakes.model import script_of, text_turn
from tests.fakes.model.http_serve import serving
from tests.integration.gateway.spool import ALLOWED, ROOT, SpoolHome

pytestmark = [
    pytest.mark.skipif(os.name != "posix", reason="POSIX sockets and signals"),
    pytest.mark.slow,
]


def indus(home: SpoolHome, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "indus", *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=home.work,
        env=home.env(**env),
        check=False,
    )


def ledger_states(home: SpoolHome) -> list[str]:
    import sqlite3

    db = home.home / "state.db"
    with sqlite3.connect(db) as conn:
        return [row[0] for row in conn.execute("SELECT state FROM delivery_ledger")]


def test_dm_send_status_drain_and_crash_recovery(tmp_path: Path) -> None:
    script = script_of(text_turn("First answer"), text_turn("Second answer"))
    with serving(script) as model:
        home = SpoolHome(tmp_path, model.port, env_extra={"INDUS_SPOOL_HOME_CHANNEL": ALLOWED})
        daemon = home.start()
        try:
            status = daemon.running()
            assert status.platforms["spool"].state == "connected"

            # an inbound DM is answered through the ledgered sender
            home.send("hello there")
            daemon.until(lambda: home.outbound() == ["First answer"], "the first reply")
            daemon.until(lambda: ledger_states(home) == ["delivered"], "the ledger row")

            # indus send to the home channel (the plugin platform is found by indus send)
            sent = indus(home, "send", "spool", "deploy finished")
            assert sent.returncode == 0, sent.stderr
            assert "Sent to spool." in sent.stdout
            daemon.until(lambda: "deploy finished" in home.outbound(), "the indus send message")

            # status over the control socket
            ran = indus(home, "gateway", "status")
            assert ran.returncode == 0, ran.stdout + ran.stderr
            assert "answered on the control socket" in ran.stdout
            assert "spool" in ran.stdout and "connected" in ran.stdout
            as_json = json.loads(indus(home, "gateway", "status", "--json").stdout)
            assert as_json["status"]["answering_pid"] == daemon.proc.pid

            # SIGTERM drains and stops
            daemon.signal(signal.SIGTERM)
            assert daemon.wait() == EXIT_OK
        finally:
            daemon.kill()
        stopped = home.status()
        assert stopped is not None and stopped.state == "stopped"
        assert not pid_path(home.home).exists()

        # a crash while the reply is being sent, then a restart
        crashing = home.start()
        try:
            crashing.running()
            (home.spool / "die-on-send").touch()
            home.send("one more question")
            assert crashing.wait() == 70  # died inside the platform send
        finally:
            crashing.kill()
        (home.spool / "die-on-send").unlink()
        before = list(home.outbound())
        assert "Second answer" not in "\n".join(before)

        again = home.start()
        try:
            again.running()
            again.until(
                lambda: any("Second answer" in text for text in home.outbound()),
                "the recovered reply",
            )
            again.signal(signal.SIGTERM)
            assert again.wait() == EXIT_OK
        finally:
            again.kill()
    recovered = [t for t in home.outbound() if "Second answer" in t]
    assert len(recovered) == 1
    assert recovered[0].startswith(DUPLICATE_PREFIX)
    assert model.model_turns == 2  # the reply was never regenerated
    assert ledger_states(home).count("delivered") == 2
    assert home.stray_processes() == []
    assert not list(home.home.glob("*.sock"))
    assert ROOT.is_dir()
