"""P18-01: ``indus serve ensure`` / ``discover`` against a real headless backend on loopback.

Every server runs in a temp ``INDUS_HOME`` with a temp ``HOME``, binds 127.0.0.1 on a port the OS
picks, and is stopped by the test (its pid is SIGTERMed and waited for).
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from indus.service.ws_auth import stable_profile_id
from indus.state import gateway_runtime
from indus.web import serve_runtime

pytestmark = pytest.mark.skipif(os.name != "posix", reason="detached start needs POSIX")


def _stop(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    waited = threading.Event()
    for _ in range(100):
        if not gateway_runtime.pid_alive(pid):
            return
        waited.wait(0.1)
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[Path, dict[str, str], list[int]]]:
    home = tmp_path / "indus-home"
    home.mkdir(mode=0o700)
    user = tmp_path / "user-home"
    user.mkdir()
    token = secrets.token_urlsafe(32)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(user),
        "INDUS_HOME": str(home),
        "INDUS_DASHBOARD_SESSION_TOKEN": token,
        "LANG": "C.UTF-8",
    }
    pids: list[int] = []
    yield home, env, pids
    for pid in pids:
        _stop(pid)
    record = serve_runtime.read_record(serve_runtime.serve_record_path(home))
    if record is not None:
        _stop(record.pid)


def test_ensure_twice_starts_one_server(served: tuple[Path, dict[str, str], list[int]]) -> None:
    home, env, pids = served
    assert serve_runtime.discover(home) is None
    first = serve_runtime.ensure(home, env, timeout=60)
    assert isinstance(first, serve_runtime.Served), first
    pids.append(first.pid)
    assert first.started and first.url.startswith("http://127.0.0.1:")
    assert first.profile_id == stable_profile_id(home)
    second = serve_runtime.ensure(home, env, timeout=60)
    assert isinstance(second, serve_runtime.Served)
    assert second.pid == first.pid and not second.started
    # the caller's token works; the backend never shows HTML
    token = env["INDUS_DASHBOARD_SESSION_TOKEN"]
    with httpx.Client(trust_env=False, timeout=5) as client:
        me = client.get(first.url + "/api/auth/me", headers={"X-Indus-Session-Token": token})
        assert me.status_code == 200 and me.json()["provider"] == "loopback"
        page = client.get(first.url + "/")
        assert page.status_code == 404 and "html" not in page.headers["content-type"]


def test_cli_prints_no_token(served: tuple[Path, dict[str, str], list[int]]) -> None:
    home, env, pids = served
    done = subprocess.run(
        [sys.executable, "-m", "indus", "serve", "ensure", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=str(home),
    )
    assert done.returncode == 0, done.stderr
    answer = json.loads(done.stdout.strip().splitlines()[-1])
    pids.append(int(answer["pid"]))
    assert set(answer) == {"url", "port", "pid", "profile_id", "code_version"}
    token = env["INDUS_DASHBOARD_SESSION_TOKEN"]
    assert token not in done.stdout and token not in done.stderr
    record_text = serve_runtime.serve_record_path(home).read_text(encoding="utf-8")
    assert token not in record_text
    found = subprocess.run(
        [sys.executable, "-m", "indus", "serve", "discover", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=str(home),
    )
    assert found.returncode == 0
    assert json.loads(found.stdout.strip().splitlines()[-1])["pid"] == answer["pid"]
    assert token not in found.stdout


def test_a_stale_code_version_restarts_once(served: tuple[Path, dict[str, str], list[int]]) -> None:
    home, env, pids = served
    first = serve_runtime.ensure(home, env, timeout=60)
    assert isinstance(first, serve_runtime.Served), first
    pids.append(first.pid)
    newer = serve_runtime.ensure(home, env, timeout=60, version="99.0.0")
    assert isinstance(newer, serve_runtime.Served), newer
    pids.append(newer.pid)
    assert newer.restarted and newer.pid != first.pid
    assert not gateway_runtime.pid_alive(first.pid)
    # the new one still runs this tree's version: accepted, not restarted again
    assert newer.code_version != "99.0.0"
    again = serve_runtime.ensure(home, env, timeout=60)
    assert isinstance(again, serve_runtime.Served) and again.pid == newer.pid
