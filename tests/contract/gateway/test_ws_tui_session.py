"""W48-h: the attached TUI's session header and server-run slash commands over the attach socket.

The daemon sends ``session.info`` (model, provider, folder, tools, title) to the connections that
attached a session, when they attach and when the model, folder or title changes, and
``session.title`` when the title changes, with the shapes the in-process bridge sends (D-16.550).
``slash.exec`` / ``command.dispatch`` run the bridge's server commands with the bridge's answers;
the commands a shared daemon does not run (a viewer's own queue, the session switches
``config.set`` governs, quick ``exec`` commands) are refused with a reason, and the daemon's
catalogue marks them ``unavailable``. A connection reaches only the sessions it attached.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from indus.foundation.scope import Scope
from indus.protocol.errors import SESSION_NOT_FOUND, SLASH_NOT_OWNED, UNSUPPORTED_COMMAND
from indus.state.registry import StoreHandle
from tests.contract.gateway.test_ws_endpoint import done, error_of, rig, store
from tests.contract.tui_bridge._harness import open_rig
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn
from tests.unit.backends._aio import aio

__all__ = ["store"]  # the fixture, imported for pytest

DAEMON_REFUSED = ("queue", "yolo", "reasoning", "fast")
"""Server commands a shared daemon does not run (``picker_reads.DAEMON_REFUSED``)."""
SAME_TEXT = ("version", "help", "usage", "title", "title Alpine", "title", "undo", "history")
"""Commands whose output is the same text on both hosts (no session id in it)."""


def result_of(frame: dict[str, Any]) -> Any:
    assert "result" in frame, frame
    return frame["result"]


def notes_of(client: Any, method: str, sid: str) -> list[dict[str, Any]]:
    return [
        n["params"]
        for n in client.notes
        if n.get("method") == method and n.get("params", {}).get("session_id") == sid
    ]


async def wait_note(client: Any, method: str, sid: str, count: int = 1) -> None:
    """Read until ``count`` notifications ``method`` of ``sid`` arrived (some may be in already)."""
    while len(notes_of(client, method, sid)) < count:
        await client.until(lambda f: f.get("method") == method)


def header(info: dict[str, Any]) -> dict[str, Any]:
    """The fields a status bar shows (the event's numbering aside)."""
    return {k: v for k, v in info.items() if k not in ("seq", "session_id", "replay_epoch")}


@aio
async def test_session_info_reaches_only_the_attached_connections(
    scope: Scope, store: StoreHandle, fake_clock: FakeClock, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    r = await rig(scope, store, tmp_path, text_turn("hi"))
    tui = await r.attached("tui")
    bystander = await r.attached("tui")
    sid = result_of(await tui.request("session.create", {"cwd": str(work)}))["session_id"]
    await wait_note(tui, "session.info", sid)
    first = notes_of(tui, "session.info", sid)[0]
    rename = {"title": "Alpine"}
    renamed = await tui.request(
        "session.mutate",
        {"session_id": sid, "operation": "rename", "payload": rename, "request_id": "a"},
    )
    assert result_of(renamed)["status"] == "applied"
    await wait_note(tui, "session.title", sid)
    moved = tmp_path / "elsewhere"
    moved.mkdir()
    await tui.request(
        "session.mutate",
        {
            "session_id": sid,
            "operation": "workspace",
            "payload": {"cwd": str(moved)},
            "request_id": "b",
        },
    )
    await wait_note(tui, "session.info", sid, 2)
    # A second viewer of the same session gets the header when it attaches.
    viewer = await r.attached("tui")
    await viewer.request("session.subscribe", {"session_id": sid})
    await wait_note(viewer, "session.info", sid)
    await bystander.request("gateway.ping", {})
    seen_by_bystander = [n for n in bystander.notes if n.get("method", "").startswith("session.")]
    for client in (viewer, bystander, tui):
        await client.close()
    await r.close()

    assert first["cwd"] == str(work) and first["model"] and first["provider"]
    assert "title" not in first and first["running"] is False  # no title yet: left out
    assert [t["title"] for t in notes_of(tui, "session.title", sid)] == ["Alpine"]
    after = notes_of(tui, "session.info", sid)[-1]
    assert after["cwd"] == str(moved) and after["title"] == "Alpine"
    assert notes_of(viewer, "session.info", sid)[-1]["cwd"] == str(moved)
    assert seen_by_bystander == []  # never attached it: nothing of it arrives
    # The bridge's own header for the same kind of session has the same fields.
    async with open_rig(scope, fake_clock, tmp_path) as local:
        await local.ok("session.create", cwd=str(work))
        here = header(local.recorder.of("session.info")[0])
    assert set(header(first)) == set(here)
    shown = ("cwd", "running", "tool_progress")
    assert {k: header(first)[k] for k in shown} == {k: here[k] for k in shown}


@aio
async def test_server_commands_over_the_socket_match_local(
    scope: Scope, store: StoreHandle, fake_clock: FakeClock, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    r = await rig(scope, store, tmp_path)
    tui = await r.attached("tui")
    sid = result_of(await tui.request("session.create", {"cwd": str(work)}))["session_id"]
    attached = [
        result_of(await tui.request("slash.exec", {"session_id": sid, "command": c}))
        for c in SAME_TEXT
    ]
    status = result_of(await tui.request("slash.exec", {"session_id": sid, "command": "status"}))
    dispatched = result_of(
        await tui.request("command.dispatch", {"session_id": sid, "name": "version", "arg": ""})
    )
    other = await r.attached("tui")
    foreign = [
        await other.request("slash.exec", {"session_id": sid, "command": "status"}),
        await other.request("command.dispatch", {"session_id": sid, "name": "usage", "arg": ""}),
    ]
    await other.close()
    await tui.close()
    await r.close()

    assert [error_of(f) for f in foreign] == [SESSION_NOT_FOUND.code] * 2
    assert f"Session: {sid}" in status["output"] and f"Folder: {work}" in status["output"]
    assert dispatched == {"kind": "exec", "output": attached[0]["output"]}
    async with open_rig(scope, fake_clock, tmp_path) as local:
        local_sid = (await local.ok("session.create", cwd=str(work)))["session_id"]
        here = [await local.ok("slash.exec", session_id=local_sid, command=c) for c in SAME_TEXT]
        local_status = await local.ok("slash.exec", session_id=local_sid, command="status")
    # The daemon writes the row of a session with no reply yet when it is titled (session.mutate
    # rename), so its title is never "pending" (W48-h-4).
    pending = " (saved after the first reply)"
    assert attached == [{**r, "output": r["output"].replace(pending, "")} for r in here]
    assert status["output"].replace(sid, "S") == local_status["output"].replace(local_sid, "S")


@aio
async def test_commands_a_shared_daemon_does_not_run_are_refused_and_marked(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    (scope.home / "config.yaml").write_text(
        "quick_commands:\n  gs:\n    type: exec\n    command: git status\n"
        "  ver:\n    type: alias\n    target: /version\n",
        encoding="utf-8",
    )
    r = await rig(scope, store, tmp_path)
    tui = await r.attached("tui")
    sid = result_of(await tui.request("session.create", {}))["session_id"]
    catalog = result_of(await tui.request("commands.catalog", {"session_id": sid}))
    refused: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for name in [*DAEMON_REFUSED, "gs"]:
        refused[name] = (
            await tui.request("slash.exec", {"session_id": sid, "command": name}),
            await tui.request("command.dispatch", {"session_id": sid, "name": name, "arg": ""}),
        )
    alias = result_of(
        await tui.request("command.dispatch", {"session_id": sid, "name": "ver", "arg": ""})
    )
    offered = result_of(await tui.request("complete.slash", {"text": "/", "session_id": sid}))
    await tui.close()
    await r.close()

    runs = {entry["name"]: entry["runs"] for entry in catalog["commands"]}
    assert {name: runs[name] for name in [*DAEMON_REFUSED, "gs"]} == dict.fromkeys(
        [*DAEMON_REFUSED, "gs"], "unavailable"
    )
    assert runs["version"] == runs["status"] == runs["ver"] == "server"
    for name, (exec_frame, dispatch_frame) in refused.items():
        assert error_of(exec_frame) == SLASH_NOT_OWNED.code, name
        assert error_of(dispatch_frame) == UNSUPPORTED_COMMAND.code, name
        reason = exec_frame["error"]["message"]
        assert "daemon" in reason and reason == dispatch_frame["error"]["message"], name
    assert alias == {"kind": "alias", "target": "/version"}
    names = {item["text"].lstrip("/") for item in offered["items"]}
    assert "ver" in names and "gs" not in names  # never offered where it cannot run


@aio
async def test_stop_and_steer_reach_the_running_turn(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path, text_turn("one"))
    tui = await r.attached("tui")
    sid = result_of(await tui.request("session.create", {}))["session_id"]
    idle_stop = result_of(await tui.request("slash.exec", {"session_id": sid, "command": "stop"}))
    idle_steer = result_of(
        await tui.request("slash.exec", {"session_id": sid, "command": "steer go on"})
    )
    await tui.request("prompt.submit", {"session_id": sid, "text": "hi"})
    await tui.until(done(sid))
    await tui.close()
    await r.close()

    assert idle_stop == {"output": "Nothing was running."}
    steered = {"output": "Nothing is running; sending it as a message.", "submit": "go on"}
    assert idle_steer == steered
