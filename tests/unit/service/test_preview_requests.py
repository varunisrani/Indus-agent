"""Preview automation through server requests (P24-25 wiring, D-16.799): ``drive_preview`` and
``annotate_preview`` ask ``preview.act`` / ``preview.read`` through the session's broker; the
daemon's request book offers them to the desktop windows that render those kinds; the window
showing the preview answers, the others decline (4404) and an all-decline is ``not_shown``. Only a
session a desktop window created gets the tools."""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from indus.core.agent import AgentOptions
from indus.core.persist import StateStore
from indus.foundation.scope import Scope
from indus.protocol.methods import SessionCreateParams
from indus.protocol.server_requests import (
    PERSON_REQUEST_KINDS,
    PREVIEW_REQUEST_KINDS,
    SERVER_REQUEST_KINDS,
    PreviewActRequest,
)
from indus.service.broker import ServerRequestBroker
from indus.service.daemon_host import DaemonSessionHost
from indus.service.request_redelivery import (
    NOT_SHOWN_CODE,
    UNRENDERABLE_CODE,
    Offer,
    RequestBook,
    RequestViewer,
)
from indus.service.session_mutations import with_preview
from indus.service.session_service import SessionService
from indus.service.subscriptions import capabilities_from
from indus.service.viewers import Viewer
from indus.state import registry
from indus.tools import toolsets
from indus.tools.builtin import load_builtins
from indus.tools.builtin.preview import PREVIEW_TOOLSET, drive_preview
from indus.tools.registry import ToolRegistry
from indus.tools.session_registry import SessionRegistry
from tests.fakes.agent import fake_options
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, fake_route, script_of
from tests.unit.backends._aio import aio


async def _inline[T](fn: Callable[[], T]) -> T:
    return fn()


class Rig:
    def __init__(self) -> None:
        self.clock = FakeClock(datetime(2026, 10, 21, 9, 0, tzinfo=UTC))
        self.book = RequestBook("s1", emit=lambda _event: None, generation=lambda: 3)
        seqs = itertools.count(1)
        self.broker = ServerRequestBroker(
            session_id="s1",
            deliver=lambda _request: None,
            emit=lambda _event: None,
            next_seq=lambda: next(seqs),
            clock=self.clock,
            book=self.book,
        )
        self.offers: dict[str, list[Offer]] = {}

    def window(self, name: str, kinds: tuple[str, ...] = tuple(PREVIEW_REQUEST_KINDS)) -> None:
        got = self.offers.setdefault(name, [])
        self.book.attach(RequestViewer.of(name, kinds, got.append, can_decline=True))


def test_kinds_a_person_answers_and_the_preview_kinds() -> None:
    assert set(PREVIEW_REQUEST_KINDS) <= set(SERVER_REQUEST_KINDS)
    assert set(PERSON_REQUEST_KINDS) == {"approval", "clarify", "sudo", "secret"}
    # "server_requests" means the person kinds; a desktop window names the preview kinds itself
    assert capabilities_from(["server_requests"]) == frozenset(PERSON_REQUEST_KINDS)
    desktop = capabilities_from(
        ["server_requests", "server_requests.preview.act", "server_requests.preview.read"]
    )
    assert desktop == frozenset(SERVER_REQUEST_KINDS)


def test_the_window_showing_the_preview_answers() -> None:
    async def main() -> tuple[str, Any]:
        rig = Rig()
        rig.window("w1")
        rig.window("w2")
        task = asyncio.ensure_future(
            drive_preview(
                {"action": "click", "selector": "button#go"}, broker=rig.broker, turn_id="t1"
            )
        )
        await rig.clock.run_until_idle()
        offered = rig.offers["w1"][0].request
        assert isinstance(offered, PreviewActRequest)
        assert (offered.action, offered.selector, offered.turn_id) == ("click", "button#go", "t1")
        rig.book.refuse("w1", offered.request_id, NOT_SHOWN_CODE)  # w1 shows another session
        rig.broker.answer(
            offered.request_id,
            {"kind": "preview.act", "ok": True, "result": {"detail": "clicked"}},
        )
        return await task, offered

    out, _ = asyncio.run(main())
    assert json.loads(out) == {"detail": "clicked", "ok": True}


def test_a_snapshot_reads_and_a_refusal_comes_back_as_an_error() -> None:
    async def main() -> list[str]:
        rig = Rig()
        rig.window("w1")
        read = asyncio.ensure_future(drive_preview({"action": "snapshot"}, broker=rig.broker))
        await rig.clock.run_until_idle()
        first = rig.offers["w1"][0].request
        assert first.kind == "preview.read"
        rig.broker.answer(first.request_id, {"kind": "preview.read", "ok": True, "result": {}})
        act = asyncio.ensure_future(
            drive_preview({"action": "scroll", "dy": 300}, broker=rig.broker)
        )
        await rig.clock.run_until_idle()
        second = rig.offers["w1"][1].request
        rig.broker.answer(
            second.request_id, {"kind": "preview.act", "ok": False, "error": "no page loaded"}
        )
        return [await read, await act]

    snapshot, refused = asyncio.run(main())
    assert json.loads(snapshot) == {"ok": True}
    assert "no page loaded" in refused


def test_every_window_declining_is_not_shown_and_no_window_is_undelivered() -> None:
    async def main() -> list[str]:
        rig = Rig()
        rig.window("w1")
        rig.window("w2")
        task = asyncio.ensure_future(drive_preview({"action": "snapshot"}, broker=rig.broker))
        await rig.clock.run_until_idle()
        request_id = rig.offers["w1"][0].request.request_id
        rig.book.refuse("w1", request_id, NOT_SHOWN_CODE)
        rig.book.refuse("w2", request_id, NOT_SHOWN_CODE)
        declined = await task
        terminal_only = Rig()
        terminal_only.window("tui", tuple(PERSON_REQUEST_KINDS))
        nobody = await drive_preview({"action": "snapshot"}, broker=terminal_only.broker)
        cannot = Rig()
        cannot.window("w9")
        task = asyncio.ensure_future(drive_preview({"action": "snapshot"}, broker=cannot.broker))
        await cannot.clock.run_until_idle()
        cannot.book.refuse("w9", cannot.offers["w9"][0].request.request_id, UNRENDERABLE_CODE)
        return [declined, nobody, await task]

    declined, nobody, cannot = asyncio.run(main())
    assert "not open in a desktop window" in declined
    assert "No desktop window is attached" in nobody
    assert "No desktop window is attached" in cannot


def test_a_desktop_session_gets_the_preview_tools_and_no_other_session_does() -> None:
    shared = ToolRegistry()
    load_builtins(shared)
    options = AgentOptions(registry=shared)
    shaped, chosen = with_preview(options, ["indus-gateway"])
    own = shaped.registry
    assert isinstance(own, SessionRegistry) and own.base is shared
    assert own.get("drive_preview") is not None and own.get("annotate_preview") is not None
    assert shared.get("drive_preview") is None  # never in the shared registry
    assert chosen == ["indus-gateway", PREVIEW_TOOLSET]
    every = toolsets.select(None, registry=own, platform="gateway").tools
    assert {"drive_preview", "annotate_preview"} <= every
    assert "drive_preview" not in toolsets.select(None, registry=shared, platform="gateway").tools
    again, kept = with_preview(shaped, None)  # a rebuild keeps one copy
    assert again.registry is own and kept is None


@aio
async def test_the_daemon_gives_only_window_sessions_the_preview_tools(
    scope: Scope, tmp_path: Path
) -> None:
    store = registry.open_unshared(scope)
    server = FakeModelServer(script_of(), clock=scope.clock)
    shared = ToolRegistry()
    load_builtins(shared)
    service = SessionService(
        scope,
        fake_route(server),
        toolsets=None,
        options=fake_options(server, session_id=None, registry=shared),
        store=StateStore(store),
        lock_dir=tmp_path / "locks",
    )
    host = await DaemonSessionHost.open(service, store, instance_id="daemon-1", offload=_inline)
    try:
        made = {}
        for source in ("gui", "tui"):
            viewer = Viewer.of(f"v-{source}", source, send=lambda _frame: None)
            created = await host.create(SessionCreateParams(), source=source, viewer=viewer)
            made[source] = created.session_id
        window = service.agent(made["gui"]).tool_names
        terminal = service.agent(made["tui"]).tool_names
        assert {"drive_preview", "annotate_preview"} <= set(window)
        assert "drive_preview" not in terminal
        # a reload (the stored row says gui) keeps them
        await service.reload(made["gui"])
        assert "drive_preview" in service.agent(made["gui"]).tool_names
    finally:
        await host.aclose()
        store.close()
