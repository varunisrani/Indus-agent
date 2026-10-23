"""W50-b: the owner's own shell command over the attach socket (``shell.exec``, quick ``exec``).

An attached TUI's ``!command`` and quick ``exec`` commands run on the daemon with the rules
``--local`` applies and nothing looser: the same refusals (dangerous-command detection, an empty
command), the same answer for the same command, the child environment from the builder (no
provider keys), the limit on the scope's clock and redaction before the tail cut. The command runs
in the attached session's folder; a session another connection attached reads as unknown.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from indus.foundation.scope import Scope
from indus.protocol.errors import SESSION_NOT_FOUND, SHELL_REFUSED, SHELL_TIMEOUT, TEXT_EMPTY
from indus.service.host_shell import STDERR_KEEP, STDOUT_KEEP
from indus.state.registry import StoreHandle
from tests.contract.gateway.test_ws_endpoint import error_of, real_pause, rig, store
from tests.contract.tui_bridge._harness import open_rig
from tests.fakes.agent import eventually
from tests.fakes.clock import FakeClock
from tests.unit.backends._aio import aio

__all__ = ["store"]  # the fixture, imported for pytest


def result_of(frame: dict[str, Any]) -> Any:
    assert "result" in frame, frame
    return frame["result"]


def refusal_of(frame: dict[str, Any]) -> tuple[int, str]:
    return error_of(frame), str(frame["error"]["message"])


@aio
async def test_refused_and_empty_commands_match_local(
    scope: Scope, store: StoreHandle, fake_clock: FakeClock, tmp_path: Path
) -> None:
    commands = ("rm -rf /", f"rm -rf {tmp_path.parent}/elsewhere", "   ")
    r = await rig(scope, store, tmp_path)
    tui = await r.attached("tui")
    sid = result_of(await tui.request("session.create", {}))["session_id"]
    attached = [
        await tui.request("shell.exec", {"command": c, "session_id": sid}) for c in commands
    ]
    await tui.close()
    await r.close()
    async with open_rig(scope, fake_clock, tmp_path) as local:
        here = [await local.err("shell.exec", command=c) for c in commands]

    codes = [error_of(frame) for frame in attached]
    assert codes == [SHELL_REFUSED.code, SHELL_REFUSED.code, TEXT_EMPTY.code]
    assert "never allowed" in attached[0]["error"]["message"]
    assert [refusal_of(frame) for frame in attached] == [
        (int(e["code"]), str(e["message"])) for e in here
    ]


@aio
async def test_a_command_answers_as_in_local_and_runs_in_the_sessions_folder(
    scope: Scope, store: StoreHandle, fake_clock: FakeClock, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    line = "echo out; echo err >&2; exit 3"
    r = await rig(scope, store, tmp_path)
    tui = await r.attached("tui")
    sid = result_of(await tui.request("session.create", {"cwd": str(work)}))["session_id"]
    ran = result_of(await tui.request("shell.exec", {"command": line, "session_id": sid}))
    folder = result_of(await tui.request("shell.exec", {"command": "pwd", "session_id": sid}))
    await tui.close()
    await r.close()
    async with open_rig(scope, fake_clock, tmp_path) as local:
        local_ran = await local.ok("shell.exec", command=line)

    assert ran == local_ran == {"stdout": "out\n", "stderr": "err\n", "code": 3}
    assert Path(folder["stdout"].strip()).resolve() == work.resolve()


@aio
async def test_without_a_session_it_runs_in_the_profiles_terminal_folder(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    folder = tmp_path / "terminal-home"
    folder.mkdir()
    (scope.home / "config.yaml").write_text(f"terminal:\n  cwd: {folder}\n", encoding="utf-8")
    r = await rig(scope, store, tmp_path)
    tui = await r.attached("tui")
    ran = result_of(await tui.request("shell.exec", {"command": "pwd"}))
    await tui.close()
    await r.close()

    assert ran["code"] == 0
    assert Path(ran["stdout"].strip()).resolve() == folder.resolve()


@aio
async def test_a_session_another_connection_attached_reads_as_unknown(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    marker = tmp_path / "ran"
    r = await rig(scope, store, tmp_path)
    owner = await r.attached("tui")
    sid = result_of(await owner.request("session.create", {}))["session_id"]
    other = await r.attached("tui")
    foreign = await other.request("shell.exec", {"command": f"touch {marker}", "session_id": sid})
    unknown = await other.request(
        "shell.exec", {"command": f"touch {marker}", "session_id": "no-such-session"}
    )
    for client in (other, owner):
        await client.close()
    await r.close()

    assert error_of(foreign) == error_of(unknown) == SESSION_NOT_FOUND.code
    assert not marker.exists()  # nothing ran


@aio
async def test_the_child_env_has_no_provider_keys(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    secret = "sk-" + "proj-" + "A1b2C3d4E5f6G7h8I9j0" * 2
    scoped = scope.with_env(scope.env.with_overlay({"OPENAI_API_KEY": secret}))
    r = await rig(scoped, store, tmp_path)
    tui = await r.attached("tui")
    sid = result_of(await tui.request("session.create", {}))["session_id"]
    env = result_of(await tui.request("shell.exec", {"command": "env", "session_id": sid}))
    echoed = result_of(
        await tui.request("shell.exec", {"command": f"echo token {secret}", "session_id": sid})
    )
    await tui.close()
    await r.close()

    assert env["code"] == 0 and env["stdout"]
    assert "OPENAI_API_KEY" not in env["stdout"] and secret not in env["stdout"]
    assert echoed["stdout"].startswith("token") and secret not in echoed["stdout"]


@aio
async def test_a_command_is_stopped_at_the_limit_on_the_scope_clock(
    scope: Scope, store: StoreHandle, fake_clock: FakeClock, tmp_path: Path
) -> None:
    started = tmp_path / "started"
    r = await rig(scope, store, tmp_path)
    tui = await r.attached("tui")
    sid = result_of(await tui.request("session.create", {}))["session_id"]
    call = asyncio.ensure_future(
        tui.request("shell.exec", {"command": f"touch {started}; sleep 40", "session_id": sid})
    )
    await eventually(started.exists)
    fake_clock.advance(29.0)
    await real_pause(0.3)
    still_running = not call.done()
    for _ in range(60):  # the runner reads its start a moment after the child starts
        if call.done():
            break
        fake_clock.advance(1.0)
        await real_pause(0.1)
    answer = await call
    await tui.close()
    await r.close()

    assert still_running
    assert error_of(answer) == SHELL_TIMEOUT.code
    assert answer["error"]["message"] == "stopped after 30 s"


@aio
async def test_long_output_keeps_the_redacted_tail(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    script = tmp_path / "emit.py"
    script.write_text(
        "import sys\n"
        f"sys.stdout.write('a' * 9000 + ' {token} ' + 'b' * 900)\n"
        f"sys.stderr.write('e' * 5000 + ' {token}')\n",
        encoding="utf-8",
    )
    r = await rig(scope, store, tmp_path)
    tui = await r.attached("tui")
    sid = result_of(await tui.request("session.create", {}))["session_id"]
    ran = result_of(
        await tui.request(
            "shell.exec", {"command": f"{sys.executable} {script}", "session_id": sid}
        )
    )
    await tui.close()
    await r.close()

    assert ran["code"] == 0
    assert len(ran["stdout"]) == STDOUT_KEEP and ran["stdout"].endswith("b" * 900)
    assert token not in ran["stdout"] and "ghp_" in ran["stdout"]
    assert len(ran["stderr"]) <= STDERR_KEEP and token not in ran["stderr"]


@aio
async def test_quick_exec_commands_run_on_the_daemon_as_in_local(
    scope: Scope, store: StoreHandle, fake_clock: FakeClock, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (scope.home / "config.yaml").write_text(
        "quick_commands:\n"
        "  gs:\n    type: exec\n    command: echo hi; echo oops >&2; exit 2\n"
        "  here:\n    type: exec\n    command: pwd\n"
        "  wipe:\n    type: exec\n    command: rm -rf /\n",
        encoding="utf-8",
    )
    r = await rig(scope, store, tmp_path)
    tui = await r.attached("tui")
    sid = result_of(await tui.request("session.create", {"cwd": str(work)}))["session_id"]

    async def dispatch(name: str) -> dict[str, Any]:
        return await tui.request("command.dispatch", {"session_id": sid, "name": name, "arg": ""})

    ran, here, wipe = await dispatch("gs"), await dispatch("here"), await dispatch("wipe")
    slash = await tui.request("slash.exec", {"session_id": sid, "command": "gs"})
    catalog = result_of(await tui.request("commands.catalog", {"session_id": sid}))
    offered = result_of(await tui.request("complete.slash", {"text": "/", "session_id": sid}))
    await tui.close()
    await r.close()
    async with open_rig(scope, fake_clock, tmp_path) as local:
        local_sid = (await local.ok("session.create", cwd=str(work)))["session_id"]
        local_ran = await local.ok("command.dispatch", session_id=local_sid, name="gs", arg="")
        local_wipe = await local.err("command.dispatch", session_id=local_sid, name="wipe", arg="")
        local_slash = await local.err("slash.exec", session_id=local_sid, command="gs")

    assert result_of(ran) == local_ran == {"kind": "exec", "output": "hi\n\noops\n\n(exit 2)"}
    assert Path(result_of(here)["output"].strip()).resolve() == work.resolve()
    assert refusal_of(wipe) == (int(local_wipe["code"]), str(local_wipe["message"]))
    assert error_of(wipe) == SHELL_REFUSED.code
    # slash.exec hands a quick command to command.dispatch, as in --local (no daemon refusal).
    assert refusal_of(slash) == (int(local_slash["code"]), str(local_slash["message"]))
    assert "daemon" not in slash["error"]["message"]
    runs = {entry["name"]: entry["runs"] for entry in catalog["commands"]}
    assert runs["gs"] == runs["here"] == "server"
    names = {item["text"].lstrip("/") for item in offered["items"]}
    assert {"gs", "here"} <= names


def test_the_endpoint_serves_shell_exec() -> None:
    from indus.gateway.ws.methods import served

    assert "shell.exec" in served()
