"""P19-03/04: ``indus acp`` as a real process beside a real ``indus gateway run`` daemon.

The profile is a temp home (``tests.fakes.daemon.ProfileHome``) whose model is the loopback fake.
The editor side is this test, speaking ACP frames (one JSON object per line) to the child's stdin
and reading its stdout.

- Attached (the default): the adapter dials the daemon with a single-use ticket, reads
  ``runtime.describe``, and refuses ``session/new`` with ``acp_policy_unavailable`` because the
  daemon does not advertise the ACP editor policy yet (``session.mutate`` is not served, D-16.602).
- ``--local``: the same profile, the same frames, a session is created in process.

Prompting is P19-09 (``session/prompt``); until it lands, ``--local`` is the path that creates
sessions, and the attached path is pinned to its refusal against the real daemon.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from pathlib import Path
from typing import IO, Any

import pytest

from indus.service.daemon_host import HOST_CAPABILITIES
from tests.fakes.daemon import ProfileHome, RunningDaemon
from tests.fakes.model.http_serve import serving
from tests.integration.service.conftest_hosts import steps_script

pytestmark = [
    pytest.mark.skipif(os.name != "posix", reason="POSIX sockets and signals"),
    pytest.mark.slow,
]

TIMEOUT = 60.0


class Editor:
    """An ``indus acp`` child and the frames it wrote, read on a thread."""

    def __init__(self, profile: ProfileHome, *args: str) -> None:
        self.err_path = profile.root / f"acp-{len(args)}.err"
        self._err = self.err_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            profile.argv("acp", *args),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._err,
            text=True,
            cwd=profile.work,
            env=profile.env(),
        )
        self.lines: queue.Queue[str | None] = queue.Queue()
        self._next = 0
        stdout = self.proc.stdout
        assert stdout is not None
        threading.Thread(target=self._read, args=(stdout,), daemon=True).start()

    def _read(self, stdout: IO[str]) -> None:
        for line in stdout:
            self.lines.put(line)
        self.lines.put(None)

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next += 1
        ident = self._next
        stdin = self.proc.stdin
        assert stdin is not None
        frame = {"jsonrpc": "2.0", "id": ident, "method": method, "params": params}
        stdin.write(json.dumps(frame) + "\n")
        stdin.flush()
        while True:
            try:
                line = self.lines.get(timeout=TIMEOUT)
            except queue.Empty:
                raise AssertionError(f"no answer to {method}:\n{self.stderr()}") from None
            if line is None:
                raise AssertionError(f"the adapter exited before {method}:\n{self.stderr()}")
            answer = json.loads(line)  # stdout carries frames only (R09.14)
            assert answer.get("jsonrpc") == "2.0", answer
            if answer.get("id") == ident:
                return answer

    def stderr(self) -> str:
        if not self._err.closed:
            self._err.flush()
        return self.err_path.read_text(encoding="utf-8", errors="replace")

    def close(self) -> int:
        stdin = self.proc.stdin
        if stdin is not None and not stdin.closed:
            stdin.close()
        try:
            return self.proc.wait(TIMEOUT)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(10)
            raise AssertionError(f"the adapter did not exit:\n{self.stderr()}") from None
        finally:
            self._err.close()


INIT = {"protocolVersion": 1, "clientCapabilities": {}}


def test_attached_refuses_new_sessions_without_the_editor_policy_and_local_creates_one(
    tmp_path: Path,
) -> None:
    with serving(steps_script([{"text": "unused"}])) as model:
        profile = ProfileHome(tmp_path / "acp", model.port)
        try:
            daemon: RunningDaemon = profile.start_daemon()
            described = daemon.attachable()
            assert "acp" in described["creatable_sources"]
            assert sorted(described["capabilities"]) == sorted(HOST_CAPABILITIES)

            attached = Editor(profile)
            ready = attached.call("initialize", INIT)
            assert ready["result"]["agentInfo"]["name"] == "indus-agent"
            refused = attached.call("session/new", {"cwd": str(profile.work), "mcpServers": []})
            assert refused["error"]["data"]["reason"] == "acp_policy_unavailable", refused
            assert "--local" in json.dumps(refused["error"])
            assert attached.close() == 0, attached.stderr()
            assert "Traceback" not in attached.stderr()

            local = Editor(profile, "--local")
            local.call("initialize", INIT)
            created = local.call("session/new", {"cwd": str(profile.work), "mcpServers": []})
            assert created.get("result", {}).get("sessionId"), created
            assert created["result"]["modes"]["currentModeId"] == "default"
            assert local.close() == 0, local.stderr()
            assert daemon.proc.poll() is None  # --local never touched the daemon
        finally:
            profile.stop_all()
    assert profile.stray_processes() == []
