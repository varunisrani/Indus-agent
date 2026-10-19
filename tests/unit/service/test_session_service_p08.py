"""SessionService additions of P08: ``@`` references in ``submit`` (P08-19) and the auto-title
after the first exchange (P08-17)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from indus.core.persist import MemoryStore
from indus.core.references import SECTION_OPEN
from indus.cron import tool as cron_tool
from indus.foundation.scope import Scope
from indus.protocol.events import Notice
from indus.providers.auxiliary import AuxClient
from indus.service.session_service import SessionService
from tests.fakes.agent import eventually, fake_options
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn


def service(
    scope: Scope,
    clock: FakeClock,
    tmp_path: Path,
    *texts: str,
    aux: AuxClient | None = None,
    cwd: Path | None = None,
) -> tuple[SessionService, MemoryStore, FakeModelServer]:
    server = FakeModelServer(script_of(*(text_turn(t) for t in texts)), clock=clock)
    options = fake_options(server, session_id=None)
    if cwd is not None:
        options = replace(options, cwd=cwd)
    store = MemoryStore()
    svc = SessionService(
        scope,
        fake_route(server),
        toolsets=[],
        options=options,
        store=store,
        lock_dir=tmp_path / "locks",
        aux=aux,
    )
    return svc, store, server


def test_submit_stores_typed_text_and_sends_the_expansion(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    work = tmp_path / "proj"
    work.mkdir()
    (work / "notes.md").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    (work / ".env").write_text("TOKEN=abc\n", encoding="utf-8")

    async def main() -> None:
        svc, store, server = service(scope, fake_clock, tmp_path, "ok", cwd=work)
        sid = (await svc.create()).session_id
        typed = "summarise @file:notes.md:2-3 and @file:.env"
        await svc.submit(sid, typed)
        await svc.wait(sid)
        (row,) = [r for r in store.rows[sid] if r.role == "user"]
        assert row.content == typed
        assert row.api_content is not None and SECTION_OPEN in row.api_content
        assert "beta\ngamma" in row.api_content and "TOKEN=abc" not in row.api_content
        sent = server.model_requests[0].body["messages"][-1]["content"]
        assert "beta\ngamma" in sent
        notices = [e for e in svc.events(sid) if isinstance(e, Notice)]
        assert any("@file:.env" in n.text for n in notices)
        await svc.aclose()

    asyncio.run(main())


def test_plain_submit_is_untouched(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        svc, store, _ = service(scope, fake_clock, tmp_path, "ok")
        sid = (await svc.create()).session_id
        await svc.submit(sid, "hello there")
        await svc.wait(sid)
        (row,) = [r for r in store.rows[sid] if r.role == "user"]
        assert row.content == "hello there" and row.api_content is None
        await svc.aclose()

    asyncio.run(main())


class _TitleModel:
    """A fake auxiliary endpoint that answers only when released."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        await self.release.wait()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Planning the garden"},
                        "finish_reason": "stop",
                    }  # fmt: skip
                ],
                "usage": {"prompt_tokens": 40, "completion_tokens": 4},
            },
        )

    def factory(self, route: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


def _aux(scope: Scope, svc_route: Callable[[], Any], model: _TitleModel) -> AuxClient:
    return AuxClient(scope, svc_route, client_factory=model.factory)


def test_title_after_first_exchange_does_not_block_the_turn(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        model = _TitleModel()
        holder: list[SessionService] = []
        aux = _aux(scope, lambda: holder[0].route, model)
        svc, store, _ = service(
            scope, fake_clock, tmp_path, "Sure, start with beds.", "More.", aux=aux
        )
        holder.append(svc)
        sid = (await svc.create()).session_id
        await svc.submit(sid, "help me plan a garden")
        result = await svc.wait(sid)
        assert result.final_text == "Sure, start with beds."
        await eventually(lambda: model.calls == 1)
        assert "title" not in store.sessions.get(sid, {})  # the turn finished before the title
        model.release.set()
        await eventually(lambda: store.sessions.get(sid, {}).get("title") == "Planning the garden")
        agent = svc.agent(sid)
        assert agent.usage.snapshot().side_calls == {"title": 1}
        await svc.submit(sid, "and the soil?")
        await svc.wait(sid)
        assert model.calls == 1  # only after the first exchange
        await svc.aclose()

    asyncio.run(main())


def test_aux_reaches_every_agent_bound_to_its_route(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        model = _TitleModel()
        holder: list[SessionService] = []
        aux = _aux(scope, lambda: holder[0].route, model)
        svc, _, _ = service(scope, fake_clock, tmp_path, "ok", aux=aux)
        holder.append(svc)
        sid = (await svc.create()).session_id
        agent = svc.agent(sid)
        assert agent.options.aux is aux
        assert agent.aux is not None and agent.aux.main is agent.binding.state.route
        await svc.aclose()

    asyncio.run(main())


def test_reference_budget_follows_the_compression_trigger(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    from indus.core import references
    from indus.service.session_service import (
        _reference_budget,  # pyright: ignore[reportPrivateUsage]
    )

    async def main() -> None:
        svc, _, _ = service(scope, fake_clock, tmp_path, "ok")
        sid = (await svc.create()).session_id
        agent = svc.agent(sid)
        engine = agent.context_engine
        assert engine is not None and engine.trigger > 0
        assert _reference_budget(agent) == engine.trigger * references.CHARS_PER_TOKEN
        agent.usage.last_prompt_tokens = engine.trigger - 1_000
        assert _reference_budget(agent) == 1_000 * references.CHARS_PER_TOKEN
        agent.usage.last_prompt_tokens = engine.trigger + 5
        assert _reference_budget(agent) == 0  # no room: every reference becomes a pointer
        await svc.aclose()

    asyncio.run(main())


def test_compress_with_rotation_moves_the_attached_session(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    from indus.core.persist import StateStore
    from indus.protocol.events import EventBase
    from indus.state import registry
    from indus.state.session_lock import LockBusy, try_lock_session
    from tests.fakes.agent import DictConfig, run_driven
    from tests.unit.core.compression.test_engine import history

    rotating = scope.with_config(DictConfig({"compression": {"in_place": False}}))
    server = FakeModelServer(
        script_of(text_turn("## Goal\nnotes"), text_turn("carried on")), clock=fake_clock
    )
    handle = registry.open_unshared(scope)
    store = StateStore(handle)
    locks = tmp_path / "locks"
    svc = SessionService(
        rotating,
        fake_route(server),
        toolsets=[],
        options=fake_options(server, session_id=None),
        store=store,
        lock_dir=locks,
    )
    seen: list[EventBase] = []

    async def main() -> tuple[str, str]:
        sid = (await svc.create()).session_id
        svc.subscribe(sid, seen.append)
        await store.ensure_session(sid, source="cli", system_prompt="p", fields={})
        for row in history():
            await store.append(sid, row)
        preview = await svc.compress(sid, preview=True)
        assert preview.status == "preview"
        outcome = await svc.compress(sid, "the refactor")
        assert outcome.status == "compressed", outcome
        child = svc.current_id(sid)
        # `cronjob_manage run` results follow the session to its new id (P14-18)
        assert cron_tool._sink_for(child) is not None  # pyright: ignore[reportPrivateUsage]
        # the old id keeps working as an alias
        ticket = await svc.submit(sid, "next step")
        result = await svc.wait(sid, ticket.turn_id)
        assert result.session_id == child and result.final_text == "carried on"
        return sid, child

    try:
        sid, child = asyncio.run(run_driven(main(), fake_clock))
        assert child != sid and svc.attached() == [child]
        assert svc.agent(sid) is svc.agent(child) and svc.agent(child).session_id == child
        assert isinstance(try_lock_session(locks, child, owner="other"), LockBusy)
        free = try_lock_session(locks, sid, owner="other")
        assert not isinstance(free, LockBusy)  # the parent's lock was handed back
        free.release()
        assert seen[-1].session_id == child
        assert [r.content for r in asyncio.run(store.load(child)) if r.role == "user"][-1] == (
            "next step"
        )
        asyncio.run(svc.aclose())
        assert cron_tool._sink_for(child) is None  # pyright: ignore[reportPrivateUsage]
    finally:
        handle.close()
