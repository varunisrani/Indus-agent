"""P19-01: ``indus acp`` as a real process — informational flags, stdout purity, probes, setup."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from indus.acp import entry
from indus.acp.stdout_guard import StrayWriter

TIMEOUT = 60


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("INDUS_")}
    home = tmp_path / "indus-home"
    user = tmp_path / "user-home"
    home.mkdir(exist_ok=True)
    user.mkdir(exist_ok=True)
    env.update({"INDUS_HOME": str(home), "HOME": str(user), "USERPROFILE": str(user)})
    env.pop("OPENROUTER_API_KEY", None)
    env.update(extra)
    return env


def _indus(
    tmp_path: Path, *args: str, stdin: str = "", **extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "indus", *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=_env(tmp_path, **extra),
        cwd=tmp_path,
        timeout=TIMEOUT,
        check=False,
    )


def test_version_prints_and_exits(tmp_path: Path) -> None:
    done = _indus(tmp_path, "acp", "--version")
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("Indus Agent ")


def test_check_imports_the_sdk_and_the_adapter(tmp_path: Path) -> None:
    done = _indus(tmp_path, "acp", "--check")
    assert done.returncode == 0, done.stderr
    assert "ACP check OK" in done.stdout and "agent-client-protocol 0.12.1" in done.stdout


def test_check_without_the_sdk_exits_1_with_a_hint(tmp_path: Path) -> None:
    probe = (
        "import sys\n"
        "sys.modules['acp'] = None\n"
        "from indus.cli.entry import main\n"
        "raise SystemExit(main(['acp', '--check']))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=_env(tmp_path),
        cwd=tmp_path,
        timeout=TIMEOUT,
        check=False,
    )
    assert done.returncode == 1
    assert "acp` extra" in done.stderr
    assert "Traceback" not in done.stderr


SERVER = """
import sys
from indus.acp.agent import handler

@handler("test/print")
async def noisy(agent, params):
    print("this must never reach the protocol stream")
    sys.stdout.write("nor this")
    return {"ok": True}

from indus.cli.entry import main
raise SystemExit(main(["acp", "--local"]))
"""


def _frames(*messages: dict[str, object]) -> str:
    return "".join(json.dumps(m) + "\n" for m in messages)


def test_stdout_carries_frames_only_and_probes_are_quiet(tmp_path: Path) -> None:
    requests = _frames(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}},
        {"jsonrpc": "2.0", "id": 2, "method": "test/print", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}},
        {"jsonrpc": "2.0", "id": 4, "method": "healthcheck"},
    )
    done = subprocess.run(
        [sys.executable, "-c", SERVER],
        input=requests,
        capture_output=True,
        text=True,
        env=_env(tmp_path),
        cwd=tmp_path,
        timeout=TIMEOUT,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    frames = [json.loads(line) for line in lines]  # every stdout line is a JSON-RPC frame
    assert all(f.get("jsonrpc") == "2.0" for f in frames)
    by_id = {f["id"]: f for f in frames}
    assert by_id[1]["result"]["agentInfo"]["name"] == "indus-agent"
    assert by_id[2]["result"] == {"ok": True}
    assert by_id[3]["error"]["code"] == -32601
    assert by_id[4]["error"]["code"] == -32601
    assert "never reach" not in done.stdout + done.stderr
    assert "dropped" in done.stderr  # the stray write is reported (length only)
    assert "Traceback" not in done.stderr


def test_setup_runs_indus_setup_as_a_child(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "argv.txt"
    fake = bin_dir / "indus"
    fake.write_text(f'#!/bin/sh\necho "$@" >> "{record}"\nexit 0\n')
    fake.chmod(0o755)
    path = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    done = _indus(tmp_path, "acp", "--setup", PATH=path)
    assert done.returncode == 0, done.stderr
    assert record.read_text().split("\n")[0] == "setup"  # no TTY: no browser question


def test_setup_offers_the_browser_tools_on_a_terminal() -> None:
    calls: list[list[str]] = []

    class Done:
        def __init__(self, code: int) -> None:
            self.returncode = code

    def run_child(argv: list[str], check: bool) -> Any:
        del check
        calls.append(argv[-3:] if "tools" in argv else argv[-1:])
        return Done(0)

    class Tty:
        def isatty(self) -> bool:
            return True

    terminal: Any = Tty()
    code = entry.setup(run_child=run_child, stdin=terminal, ask=lambda _q: "y")
    assert code == 0
    assert calls == [["setup"], ["tools", "install", "browser"]]
    calls.clear()
    assert entry.setup(run_child=run_child, stdin=terminal, ask=lambda _q: "") == 0
    assert calls == [["setup"]]


def test_the_stray_writer_drops_text_and_reports_its_length(
    caplog: pytest.LogCaptureFixture,
) -> None:
    writer = StrayWriter()
    assert writer.write("secret-ish words") == 16
    assert writer.dropped == 16
    assert "16 characters" in caplog.text and "secret-ish" not in caplog.text
