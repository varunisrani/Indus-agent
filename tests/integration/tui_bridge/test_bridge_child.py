"""P12-33: the real ``indus tui-bridge --stdio`` child, driven over its pipes as the TUI does.

The model is the P03 fake served on a loopback port (``tests/fakes/model/http_serve.py``) and the
profile points a ``custom`` provider at it, so the child runs its real provider stack. Covered:
the Demo 2 turn (stream, two tool calls, an approval answered with "once"), a ``kill -9``
mid-turn followed by a new child that resumes the same stored session without anything being sent
to the model again (ACC-J17-1), and the two settings the TUI reads at start.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO, Any

import pytest

from indus.foundation.clock import SystemClock
from tests.fakes.model.http_serve import HttpFakeModel, serving
from tests.fakes.turn import check_alternation

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(os.name != "posix", reason="POSIX signals and pipes"),
]

TIMEOUT = 60.0
# A real child process: waiting here is wall-clock time.
CLOCK = SystemClock()
Frame = dict[str, Any]


class Child:
    """One bridge process; frames read on a thread, requests answered by id."""

    def __init__(self, home: Path, work: Path) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("INDUS_")}
        env.update(
            {
                "INDUS_HOME": str(home),
                "INDUS_PROTOCOL_STRICT": "1",
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            }
        )
        self.stderr_path = home.parent / f"bridge-{time.monotonic_ns()}.err"
        self._stderr = self.stderr_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "indus", "tui-bridge", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            cwd=work,
            env=env,
            text=True,
            encoding="utf-8",
        )
        self.frames: list[Frame] = []
        self._new: queue.Queue[Frame] = queue.Queue()
        self._ids = 0
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        out: IO[str] | None = self.proc.stdout
        assert out is not None
        for line in out:
            frame: Frame = json.loads(line)  # stdout carries frames only
            self.frames.append(frame)
            self._new.put(frame)

    def wait_for(self, check: Callable[[Frame], bool], what: str) -> Frame:
        for frame in list(self.frames):
            if check(frame):
                return frame
        end = time.monotonic() + TIMEOUT
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise AssertionError(f"no {what}; stderr:\n{self.stderr()}")
            try:
                frame = self._new.get(timeout=left)
            except queue.Empty:
                continue
            if check(frame):
                return frame

    def send(self, method: str, **params: Any) -> int:
        self._ids += 1
        stdin = self.proc.stdin
        assert stdin is not None
        line = {"jsonrpc": "2.0", "id": self._ids, "method": method, "params": params}
        stdin.write(json.dumps(line) + "\n")
        stdin.flush()
        return self._ids

    def call(self, method: str, **params: Any) -> Any:
        rid = self.send(method, **params)
        answer = self.wait_for(lambda f: f.get("id") == rid, f"answer to {method}")
        assert "result" in answer, answer
        return answer["result"]

    def events(self, method: str) -> list[Frame]:
        return [f["params"] for f in self.frames if f.get("method") == method]

    def stderr(self) -> str:
        self._stderr.flush()
        return self.stderr_path.read_text(encoding="utf-8")[-4000:]

    def close(self) -> None:
        if self.proc.poll() is None:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self._stderr.close()


@pytest.fixture
def home(isolated_home: Path) -> Path:
    return isolated_home


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


def configure(home: Path, model: HttpFakeModel) -> None:
    (home / "config.yaml").write_text(
        "model:\n"
        "  default: fake-model\n"
        "  provider: custom\n"
        f"  base_url: http://127.0.0.1:{model.port}/v1\n",
        encoding="utf-8",
    )


@pytest.fixture
def children(home: Path, work: Path) -> Iterator[list[Child]]:
    started: list[Child] = []
    yield started
    for child in started:
        child.close()


def start(children: list[Child], home: Path, work: Path) -> Child:
    child = Child(home, work)
    children.append(child)
    ready = child.wait_for(lambda f: True, "first frame")
    assert ready["method"] == "gateway.ready"
    child.call("client.capabilities", features=["server_requests"])
    return child


def stored_rows(home: Path, session_id: str) -> list[Any]:
    from indus.core.persist import StateStore
    from indus.foundation.scope import make_scope
    from indus.state import registry

    with registry.open_unshared(make_scope(home)) as handle:
        return list(asyncio.run(StateStore(handle).load(session_id)))


def user_texts(home: Path, session_id: str) -> list[str]:
    return [str(r.content) for r in stored_rows(home, session_id) if r.role == "user"]


def streamed(child: Child, turn_id: str) -> str:
    return "".join(
        e.get("text") or "" for e in child.events("message.delta") if e["turn_id"] == turn_id
    )


def test_demo_turn_streams_runs_tools_and_takes_an_approval(
    home: Path, work: Path, children: list[Child]
) -> None:
    with serving("tui_demo2") as model:
        configure(home, model)
        child = start(children, home, work)
        sid = child.call("session.create", title="demo two")["session_id"]
        turn = child.call("prompt.submit", session_id=sid, text="make a counter script and run it")
        ask = child.wait_for(
            lambda f: f.get("method") == "server_request" and f["params"]["kind"] == "approval",
            "approval request",
        )["params"]
        assert ask["command"] == "chmod +x count.sh && ./count.sh"
        assert not (work / "runs.log").exists()
        child.call(
            "server_request.answer",
            session_id=sid,
            request_id=ask["request_id"],
            answer={"kind": "approval", "choice": "once"},
        )
        done = child.wait_for(lambda f: f.get("method") == "message.complete", "the final message")
        assert done["params"]["text"] == "All done: count.sh printed **counted**."
        assert done["params"]["reason"] == "completed"
        text = streamed(child, turn["turn_id"])
        assert text.startswith("Writing count.sh for you.Running it now.")
        tools = [(e["name"], e["ok"]) for e in child.events("tool.completed")]
        assert tools == [("terminal", True), ("terminal", True)]
        assert (work / "runs.log").read_text(encoding="utf-8") == "once\n"
        assert model.model_turns == 3
        rows = stored_rows(home, sid)
        check_alternation(rows)
        assert user_texts(home, sid) == ["make a counter script and run it"]


def test_killed_child_mid_turn_resumes_the_same_session_without_resending(
    home: Path, work: Path, children: list[Child]
) -> None:
    script = Path(work.parent / "kill.json")
    script.write_text(
        json.dumps(
            {
                "turns": [
                    {"steps": [{"text": ["Long ", "partial"]}, {"stall": 60}]},
                    {"steps": [{"text": "After the restart."}]},
                ]
            }
        ),
        encoding="utf-8",
    )
    with serving(script) as model:
        configure(home, model)
        first = start(children, home, work)
        sid = first.call("session.create")["session_id"]
        turn = first.call("prompt.submit", session_id=sid, text="start a long one")
        first.wait_for(
            lambda f: f.get("method") == "message.delta" and f["params"].get("text") == "partial",
            "the partial reply",
        )
        assert streamed(first, turn["turn_id"]) == "Long partial"
        os.kill(first.proc.pid, signal.SIGKILL)
        assert first.proc.wait(10) == -signal.SIGKILL

        second = start(children, home, work)
        resumed = second.call("session.resume", session_id=sid)
        assert resumed["session_id"] == sid
        CLOCK.sleep_sync(0.5)
        assert model.model_turns == 1  # nothing was sent again
        assert user_texts(home, sid) == ["start a long one"]

        second.call("prompt.submit", session_id=sid, text="and now?")
        done = second.wait_for(lambda f: f.get("method") == "message.complete", "the reply")
        assert done["params"]["text"] == "After the restart."
        assert model.model_turns == 2
        assert user_texts(home, sid) == ["start a long one", "and now?"]
        assert [s["session_id"] for s in second.call("session.list", limit=10)["sessions"]] == [sid]


def test_the_settings_the_tui_reads_at_start(home: Path, work: Path, children: list[Child]) -> None:
    with serving("tui_demo2") as model:
        configure(home, model)
        child = start(children, home, work)
        options = child.call("model.options")
        custom = next(p for p in options["providers"] if p["slug"] == "custom")
        # A provider whose key is optional is usable without one: the picker may switch to it.
        assert custom["authenticated"] is True
        assert custom["current"] is True
        # An unset busy mode stays null so the TUI keeps its own default (queue, R05.18).
        assert child.call("config.get", key="full")["value"]["busy"] is None
        child.call("config.set", key="busy", value="steer")
        assert child.call("config.get", key="full")["value"]["busy"] == "steer"
        assert model.model_turns == 0
