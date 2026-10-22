"""W47-c: the TUI's slash menu, model picker and sessions picker over the attach socket.

An attached TUI (the default) asks the daemon for ``complete.slash``, ``model.options`` and
``session.list``. They are answered over the WebSocket with the same results the in-process bridge
(``--local``) gives for the same profile; a named session must be one this connection attached
(another connection's reads as unknown, like every session-addressed call on the daemon).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from indus.foundation.scope import Scope
from indus.protocol.errors import SESSION_NOT_FOUND
from indus.state.registry import StoreHandle
from tests.contract.gateway.test_ws_endpoint import done, error_of, rig, store
from tests.contract.tui_bridge._harness import open_rig
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn
from tests.unit.backends._aio import aio

__all__ = ["store"]  # the fixture, imported for pytest

SLASH_TEXTS = ("/he", "/g", "/reasoning hig", "/")


def result_of(frame: dict[str, Any]) -> Any:
    assert "result" in frame, frame
    return frame["result"]


def texts(result: dict[str, Any]) -> list[str]:
    return [str(item["text"]) for item in result["items"]]


@aio
async def test_slash_completion_over_the_socket_matches_local(
    scope: Scope, store: StoreHandle, fake_clock: FakeClock, tmp_path: Path
) -> None:
    (scope.home / "config.yaml").write_text(
        "quick_commands:\n  gs:\n    type: exec\n    command: git status\n", encoding="utf-8"
    )
    r = await rig(scope, store, tmp_path)
    tui = await r.attached("tui")
    sid = result_of(await tui.request("session.create", {}))["session_id"]
    attached: dict[str, Any] = {}
    for text in SLASH_TEXTS:
        attached[text] = result_of(await tui.request("complete.slash", {"text": text}))
    with_session = await tui.request("complete.slash", {"text": "/he", "session_id": sid})
    assert result_of(with_session) == attached["/he"]
    other = await r.attached("tui")
    foreign = await other.request("complete.slash", {"text": "/he", "session_id": sid})
    assert error_of(foreign) == SESSION_NOT_FOUND.code
    await other.close()
    await tui.close()
    await r.close()

    assert "/help" in texts(attached["/he"])
    assert {"/gs", "/goal"} <= set(texts(attached["/g"]))
    assert texts(attached["/reasoning hig"]) == ["high"]
    async with open_rig(scope, fake_clock, tmp_path) as local:
        for text in SLASH_TEXTS:
            assert await local.ok("complete.slash", text=text) == attached[text], text


@aio
async def test_model_options_over_the_socket_match_local(
    scope: Scope, store: StoreHandle, fake_clock: FakeClock, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path)
    tui = await r.attached("tui")
    profile_wide = result_of(await tui.request("model.options", {}))
    sid = result_of(await tui.request("session.create", {}))["session_id"]
    for_session = result_of(await tui.request("model.options", {"session_id": sid}))
    other = await r.attached("tui")
    foreign = await other.request("model.options", {"session_id": sid})
    assert error_of(foreign) == SESSION_NOT_FOUND.code
    await other.close()
    await tui.close()
    await r.close()

    assert profile_wide["providers"]
    assert for_session["model"] and for_session["provider"]
    async with open_rig(scope, fake_clock, tmp_path) as local:
        assert await local.ok("model.options") == profile_wide
        local_sid = (await local.ok("session.create"))["session_id"]
        here = await local.ok("model.options", session_id=local_sid)
        assert (here["provider"], here["model"]) == (for_session["provider"], for_session["model"])
        assert here["providers"] == for_session["providers"]


@aio
async def test_session_list_over_the_socket_matches_local(
    scope: Scope, store: StoreHandle, fake_clock: FakeClock, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path, text_turn("first reply"), text_turn("second reply"))
    tui = await r.attached("tui")
    stored: list[str] = []
    for text in ("export the users table", "why does the build fail"):
        sid = result_of(await tui.request("session.create", {}))["session_id"]
        await tui.request("prompt.submit", {"session_id": sid, "text": text})
        await tui.until(done(sid))
        stored.append(sid)
    # Another connection lists the profile's stored sessions too (the owner's store, as --local).
    other = await r.attached("tui")
    listed = result_of(await other.request("session.list", {"limit": 50}))
    found = result_of(await other.request("session.list", {"query": "build"}))
    await other.close()
    await tui.close()
    await r.close()

    assert {row["session_id"] for row in listed["sessions"]} >= set(stored)
    assert [row["session_id"] for row in found["sessions"]] == [stored[1]]
    async with open_rig(scope, fake_clock, tmp_path) as local:
        assert await local.ok("session.list", limit=50) == listed
        assert await local.ok("session.list", query="build") == found


def test_the_endpoint_serves_the_tui_picker_methods() -> None:
    from indus.gateway.ws.methods import served

    assert {"complete.slash", "model.options", "session.list"} <= set(served())
