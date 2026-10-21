"""P19-03/04/05/06: ``indus acp`` as a real process beside a real ``indus gateway run`` daemon.

The profile is a temp home (``tests.fakes.daemon.ProfileHome``) whose model is the loopback fake.
The editor side is this test, speaking ACP frames (one JSON object per line) to the child's stdin
and reading its stdout.

- Attached (the default): the adapter dials the daemon with a single-use ticket, reads
  ``runtime.describe`` (the daemon advertises ``acp.editor_policy``), creates a session on the
  daemon, prompts it on the fake model (``session/prompt``, P19-09), switches its model through
  the editor's model picker (``session/set_config_option``, P19-12 → ``session.mutate model``,
  D-16.602), answers a command on the daemon (``/usage`` → ``session.mutate command``) and forks
  it.
- Reattach (ACC-M8-2): the editor process exits and a new one loads the same session from the
  daemon — the history is replayed and the session keeps the model the first editor picked (the
  daemon wrote it to the session row).
- ``--local``: the same profile, the same frames, a session is created in process.
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import subprocess
import threading
from pathlib import Path
from typing import IO, Any

import pytest

from indus.protocol.attach import ACP_EDITOR_POLICY
from indus.service.daemon_host import HOST_CAPABILITIES
from tests.fakes.daemon import ProfileHome, RunningDaemon
from tests.fakes.model.http_serve import serving
from tests.integration.service.conftest_hosts import steps_script

pytestmark = [
    pytest.mark.skipif(os.name != "posix", reason="POSIX sockets and signals"),
    pytest.mark.slow,
]

TIMEOUT = 60.0
METHOD_NOT_FOUND = -32601


class Editor:
    """An ``indus acp`` child and the frames it wrote, read on a thread. Permission cards the
    agent asks for are allowed once; ``updates`` keeps every ``session/update``."""

    def __init__(self, profile: ProfileHome, *args: str, name: str = "") -> None:
        self.err_path = profile.root / f"acp-{name or len(args)}.err"
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
        self.updates: list[dict[str, Any]] = []
        self._next = 0
        stdout = self.proc.stdout
        assert stdout is not None
        threading.Thread(target=self._read, args=(stdout,), daemon=True).start()

    def _read(self, stdout: IO[str]) -> None:
        for line in stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _send(self, frame: dict[str, Any]) -> None:
        stdin = self.proc.stdin
        assert stdin is not None
        stdin.write(json.dumps(frame) + "\n")
        stdin.flush()

    def _answer(self, frame: dict[str, Any]) -> None:
        if frame.get("method") == "session/request_permission":
            outcome = {"outcome": {"outcome": "selected", "optionId": "allow_once"}}
            self._send({"jsonrpc": "2.0", "id": frame["id"], "result": outcome})
            return
        error = {"code": METHOD_NOT_FOUND, "message": "not offered by this editor"}
        self._send({"jsonrpc": "2.0", "id": frame["id"], "error": error})

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next += 1
        ident = self._next
        self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        while True:
            try:
                line = self.lines.get(timeout=TIMEOUT)
            except queue.Empty:
                raise AssertionError(f"no answer to {method}:\n{self.stderr()}") from None
            if line is None:
                raise AssertionError(f"the adapter exited before {method}:\n{self.stderr()}")
            frame = json.loads(line)  # stdout carries frames only (R09.14)
            assert frame.get("jsonrpc") == "2.0", frame
            if "method" in frame and "id" in frame:
                self._answer(frame)
            elif frame.get("method") == "session/update":
                self.updates.append(frame["params"])
            elif frame.get("id") == ident:
                return frame

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


def _prompt(editor: Editor, sid: str, text: str) -> dict[str, Any]:
    answer = editor.call(
        "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]}
    )
    assert answer.get("result", {}).get("stopReason") == "end_turn", answer
    return answer["result"]


def _said(editor: Editor, sid: str) -> str:
    return "".join(
        u["update"]["content"]["text"]
        for u in editor.updates
        if u.get("sessionId") == sid and u["update"].get("sessionUpdate") == "agent_message_chunk"
    )


def test_an_attached_editor_session_prompts_switches_model_and_forks_on_the_daemon(
    tmp_path: Path,
) -> None:
    script = steps_script([{"text": "hello from the daemon"}], [{"text": "after the switch"}])
    with serving(script) as model:
        seen: list[dict[str, Any]] = []
        model.on_request = seen.append
        profile = ProfileHome(tmp_path / "acp", model.port)
        try:
            daemon: RunningDaemon = profile.start_daemon()
            described = daemon.attachable()
            assert "acp" in described["creatable_sources"]
            assert sorted(described["capabilities"]) == sorted(HOST_CAPABILITIES)
            assert ACP_EDITOR_POLICY in described["capabilities"]

            editor = Editor(profile)
            ready = editor.call("initialize", INIT)
            assert ready["result"]["agentInfo"]["name"] == "indus-agent"
            created = editor.call("session/new", {"cwd": str(profile.work), "mcpServers": []})
            assert "error" not in created, created
            sid = created["result"]["sessionId"]
            assert created["result"]["modes"]["currentModeId"] == "default"

            _prompt(editor, sid, "say hello")
            assert "hello from the daemon" in _said(editor, sid)
            asked = [r for r in seen if r.get("kind") == "model"]
            assert asked and asked[-1]["model"] == "fake-model"

            switched = editor.call(
                "session/set_config_option",
                {"sessionId": sid, "configId": "model", "value": "other-model"},
            )
            assert "error" not in switched, switched
            _prompt(editor, sid, "and now")
            asked = [r for r in seen if r.get("kind") == "model"]
            assert asked[-1]["model"] == "other-model"  # the daemon's session runs the new model

            before = len(_said(editor, sid))
            _prompt(editor, sid, "/usage")  # a command the daemon answers (session.mutate command)
            assert "Model calls:" in _said(editor, sid)[before:]

            forked = editor.call(
                "session/fork", {"sessionId": sid, "cwd": str(profile.work), "mcpServers": []}
            )
            assert "error" not in forked, forked
            child = forked["result"]["sessionId"]
            assert child != sid
            loaded = editor.call(
                "session/load", {"sessionId": child, "cwd": str(profile.work), "mcpServers": []}
            )
            assert "error" not in loaded, loaded
            replayed = [
                u["update"]["content"]["text"]
                for u in editor.updates
                if u.get("sessionId") == child
                and u["update"].get("sessionUpdate") == "agent_message_chunk"
            ]
            assert "after the switch" in "".join(replayed)  # the fork holds the conversation

            assert editor.close() == 0, editor.stderr()
            assert "Traceback" not in editor.stderr()
            assert daemon.proc.poll() is None
        finally:
            profile.stop_all()
    assert profile.stray_processes() == []


def _row(profile: ProfileHome, sid: str) -> tuple[str, str]:
    db = profile.home / "state.db"
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        found = conn.execute(
            "SELECT model, COALESCE(billing_provider, '') FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
    assert found is not None, sid
    return str(found[0]), str(found[1])


def test_a_second_editor_process_loads_the_session_on_its_switched_model(tmp_path: Path) -> None:
    script = steps_script([{"text": "the first reply"}], [{"text": "after the reattach"}])
    with serving(script) as model:
        seen: list[dict[str, Any]] = []
        model.on_request = seen.append
        profile = ProfileHome(tmp_path / "acp", model.port)
        try:
            daemon: RunningDaemon = profile.start_daemon()
            daemon.attachable()
            first = Editor(profile, name="first")
            first.call("initialize", INIT)
            created = first.call("session/new", {"cwd": str(profile.work), "mcpServers": []})
            sid = created["result"]["sessionId"]
            _prompt(first, sid, "say something")
            switched = first.call(
                "session/set_config_option",
                {"sessionId": sid, "configId": "model", "value": "other-model"},
            )
            assert "error" not in switched, switched
            assert _row(profile, sid)[0] == "other-model"  # the daemon wrote the switch
            assert first.close() == 0, first.stderr()  # the editor process is gone

            second = Editor(profile, name="second")  # the editor restarted
            second.call("initialize", INIT)
            listed = second.call("session/list", {"cwd": str(profile.work)})
            assert sid in [s["sessionId"] for s in listed["result"]["sessions"]], listed
            loaded = second.call(
                "session/load", {"sessionId": sid, "cwd": str(profile.work), "mcpServers": []}
            )
            assert "error" not in loaded, loaded
            assert "the first reply" in _said(second, sid)  # the history is replayed
            [picker] = [o for o in loaded["result"]["configOptions"] if o["id"] == "model"]
            assert str(picker["currentValue"]).endswith("other-model")
            _prompt(second, sid, "and again")
            assert "after the reattach" in _said(second, sid)
            asked = [r for r in seen if r.get("kind") == "model"]
            assert asked[-1]["model"] == "other-model"  # the same session, on its switched model
            assert second.close() == 0, second.stderr()
            assert "Traceback" not in first.stderr() + second.stderr()
            assert daemon.proc.poll() is None
        finally:
            profile.stop_all()
    assert profile.stray_processes() == []


def test_local_creates_a_session_in_process_and_leaves_the_daemon_alone(tmp_path: Path) -> None:
    with serving(steps_script([{"text": "unused"}])) as model:
        profile = ProfileHome(tmp_path / "acp", model.port)
        try:
            daemon: RunningDaemon = profile.start_daemon()
            daemon.attachable()
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
