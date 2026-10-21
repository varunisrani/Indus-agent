"""A whole dashboard stack in a temp home, driven over JSON lines (P18-30).

The browser suite (``apps/web/e2e``) and ``tests/integration/web/test_dashboard_daemon.py`` need the
same things running: the scripted fake model on a loopback port, a profile home pointing at it
(``tests.fakes.daemon.ProfileHome``), a real ``indus gateway run`` daemon, and a real ``indus
dashboard``. In Python this module is a context manager (``WebStack``); as a program —

    python -m tests.fakes.web_stack <spec.json>

— it starts the stack, prints one JSON line ``{"ready": true, ...}`` and then answers one JSON
command per stdin line with one JSON line (``{"ok": true, ...}`` or ``{"ok": false, "error":
...}``). The spec: ``{"root": <dir>, "turns": [<P03 step lists>], "daemon": true, "dashboard":
{"host": "127.0.0.1", "args": [], "env": {}}, "config_extra": "<yaml appended to config.yaml>",
"basic_auth": {"username": ..., "password": ...}}`` (the password is stored as its argon2id hash,
the way ``indus dashboard`` saves one).

Commands (``op``):

- ``model_turns`` → ``{turns}``: script turns the model has used;
- ``dashboard.restart`` → the dashboard stopped (SIGTERM) and started again on the same port;
- ``dashboard.stop``; ``dashboard.try`` ``{args}`` → one more ``indus dashboard`` run to its end
  (``{code, output}``; for a start that must refuse);
- ``indus`` ``{args, stdin}`` → one CLI run (``{code, stdout, stderr}``);
- ``cli.open`` ``{session}`` → the classic REPL attached to the daemon on that session, in a
  pseudo-terminal; ``cli.send`` ``{text}``; ``cli.keys`` ``{keys}`` (raw); ``cli.expect``
  ``{pattern, timeout}`` (a regular expression) → ``{matched, screen}``; ``cli.screen``;
  ``cli.close``;
- ``sql`` ``{query}`` → ``{rows}`` from the profile's store, opened read-only;
- ``read`` ``{path}`` → ``{text}`` of a file under the profile home (``config.yaml``, ...);
- ``pty_children`` → pids of the dashboard's PTY children (``indus --tui`` processes);
- ``kill`` ``{pid}`` → SIGKILL to one pid this stack started (a PTY child);
- ``quit`` → everything stopped; ``{stray}`` lists any process still naming the home.

Only processes that carry this home in their environment are ever signalled. Nothing here is
product code.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tests.fakes.daemon import ProfileHome, RunningDaemon, until
from tests.fakes.model import parse_script
from tests.fakes.model.http_serve import HttpFakeModel, serving

__all__ = ["Cli", "Dashboard", "WebStack", "main", "plain"]

TIMEOUT = 90.0
_ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*\x07|[@-Z\\-_])")
_URL_LINE = re.compile(r"Dashboard on (http://\S+?)/?(?:\?\S*)?$", re.M)


def plain(text: str) -> str:
    """Terminal output without escape sequences or carriage returns."""
    return _ESCAPES.sub("", text).replace("\r", "")


def _node_dir() -> str | None:
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if folder and (Path(folder) / "node").exists():
            return folder
    return None


def _default_hangup() -> None:
    signal.signal(signal.SIGHUP, signal.SIG_DFL)


def _basic_auth(spec: dict[str, Any]) -> str:
    login = spec.get("basic_auth")
    if not login:
        return ""
    from indus.web.auth.basic import hash_password

    secret = hash_password(str(login["password"]))
    return (
        "dashboard:\n  basic_auth:\n"
        f"    username: {json.dumps(str(login['username']))}\n"
        f"    password_hash: {json.dumps(secret)}\n"
    )


class Dashboard:
    """One ``indus dashboard`` child; ``url`` once it says where it listens."""

    def __init__(
        self,
        profile: ProfileHome,
        host: str,
        port: int,
        args: list[str],
        env: dict[str, str] | None = None,
    ) -> None:
        self.profile = profile
        self.log_path = profile.root / f"dashboard-{time.monotonic_ns()}.log"
        log = self.log_path.open("w", encoding="utf-8")
        try:
            self.proc = subprocess.Popen(
                profile.argv("dashboard", "--no-open", "--host", host, "--port", str(port), *args),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=profile.work,
                env=profile.env(**(env or {})),
            )
        finally:
            log.close()
        self.url = ""

    def output(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def ready(self, limit: float = TIMEOUT) -> str:
        def seen() -> str:
            found = _URL_LINE.search(self.output())
            if found is not None:
                return str(found[1])
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"the dashboard exited ({self.proc.returncode}):\n{self.output()}"
                )
            return ""

        self.url = until(seen, "the dashboard to listen", limit)
        return self.url

    @property
    def port(self) -> int:
        return int(self.url.rsplit(":", 1)[1])

    def stop(self, limit: float = 30.0) -> int:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        try:
            return self.proc.wait(limit)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait(10)


class Cli:
    """The classic REPL attached to the daemon on one session, in a pseudo-terminal."""

    def __init__(self, profile: ProfileHome, session_id: str) -> None:
        import pexpect

        self._pexpect = pexpect
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
            preexec_fn=_default_hangup,
        )
        self.child.logfile_read = self.log
        self.expect("Attached to this profile's gateway daemon")
        self.expect("› ")

    @property
    def screen(self) -> str:
        return plain(self.log.getvalue())

    def expect(self, pattern: str, timeout: float = TIMEOUT) -> None:
        if not self.matches(pattern, timeout):
            raise AssertionError(f"the CLI never showed {pattern!r}:\n{self.screen[-3000:]}")

    def matches(self, pattern: str, timeout: float = TIMEOUT) -> bool:
        try:
            self.child.expect(pattern, timeout=timeout)
        except (self._pexpect.TIMEOUT, self._pexpect.EOF):
            return False
        return True

    def send(self, text: str) -> None:
        self.child.send(text + "\r")

    def keys(self, keys: str) -> None:
        self.child.send(keys)

    def close(self) -> None:
        if self.child.isalive():
            self.child.close(force=True)


class WebStack:
    """Model, profile, daemon and dashboard for one spec (see the module notes)."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.root = Path(spec["root"])
        self.model: HttpFakeModel | None = None
        self.profile: ProfileHome | None = None
        self.daemon: RunningDaemon | None = None
        self.dashboard: Dashboard | None = None
        self.cli: Cli | None = None
        self._serving: contextlib.ExitStack | None = None

    # -- life cycle ------------------------------------------------------------------------------

    def start(self) -> dict[str, Any]:
        turns = self.spec.get("turns") or [[{"text": "Hello."}]]
        script = parse_script({"turns": [{"steps": steps} for steps in turns]}, "web-stack")
        self._serving = contextlib.ExitStack()
        self.model = self._serving.enter_context(serving(script))
        node = _node_dir()
        path = f"{Path(sys.executable).parent}:{node + ':' if node else ''}/usr/bin:/bin"
        self.profile = ProfileHome(
            self.root,
            self.model.port,
            config_extra=str(self.spec.get("config_extra", "")) + _basic_auth(self.spec),
            env_extra={"PATH": path},
        )
        if self.spec.get("daemon", True):
            self.daemon = self.profile.start_daemon()
            self.daemon.attachable()
        board = self.spec.get("dashboard")
        if board is not None:
            self.start_dashboard(int(board.get("port", 0)))
        return self.describe()

    def start_dashboard(self, port: int = 0) -> str:
        assert self.profile is not None
        board = self.spec.get("dashboard") or {}
        self.dashboard = Dashboard(
            self.profile,
            str(board.get("host", "127.0.0.1")),
            port,
            list(board.get("args", [])),
            {str(k): str(v) for k, v in dict(board.get("env", {})).items()},
        )
        return self.dashboard.ready()

    def describe(self) -> dict[str, Any]:
        assert self.profile is not None and self.model is not None
        return {
            "url": self.dashboard.url if self.dashboard else "",
            "home": str(self.profile.home),
            "work": str(self.profile.work),
            "model_port": self.model.port,
        }

    def close(self) -> list[int]:
        """Stop everything; the pids still naming the home afterwards (should be none)."""
        if self.cli is not None:
            with contextlib.suppress(Exception):
                self.cli.close()
        if self.dashboard is not None:
            with contextlib.suppress(Exception):
                self.dashboard.stop()
        stray: list[int] = []
        if self.profile is not None:
            self.profile.stop_all()
            stray = self.profile.stray_processes()
        if self._serving is not None:
            self._serving.close()
        return stray

    def __enter__(self) -> WebStack:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- helpers ---------------------------------------------------------------------------------

    def rows(self, query: str) -> list[list[Any]]:
        assert self.profile is not None
        uri = f"file:{self.profile.home / 'state.db'}?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            return [list(row) for row in conn.execute(query).fetchall()]

    def pty_children(self) -> list[int]:
        """Our dashboard's ``indus --tui`` children (by parent pid and argv)."""
        if self.dashboard is None:
            return []
        parent = str(self.dashboard.proc.pid)
        found: list[int] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                argv = (entry / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if stat[1] == parent and b"--tui" in argv:
                found.append(int(entry.name))
        return found

    # -- the command loop ------------------------------------------------------------------------

    def handle(self, command: dict[str, Any]) -> dict[str, Any]:
        op = command.get("op")
        profile = self.profile
        assert profile is not None and self.model is not None
        if op == "model_turns":
            return {"turns": self.model.model_turns}
        if op == "dashboard.restart":
            assert self.dashboard is not None
            port = self.dashboard.port
            self.dashboard.stop()
            return {"url": self.start_dashboard(port)}
        if op == "dashboard.stop":
            code = self.dashboard.stop() if self.dashboard is not None else None
            return {"code": code}
        if op == "dashboard.try":
            ran = profile.indus("dashboard", "--no-open", *command.get("args", []), timeout=60)
            return {"code": ran.returncode, "output": ran.stdout + ran.stderr}
        if op == "indus":
            ran = profile.indus(*command.get("args", []), stdin=str(command.get("stdin", "")))
            return {"code": ran.returncode, "stdout": ran.stdout, "stderr": ran.stderr}
        if op == "cli.open":
            self.cli = Cli(profile, str(command["session"]))
            return {}
        if op == "cli.send":
            assert self.cli is not None
            self.cli.send(str(command["text"]))
            return {}
        if op == "cli.keys":
            assert self.cli is not None
            self.cli.keys(str(command["keys"]))
            return {}
        if op == "cli.expect":
            assert self.cli is not None
            matched = self.cli.matches(str(command["pattern"]), float(command.get("timeout", 60)))
            return {"matched": matched, "screen": self.cli.screen[-4000:]}
        if op == "cli.screen":
            assert self.cli is not None
            return {"screen": self.cli.screen[-4000:]}
        if op == "cli.close":
            if self.cli is not None:
                self.cli.close()
                self.cli = None
            return {}
        if op == "sql":
            return {"rows": self.rows(str(command["query"]))}
        if op == "read":
            target = (profile.home / str(command["path"])).resolve()
            if not target.is_relative_to(profile.home.resolve()):
                raise ValueError("only files under the profile home")
            return {"text": target.read_text(encoding="utf-8")}
        if op == "pty_children":
            return {"pids": self.pty_children()}
        if op == "kill":
            pid = int(command["pid"])
            if pid not in self.pty_children():
                raise ValueError(f"{pid} is not one of this dashboard's PTY children")
            os.kill(pid, signal.SIGKILL)
            return {}
        raise ValueError(f"unknown op {op!r}")


def _emit(entry: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(entry) + "\n")
    sys.stdout.flush()


@contextlib.contextmanager
def _quiet_stdout() -> Iterator[None]:
    """Keep stray prints of the libraries off the JSON channel."""
    real = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = real


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: python -m tests.fakes.web_stack <spec.json>\n")
        return 64
    spec = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    stack = WebStack(spec)
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set() or sys.stdin.close())
    try:
        with _quiet_stdout():
            ready = stack.start()
        _emit({"ready": True, **ready})
        for line in sys.stdin:
            if stopping.is_set():
                break
            if not line.strip():
                continue
            command = json.loads(line)
            if command.get("op") == "quit":
                break
            try:
                with _quiet_stdout():
                    answer = stack.handle(command)
                _emit({"ok": True, **answer})
            except Exception as failed:  # the caller sees the reason; the loop goes on
                _emit({"ok": False, "error": f"{type(failed).__name__}: {failed}"})
    except Exception as failed:
        _emit({"ready": False, "error": f"{type(failed).__name__}: {failed}"})
    finally:
        with _quiet_stdout():
            stray = stack.close()
        with contextlib.suppress(Exception):
            _emit({"stopped": True, "stray": stray})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
