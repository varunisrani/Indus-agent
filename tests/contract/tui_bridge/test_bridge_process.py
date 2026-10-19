"""The real ``indus tui-bridge --stdio`` child (P12-28, ACC-J2-1/J2-2): ready frame first, stdout
holds only frames, garbage is answered, the loop survives, end of input exits 0."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from indus.protocol.errors import PARSE_ERROR, PROVIDER_UNAVAILABLE
from indus.protocol.methods import PROTOCOL_VERSION

ROOT = Path(__file__).resolve().parents[3]


def frame(rid: int, method: str, **params: Any) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})


def run_bridge(
    *lines: str, args: tuple[str, ...] = ("tui-bridge", "--stdio")
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["INDUS_PROTOCOL_STRICT"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "indus", *args],
        input="".join(line + "\n" for line in lines),
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        timeout=120,
    )


def test_real_child_speaks_ndjson(isolated_home: Path) -> None:
    done = run_bridge(
        "this is not json",
        "",
        frame(1, "config.get", key="theme"),
        frame(2, "commands.catalog"),
        frame(3, "session.list", limit=5),
        frame(4, "client.capabilities", features=["server_requests", "made_up"]),
        frame(5, "session.create"),
        frame(6, "shell.exec", command="echo from-child"),
    )
    assert done.returncode == 0, done.stderr
    frames = [json.loads(line) for line in done.stdout.splitlines()]  # every line is a frame
    assert frames[0]["method"] == "gateway.ready"
    assert frames[0]["params"]["protocol_version"] == PROTOCOL_VERSION
    assert frames[0]["params"]["heartbeat"] is False
    errors = [f for f in frames if f.get("id") is None and "error" in f]
    assert errors and errors[0]["error"]["code"] == PARSE_ERROR.code
    answers = {f["id"]: f for f in frames if f.get("id") is not None}
    assert answers[1]["result"] == {"key": "theme", "value": "auto"}
    names = {entry["name"] for entry in answers[2]["result"]["commands"]}
    assert {"title", "quit", "help"} <= names
    assert answers[3]["result"] == {"sessions": []}
    assert answers[4]["result"] == {"features": ["server_requests"]}
    created = answers[5]
    assert "result" in created or created["error"]["code"] == PROVIDER_UNAVAILABLE.code
    assert answers[6]["result"] == {"stdout": "from-child\n", "stderr": "", "code": 0}


def test_bridge_is_hidden_from_help(isolated_home: Path) -> None:
    shown = run_bridge(args=("--help",))
    assert shown.returncode == 0 and "tui-bridge" not in shown.stdout
    bare = run_bridge(args=("tui-bridge",))
    assert bare.returncode == 2 and "--stdio" in bare.stderr


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_sigint_is_ignored_and_sigterm_closes_cleanly(isolated_home: Path) -> None:
    child = subprocess.Popen(
        [sys.executable, "-m", "indus", "tui-bridge", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=ROOT,
        text=True,
    )
    try:
        assert child.stdin is not None and child.stdout is not None
        assert json.loads(child.stdout.readline())["method"] == "gateway.ready"
        child.send_signal(signal.SIGINT)
        child.stdin.write(frame(1, "config.get", key="mtime") + "\n")
        child.stdin.flush()
        answer = json.loads(child.stdout.readline())
        assert answer["id"] == 1 and "result" in answer
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=30) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
