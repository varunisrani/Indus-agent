"""P17-27: the shared-backend demo (M6, CLI + TUI half) — one daemon, two surfaces, one session.

A real ``indus gateway run`` serves a temp profile (temp ``INDUS_HOME``, ``HOME`` and config
folder) whose model is the scripted fake on a loopback port. On one session, attached together:

- the **classic CLI**: the real ``indus -r <session>`` REPL in a pseudo-terminal (``pexpect``),
  attached to the daemon by default;
- the **TUI**: the real renderer — ``App`` in Ink's test renderer over the real ``AppRuntime``,
  ``@indus/client`` and attach transport (``indus gateway grant --json``, ticket only in the
  subprotocol) — built once from ``apps/tui/src/test/sharedBackendDriver.tsx`` and driven over its
  stdin/stdout.

The demo, step by step: a prompt sent from the CLI streams in both; the approval it raises is
answered from the TUI and the CLI's card closes; the CLI's terminal closes mid-turn and the turn
completes in the daemon (the TUI shows it); the daemon restarts while a turn runs and both clients
say its outcome is unknown and send nothing again (the model is never asked twice); with the
daemon stopped, ``indus --local`` still answers in process and starts nothing.

Everything the test starts is stopped at the end, and no process naming the temp home is left.

Beside the demo (P17-23 against the attach path): a daemon that ``indus chat -q`` started on its
own, left with nothing attached, exits by itself (``idle_exit``) and the next ``indus chat -q``
starts a fresh one without a word about it.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.fakes.daemon import ROOT, ProfileHome, RunningDaemon, pause, until
from tests.fakes.model.http_serve import serving
from tests.integration.service.conftest_hosts import steps_script

pexpect = pytest.importorskip("pexpect")

pytestmark = [
    pytest.mark.skipif(os.name != "posix", reason="POSIX pseudo-terminals, sockets and signals"),
    pytest.mark.slow,
    pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning"),
]

TIMEOUT = 90.0
APP = ROOT / "apps" / "tui"
DRIVER_SOURCE = "src/test/sharedBackendDriver.tsx"
DRIVER_DIR = APP / "node_modules" / ".shared-backend"
DRIVER = DRIVER_DIR / "sharedBackendDriver.js"
INDUS = Path(sys.executable).parent / "indus"
_ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*\x07|[@-Z\\-_])")

OUTCOME_UNKNOWN_TUI = "its outcome is unknown and it was not sent again"
OUTCOME_UNKNOWN_CLI = "its outcome is unknown and it was not run again"


def plain(text: str) -> str:
    return _ESCAPES.sub("", text).replace("\r", "")


def node() -> str | None:
    found = shutil.which("node")
    if found is None:
        return None
    try:
        version = subprocess.run(
            [found, "--version"], capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    parts = re.match(r"v(\d+)\.(\d+)", version)
    return found if parts is not None and (int(parts[1]), int(parts[2])) >= (22, 18) else None


@pytest.fixture(scope="module")
def driver() -> str:
    """Build the TUI driver once (vite SSR, beside the app's own dependencies)."""
    program = node()
    if program is None:
        pytest.skip("needs Node 22.18 or newer")
    vites = [APP / "node_modules" / ".bin" / "vite", ROOT / "node_modules" / ".bin" / "vite"]
    vite = next((v for v in vites if v.exists()), None)
    if vite is None or not INDUS.exists():
        pytest.skip("needs `pnpm install` and `uv sync`")
    built = subprocess.run(
        [
            str(vite),
            "build",
            "--ssr",
            DRIVER_SOURCE,
            "--outDir",
            str(DRIVER_DIR),
            "--emptyOutDir",
            "--logLevel",
            "warn",
        ],
        cwd=APP,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert built.returncode == 0 and DRIVER.exists(), built.stdout + built.stderr
    return program


# -- the two surfaces ------------------------------------------------------------------------------


class Tui:
    """The TUI driver: one JSON command per line in, one JSON state per line out."""

    def __init__(self, program: str, profile: ProfileHome, session_id: str) -> None:
        self.err_path = profile.root / "tui.err"
        self._err = self.err_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [program, str(DRIVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._err,
            text=True,
            cwd=APP,
            env=profile.env(
                INDUS_TUI_RESUME=session_id,
                INDUS_E2E_INDUS=str(INDUS),
                NODE_NO_WARNINGS="1",
            ),
        )
        self.last: dict[str, Any] = {}

    def ask(self, command: dict[str, Any]) -> dict[str, Any]:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(json.dumps(command) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f"the TUI driver ended:\n{self.stderr()}"
        self.last = json.loads(line)
        return self.last

    def state(self) -> dict[str, Any]:
        return self.ask({"op": "state"})

    def until(self, check: Callable[[dict[str, Any]], bool], what: str) -> dict[str, Any]:
        def seen() -> dict[str, Any] | None:
            state = self.state()
            return state if check(state) else None

        try:
            found = until(seen, f"the TUI: {what}", TIMEOUT)
        except AssertionError as failed:
            raise AssertionError(f"{failed}\nlast TUI frame:\n{self.last.get('frame')}") from None
        assert found is not None
        return found

    def texts(self, role: str) -> list[str]:
        return [m["text"] for m in self.last.get("messages", []) if m["role"] == role]

    def stderr(self) -> str:
        self._err.flush()
        return self.err_path.read_text(encoding="utf-8", errors="replace")[-3000:]

    def close(self) -> None:
        if self.proc.poll() is None:
            with contextlib.suppress(Exception):
                self.ask({"op": "quit"})
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self._err.close()


def _default_hangup() -> None:
    signal.signal(signal.SIGHUP, signal.SIG_DFL)


class Cli:
    """The classic REPL attached to the daemon, in a pseudo-terminal."""

    def __init__(self, profile: ProfileHome, session_id: str) -> None:
        self.log = io.StringIO()
        self.child = pexpect.spawn(
            sys.executable,
            ["-m", "indus", "-r", session_id],
            cwd=str(profile.work),
            env={**profile.env(), "TERM": "xterm-256color", "COLUMNS": "110", "LINES": "40"},
            encoding="utf-8",
            codec_errors="replace",
            dimensions=(40, 110),
            timeout=TIMEOUT,
            # A closing terminal's SIGHUP must end the REPL as it does for a person, even when
            # this test runs under ``nohup`` (which leaves SIGHUP ignored for every child).
            preexec_fn=_default_hangup,
        )
        self.child.logfile_read = self.log
        self.expect("Attached to this profile's gateway daemon")
        self.expect("› ")

    @property
    def screen(self) -> str:
        return plain(self.log.getvalue())

    def expect(self, pattern: str, timeout: float = TIMEOUT) -> None:
        try:
            self.child.expect(pattern, timeout=timeout)
        except (pexpect.TIMEOUT, pexpect.EOF):
            raise AssertionError(
                f"the CLI never showed {pattern!r}:\n{self.screen[-3000:]}"
            ) from None

    def send(self, text: str) -> None:
        self.child.send(text + "\r")

    def close(self) -> None:
        if self.child.isalive():
            self.child.close(force=True)


# -- helpers ---------------------------------------------------------------------------------------


def admissions(profile: ProfileHome) -> list[tuple[str, str]]:
    uri = f"file:{profile.home / 'state.db'}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        rows = conn.execute(
            "SELECT state, coalesce(outcome, '') FROM runtime_admissions ORDER BY seq"
        ).fetchall()
    return [(str(a), str(b)) for a, b in rows]


def described_instance(profile: ProfileHome) -> str | None:
    found = profile.described()
    return str(found["instance_id"]) if found and found.get("ws_url") else None


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[list[Any]]:
    """Everything started here, closed in reverse order (then the profile's processes)."""
    started: list[Any] = []
    try:
        yield started
    finally:
        for item in reversed(started):
            with contextlib.suppress(Exception):
                if isinstance(item, ProfileHome):
                    item.stop_all()
                else:
                    item.close()


SCRIPT = steps_script(
    [{"text": "Hello first."}],
    [
        {"text": ["Streaming ", "from the daemon. "]},
        {
            "tool_call": {
                "name": "terminal",
                "arguments": {"command": "find victim -delete"},
                "id": "call_clean",
            }
        },
        {"finish": "tool_calls"},
    ],
    [{"text": ["Cleaned ", "up."]}],
    [{"text": ["Working slowly"]}, {"stall": 4}, {"text": " and finished."}],
    [{"text": ["Half ", "way"]}, {"stall": 600}],
    [{"text": "Local answer."}],
)


def test_cli_and_tui_share_one_session_on_one_daemon(
    tmp_path: Path, driver: str, stack: list[Any]
) -> None:
    with serving(SCRIPT) as model:
        profile = ProfileHome(tmp_path / "demo", model.port)
        stack.append(profile)
        (profile.work / "victim").mkdir()
        daemon: RunningDaemon = profile.start_daemon()
        first_instance = str(daemon.attachable()["instance_id"])

        # The session: one finite attached run stores it (and its row) in the daemon.
        opened = profile.indus("chat", "-q", "hello")
        assert opened.returncode == 0, opened.stderr + daemon.log()
        assert opened.stdout == "Hello first.\n"
        found = re.search(r"session_id: (\S+)", opened.stderr)
        assert found is not None
        sid = found[1]

        # The TUI and the CLI attach to it.
        tui = Tui(driver, profile, sid)
        stack.append(tui)
        tui.until(lambda s: s["sid"] == sid and s["connected"], "the session open")
        cli = Cli(profile, sid)
        stack.append(cli)

        # 1. A prompt sent from the CLI streams in both.
        cli.send("please clean up")
        cli.expect("Streaming from the daemon")
        tui.until(
            lambda s: (
                "Streaming from the daemon" in s["frame"]
                or any("Streaming from the daemon" in t for t in tui.texts("assistant"))
            ),
            "the CLI's prompt streaming",
        )

        # 2. The approval it raises is answered from the TUI; the CLI's card closes.
        cli.expect("Approval needed")
        cli.expect("find victim -delete")
        tui.until(lambda s: s["card"] is not None and s["card"]["kind"] == "approval", "the card")
        assert "find victim -delete" in tui.last["frame"]
        tui.ask({"op": "press", "keys": ["\r"]})  # the first choice: allow once
        cli.expect("question answered in another window")
        cli.expect("Cleaned up.")
        tui.until(lambda s: "Cleaned up." in tui.texts("assistant") and not s["busy"], "settled")
        assert tui.last["card"] is None
        assert not (profile.work / "victim").exists()  # approved once, ran once
        assert model.model_turns == 3

        # 3. The CLI's terminal closes mid-turn; the turn completes in the daemon.
        cli.send("a slow one")
        cli.expect("Working slowly")
        cli.child.kill(signal.SIGHUP)
        until(lambda: not cli.child.isalive(), "the CLI gone", 30)
        tui.until(
            lambda s: "Working slowly and finished." in tui.texts("assistant") and not s["busy"],
            "the detached turn's end",
        )
        assert admissions(profile)[-1] == ("terminal", "completed")
        assert model.model_turns == 4

        # 4. The daemon restarts while a turn runs: both say the outcome is unknown, nothing
        #    is sent again.
        cli = Cli(profile, sid)
        stack.append(cli)
        tui.ask({"op": "type", "text": "one more"})
        tui.until(lambda s: "Half way" in s["frame"] and s["busy"], "the turn streaming")
        until(lambda: model.model_turns == 5, "the model asked", TIMEOUT)
        cli.expect("◇ indus")  # the CLI watches the TUI's turn
        daemon.kill()
        replacement = profile.start_daemon()
        # The clients' own recovery may start it first: wait for whichever daemon now serves.
        until(
            lambda: (described_instance(profile) or first_instance) != first_instance,
            "a restarted daemon",
            TIMEOUT,
        )
        if replacement.proc.poll() is None:
            replacement.attachable()
        tui.until(
            lambda s: any(OUTCOME_UNKNOWN_TUI in t for t in tui.texts("system")) and s["connected"],
            "the TUI's outcome-unknown line",
        )
        assert not tui.last["busy"]
        cli.expect(OUTCOME_UNKNOWN_CLI)
        cli.expect("/discard")
        assert admissions(profile)[-1] == ("unknown", "")
        pause(3.0)
        assert model.model_turns == 5  # never resent by either client

        # 5. With the daemon stopped, --local still answers in this process.
        tui.close()
        cli.close()
        profile.stop_all()
        assert profile.described() is None
        local = profile.indus("--local", "-z", "local now")
        assert local.returncode == 0, local.stderr
        assert local.stdout == "Local answer.\n"
        assert model.model_turns == 6
        assert profile.described() is None  # --local started no daemon
        assert profile.stray_processes() == []


def test_an_auto_started_daemon_idles_out_and_the_next_chat_restarts_it_silently(
    tmp_path: Path, stack: list[Any]
) -> None:
    from indus.service import gateway_control
    from indus.service.gateway_ensure import IDLE_EXIT_REASON, planned_stop_reason
    from indus.state.gateway_runtime import pid_alive, read_status

    script = steps_script([{"text": "First answer."}], [{"text": "Second answer."}])
    with serving(script) as model:
        profile = ProfileHome(tmp_path / "idle", model.port, idle_exit_seconds=3.0)
        stack.append(profile)
        assert profile.described() is None

        # No daemon: the attached chat ensures one (detached, auto-started: idle exit armed).
        first = profile.indus("chat", "-q", "hello")
        assert first.returncode == 0, first.stderr + profile.logs()
        assert first.stdout == "First answer.\n"
        found = gateway_control.identify(profile.home)
        assert found is not None
        pid = int(found["pid"])

        # Nothing attached and nothing to do: it stops itself, as an idle exit (not planned).
        until(lambda: not pid_alive(pid), "the idle daemon to exit", 60.0)
        status = read_status(profile.home)
        assert status is not None and status.state == "stopped", status
        assert (status.exit_reason, status.exit_code) == (IDLE_EXIT_REASON, 0)
        assert planned_stop_reason(profile.home) is None
        assert profile.described() is None

        # The next chat starts a new daemon and answers, saying nothing more than the first did.
        second = profile.indus("chat", "-q", "again")
        assert second.returncode == 0, second.stderr + profile.logs()
        assert second.stdout == "Second answer.\n"

        def noted(err: str) -> list[str]:
            return [line for line in err.splitlines() if not line.startswith("session_id: ")]

        assert noted(second.stderr) == noted(first.stderr) == []
        again = gateway_control.identify(profile.home)
        assert again is not None and int(again["pid"]) != pid
        assert model.model_turns == 2
