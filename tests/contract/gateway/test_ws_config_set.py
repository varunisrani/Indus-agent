"""W50-a (D-16.549): ``config.set`` over the attach socket, and the switches that use it.

An attached TUI (the default) changes its settings with ``config.set``: ``/busy`` and the settings
pages (profile keys, saved), ``/verbose`` (its own tool-progress mode for the session, or the
profile's default), ``/yolo``, ``/fast``, ``/reasoning``, Shift+Tab and the model picker (the
session's switches). The daemon answers what the in-process bridge (``--local``) answers for the
same steps: the same results, the same refusals with the same messages; a session switch needs a
session this connection attached and reaches every viewer of it (``session.info``). The server
commands ``/yolo``, ``/reasoning``, ``/fast`` run on the daemon with the bridge's text, and the
daemon's ``/status`` counts the inputs waiting in the session's queue.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from indus.foundation.config import loader
from indus.foundation.scope import Scope
from indus.protocol.errors import (
    CONFIRM_REQUIRED,
    INVALID_PARAMS,
    PROVIDER_UNAVAILABLE,
    SESSION_NOT_FOUND,
)
from indus.state import sessions
from indus.state.registry import StoreHandle
from tests.contract.gateway.test_ws_endpoint import done, error_of, rig, store
from tests.contract.gateway.test_ws_tui_session import notes_of, wait_note
from tests.contract.tui_bridge._harness import open_rig
from tests.fakes.agent import eventually
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.session_host import Holder
from tests.unit.backends._aio import aio

__all__ = ["store"]  # the fixture, imported for pytest

PROFILE_SETS: tuple[tuple[str, Any], ...] = (
    ("theme", "ember"),
    ("mouse", True),
    ("busy", "QUEUE"),
    ("indicator", "dots"),
    ("details", "expanded"),
    ("statusbar", False),
    ("verbose", "new"),
)
"""Profile keys and a value each (``verbose`` without a session is the profile's default)."""
PROFILE_REFUSED: tuple[tuple[str, Any], ...] = (
    ("mouse", "yes"),
    ("busy", "fast"),
    ("indicator", "blink"),
    ("details", "open"),
    ("theme", " "),
    ("verbose", "loud"),
    ("colour", 1),
)
SAVED_AS = (
    "tui.theme",
    "tui.mouse",
    "display.busy_input_mode",
    "tui.indicator",
    "tui.details",
    "tui.statusbar",
    "tui.tool_progress",
)
SWITCHES: tuple[tuple[str, Any], ...] = (
    ("verbose", True),
    ("yolo", True),
    ("fast", "fast"),
    ("reasoning", "high"),
)
"""Session switches and a value each, in the order both hosts get them."""
SWITCHES_REFUSED: tuple[tuple[str, Any], ...] = (
    ("yolo", "yes"),
    ("fast", "warp"),
    ("reasoning", ""),
    ("reasoning", 3),
    ("verbose", "loud"),
    ("model", " "),
)
SLASH_STEPS = (
    "yolo",
    "yolo",
    "reasoning",
    "reasoning high",
    "fast",
    "fast fast",
    "fast status",
    "fast normal",
)
"""Server commands whose text is the same on both hosts (no session id in it)."""


def result_of(frame: dict[str, Any]) -> Any:
    assert "result" in frame, frame
    return frame["result"]


def saved(scope: Scope) -> dict[str, Any]:
    view = loader.load(scope)
    return {key: view.get(key) for key in SAVED_AS}


@aio
async def test_profile_keys_over_the_socket_match_local(
    scope: Scope, store: StoreHandle, fake_clock: FakeClock, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path)
    tui = await r.attached("tui")
    attached = [
        result_of(await tui.request("config.set", {"key": k, "value": v})) for k, v in PROFILE_SETS
    ]
    refused = [
        (await tui.request("config.set", {"key": k, "value": v}))["error"]
        for k, v in PROFILE_REFUSED
    ]
    on_disk = saved(scope)
    full = result_of(await tui.request("config.get", {"key": "full"}))
    await tui.close()
    await r.close()

    assert all(answer["saved"] is True for answer in attached)
    assert on_disk["tui.theme"] == "ember" and on_disk["display.busy_input_mode"] == "queue"
    assert on_disk["tui.tool_progress"] == "new"
    assert full["value"]["busy"] == "queue" and full["value"]["verbose"] == "new"
    assert {e["code"] for e in refused} == {INVALID_PARAMS.code}
    (scope.home / "config.yaml").unlink()
    async with open_rig(scope, fake_clock, tmp_path) as local:
        here = [await local.ok("config.set", key=k, value=v) for k, v in PROFILE_SETS]
        local_refused = [await local.err("config.set", key=k, value=v) for k, v in PROFILE_REFUSED]
        assert saved(scope) == on_disk
        assert await local.ok("config.get", key="full") == full
    assert attached == here
    assert refused == local_refused


@aio
async def test_session_switches_over_the_socket_match_local(
    scope: Scope, store: StoreHandle, fake_clock: FakeClock, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path)
    tui = await r.attached("tui")
    sid = result_of(await tui.request("session.create", {}))["session_id"]
    viewer = await r.attached("tui")
    await viewer.request("session.subscribe", {"session_id": sid})
    await wait_note(viewer, "session.info", sid)
    infos = len(notes_of(viewer, "session.info", sid))
    attached = [
        result_of(await tui.request("config.set", {"key": k, "value": v, "session_id": sid}))
        for k, v in SWITCHES
    ]
    refused = [
        (await tui.request("config.set", {"key": k, "value": v, "session_id": sid}))["error"]
        for k, v in SWITCHES_REFUSED
    ]
    alone = (await tui.request("config.set", {"key": "yolo", "value": True}))["error"]
    mine = result_of(await tui.request("config.get", {"key": "full", "session_id": sid}))
    theirs = result_of(await viewer.request("config.get", {"key": "full", "session_id": sid}))
    await wait_note(viewer, "session.info", sid, infos + 1)  # another viewer's bar follows
    stranger = await r.attached("tui")
    foreign = await stranger.request(
        "config.set", {"key": "yolo", "value": False, "session_id": sid}
    )
    for client in (stranger, viewer, tui):
        await client.close()
    await r.close()

    assert error_of(foreign) == SESSION_NOT_FOUND.code
    assert alone["code"] == INVALID_PARAMS.code and alone["data"]["path"] == "session_id"
    assert {e["code"] for e in refused} == {INVALID_PARAMS.code}
    # The session's switches are shared; the tool-progress mode is this viewer's own.
    assert mine["value"]["yolo"] is True and theirs["value"]["yolo"] is True
    assert mine["value"]["fast"] == theirs["value"]["fast"] == "fast"
    assert mine["value"]["verbose"] == "verbose" and theirs["value"]["verbose"] == "all"
    async with open_rig(scope, fake_clock, tmp_path) as local:
        local_sid = (await local.ok("session.create"))["session_id"]
        here = [
            await local.ok("config.set", key=k, value=v, session_id=local_sid) for k, v in SWITCHES
        ]
        local_refused = [
            await local.err("config.set", key=k, value=v, session_id=local_sid)
            for k, v in SWITCHES_REFUSED
        ]
        assert await local.err("config.set", key="yolo", value=True) == alone
        local_full = await local.ok("config.get", key="full", session_id=local_sid)
        await local.ok("config.set", key="yolo", value=False, session_id=local_sid)
    assert attached == here
    assert refused == local_refused
    assert mine == local_full


@aio
async def test_a_persons_yolo_outlives_an_unattended_turn(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    r = await rig(
        scope, store, tmp_path, tool_batch(("hold", {})), text_turn("done"), tools={"hold": holder}
    )
    tui = await r.attached("tui")
    sid = result_of(await tui.request("session.create", {}))["session_id"]
    await tui.request("prompt.submit", {"session_id": sid, "text": "work", "unattended": True})
    await eventually(lambda: holder.entered)
    during = result_of(await tui.request("config.get", {"key": "yolo", "session_id": sid}))
    switched = await tui.request("config.set", {"key": "yolo", "value": True, "session_id": sid})
    holder.gate.set()
    await tui.until(done(sid))
    await eventually(lambda: r.host.running_generation(r.host.key_of(sid) or sid) is None)
    after = result_of(await tui.request("config.get", {"key": "yolo", "session_id": sid}))
    kept = sessions.get(store, sid)
    await tui.request("config.set", {"key": "yolo", "value": False, "session_id": sid})
    off = result_of(await tui.request("config.get", {"key": "yolo", "session_id": sid}))
    await tui.close()
    await r.close()

    assert during["value"] is True  # the unattended turn's own switch
    assert result_of(switched)["value"] is True
    assert after["value"] is True  # the turn's end left the person's choice alone
    assert kept is not None and sessions.yolo_enabled(kept)  # and a resume keeps it
    stored = sessions.get(store, sid)
    assert stored is not None and not sessions.yolo_enabled(stored)
    assert off["value"] is False


@aio
async def test_a_model_switch_over_the_socket(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    (scope.home / "config.yaml").write_text(
        "tui:\n  confirm_models:\n    - big-model\n", encoding="utf-8"
    )
    holder = Holder()
    r = await rig(
        scope, store, tmp_path, tool_batch(("hold", {})), text_turn("done"), tools={"hold": holder}
    )
    route = r.host.service.route

    def resolve(model: str, provider: str | None) -> Any:
        if model == "broken":
            raise ValueError("no such model at this provider")
        return replace(route, model=model)

    r.host.mutations.resolve_route = resolve
    r.host.mutations.model_guard = lambda _used, _window: None
    tui = await r.attached("tui")
    sid = result_of(await tui.request("session.create", {}))["session_id"]

    def model_set(value: str, **extra: Any) -> dict[str, Any]:
        return {"key": "model", "value": value, "session_id": sid, **extra}

    asked = await tui.request("config.set", model_set("big-model"))
    now = result_of(await tui.request("config.set", model_set("m2")))
    current = result_of(await tui.request("config.get", {"key": "model", "session_id": sid}))
    broken = await tui.request("config.set", model_set("broken"))
    r.host.mutations.model_guard = lambda _used, _window: "the context does not fit"
    tight = await tui.request("config.set", model_set("m3"))
    r.host.mutations.model_guard = lambda _used, _window: None
    await tui.request("prompt.submit", {"session_id": sid, "text": "work"})
    await eventually(lambda: holder.entered)
    deferred = result_of(await tui.request("config.set", model_set("big-model", confirm=True)))
    holder.gate.set()
    await tui.until(done(sid))
    await tui.close()
    await r.close()

    assert error_of(asked) == CONFIRM_REQUIRED.code and asked["error"]["data"]["path"] == "confirm"
    assert now == {"key": "model", "value": "m2", "applied": "now", "saved": False}
    assert current["value"] == "m2"
    assert error_of(broken) == PROVIDER_UNAVAILABLE.code
    assert error_of(tight) == CONFIRM_REQUIRED.code
    assert tight["error"]["message"] == "the context does not fit"
    assert deferred == {"key": "model", "value": "big-model", "applied": "deferred", "saved": False}


@aio
async def test_switch_commands_over_the_socket_match_local(
    scope: Scope, store: StoreHandle, fake_clock: FakeClock, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path)
    tui = await r.attached("tui")
    sid = result_of(await tui.request("session.create", {}))["session_id"]
    attached = [
        result_of(await tui.request("slash.exec", {"session_id": sid, "command": c}))
        for c in SLASH_STEPS
    ]
    dispatched = result_of(
        await tui.request("command.dispatch", {"session_id": sid, "name": "yolo", "arg": ""})
    )
    catalog = result_of(await tui.request("commands.catalog", {"session_id": sid}))
    await tui.close()
    await r.close()

    runs = {entry["name"]: entry["runs"] for entry in catalog["commands"]}
    assert runs["yolo"] == runs["reasoning"] == runs["fast"] == "server"
    assert dispatched["kind"] == "exec" and "Auto-approve is ON" in dispatched["output"]
    async with open_rig(scope, fake_clock, tmp_path) as local:
        local_sid = (await local.ok("session.create"))["session_id"]
        here = [await local.ok("slash.exec", session_id=local_sid, command=c) for c in SLASH_STEPS]
        await local.ok("slash.exec", session_id=local_sid, command="yolo")
    assert attached == here


@aio
async def test_status_counts_the_inputs_waiting_in_the_queue(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    r = await rig(
        scope,
        store,
        tmp_path,
        tool_batch(("hold", {})),
        text_turn("done"),
        text_turn("two"),
        text_turn("three"),
        tools={"hold": holder},
    )
    tui = await r.attached("tui")
    sid = result_of(await tui.request("session.create", {}))["session_id"]

    async def status() -> str:
        answer = await tui.request("slash.exec", {"session_id": sid, "command": "status"})
        return str(result_of(answer)["output"])

    idle = await status()
    await tui.request("prompt.submit", {"session_id": sid, "text": "work"})
    await eventually(lambda: holder.entered)
    for text in ("two", "three"):
        await tui.request("prompt.submit", {"session_id": sid, "text": text, "when_busy": "queue"})
    busy = await status()
    holder.gate.set()
    for _ in range(3):
        await tui.until(done(sid))
    await eventually(lambda: r.host.running_generation(r.host.key_of(sid) or sid) is None)
    drained = await status()
    await tui.close()
    await r.close()

    assert "Queued messages" not in idle
    assert "A reply is running." in busy and busy.endswith("Queued messages: 2")
    assert "Queued messages" not in drained
