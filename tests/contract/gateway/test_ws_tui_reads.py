"""W48-b: the TUI's other reads over the attach socket.

An attached TUI (the default) asks the daemon for ``complete.path`` (the composer's path and ``@``
completion), ``commands.catalog`` (the slash catalogue; what a shared daemon does not run is
listed ``unavailable``, W48-h) and ``config.get`` (its settings at start
and their polling). They answer what the in-process bridge (``--local``) answers for the same
profile; a named session must be one this connection attached (``config.set`` is served since
W50-a: ``test_ws_config_set.py``). The sessions picker's rows carry the session's model as well as
its title and source.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from indus.foundation.scope import Scope
from indus.protocol.errors import METHOD_NOT_FOUND, SESSION_NOT_FOUND
from indus.service.picker_reads import DAEMON_REFUSED
from indus.state.registry import StoreHandle
from tests.contract.gateway.test_ws_endpoint import done, error_of, rig, store
from tests.contract.tui_bridge._harness import open_rig
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn
from tests.unit.backends._aio import aio

__all__ = ["store"]  # the fixture, imported for pytest

CONFIG_KEYS = ("full", "theme", "busy", "verbose", "tui.theme", "display.show_reasoning")


def result_of(frame: dict[str, Any]) -> Any:
    assert "result" in frame, frame
    return frame["result"]


def folder(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    (work / "alpine").mkdir(parents=True)
    (work / "alpine" / "notes.md").write_text("x", encoding="utf-8")
    (work / "alpha.txt").write_text("a", encoding="utf-8")
    (work / "beta.md").write_text("b", encoding="utf-8")
    (work / ".hidden").write_text("h", encoding="utf-8")
    return work


@aio
async def test_path_completion_over_the_socket_matches_local(
    scope: Scope, store: StoreHandle, fake_clock: FakeClock, tmp_path: Path
) -> None:
    work = folder(tmp_path)
    words = ("al", "@", "@al", "@folder:al", "./b", "alpine/", ".h", "@url:x")
    r = await rig(scope, store, tmp_path)
    tui = await r.attached("tui")
    attached: dict[str, Any] = {}
    for word in words:
        frame = await tui.request("complete.path", {"word": word, "cwd": str(work)})
        attached[word] = result_of(frame)
    sid = result_of(await tui.request("session.create", {"cwd": str(work)}))["session_id"]
    in_session = result_of(await tui.request("complete.path", {"word": "al", "session_id": sid}))
    other = await r.attached("tui")
    foreign = await other.request("complete.path", {"word": "al", "session_id": sid})
    assert error_of(foreign) == SESSION_NOT_FOUND.code
    await other.close()
    await tui.close()
    await r.close()

    assert [i["text"] for i in attached["al"]["items"]] == ["alpha.txt", "alpine/"]
    assert {i["text"] for i in attached["@al"]["items"]} >= {"@file:alpha.txt", "@folder:alpine/"}
    # The session's own folder counts when the client names none.
    assert in_session == attached["al"]
    async with open_rig(scope, fake_clock, tmp_path) as local:
        for word in words:
            here = await local.ok("complete.path", word=word, cwd=str(work))
            assert here == attached[word], word
        local_sid = (await local.ok("session.create", cwd=str(work)))["session_id"]
        assert await local.ok("complete.path", word="al", session_id=local_sid) == in_session


@aio
async def test_command_catalog_over_the_socket_matches_local(
    scope: Scope, store: StoreHandle, fake_clock: FakeClock, tmp_path: Path
) -> None:
    (scope.home / "config.yaml").write_text(
        "quick_commands:\n  gs:\n    type: exec\n    command: git status\n", encoding="utf-8"
    )
    r = await rig(scope, store, tmp_path)
    tui = await r.attached("tui")
    profile_wide = result_of(await tui.request("commands.catalog", {}))
    sid = result_of(await tui.request("session.create", {}))["session_id"]
    for_session = result_of(await tui.request("commands.catalog", {"session_id": sid}))
    other = await r.attached("tui")
    foreign = await other.request("commands.catalog", {"session_id": sid})
    assert error_of(foreign) == SESSION_NOT_FOUND.code
    await other.close()
    await tui.close()
    await r.close()

    by_name = {entry["name"]: entry for entry in profile_wide["commands"]}
    assert by_name["help"]["runs"] == "server" and by_name["model"]["runs"] == "client"
    assert by_name["gs"]["kind"] == "quick"
    assert for_session == profile_wide
    async with open_rig(scope, fake_clock, tmp_path) as local:
        here = await local.ok("commands.catalog")
    # The same catalogue, but what a shared daemon does not run is ``unavailable`` there (W48-h);
    # quick exec commands run there since W50-b.
    off = set(DAEMON_REFUSED)
    assert profile_wide["commands"] == [
        {**entry, "runs": "unavailable"} if entry["name"] in off else entry
        for entry in here["commands"]
    ]


@aio
async def test_config_get_over_the_socket_matches_local(
    scope: Scope, store: StoreHandle, fake_clock: FakeClock, tmp_path: Path
) -> None:
    (scope.home / "config.yaml").write_text(
        "display:\n  busy_input_mode: steer\ntui:\n  theme: light\n  tool_progress: new\n",
        encoding="utf-8",
    )
    r = await rig(scope, store, tmp_path)
    tui = await r.attached("tui")
    attached = {k: result_of(await tui.request("config.get", {"key": k})) for k in CONFIG_KEYS}
    stamp = result_of(await tui.request("config.get", {"key": "mtime"}))
    sid = result_of(await tui.request("session.create", {}))["session_id"]
    live = result_of(await tui.request("config.get", {"key": "full", "session_id": sid}))
    model = result_of(await tui.request("config.get", {"key": "model", "session_id": sid}))
    unknown = await tui.request("config.get", {"key": "model.api_key"})
    other = await r.attached("tui")
    foreign = await other.request("config.get", {"key": "full", "session_id": sid})
    assert error_of(foreign) == SESSION_NOT_FOUND.code
    await other.close()
    await tui.close()
    await r.close()

    assert attached["full"]["value"]["busy"] == "steer"
    assert attached["theme"]["value"] == "light"
    assert isinstance(stamp["value"], float)
    assert live["value"]["model"] and live["value"]["verbose"] == "new"
    assert model["value"] == live["value"]["model"]
    assert error_of(unknown) != METHOD_NOT_FOUND.code  # refused as a key, not as a method
    async with open_rig(scope, fake_clock, tmp_path) as local:
        for key in CONFIG_KEYS:
            assert await local.ok("config.get", key=key) == attached[key], key
        local_sid = (await local.ok("session.create"))["session_id"]
        here = await local.ok("config.get", key="full", session_id=local_sid)
        assert here == live


@aio
async def test_session_rows_carry_title_model_and_source(
    scope: Scope, store: StoreHandle, fake_clock: FakeClock, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path, text_turn("the reply"))
    tui = await r.attached("tui")
    sid = result_of(await tui.request("session.create", {"title": "Release notes"}))["session_id"]
    await tui.request("prompt.submit", {"session_id": sid, "text": "draft the notes"})
    await tui.until(done(sid))
    listed = result_of(await tui.request("session.list", {"limit": 50}))
    await tui.close()
    await r.close()

    row = next(row for row in listed["sessions"] if row["session_id"] == sid)
    assert row["title"] == "Release notes"
    assert row["model"]
    assert row["source"]
    async with open_rig(scope, fake_clock, tmp_path) as local:
        assert await local.ok("session.list", limit=50) == listed


def test_the_endpoint_serves_the_tui_reads_and_config_set() -> None:
    from indus.gateway.ws.methods import served

    names = set(served())
    assert {"complete.path", "commands.catalog", "config.get", "config.set"} <= names
