"""Parity runner, product side (P25-05).

Runs the parity flows on Indus Agent, one flow at a time, each with its own fresh home, demo project
and scripted model server, and writes one capture per flow to a git-ignored cache. How each flow is
set up lives in ``FLOWS`` below (``--list`` prints it), because REA's scenario schema has no room
for setup. The scenario files themselves are ``parity/<flow>.indus.json``:

- **browser flows** (dashboard, desktop renderer): the file *is* REA's ``capture_browser_scenario``
  input (checked against the contract we pinned, ``scenarios.rea_browser_problems``). The runner
  starts our server on the canonical origin ``http://127.0.0.1:24000`` (or ``--port``), writes the
  input REA should run to the cache and hands it to REA (``--rea-cli``). Without REA the flow is
  ``blocked``; ``--serve`` keeps a dashboard flow's server up for a run through REA's MCP tools and
  ``--import`` stores that result as the flow's capture.
- **terminal flows** (classic CLI, setup, TUI, cron), **stdio flows** (the editor protocol) and the
  **platform flow** (the real gateway against a loopback Bot API stand-in): our own step list run
  by ``terminal_driver`` and the sessions below: launch, wait_for, type, press, send, inbound,
  press_button, stop, wait_exit.

The reference side is never run here: its captures are made by the owner in the REA sandbox
(``~/rea-lab``) and only compared (``--ref``).

Usage::

    python scripts/parity/runner.py --list
    python scripts/parity/runner.py cli-chat-approve [more flows] [--dry-run]
    python scripts/parity/runner.py --all [--ledger] [--ref ~/rea-lab/parity/ref]
    python scripts/parity/runner.py --serve dashboard-sessions --browser /path/to/chromium
    python scripts/parity/runner.py --import dashboard-sessions result.json

Exit codes: 0 every flow that ran passed and nothing is unclassified; 1 a flow failed or a
divergence is unclassified; 2 bad usage.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # run as a file: make `scripts.parity` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.parity.scenarios import (
    HOOK_ATTRIBUTES,
    SCENARIO_DIR,
    hook_of,
    is_browser_scenario,
    load_scenario,
    rea_browser_problems,
    scenario_path,
    secret_problems,
    step_ids,
    steps_of,
    terminal_problems,
)
from scripts.parity.scripted_model_server import ScriptedModelServer, Settings
from scripts.parity.telegram_fake import FakeBotApi, Sent
from scripts.parity.terminal_driver import (
    KEYS,
    StdioSession,
    StepTimeout,
    TerminalSession,
    TranscriptProfile,
)

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / ".cache" / "parity"
CANONICAL_PORT = 24000
CANONICAL_ORIGIN = f"http://127.0.0.1:{CANONICAL_PORT}"
CAPTURE_FORMAT = "indus-parity-capture/1"
SETTLE_SECONDS = 0.2  # quiet time between typed text and a key press (line editors batch input)
SHARED_ENVIRONMENT = {
    "viewport": {"width": 1280, "height": 720, "device_scale_factor": 1},
    "locale": "en-US",
    "timezone": "UTC",
    "color_scheme": "light",
    "reduced_motion": "reduce",
    "service_workers": "block",
}
TERMINAL_ENVIRONMENT = {"locale": "en-US", "timezone": "UTC", "columns": 110, "rows": 40}

CLI_PROFILE = TranscriptProfile(
    cards={"approval": r"⚠ Approval needed", "question": r"^\s*\? \S"},
    prompts=(r"^›", r"pick 1-\d", r"reason for denying"),
)
TUI_PROFILE = TranscriptProfile(
    cards={"approval": r"(?i)approval", "question": r"^\s*\? \S"},
    prompts=(r"^\s*[›>❯] ",),
)
PLAIN_PROFILE = TranscriptProfile()


# -- the setup table -------------------------------------------------------------------------------


@dataclass(frozen=True)
class FlowSetup:
    """How one flow's product side is set up (what the matching e2e spec does)."""

    flow: str
    surface: str
    driver: str  # terminal | stdio | browser | platform
    script: str  # scripted model script (scripted_model_server.SCRIPTS)
    project: str  # "git": demo git project with a bare origin; "empty": an empty folder
    home: str  # "configured": config points at the scripted model; "fresh": nothing set up
    needs: tuple[str, ...] = ()
    profile: TranscriptProfile = PLAIN_PROFILE
    note: str = ""
    seed: tuple[str, ...] = ()  # CLI chats run before the flow, one message each


FLOWS: tuple[FlowSetup, ...] = (
    FlowSetup(
        "first-run-setup",
        "cli",
        "terminal",
        "echo",
        "empty",
        "fresh",
        profile=CLI_PROFILE,
        note="`indus setup` on an empty home: custom endpoint at the scripted model, then a turn",
    ),
    FlowSetup(
        "cli-chat-approve",
        "cli",
        "terminal",
        "approve_or_decline",
        "git",
        "configured",
        profile=CLI_PROFILE,
        note="classic CLI (`--local`): one approval allowed, one denied, /quit",
    ),
    FlowSetup(
        "cli-resume-search",
        "cli",
        "terminal",
        "echo",
        "git",
        "configured",
        profile=CLI_PROFILE,
        note="two one-shot turns, `indus sessions search`, then `indus -c` resumes the last",
    ),
    FlowSetup(
        "tui-stream-interrupt",
        "tui",
        "terminal",
        "long_stream",
        "git",
        "configured",
        needs=("tui-bundle",),
        profile=TUI_PROFILE,
        note="`indus --tui --local`: a long streamed reply interrupted with Ctrl+C",
    ),
    FlowSetup(
        "tui-model-picker",
        "tui",
        "terminal",
        "echo",
        "git",
        "configured",
        needs=("tui-bundle",),
        profile=TUI_PROFILE,
        note="`indus --tui --local`: /model opens the picker; Escape closes it; a turn still works",
    ),
    FlowSetup(
        "dashboard-sessions",
        "dashboard",
        "browser",
        "echo",
        "git",
        "configured",
        needs=("web-bundle", "rea"),
        note="`indus dashboard --port 24000` over a home with one seeded session",
        seed=("remember the blue kite",),
    ),
    FlowSetup(
        "dashboard-chat-tab",
        "dashboard",
        "browser",
        "echo",
        "git",
        "configured",
        needs=("web-bundle", "rea"),
        note="dashboard Chat tab: the embedded terminal and the model rail",
    ),
    FlowSetup(
        "desktop-onboarding",
        "desktop",
        "browser",
        "echo",
        "empty",
        "fresh",
        needs=("desktop-build", "rea"),
        note="packaged desktop app, first run: custom endpoint onboarding (renderer over CDP)",
    ),
    FlowSetup(
        "desktop-chat-panes",
        "desktop",
        "browser",
        "edit_file",
        "git",
        "configured",
        needs=("desktop-build", "rea"),
        note="packaged desktop app: a turn that writes a file, then the files and review panes",
    ),
    FlowSetup(
        "gateway-telegram-approve",
        "gateway",
        "platform",
        "approve_or_decline",
        "git",
        "configured",
        note="`indus gateway run` against a loopback Telegram Bot API fake; approval by button",
    ),
    FlowSetup(
        "cron-schedule-run",
        "cron",
        "terminal",
        "echo",
        "git",
        "configured",
        profile=PLAIN_PROFILE,
        note="`indus cron create`, `cron list`, `cron run` now, then the job's output",
    ),
    FlowSetup(
        "acp-session",
        "acp",
        "stdio",
        "echo",
        "git",
        "configured",
        note="`indus acp --local` over stdio: initialize, session/new, one prompt, updates",
    ),
)
FLOW_BY_NAME = {f.flow: f for f in FLOWS}


def flow_table() -> str:
    """The setup table ``--list`` prints."""
    head = ("flow", "surface", "driver", "model script", "project", "home", "needs")
    rows = [
        (f.flow, f.surface, f.driver, f.script, f.project, f.home, ",".join(f.needs) or "-")
        for f in FLOWS
    ]
    widths = [max(len(r[i]) for r in [head, *rows]) for i in range(len(head))]
    lines = [
        "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)) for row in [head, *rows]
    ]
    lines.insert(1, "  ".join("-" * w for w in widths))
    lines.append("")
    lines.extend(f"{f.flow}: {f.note}" for f in FLOWS)
    return "\n".join(lines)


def scenario_problems(flow: str, data: Mapping[str, Any]) -> list[str]:
    """Contract, step-id and timeout problems of one scenario file (empty when it is good)."""
    setup = FLOW_BY_NAME.get(flow)
    if setup is None:
        return [f"{flow}: no row in the runner's flow table"]
    if setup.driver == "browser" or is_browser_scenario(data):
        out = rea_browser_problems(data)
        for index, step in enumerate(steps_of(data)):
            if step.get("action") == "wait_for_timeout":
                out.append(f"actions[{index}]: wait for states, not time (no wait_for_timeout)")
            locator = step.get("locator")
            if isinstance(locator, dict) and hook_of(locator) is None:  # pyright: ignore[reportUnknownArgumentType]
                out.append(
                    f"actions[{index}]: locator does not use a {'/'.join(HOOK_ATTRIBUTES)} hook"
                )
        out += secret_problems(data)
        if data.get("environment") != SHARED_ENVIRONMENT:
            out.append(
                "environment: must be the shared block (viewport, en-US, UTC, reduced motion)"
            )
    else:
        out = terminal_problems(data)
        if data.get("driver") != setup.driver:
            out.append(f"driver {data.get('driver')!r} does not match the table ({setup.driver})")
    ids = step_ids(data)
    seen: set[str] = set()
    for step_id in ids:
        if step_id in seen:
            out.append(f"duplicate step_id {step_id!r}")
        seen.add(step_id)
    for index, step in enumerate(steps_of(data)):
        if step.get("action") != "wait_for_timeout" and not isinstance(step.get("timeout_ms"), int):
            out.append(f"actions[{index}] ({step.get('step_id')}): no timeout_ms")
    return out


# -- run context -----------------------------------------------------------------------------------


@dataclass
class RunOptions:
    cache: Path = CACHE_DIR
    scenarios: Path = SCENARIO_DIR
    port: int = CANONICAL_PORT
    browser: str | None = None
    rea_cli: Sequence[str] | None = None
    dry_run: bool = False
    keep: bool = False


@dataclass
class FlowContext:
    setup: FlowSetup
    base: Path
    server: ScriptedModelServer
    options: RunOptions
    home: Path = field(init=False)
    user_home: Path = field(init=False)
    work: Path = field(init=False)

    def __post_init__(self) -> None:
        self._telegram: FakeBotApi | None = None
        self.home = self.base / "indus-home"
        self.user_home = self.base / "user"
        self.work = self.base / "project"
        for folder in (self.home, self.user_home, self.work):
            folder.mkdir(parents=True, exist_ok=True)

    @property
    def variables(self) -> dict[str, str]:
        return {
            "model_base_url": self.server.base_url,
            "model": self.setup.script,
            "work": str(self.work),
            "origin": f"http://127.0.0.1:{self.options.port}",
        }

    def expand(self, value: Any) -> Any:
        """Replace ``${name}`` placeholders in strings (scenario variables and step results)."""
        if isinstance(value, str):
            return re.sub(
                r"\$\{([a-z_]+)\}", lambda m: self.variables.get(m.group(1), m.group(0)), value
            )
        if isinstance(value, list):
            return [self.expand(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
        if isinstance(value, dict):
            return {k: self.expand(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
        return value

    def env(self) -> dict[str, str]:
        """A clean environment (nothing inherited but PATH): fresh home, UTC, no keychain."""
        venv_bin = str(Path(sys.executable).parent)
        return {
            "PATH": f"{venv_bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.user_home),
            "INDUS_HOME": str(self.home),
            "LANG": "C.UTF-8",
            "TZ": "UTC",
            "TERM": "xterm-256color",
            "COLUMNS": str(TERMINAL_ENVIRONMENT["columns"]),
            "LINES": str(TERMINAL_ENVIRONMENT["rows"]),
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
            "GIT_CONFIG_GLOBAL": str(self.user_home / ".gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            # one terminal per flow, so `indus -c` continues this flow's newest session
            "INDUS_TERMINAL_ID": f"parity-{self.setup.flow}",
            # the gateway flow: a fake token built at run time, one allowed person, own locks
            "TELEGRAM_BOT_TOKEN": "fake" + "-" + "telegram" + "-" + "value",
            "INDUS_TELEGRAM_ALLOWED_USERS": str(PARITY_TELEGRAM_USER),
            "INDUS_GATEWAY_LOCK_DIR": str(self.base / "token-locks"),
        }

    def telegram(self) -> FakeBotApi:
        """The flow's Bot API fake (started on first use, stopped by ``cleanup``)."""
        if self._telegram is None:
            self._telegram = FakeBotApi(user_id=PARITY_TELEGRAM_USER)
            self._telegram.start()
            with (self.home / "config.yaml").open("a", encoding="utf-8") as config:
                config.write(f"platforms:\n  telegram:\n    base_url: {self._telegram.base_url}\n")
        return self._telegram

    def product_argv(self, argv: Sequence[str]) -> list[str]:
        return [sys.executable, "-m", "indus", *[str(self.expand(a)) for a in argv[1:]]]


PARITY_TELEGRAM_USER = 200

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Parity",
    "GIT_AUTHOR_EMAIL": "parity@example.invalid",
    "GIT_COMMITTER_NAME": "Parity",
    "GIT_COMMITTER_EMAIL": "parity@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def prepare(ctx: FlowContext) -> None:
    """The flow's home and project, as its e2e spec sets them up."""
    (ctx.user_home / ".gitconfig").write_text(
        "[user]\n\tname = Parity\n\temail = parity@example.invalid\n"
        "[init]\n\tdefaultBranch = main\n",
        encoding="utf-8",
    )
    if ctx.setup.home == "configured":
        (ctx.home / "config.yaml").write_text(
            "model:\n"
            f"  default: {ctx.setup.script}\n"
            "  provider: custom\n"
            f"  base_url: {ctx.server.base_url}\n",
            encoding="utf-8",
        )
    if ctx.setup.project == "git":
        env = {**ctx.env(), **GIT_IDENTITY}
        origin = ctx.base / "origin.git"

        def git(*args: str, cwd: Path) -> None:
            subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)

        git("init", "-q", "--bare", "-b", "main", str(origin), cwd=ctx.base)
        git("init", "-q", "-b", "main", cwd=ctx.work)
        (ctx.work / "README.md").write_text("# Parity demo\n\nA small project.\n", encoding="utf-8")
        (ctx.work / "notes.txt").write_text("first line\n", encoding="utf-8")
        git("add", ".", cwd=ctx.work)
        git("commit", "-q", "-m", "parity seed", cwd=ctx.work)
        git("remote", "add", "origin", str(origin), cwd=ctx.work)
        git("push", "-q", "origin", "main", cwd=ctx.work)
    seed_chats(ctx)


def seed_chats(ctx: FlowContext) -> None:
    """Run the flow's seed chats in the classic CLI, so pages have sessions to show."""
    for number, message in enumerate(ctx.setup.seed, start=1):
        scenario = {
            "actions": [
                {
                    "step_id": f"seed-{number}-launch",
                    "action": "launch",
                    "argv": ["indus", "--local"],
                },
                {
                    "step_id": f"seed-{number}-ready",
                    "action": "wait_for",
                    "locator": {"kind": "text", "pattern": "ready"},
                    "timeout_ms": 30_000,
                },
                {"step_id": f"seed-{number}-type", "action": "type", "text": message},
                {"step_id": f"seed-{number}-send", "action": "press", "key": "Enter"},
                {
                    "step_id": f"seed-{number}-answer",
                    "action": "wait_for",
                    "locator": {"kind": "text", "pattern": "You said"},
                    "timeout_ms": 30_000,
                },
                {"step_id": f"seed-{number}-quit", "action": "type", "text": "/quit"},
                {"step_id": f"seed-{number}-enter", "action": "press", "key": "Enter"},
                {"step_id": f"seed-{number}-exit", "action": "wait_exit", "timeout_ms": 20_000},
            ]
        }
        steps = run_scripted(ctx, scenario, driver="terminal")
        bad = next((s for s in steps if s["status"] == "failed"), None)
        if bad is not None:
            raise StepFailed(f"seeding failed at {bad['step_id']}: {bad['error']}")


def cleanup(ctx: FlowContext) -> None:
    """Stop a gateway daemon the flow may have started (ours only: this flow's home)."""
    if ctx._telegram is not None:  # pyright: ignore[reportPrivateUsage]
        ctx._telegram.stop()  # pyright: ignore[reportPrivateUsage]
    if not any(ctx.home.glob("**/*.pid")) and not any(ctx.home.glob("**/gateway*")):
        return
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        subprocess.run(
            ctx.product_argv(["indus", "gateway", "stop"]),
            cwd=ctx.work,
            env=ctx.env(),
            capture_output=True,
            timeout=30,
            check=False,
        )


# -- terminal and stdio flows ----------------------------------------------------------------------


class StepFailed(Exception):
    pass


class PlatformSession:
    """``indus gateway run`` against the loopback Bot API fake; waits read the bot's messages."""

    def __init__(
        self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path, fake: FakeBotApi
    ) -> None:
        self.fake = fake
        self.changed = fake.changed
        self.log = (cwd.parent / "gateway.log").open("ab")
        self.proc = subprocess.Popen(  # noqa: S603 - our own product command
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.exit_code: int | None = None

    @property
    def exited(self) -> bool:
        if self.exit_code is None and self.proc.poll() is not None:
            self.exit_code = self.proc.returncode
        return self.exit_code is not None

    def wait_until(self, check: Callable[[], bool], timeout: float, what: str) -> None:
        deadline = time.monotonic() + timeout
        with self.changed:
            while not check():
                if self.exited:
                    raise StepFailed(f"the gateway exited ({self.exit_code}) waiting for {what}")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise StepTimeout(f"timed out after {timeout:g} s waiting for {what}")
                self.changed.wait(min(left, 0.25))

    def wait_exit(self, timeout: float) -> int | None:
        try:
            self.exit_code = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            raise StepTimeout(f"the gateway did not exit within {timeout:g} s") from None
        return self.exit_code

    def matches(self, locator: Mapping[str, Any]) -> list[Sent]:
        op = locator.get("op")
        has = locator.get("has")
        wants_buttons = bool(locator.get("buttons"))
        out: list[Sent] = []
        for sent in list(self.fake.sent):
            if op not in (None, "connect") and sent.op != op:
                continue
            if has is not None and str(has) not in sent.text:
                continue
            if wants_buttons and not sent.buttons:
                continue
            out.append(sent)
        return out

    def stop(self) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, signal.SIGTERM)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.stop()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(5)
        self.log.close()


Session = TerminalSession | StdioSession | PlatformSession


@dataclass
class ScriptRun:
    ctx: FlowContext
    driver: str = "terminal"
    session: Session | None = None
    results: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    last_input: float = 0.0

    def settle(self) -> None:
        wait = SETTLE_SECONDS - (time.monotonic() - self.last_input)
        if wait > 0:
            threading.Event().wait(wait)

    def need(self) -> Session:
        if self.session is None:
            raise StepFailed("no process is running (launch first)")
        return self.session

    def run_step(self, step: Mapping[str, Any]) -> dict[str, Any]:
        action = str(step["action"])
        timeout = int(step.get("timeout_ms", 10_000)) / 1000
        driver = self.driver
        if action == "launch":
            if self.session is not None and not self.session.exited:
                raise StepFailed("the previous process is still running (wait_exit first)")
            if self.session is not None:
                self.session.close()
            argv = self.ctx.product_argv(list(step["argv"]))
            if driver == "platform":
                self.session = PlatformSession(
                    argv, env=self.ctx.env(), cwd=self.ctx.work, fake=self.ctx.telegram()
                )
            elif driver == "stdio":
                self.session = StdioSession(argv, env=self.ctx.env(), cwd=self.ctx.work)
            else:
                self.session = TerminalSession(
                    argv,
                    env=self.ctx.env(),
                    cwd=self.ctx.work,
                    rows=TERMINAL_ENVIRONMENT["rows"],
                    cols=TERMINAL_ENVIRONMENT["columns"],
                    profile=self.ctx.setup.profile,
                )
            return {"argv": [str(a) for a in step["argv"]]}
        session = self.need()
        if action == "type":
            if not isinstance(session, TerminalSession):
                raise StepFailed("type needs a terminal")
            self.settle()
            session.send(str(self.ctx.expand(step["text"])))
            self.last_input = time.monotonic()
            return {}
        if action == "press":
            if not isinstance(session, TerminalSession):
                raise StepFailed("press needs a terminal")
            self.settle()
            session.press(str(step["key"]))
            self.last_input = time.monotonic()
            return {}
        if action == "send":
            if not isinstance(session, StdioSession):
                raise StepFailed("send needs a stdio program")
            message = self.substitute(self.ctx.expand(step["message"]))
            session.send(message)
            return {"method": message.get("method"), "id": message.get("id")}
        if action in ("inbound", "press_button", "stop"):
            if not isinstance(session, PlatformSession):
                raise StepFailed(f"{action} needs the gateway platform driver")
            return self.platform_action(session, action, step)
        if action == "close_input":
            if isinstance(session, StdioSession):
                session.close_input()
            return {}
        if action == "wait_exit":
            code = session.wait_exit(timeout)
            if "code" in step and step["code"] is not None and code != step["code"]:
                raise StepFailed(f"exit code {code}, expected {step['code']}")
            return {"exit_code": code}
        if action == "wait_for":
            return self.wait_for(session, dict(step["locator"]), timeout)
        raise StepFailed(f"unknown action {action!r}")

    def platform_action(
        self, session: PlatformSession, action: str, step: Mapping[str, Any]
    ) -> dict[str, Any]:
        if action == "inbound":
            text = str(self.ctx.expand(dict(step["message"])["text"]))
            session.fake.say(text)
            return {"text": text}
        if action == "stop":
            session.stop()
            return {}
        with_buttons = [s for s in session.fake.sent if s.buttons]
        if not with_buttons:
            raise StepFailed("no message with buttons to press")
        target = with_buttons[-1]
        choice = dict(step["button"])
        if "index" in choice:
            index = int(choice["index"])
            if not 0 <= index < len(target.buttons):
                raise StepFailed(f"no button {index} under message {target.message_id}")
            button = target.buttons[index]
        else:
            label = str(choice.get("label", "")).lower()
            button = next((b for b in target.buttons if label in b["text"].lower()), None)
            if button is None:
                raise StepFailed(f"no button labelled {label!r} in {target.buttons}")
        session.fake.press(target, button["data"])
        return {"pressed": button["text"]}

    def substitute(self, value: Any) -> Any:
        """``{"$result": "<step_id>", "path": ["sessionId"]}`` → that step's response field."""
        if isinstance(value, dict):
            if "$result" in value:
                found: Any = self.results.get(str(value["$result"]), {}).get("message", {})
                for part in value.get("path", []):  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                    found = found.get(part) if isinstance(found, dict) else None  # pyright: ignore[reportUnknownMemberType]
                if found is None:
                    raise StepFailed(f"no result for {value}")
                return found
            return {k: self.substitute(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
        if isinstance(value, list):
            return [self.substitute(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
        return value

    def wait_for(self, session: Session, locator: dict[str, Any], timeout: float) -> dict[str, Any]:
        kind = locator.get("kind")
        if kind == "file":
            target = self.ctx.work / str(locator["path"])
            want = str(locator.get("contains", ""))
            session.wait_until(
                lambda: target.is_file() and want in target.read_text("utf-8", "replace"),
                timeout,
                f"file {locator['path']}",
            )
            return {"file": str(locator["path"])}
        if isinstance(session, PlatformSession):
            nth = int(locator.get("nth", 1))
            if locator.get("op") == "connect":
                session.wait_until(
                    lambda: "getUpdates" in session.fake.calls, timeout, "the gateway to poll"
                )
                return {"shape": ["connected"]}
            session.wait_until(
                lambda: len(session.matches(locator)) >= nth, timeout, f"a bot message {locator}"
            )
            found = session.matches(locator)[nth - 1]
            shape = [found.op, *(["buttons"] if found.buttons else [])]
            return {"message": found.as_dict(), "shape": shape}
        if isinstance(session, StdioSession):
            match = self.message_matcher(locator)
            session.wait_until(
                lambda: session.find(match) is not None, timeout, f"message {locator}"
            )
            found = session.find(match) or {}
            return {"message": found, "shape": message_shape(found)}
        if kind == "text":
            pattern = re.compile(str(self.ctx.expand(locator["pattern"])))
            scope = str(locator.get("scope", "transcript"))
            session.wait_until(
                lambda: pattern.search(session.text(scope)) is not None,
                timeout,
                f"text /{pattern.pattern}/",
            )
        elif kind == "card":
            want = str(locator["card"])
            session.wait_until(
                lambda: any(c["kind"] == want for c in session.snapshot()["cards"]),
                timeout,
                f"a {want} card",
            )
        elif kind == "prompt":
            session.wait_until(lambda: bool(session.snapshot()["prompts"]), timeout, "a prompt")
        else:
            raise StepFailed(f"locator kind {kind!r} needs a stdio program")
        return session.snapshot()

    def message_matcher(self, locator: Mapping[str, Any]) -> Callable[[dict[str, Any]], bool]:
        kind = locator.get("kind")
        if kind == "response":
            sent = self.results.get(str(locator["to"]), {})
            wanted_id = sent.get("id")
            return lambda m: "method" not in m and m.get("id") == wanted_id
        method = locator.get("method")
        has = locator.get("has")

        def match(m: dict[str, Any]) -> bool:
            if m.get("method") != method:
                return False
            return has is None or str(has) in json.dumps(m)

        return match


def message_shape(message: Mapping[str, Any]) -> list[str]:
    """Key paths of a JSON message (values left out), for structural comparison."""
    out: list[str] = []

    def walk(value: Any, prefix: str) -> None:
        if isinstance(value, dict):
            for key in sorted(value):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
                walk(value[key], f"{prefix}.{key}" if prefix else str(key))  # pyright: ignore[reportUnknownArgumentType]
        elif isinstance(value, list):
            for item in value[:3]:  # pyright: ignore[reportUnknownVariableType]
                walk(item, f"{prefix}[]")
        else:
            out.append(prefix)

    walk(message, "")
    return sorted(set(out))


def run_scripted(
    ctx: FlowContext, scenario: Mapping[str, Any], driver: str | None = None
) -> list[dict[str, Any]]:
    run = ScriptRun(ctx, driver=driver or str(scenario.get("driver") or ctx.setup.driver))
    steps: list[dict[str, Any]] = []
    failed = False
    try:
        for step in steps_of(scenario):
            record: dict[str, Any] = {"step_id": step["step_id"], "action": step["action"]}
            if failed:
                steps.append({**record, "status": "not_run", "error": None, "elapsed_ms": 0})
                continue
            started = time.monotonic()
            try:
                observed = run.run_step(step)
                record.update(status="completed", error=None, observed=observed)
                if step["action"] == "send":
                    run.results[str(step["step_id"])] = observed
                if step["action"] == "wait_for" and observed.get("message") is not None:
                    run.results[str(step["step_id"])] = {"message": observed["message"]}
            except (StepFailed, StepTimeout, ValueError, OSError) as exc:
                failed = True
                record.update(status="failed", error=str(exc))
                if isinstance(run.session, TerminalSession):
                    record["observed"] = run.session.snapshot()
                elif isinstance(run.session, StdioSession):
                    record["observed"] = {"stderr": run.session.stderr_tail[-10:]}
            record["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            steps.append(record)
    finally:
        if run.session is not None:
            run.session.close()
    return steps


# -- browser flows ---------------------------------------------------------------------------------


def rewrite_origin(scenario: Mapping[str, Any], origin: str, browser: str | None) -> dict[str, Any]:
    """The copy REA runs: canonical origin swapped for ``origin``, the local browser filled in."""
    text = json.dumps(scenario).replace(CANONICAL_ORIGIN, origin)
    data: dict[str, Any] = json.loads(text)
    current = data.get("browser")
    if browser is not None and isinstance(current, dict) and current.get("mode") == "launch":  # pyright: ignore[reportUnknownMemberType]
        data["browser"] = {"mode": "launch", "executable_path": browser, "headless": True}
    return data


def browser_steps(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Our step records from REA's normalized result (accessibility text kept for roles)."""
    out: list[dict[str, Any]] = []
    for step in result.get("steps", []):
        if step.get("step_id") == "scenario_start":
            continue
        accessibility = (step.get("artifacts") or {}).get("accessibility") or {}
        text = (accessibility.get("value") or {}).get("text")
        out.append(
            {
                "step_id": step.get("step_id"),
                "action": step.get("action"),
                "status": step.get("status"),
                "error": step.get("error"),
                "elapsed_ms": step.get("elapsed_ms", 0),
                "observed": {"accessibility": text} if text is not None else {},
            }
        )
    return out


def page_errors(result: Mapping[str, Any]) -> list[str]:
    items = ((result.get("events") or {}).get("items")) or []
    return [str(e.get("message")) for e in items if e.get("kind") == "page-error"]


@contextlib.contextmanager
def dashboard(ctx: FlowContext) -> Iterator[str]:
    """`indus dashboard` on the flow's port, until the block ends (its process group only)."""
    port = ctx.options.port
    proc = subprocess.Popen(  # noqa: S603 - our own product command
        ctx.product_argv(
            ["indus", "dashboard", "--host", "127.0.0.1", "--port", str(port), "--no-open"]
        ),
        cwd=ctx.work,
        env=ctx.env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    origin = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen(f"{origin}/api/health", timeout=2):  # noqa: S310 - loopback
                    break
            except (urllib.error.URLError, OSError):
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise StepFailed("the dashboard did not come up") from None
                threading.Event().wait(0.25)
        yield origin
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(5)


def write_rea_input(ctx: FlowContext, scenario: Mapping[str, Any]) -> Path:
    """The scenario as REA should run it on this machine, in the cache."""
    path = ctx.options.cache / "indus" / f"{ctx.setup.flow}.rea-input.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    prepared = rewrite_origin(scenario, f"http://127.0.0.1:{ctx.options.port}", ctx.options.browser)
    path.write_text(json.dumps(prepared, indent=2) + "\n", encoding="utf-8")
    return path


def run_browser(
    ctx: FlowContext, inputs: Path
) -> tuple[str, str, list[dict[str, Any]], dict[str, Any]]:
    """(status, reason, steps, extra) of one browser flow handed to the REA command."""
    if ctx.setup.surface != "dashboard":
        return "not_run", "only dashboard flows have a product server in this runner", [], {}
    assert ctx.options.rea_cli
    with dashboard(ctx):
        done = subprocess.run(  # noqa: S603 - the operator's REA command
            [*ctx.options.rea_cli, str(inputs)],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    if done.returncode != 0:
        return "failed", f"REA exited {done.returncode}: {done.stderr.strip()[-400:]}", [], {}
    result: dict[str, Any] = json.loads(done.stdout)
    result = result.get("normalized_result", result)
    steps = browser_steps(result)
    errors = page_errors(result)
    bad = [s for s in steps if s["status"] != "completed"]
    status = "failed" if bad or errors else "passed"
    reason = bad[0]["error"] if bad else ("uncaught page error" if errors else "")
    return status, str(reason or ""), steps, {"page_errors": errors}


# -- one flow, many flows --------------------------------------------------------------------------


def missing_needs(setup: FlowSetup, options: RunOptions) -> list[str]:
    """Prerequisites this machine lacks for ``setup`` (each with what to do)."""
    out: list[str] = []
    for need in setup.needs:
        if need == "tui-bundle":
            built = (ROOT / "apps" / "tui" / "dist" / "entry.js").is_file() or (
                ROOT / "src" / "indus" / "_tui_dist" / "entry.js"
            ).is_file()
            if not built or shutil.which("node") is None:
                out.append("TUI bundle not built (pnpm --filter @indus/tui build) or no node")
        elif need == "web-bundle":
            from indus.web.bundle import locate

            if not locate().found:
                out.append("dashboard bundle not built (pnpm --filter @indus/web build)")
        elif need == "rea":
            if not options.rea_cli:
                out.append("REA capture not reachable from the runner (no --rea-cli)")
        elif need == "desktop-build":
            out.append("needs the packaged desktop app and REA's CDP connect mode (owner lane)")
    return out


@dataclass
class FlowResult:
    flow: str
    status: str  # passed | failed | blocked | not_run
    reason: str = ""
    capture: Path | None = None
    steps: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


def versions() -> dict[str, str]:
    from indus import __version__

    return {"indus": __version__, "python": sys.version.split()[0]}


def write_capture(
    options: RunOptions,
    setup: FlowSetup,
    *,
    status: str,
    reason: str,
    steps: list[dict[str, Any]],
    extra: Mapping[str, Any],
    started: float,
    model_requests: int,
) -> Path:
    capture = {
        "format": CAPTURE_FORMAT,
        "flow": setup.flow,
        "side": "indus",
        "driver": setup.driver,
        "surface": setup.surface,
        "script": setup.script,
        "scenario_sha256": hashlib.sha256(
            scenario_path(setup.flow, options.scenarios).read_bytes()
        ).hexdigest(),
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "versions": versions(),
        "status": status,
        "reason": reason,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "model_requests": model_requests,
        "steps": steps,
        **extra,
    }
    out = options.cache / "indus" / f"{setup.flow}.capture.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(capture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def import_capture(flow: str, result_file: Path, options: RunOptions) -> FlowResult:
    """Turn a REA ``capture_browser_scenario`` result (run through REA's MCP tools against a
    ``--serve`` session) into this flow's product capture."""
    setup = FLOW_BY_NAME[flow]
    raw: dict[str, Any] = json.loads(result_file.read_text(encoding="utf-8"))
    result: dict[str, Any] = raw.get("normalized_result", raw)
    steps = browser_steps(result)
    errors = page_errors(result)
    bad = [s for s in steps if s["status"] != "completed"]
    status = "failed" if bad or errors else "passed"
    reason = str(bad[0]["error"]) if bad else ("uncaught page error" if errors else "")
    extra = {
        "page_errors": errors,
        "rea": {"browser": result.get("browser"), "limitations": result.get("limitations", [])},
    }
    out = write_capture(
        options,
        setup,
        status=status,
        reason=reason,
        steps=steps,
        extra=extra,
        started=time.monotonic() - result.get("duration_ms", 0) / 1000,
        model_requests=0,
    )
    return FlowResult(flow, status, reason, out, steps)


def serve_flow(flow: str, options: RunOptions, until: threading.Event) -> int:
    """Set a browser flow up and keep its product server running until ``until`` is set, so a
    person or agent can run the prepared input through REA's MCP tools (then ``--import``)."""
    setup = FLOW_BY_NAME[flow]
    if setup.surface != "dashboard":
        print(f"{flow}: only dashboard flows have a server here", file=sys.stderr)
        return 2
    scenario = load_scenario(flow, options.scenarios)
    base = Path(tempfile.mkdtemp(prefix=f"parity-{flow}-"))
    try:
        (base / "project").mkdir()
        with ScriptedModelServer(settings=Settings(edit_root=base / "project")) as server:
            ctx = FlowContext(setup, base, server, options)
            prepare(ctx)
            try:
                with dashboard(ctx) as origin:
                    inputs = write_rea_input(ctx, scenario)
                    print(json.dumps({"flow": flow, "origin": origin, "rea_input": str(inputs)}))
                    sys.stdout.flush()
                    until.wait()
            finally:
                cleanup(ctx)
    finally:
        if not options.keep:
            shutil.rmtree(base, ignore_errors=True)
    return 0


def run_flow(flow: str, options: RunOptions) -> FlowResult:
    setup = FLOW_BY_NAME.get(flow)
    if setup is None:
        return FlowResult(flow, "failed", "unknown flow (see --list)")
    try:
        scenario = load_scenario(flow, options.scenarios)
    except (OSError, ValueError) as exc:
        return FlowResult(flow, "failed", f"scenario unreadable: {exc}")
    problems = scenario_problems(flow, scenario)
    if problems:
        return FlowResult(flow, "failed", "scenario invalid: " + "; ".join(problems[:5]))
    missing = missing_needs(setup, options)
    if setup.driver == "browser" and options.dry_run:
        missing = [*missing, "dry run: browser flows only prepare their input"]
    started = time.monotonic()
    base = Path(tempfile.mkdtemp(prefix=f"parity-{flow}-"))
    log_lines: list[dict[str, Any]] = []
    try:
        settings = Settings(edit_root=base / "project")
        (base / "project").mkdir()
        with ScriptedModelServer(settings=settings, log=log_lines.append) as server:
            ctx = FlowContext(setup, base, server, options)
            steps: list[dict[str, Any]] = []
            extra: dict[str, Any] = {}
            if setup.driver == "browser":
                extra["rea_input"] = str(write_rea_input(ctx, scenario))
            if missing:
                only_rea = all("REA" in m for m in missing)
                status, reason = ("blocked" if only_rea else "not_run"), "; ".join(missing)
            else:
                prepare(ctx)
                try:
                    if setup.driver == "browser":
                        status, reason, steps, more = run_browser(ctx, Path(extra["rea_input"]))
                        extra.update(more)
                    else:
                        steps = run_scripted(ctx, scenario)
                        bad = next((s for s in steps if s["status"] == "failed"), None)
                        status = "failed" if bad else "passed"
                        reason = f"{bad['step_id']}: {bad['error']}" if bad else ""
                finally:
                    cleanup(ctx)
        out = write_capture(
            options,
            setup,
            status=status,
            reason=reason,
            steps=steps,
            extra=extra,
            started=started,
            model_requests=sum(1 for e in log_lines if e.get("event") == "request"),
        )
        return FlowResult(flow, status, reason, out, steps)
    finally:
        if not options.keep:
            shutil.rmtree(base, ignore_errors=True)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="runner.py", description="Run the parity flows on Indus Agent (product side)."
    )
    parser.add_argument("flows", nargs="*", help="flow names (see --list)")
    parser.add_argument("--list", action="store_true", help="print the flow setup table")
    parser.add_argument("--all", action="store_true", help="run every flow")
    parser.add_argument("--dry-run", action="store_true", help="product only: no REA, no compare")
    parser.add_argument("--cache", type=Path, default=CACHE_DIR)
    parser.add_argument("--scenarios", type=Path, default=SCENARIO_DIR)
    parser.add_argument("--port", type=int, default=CANONICAL_PORT)
    parser.add_argument("--browser", default=os.environ.get("INDUS_PARITY_BROWSER"))
    parser.add_argument("--rea-cli", default=os.environ.get("INDUS_PARITY_REA"))
    parser.add_argument("--ref", type=Path, help="reference captures (from ~/rea-lab) to compare")
    parser.add_argument("--out", type=Path, help="where comparisons go (default <cache>/compare)")
    parser.add_argument("--ledger", action="store_true", help="print the obligation ledger after")
    parser.add_argument("--keep", action="store_true", help="keep each flow's temp folder")
    parser.add_argument(
        "--serve", metavar="FLOW", help="set up a dashboard flow and serve it until stdin closes"
    )
    parser.add_argument(
        "--import",
        dest="import_from",
        nargs=2,
        metavar=("FLOW", "RESULT"),
        help="store a REA capture_browser_scenario result as FLOW's product capture",
    )
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.list:
        print(flow_table())
        return 0
    options = RunOptions(
        cache=args.cache,
        scenarios=args.scenarios,
        port=args.port,
        browser=args.browser,
        rea_cli=shlex.split(args.rea_cli) if args.rea_cli else None,
        dry_run=args.dry_run,
        keep=args.keep,
    )
    if args.import_from:
        flow, result_file = args.import_from
        if flow not in FLOW_BY_NAME:
            print(f"unknown flow {flow!r}", file=sys.stderr)
            return 2
        imported = import_capture(flow, Path(result_file), options)
        print(f"{imported.status:8} {flow}" + (f"  ({imported.reason})" if imported.reason else ""))
        return 1 if imported.status == "failed" else 0
    if args.serve:
        if args.serve not in FLOW_BY_NAME:
            print(f"unknown flow {args.serve!r}", file=sys.stderr)
            return 2
        stop = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stop.set())
        threading.Thread(target=lambda: (sys.stdin.read(), stop.set()), daemon=True).start()
        return serve_flow(args.serve, options, stop)
    flows = [f.flow for f in FLOWS] if args.all else list(args.flows)
    if not flows:
        print("name flows, or --all, or --list", file=sys.stderr)
        return 2
    unknown = [f for f in flows if f not in FLOW_BY_NAME]
    if unknown:
        print(f"unknown flow(s): {', '.join(unknown)} (see --list)", file=sys.stderr)
        return 2
    results: list[FlowResult] = []
    for flow in flows:  # strictly one flow at a time
        result = run_flow(flow, options)
        results.append(result)
        line = f"{result.status:8} {flow}"
        print(line + (f"  ({result.reason})" if result.reason else ""), flush=True)
    worst = 1 if any(r.status == "failed" for r in results) else 0
    if not args.dry_run and args.ref is not None:
        from scripts.parity import compare

        out = args.out or (options.cache / "compare")
        unclassified = compare.compare_all(options.cache / "indus", args.ref, out, flows)
        if unclassified:
            print(f"{unclassified} divergence(s) to classify (see {out})")
            worst = 1
    if args.ledger:
        from scripts.parity import ledger

        print(ledger.report(options.cache, args.ref, args.out or options.cache / "compare"))
    return worst


__all__ = [
    "CANONICAL_ORIGIN",
    "FLOWS",
    "FLOW_BY_NAME",
    "HOOK_ATTRIBUTES",
    "KEYS",
    "SHARED_ENVIRONMENT",
    "FlowResult",
    "FlowSetup",
    "RunOptions",
    "flow_table",
    "load_scenario",
    "main",
    "rea_browser_problems",
    "run_flow",
    "scenario_problems",
    "step_ids",
    "steps_of",
]

if __name__ == "__main__":
    raise SystemExit(main())
