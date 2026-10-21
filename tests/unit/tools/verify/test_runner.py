"""P24-17: the phase runner over a scripted backend, the start/readiness step on a fake clock,
and ``indus verify`` on a real workspace with the local backend."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from indus.backends.contract import ExecResult
from indus.foundation.scope import Scope
from indus.tools.verify.recipes import Recipe
from indus.tools.verify.runner import (
    PHASE_TIMEOUT_S,
    READY_TIMEOUT_S,
    TAIL_CHARS,
    run_verify,
)
from tests.conftest import FAKE_CLOCK_START
from tests.fakes.clock import FakeClock
from tests.fakes.terminal import ScriptedEnv
from tests.unit.cli.commands._helpers_d import run

WORKSPACES = Path(__file__).resolve().parents[3] / "fixtures" / "verify" / "workspaces"


def ok(output: str = "") -> ExecResult:
    return ExecResult(output=output, returncode=0)


def recipe(**changes: object) -> Recipe:
    base = Recipe(
        "Demo",
        "make",
        bootstrap=["make install"],
        build=["make build"],
        test=["make test", "make lint"],
        start="make serve",
        port=9000,
    )
    for key, value in changes.items():
        setattr(base, key, value)
    return base


def test_numbers_match_the_row() -> None:
    assert (PHASE_TIMEOUT_S, READY_TIMEOUT_S, TAIL_CHARS) == (600.0, 60.0, 2000)


def test_phases_run_in_order_in_the_workspace() -> None:
    env = ScriptedEnv([ok("i"), ok("b"), ok("t"), ok("l")])
    result = asyncio.run(run_verify(env, "/ws", recipe()))
    assert [c.command for c in env.calls] == [
        "make install",
        "make build",
        "make test",
        "make lint",
    ]
    assert all(c.cwd == "/ws" and c.timeout == 600.0 and c.bounded is False for c in env.calls)
    assert result.ok and [p.phase for p in result.phases] == ["bootstrap", "build", "test", "test"]
    assert result.readiness is None  # no starter given: nothing boots


def test_first_failure_stops_the_run_and_keeps_the_tail() -> None:
    long = "x" * 5000 + "THE END"
    env = ScriptedEnv([ok(), ExecResult(output=long, returncode=2)])
    result = asyncio.run(run_verify(env, "/ws", recipe()))
    assert not result.ok and len(env.calls) == 2
    failed = result.phases[-1]
    assert (failed.phase, failed.exit_code, failed.ok) == ("build", 2, False)
    assert len(failed.tail) == TAIL_CHARS and failed.tail.endswith("THE END")
    keep_going = ScriptedEnv([ok(), ExecResult(output="", returncode=1), ok(), ok()])
    whole = asyncio.run(run_verify(keep_going, "/ws", recipe(), stop_on_failure=False))
    assert len(whole.phases) == 4 and not whole.ok


def test_phase_timeout_is_reported() -> None:
    env = ScriptedEnv([ExecResult(output="slow", returncode=124, timed_out=True)])
    result = asyncio.run(run_verify(env, "/ws", recipe(), phases=["bootstrap"], phase_timeout=5.0))
    only = result.phases[0]
    assert only.timed_out and only.exit_code is None and not result.ok
    assert env.calls[0].timeout == 5.0
    assert only.as_dict()["timed_out"] is True


def test_phase_selection() -> None:
    env = ScriptedEnv()
    result = asyncio.run(run_verify(env, "/ws", recipe(), phases=["test"]))
    assert [c.command for c in env.calls] == ["make test", "make lint"]
    assert result.ok


def test_a_refused_command_never_runs() -> None:
    env = ScriptedEnv()
    asked: list[tuple[str, str]] = []

    async def gate(command: str, cwd: str) -> str | None:
        asked.append((command, cwd))
        return "it would delete files" if command == "make build" else None

    result = asyncio.run(run_verify(env, "/ws", recipe(), gate=gate))
    assert [c.command for c in env.calls] == ["make install"]
    assert result.phases[-1].refused == "it would delete files" and not result.ok
    assert asked[-1] == ("make build", "/ws")


@pytest.mark.parametrize(
    ("probe", "refused"),
    [
        (ExecResult(output="proj-web-1\nproj-db-1\n", returncode=0), "proj-web-1, proj-db-1"),
        (ExecResult(output="", returncode=0), None),
        (ExecResult(output="bash: docker: command not found", returncode=127), None),
        (ExecResult(output="Cannot connect to the Docker daemon", returncode=1), "exit 1"),
        (ExecResult(output="", returncode=124, timed_out=True), "did not answer"),
    ],
)
def test_compose_with_running_containers_is_refused(probe: ExecResult, refused: str | None) -> None:
    env = ScriptedEnv([probe, ok()])
    compose = Recipe("Compose project", "compose", build=["docker compose build"])
    result = asyncio.run(run_verify(env, "/ws", compose))
    if refused is None:
        assert result.ok and env.calls[-1].command == "docker compose build"
    else:
        assert result.refused is not None and refused in result.refused
        assert not result.ok and len(env.calls) == 1


class FakeStarter:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []
        self.stopped = 0

    async def start(self, command: str, cwd: str) -> object:
        self.started.append((command, cwd))
        return "handle"

    async def stop(self, handle: object) -> str:
        assert handle == "handle"
        self.stopped += 1
        return "server log line\n"


@pytest.mark.parametrize("answers_after", [3, None])
def test_readiness_polled_on_loopback_until_the_deadline(answers_after: int | None) -> None:
    clock = FakeClock(FAKE_CLOCK_START)
    starter = FakeStarter()
    asked: list[str] = []

    async def probe(url: str) -> int | None:
        asked.append(url)
        return 200 if answers_after is not None and len(asked) >= answers_after else None

    async def scenario() -> None:
        task = asyncio.ensure_future(
            run_verify(ScriptedEnv(), "/ws", recipe(), clock=clock, starter=starter, probe=probe)
        )
        while not task.done():
            await clock.advance_and_settle(1.0)
        result = task.result()
        assert starter.started == [("make serve", "/ws")] and starter.stopped == 1
        readiness = result.readiness
        assert readiness is not None and readiness.url == "http://127.0.0.1:9000/"
        if answers_after is None:
            assert not readiness.ready and not result.ok
            assert len(asked) == 61  # one probe a second, at 0 s through 60 s
            assert readiness.tail == "server log line\n"
        else:
            assert readiness.ready and readiness.status == 200 and result.ok

    asyncio.run(scenario())


# -- indus verify ---------------------------------------------------------------------------------


def test_cli_runs_a_makefile_workspace(scope: Scope, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    shutil.copytree(WORKSPACES / "make_test", ws)
    result = run(scope, "verify", str(ws), "--skip-start", "--json")
    assert result.code == 0, result.err
    payload = json.loads(result.out)
    assert payload["ok"] and [p["command"] for p in payload["phases"]] == [
        "make build",
        "make test",
    ]
    assert "ok" in payload["phases"][1]["tail"]

    (ws / "Makefile").write_text("test:\n\t@echo broken; exit 3\n", encoding="utf-8")
    failed = run(scope, "verify", str(ws), "--phase", "test")
    assert failed.code == 1
    assert "FAIL" in failed.out and "broken" in failed.out and "Result: FAILED" in failed.out


def test_cli_detect_only_no_recipe_and_bad_path(scope: Scope, tmp_path: Path) -> None:
    detected = run(scope, "verify", str(WORKSPACES / "go_mod"), "--detect-only", "--json")
    assert detected.code == 0 and json.loads(detected.out)["recipe"]["kind"] == "go"
    empty = tmp_path / "empty"
    empty.mkdir()
    nothing = run(scope, "verify", str(empty))
    assert nothing.code == 1 and "Nothing here says how" in nothing.err
    as_json = run(scope, "verify", str(empty), "--json")
    assert json.loads(as_json.out)["error"] == "no_recipe"
    missing = run(scope, "verify", str(tmp_path / "nope"))
    assert missing.code == 2


def test_local_starter_and_loopback_probe(tmp_path: Path) -> None:
    import os

    from indus.tools.verify.runner import LocalStarter, http_probe

    async def scenario() -> None:
        async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readline()
            writer.write(b"HTTP/1.0 204 No Content\r\n\r\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(answer, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            assert await http_probe(f"http://127.0.0.1:{port}/health") == 204
        finally:
            server.close()
            await server.wait_closed()
        assert await http_probe(f"http://127.0.0.1:{port}/") is None  # nothing listens now
        with pytest.raises(ValueError, match="loopback"):
            await http_probe("http://example.com/")

        starter = LocalStarter({"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
        handle = await starter.start("echo started; touch ready; exec sleep 30", str(tmp_path))
        pid = handle.pid  # type: ignore[attr-defined]
        from tests.unit.tools.lsp._helpers import wait_until

        await wait_until(lambda: (tmp_path / "ready").exists())
        output = await starter.stop(handle)
        assert "started" in output
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    asyncio.run(scenario())
