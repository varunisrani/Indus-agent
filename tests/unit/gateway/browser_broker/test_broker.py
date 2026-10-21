"""P24-47: the broker — single-use 30 s tickets bound to the identity, identity-exact routing,
complete-once commands, detach vs disconnect, the developer gate, deferred cancels, artefacts."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.gateway.browser_broker import broker as bb
from indus.gateway.browser_broker.artifacts import ArtifactStore
from indus.gateway.browser_broker.broker import BrowserBroker, Identity
from indus.service.ws_auth import (
    BROWSER_CONTROLLER,
    WS_INTERACTIVE,
    TicketBook,
    UpgradeRefused,
)
from tests.fakes.clock import FakeClock, checkpoint

PROFILE = "p" * 16
ME = Identity("api", "key-1", "default", "ext-1", "sess-1", PROFILE)
EVERYTHING = sorted(bb.ALLOWED_CAPABILITIES | bb.ARTIFACT_CAPABILITIES | bb.DEVELOPER_CAPABILITIES)


class Controller:
    """A fake controller socket: frames it was sent; ``fail`` makes sending raise."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.fail = False

    async def send(self, frame: dict[str, Any]) -> None:
        if self.fail:
            raise ConnectionError("socket gone")
        self.frames.append(frame)

    def commands(self) -> list[dict[str, Any]]:
        return [f["params"] for f in self.frames if f["method"] == bb.FRAME_COMMAND]

    def cancels(self) -> list[dict[str, Any]]:
        return [f["params"] for f in self.frames if f["method"] == bb.FRAME_CANCEL]


@dataclasses.dataclass
class World:
    clock: FakeClock
    broker: BrowserBroker
    dev: list[bool]


def run(work: Callable[[World], Awaitable[None]], tmp_path: Path | None = None, **kw: Any) -> None:
    async def main() -> None:
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        book = TicketBook(profile_id=PROFILE, monotonic=clock.monotonic)
        dev = [False]
        store = ArtifactStore(tmp_path / "art", monotonic=clock.monotonic) if tmp_path else None
        broker = BrowserBroker(
            book, clock=clock, developer_mode=lambda: dev[0], artifacts=store, **kw
        )
        await work(World(clock, broker, dev))
        await broker.aclose()

    asyncio.run(main())


def upgrade(ticket: str | None, *, query: str = "", peer: str = "127.0.0.1",
            base: bool = True) -> dict[str, Any]:  # fmt: skip
    offered = [bb.SUBPROTOCOL] if base else []
    if ticket is not None:
        offered.append(bb.TICKET_PREFIX + ticket)
    return {
        "type": "websocket",
        "client": (peer, 40000),
        "headers": [],
        "subprotocols": offered,
        "query_string": query.encode(),
    }


async def attached(w: World, identity: Identity = ME, caps: list[str] | None = None) -> Controller:
    fake = Controller()
    await w.broker.attach(identity, caps if caps is not None else EVERYTHING, fake.send, owner=fake)
    return fake


async def started(
    w: World,
    capability: str = "navigate",
    identity: Identity = ME,
    *,
    arguments: dict[str, Any] | None = None,
    call_id: str | None = None,
) -> asyncio.Task[Any]:
    """Start one call and let it reach the controller."""
    if arguments is None and capability == "navigate":
        arguments = {"url": "https://example.org"}
    task = asyncio.ensure_future(w.broker.call(identity, capability, arguments, call_id=call_id))
    for _ in range(5):
        await checkpoint()
    return task


# -- tickets ------------------------------------------------------------------------------------


def test_ticket_admits_once_and_binds_the_identity() -> None:
    async def work(w: World) -> None:
        reg = w.broker.register(ME)
        assert reg.ticket not in repr(reg) and reg.expires_in == 30.0
        body = reg.body()
        assert body["subprotocol"] == bb.SUBPROTOCOL and body["protocol"] == 1
        assert w.broker.admit(upgrade(reg.ticket)) == ME
        again = w.broker.admit(upgrade(reg.ticket))
        assert isinstance(again, UpgradeRefused) and again.close_code == 4401

    run(work)


def test_ticket_older_than_30_s_refused() -> None:
    async def work(w: World) -> None:
        reg = w.broker.register(ME)
        w.clock.advance(30.0)
        refused = w.broker.admit(upgrade(reg.ticket))
        assert isinstance(refused, UpgradeRefused) and refused.close_code == 4401
        fresh = w.broker.register(ME)
        w.clock.advance(29.0)
        assert w.broker.admit(upgrade(fresh.ticket)) == ME

    run(work)


def test_ticket_in_the_query_is_spent_and_refused() -> None:
    async def work(w: World) -> None:
        reg = w.broker.register(ME)
        refused = w.broker.admit(upgrade(None, query=f"ticket={reg.ticket}"))
        assert isinstance(refused, UpgradeRefused) and refused.close_code == 4401
        after = w.broker.admit(upgrade(reg.ticket))
        assert isinstance(after, UpgradeRefused)

    run(work)


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [({"peer": "10.0.0.5"}, 4403), ({"base": False}, 4401)],
)
def test_refused_upgrades_spend_the_ticket(kwargs: dict[str, Any], code: int) -> None:
    async def work(w: World) -> None:
        reg = w.broker.register(ME)
        refused = w.broker.admit(upgrade(reg.ticket, **kwargs))
        assert isinstance(refused, UpgradeRefused) and refused.close_code == code
        assert isinstance(w.broker.admit(upgrade(reg.ticket)), UpgradeRefused)

    run(work)


def test_ticket_minted_elsewhere_or_for_another_purpose_refused() -> None:
    async def work(w: World) -> None:
        stray = w.broker.book.mint(BROWSER_CONTROLLER)  # no identity bound by register
        refused = w.broker.admit(upgrade(stray.ticket))
        assert isinstance(refused, UpgradeRefused) and refused.reason == "ticket unknown"
        other = w.broker.book.mint(WS_INTERACTIVE)
        wrong = w.broker.admit(upgrade(other.ticket))
        assert isinstance(wrong, UpgradeRefused) and wrong.close_code == 4401

    run(work)


def test_disabled_broker_registers_nothing() -> None:
    async def work(w: World) -> None:
        with pytest.raises(bb.RegistrationRefused):
            w.broker.register(ME)

    run(work, enabled=lambda: False)


def test_book_without_the_purpose_is_refused() -> None:
    clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
    book = TicketBook(profile_id=PROFILE, purposes={WS_INTERACTIVE})
    with pytest.raises(ValueError):
        BrowserBroker(book, clock=clock)


def test_protocol_version_is_exactly_int_one() -> None:
    assert bb.protocol_supported(1)
    for bad in (True, 1.0, "1", 2, None):
        assert not bb.protocol_supported(bad)


# -- routing ------------------------------------------------------------------------------------


@pytest.mark.parametrize("attribute", [f.name for f in dataclasses.fields(Identity)])
def test_one_differing_attribute_gets_no_controller(attribute: str) -> None:
    async def work(w: World) -> None:
        fake = await attached(w)
        other = dataclasses.replace(ME, **{attribute: "someone-else"})
        with pytest.raises(bb.ControllerUnavailable):
            await w.broker.call(other, "navigate", {"url": "https://example.org"})
        assert fake.frames == []

    run(work)


def test_capability_must_be_negotiated_and_known() -> None:
    async def work(w: World) -> None:
        fake = await attached(w, caps=["navigate", "teleport", 7])  # type: ignore[list-item]
        with pytest.raises(bb.ControllerUnavailable):
            await w.broker.call(ME, "click", {"ref": "e1"})
        with pytest.raises(bb.CapabilityRefused):
            await w.broker.call(ME, "teleport")
        assert fake.frames == []

    run(work)


def test_lane_lookup() -> None:
    async def work(w: World) -> None:
        await attached(w)
        assert w.broker.lane(session="sess-1", principal="key-1", transport_family="api") == ME
        assert w.broker.lane(session="sess-1", principal="key-2", transport_family="api") is None

    run(work)


# -- commands -----------------------------------------------------------------------------------


def test_a_command_answered_twice_keeps_the_first() -> None:
    async def work(w: World) -> None:
        fake = await attached(w)
        task = await started(w, call_id="tc-1")
        (command,) = fake.commands()
        assert command["capability"] == "navigate" and command["call_id"] == "tc-1"
        assert len(command["command_id"]) == 32
        intruder = dataclasses.replace(ME, controller_id="ext-2")
        assert not w.broker.complete(command["command_id"], intruder, ok=True, result="forged")
        assert w.broker.complete(command["command_id"], ME, ok=True, result={"title": "first"})
        assert not w.broker.complete(command["command_id"], ME, ok=True, result={"title": "second"})
        assert await task == {"title": "first"}
        assert w.broker.pending_count == 0

    run(work)


def test_rejected_answer_raises() -> None:
    async def work(w: World) -> None:
        fake = await attached(w)
        task = await started(w)
        w.broker.complete(fake.commands()[0]["command_id"], ME, ok=False, result="nope")
        with pytest.raises(bb.ControllerRejected):
            await task

    run(work)


def test_timeout_after_30_s_sends_a_cancel() -> None:
    async def work(w: World) -> None:
        fake = await attached(w)
        task = await started(w, call_id="tc-9")
        await w.clock.advance_and_settle(29.0)
        assert not task.done()
        await w.clock.advance_and_settle(1.0)
        with pytest.raises(bb.ControllerTimeout):
            await task
        assert fake.cancels() == [
            {"command_id": fake.commands()[0]["command_id"], "call_id": "tc-9"}
        ]
        assert not w.broker.complete(fake.commands()[0]["command_id"], ME, ok=True)

    run(work)


def test_detach_fails_pending_work_while_a_drop_only_marks_offline() -> None:
    async def work(w: World) -> None:
        fake = await attached(w)
        waiting = await started(w)
        assert w.broker.disconnect(ME, owner=fake)
        assert not w.broker.is_connected(ME) and not waiting.done()
        with pytest.raises(bb.ControllerUnavailable):
            await w.broker.call(ME, "back")
        again = Controller()
        await w.broker.attach(ME, EVERYTHING, again.send, owner=again)
        assert w.broker.complete(fake.commands()[0]["command_id"], ME, ok=True, result="done")
        assert await waiting == "done"
        doomed = await started(w)
        assert await w.broker.detach(ME) == 1
        with pytest.raises(bb.ControllerCancelled):
            await doomed
        assert again.cancels() and w.broker.pending_count == 0
        with pytest.raises(bb.ControllerUnavailable):
            await w.broker.call(ME, "back")

    run(work)


def test_disconnect_needs_the_owning_socket() -> None:
    async def work(w: World) -> None:
        await attached(w)
        assert not w.broker.disconnect(ME, owner=object())
        assert w.broker.is_connected(ME)

    run(work)


def test_another_controller_in_the_lane_replaces_the_old_one() -> None:
    async def work(w: World) -> None:
        await attached(w)
        task = await started(w)
        newer = dataclasses.replace(ME, controller_id="ext-2", browser_profile="work")
        await attached(w, newer)
        with pytest.raises(bb.ControllerCancelled):
            await task
        assert w.broker.lane(session="sess-1", principal="key-1", transport_family="api") == newer

    run(work)


def test_a_send_failure_is_unavailable_and_leaves_nothing_pending() -> None:
    async def work(w: World) -> None:
        fake = await attached(w)
        fake.fail = True
        with pytest.raises(bb.ControllerUnavailable):
            await w.broker.call(ME, "back")
        assert w.broker.pending_count == 0

    run(work)


def test_cancel_by_call_id() -> None:
    async def work(w: World) -> None:
        fake = await attached(w)
        task = await started(w, call_id="tc-2")
        assert await w.broker.cancel(ME, "tc-2")
        with pytest.raises(bb.ControllerCancelled):
            await task
        assert [c["call_id"] for c in fake.cancels()] == ["tc-2"]
        assert not await w.broker.cancel(ME, "tc-2")

    run(work)


def test_deferred_cancels_are_bounded_and_flushed_on_reconnect() -> None:
    async def work(w: World) -> None:
        fake = await attached(w)
        tasks = [
            await started(w, "back", call_id=f"tc-{i}") for i in range(bb.MAX_DEFERRED_CANCELS + 8)
        ]
        w.broker.disconnect(ME, owner=fake)
        await w.clock.advance_and_settle(bb.COMMAND_TIMEOUT_S)
        for task in tasks:
            with pytest.raises(bb.ControllerTimeout):
                await task
        assert w.broker.deferred_cancels(ME) == bb.MAX_DEFERRED_CANCELS
        back = Controller()
        await w.broker.attach(ME, EVERYTHING, back.send, owner=back)
        flushed = [c["call_id"] for c in back.cancels()]
        assert len(flushed) == bb.MAX_DEFERRED_CANCELS
        assert flushed[0] == "tc-8" and flushed[-1] == f"tc-{bb.MAX_DEFERRED_CANCELS + 7}"
        assert w.broker.deferred_cancels(ME) == 0

    run(work)


# -- the developer gate -------------------------------------------------------------------------


def test_evaluate_refused_outside_developer_mode() -> None:
    async def work(w: World) -> None:
        fake = await attached(w)
        with pytest.raises(bb.CapabilityRefused):
            await w.broker.call(ME, "evaluate", {"expression": "1+1"})
        assert bb.DEVELOPER_CAPABILITIES.isdisjoint(bb.negotiate(EVERYTHING, developer_mode=False))
        w.dev[0] = True
        dev = await attached(w)  # negotiated again with developer mode on
        task = await started(w, "evaluate", arguments={"expression": "1+1"})
        w.broker.complete(dev.commands()[0]["command_id"], ME, ok=True, result=2)
        assert await task == 2
        w.dev[0] = False  # switched off later: refused at once, though negotiated
        with pytest.raises(bb.CapabilityRefused):
            await w.broker.call(ME, "cdp", {"method": "Page.reload"})
        assert fake.frames == []

    run(work)


def test_developer_capability_needs_negotiation_too() -> None:
    async def work(w: World) -> None:
        w.dev[0] = True
        await attached(w, caps=["navigate"])
        with pytest.raises(bb.ControllerUnavailable):
            await w.broker.call(ME, "evaluate", {"expression": "1"})

    run(work)


# -- artefact capabilities ----------------------------------------------------------------------


def test_artifact_commands_carry_only_a_live_id_of_the_same_scope(tmp_path: Path) -> None:
    async def work(w: World) -> None:
        store = w.broker.artifacts
        assert store is not None
        fake = await attached(w)
        mine = store.put(ME.scope_key(), b"%PDF-1.7", "application/pdf")
        theirs = store.put(
            dataclasses.replace(ME, session="sess-2").scope_key(), b"x", "text/plain"
        )
        for args in (
            {"artifact_id": theirs.artifact_id},
            {"artifact_id": "f" * 32},
            {"artifact_id": mine.artifact_id, "path": "/etc/passwd"},
            {"bytes": "AAAA"},
        ):
            with pytest.raises(bb.ControllerRejected):
                await w.broker.call(ME, "artifact_upload", args)
        assert fake.frames == []
        task = await started(w, "artifact_upload", arguments={"artifact_id": mine.artifact_id})
        assert fake.commands()[0]["arguments"] == {"artifact_id": mine.artifact_id}
        w.broker.complete(fake.commands()[0]["command_id"], ME, ok=True)
        assert await task is None

    run(work, tmp_path)


def test_scope_key_changes_with_every_attribute() -> None:
    keys = {ME.scope_key()} | {
        dataclasses.replace(ME, **{f.name: "x"}).scope_key() for f in dataclasses.fields(Identity)
    }
    assert len(keys) == 7


def test_a_failed_flush_keeps_the_rest_of_the_cancels() -> None:
    async def work(w: World) -> None:
        fake = await attached(w)
        tasks = [await started(w, "back", call_id=f"tc-{i}") for i in range(3)]
        w.broker.disconnect(ME, owner=fake)
        await w.clock.advance_and_settle(bb.COMMAND_TIMEOUT_S)
        for task in tasks:
            with pytest.raises(bb.ControllerTimeout):
                await task
        broken = Controller()
        broken.fail = True
        with pytest.raises(bb.ControllerUnavailable):
            await w.broker.attach(ME, EVERYTHING, broken.send, owner=broken)
        assert w.broker.deferred_cancels(ME) == 3 and not w.broker.is_connected(ME)
        good = Controller()
        await w.broker.attach(ME, EVERYTHING, good.send, owner=good)
        assert [c["call_id"] for c in good.cancels()] == ["tc-0", "tc-1", "tc-2"]

    run(work)
