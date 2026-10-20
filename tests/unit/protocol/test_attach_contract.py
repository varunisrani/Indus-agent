"""P17-25: the attach transport's methods, events, notifications and error codes are declared in
``indus.protocol``, generated into ``packages/protocol``, and served as declared by the daemon.

The daemon side runs over a real ``DaemonSessionHost`` on the fake model through the in-loop ASGI
WebSocket double (no socket is opened).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import pytest

from indus.foundation.scope import Scope
from indus.gateway.ws.server import WsEndpoint
from indus.protocol import codegen, errors
from indus.protocol.attach import (
    ATTACH_NOTIFICATIONS,
    DAEMON_ONLY_METHODS,
    RuntimeDescribeParams,
    SessionReplayGap,
    SessionUpdated,
)
from indus.protocol.base import ProtocolModel
from indus.protocol.codegen import GENERATED_DIR, render_all
from indus.protocol.events import EVENTS, RequestSettled, SessionViewers
from indus.protocol.methods import METHODS, NOTIFICATIONS, PROTOCOL_VERSION, MethodSpec
from indus.service.event_log import ReplayGap
from indus.service.request_redelivery import NOT_SHOWN_CODE
from indus.service.subscriptions import Outbox
from indus.service.ws_auth import (
    CLOSE_BAD_TICKET,
    SUBPROTOCOL,
    TICKET_PROTOCOL_PREFIX,
    WS_INTERACTIVE,
    TicketBook,
)
from indus.state.registry import StoreHandle
from tests.fakes.asgi_ws import AsgiWsClient
from tests.fakes.model import text_turn
from tests.fakes.session_host import open_host
from tests.unit.backends._aio import aio

NEW_METHODS = (
    "runtime.describe",
    "session.subscribe",
    "session.unsubscribe",
    "sessions.watch",
    "events.since",
    "admission.receipt",
    "admission.discard",
    "maintenance.run",
)
NEW_EVENTS = ("session.viewers", "admission.state", "request.settled")
NEW_NOTIFICATIONS = ("gateway.ready", "session.replay_gap", "session.updated")
NEW_CODES = (
    "STALE_GENERATION",
    "TICKET_INVALID",
    "PROFILE_MISMATCH",
    "ADMISSION_CONFLICT",
    "QUEUE_FULL",
    "OUTCOME_UNKNOWN",
    "POLICY_CONFLICT",
    "LEASE_LOST",
    "NOT_SHOWN",
)


# -- the declared contract ------------------------------------------------------------------------


def test_schema_snapshot_includes_every_new_name() -> None:
    schema = json.loads(render_all()["schema.json"])
    catalog = schema["x-indus-catalog"]
    defs = schema["$defs"]
    for name in NEW_METHODS:
        assert name in catalog["methods"], name
        entry = catalog["methods"][name]
        assert entry["params"] in defs and entry["result"] in defs
        assert name in DAEMON_ONLY_METHODS  # the stdio bridge answers them -32601
    for name in NEW_EVENTS:
        assert name in catalog["events"] and catalog["events"][name] in defs, name
    for name in NEW_NOTIFICATIONS:
        assert name in catalog["notifications"], name
        assert catalog["notifications"][name] in defs
    for name in NEW_CODES:
        assert name in catalog["errors"], name
    types_ts = render_all()["types.ts"]
    for type_name in ("RuntimeDescribeResult", "SessionViewers", "RequestSettled"):
        assert f"export interface {type_name} " in types_ts


def test_each_error_code_appears_once_with_one_meaning() -> None:
    names = Counter(e.name for e in errors.ERROR_TABLE)
    numbers = Counter(e.code for e in errors.ERROR_TABLE)
    assert all(n == 1 for n in names.values()) and all(n == 1 for n in numbers.values())
    meanings = Counter(e.meaning for e in errors.ERROR_TABLE)
    assert all(n == 1 for n in meanings.values())
    catalog = json.loads(render_all()["schema.json"])["x-indus-catalog"]["errors"]
    for name in NEW_CODES:
        row = errors.BY_NAME[name]
        assert catalog[name]["code"] == row.code and catalog[name]["meaning"] == row.meaning
    # Numbers that stand for the wire values they name.
    assert errors.TICKET_INVALID.code == CLOSE_BAD_TICKET == 4401
    assert errors.NOT_SHOWN.code == NOT_SHOWN_CODE == 4404
    assert errors.STALE_GENERATION.group is errors.ErrorGroup.CONFLICT


@pytest.mark.parametrize(
    ("product", "expected"),
    [
        ("runtime.stale_generation", "STALE_GENERATION"),
        ("runtime.admission_conflict", "ADMISSION_CONFLICT"),
        ("runtime.queue_full", "QUEUE_FULL"),
        ("runtime.unknown_execution", "OUTCOME_UNKNOWN"),
        ("runtime.policy_conflict", "POLICY_CONFLICT"),
        ("store.turn_lease_lost", "LEASE_LOST"),
    ],
)
def test_product_codes_reach_their_one_row(product: str, expected: str) -> None:
    assert errors.error_for_indus_code(product) is errors.BY_NAME[expected]


def test_codes_outside_method_answers_say_where_they_travel() -> None:
    out_of_band = {e.name for e in errors.OUT_OF_BAND_ERRORS}
    assert out_of_band == {"STALE_GENERATION", "PROFILE_MISMATCH", "TICKET_INVALID", "NOT_SHOWN"}
    declared = {e.name for spec in METHODS.values() for e in spec.errors}
    assert {"ADMISSION_CONFLICT", "QUEUE_FULL", "OUTCOME_UNKNOWN", "LEASE_LOST"} <= set(
        e.name for e in METHODS["prompt.submit"].errors
    )
    assert "POLICY_CONFLICT" in {e.name for e in METHODS["session.resume"].errors}
    assert set(NEW_CODES) <= declared | out_of_band


def test_codegen_check_fails_when_an_attach_model_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert codegen.main(["--root", str(tmp_path)]) == 0
    assert codegen.main(["--check", "--root", str(tmp_path)]) == 0

    class RuntimeDescribeResult(ProtocolModel):
        """What the control socket's ``describe`` answers, plus the replay epoch."""

        profile_id: str
        """The profile the daemon serves."""
        pid: int
        """A field added after the files were generated."""

    old = METHODS["runtime.describe"]
    changed = dict(METHODS)
    changed["runtime.describe"] = MethodSpec(
        old.name, RuntimeDescribeParams, RuntimeDescribeResult, old.errors
    )
    monkeypatch.setattr(codegen, "METHODS", MappingProxyType(changed))
    assert codegen.main(["--check", "--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "+  pid: number;" in out and "stale" in out

    monkeypatch.undo()

    class SessionUpdatedNext(SessionUpdated):
        """A session changed in a way list views show."""

        pinned_at: float | None = None
        """A field added after the files were generated."""

    monkeypatch.setattr(
        codegen,
        "NOTIFICATIONS",
        MappingProxyType({**NOTIFICATIONS, "session.updated": SessionUpdatedNext}),
    )
    assert codegen.main(["--check", "--root", str(tmp_path)]) == 1


def test_committed_output_is_current() -> None:
    assert codegen.main(["--check"]) == 0
    assert (codegen.REPO_ROOT / GENERATED_DIR / "catalog.ts").read_text(encoding="utf-8").count(
        '"runtime.describe"'
    ) >= 1


# -- what the daemon emits matches the declared models --------------------------------------------


def test_unnumbered_frames_match_their_notification_models() -> None:
    gap = ReplayGap(session_id="s1", replay_epoch="e1", last_seq=4, reason="evicted").to_wire()
    assert SessionReplayGap.model_validate(gap).reason == "evicted"
    outbox = Outbox()
    assert outbox.put_update("s1", {"running": True, "last_activity": 1.0})
    assert outbox.put_update("s1", {"title": None, "last_activity": 2.0})  # merged in place
    text = outbox.take()
    assert text is not None
    frame = json.loads(text)
    assert frame["method"] == "session.updated"
    updated = SessionUpdated.model_validate(frame["params"])
    assert updated.running is True and updated.last_activity == 2.0
    assert set(ATTACH_NOTIFICATIONS) <= set(NOTIFICATIONS)
    assert not set(NOTIFICATIONS) & set(EVENTS)  # numbered events and plain notifications apart


# -- the daemon serves the new methods ------------------------------------------------------------


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    from indus.state import registry

    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


class Rig:
    def __init__(self, endpoint: WsEndpoint, book: TicketBook) -> None:
        self.endpoint = endpoint
        self.book = book
        self.host = endpoint.host

    async def client(self, surface: Literal["cli", "tui"] = "cli") -> AsgiWsClient:
        ticket = self.book.mint(WS_INTERACTIVE).ticket
        client = AsgiWsClient(
            self.endpoint.app, subprotocols=[SUBPROTOCOL, TICKET_PROTOCOL_PREFIX + ticket]
        )
        assert await client.open() == SUBPROTOCOL
        assert (await client.recv())["method"] == "gateway.ready"
        await client.request(
            "client.capabilities", {"features": ["server_requests"], "surface": surface}
        )
        return client

    async def close(self) -> None:
        await self.endpoint.stop()
        assert self.endpoint.violations == []  # every emitted event matched its model
        await self.host.aclose()


async def rig(scope: Scope, store: StoreHandle, tmp_path: Path, *turns: Any) -> Rig:
    host = await open_host(scope, store, tmp_path, *turns)
    book = TicketBook.for_home(scope.home)
    return Rig(WsEndpoint(host, book, scope=scope, strict=True), book)


def result_of(frame: dict[str, Any]) -> dict[str, Any]:
    assert "result" in frame, frame
    return frame["result"]


def error_of(frame: dict[str, Any]) -> int:
    assert "error" in frame, frame
    return int(frame["error"]["code"])


@aio
async def test_runtime_describe_over_the_websocket(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path)
    client = await r.client()
    described = result_of(await client.request("runtime.describe", {}))
    assert described["profile_id"] == r.book.profile_id
    assert described["instance_id"] == r.book.instance_id
    assert described["protocol_version"] == PROTOCOL_VERSION
    assert described["replay_epoch"] == r.host.replay_epoch
    assert described["creatable_sources"] == list(r.host.creatable_sources())
    assert described["capabilities"] == list(r.host.capabilities())
    assert "ticket" not in json.dumps(described)
    assert error_of(await client.request("runtime.describe", {"x": 1})) == (
        errors.UNKNOWN_PARAM_KEY.code
    )
    await client.close()
    await r.close()


@aio
async def test_admission_receipt_is_per_session(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path, text_turn("one"))
    first, second = await r.client(), await r.client("tui")
    sid = result_of(await first.request("session.create", {}))["session_id"]
    other = result_of(await second.request("session.create", {}))["session_id"]
    submitted = result_of(
        await first.request("prompt.submit", {"session_id": sid, "text": "hi", "request_id": "r1"})
    )
    admission = submitted["turn_id"]
    await r.host.wait(admission)
    receipt = result_of(
        await first.request("admission.receipt", {"session_id": sid, "admission_id": admission})
    )
    assert receipt["state"] == "terminal" and receipt["outcome"] == "completed"
    assert receipt["session_id"] == sid and receipt["generation"] >= max(1, submitted["generation"])
    # Another session's admission, or an unknown id, reads as not found.
    foreign = await second.request(
        "admission.receipt", {"session_id": other, "admission_id": admission}
    )
    assert error_of(foreign) == errors.ADMISSION_NOT_FOUND.code
    missing = await first.request("admission.receipt", {"session_id": sid, "admission_id": "nope"})
    assert error_of(missing) == errors.ADMISSION_NOT_FOUND.code
    # The same request id with another text is a conflict with its own code.
    conflict = await first.request(
        "prompt.submit", {"session_id": sid, "text": "changed", "request_id": "r1"}
    )
    assert error_of(conflict) == errors.ADMISSION_CONFLICT.code
    # session.viewers went out (two connections) and matched its model in strict mode.
    viewers = [n for n in first.notes if n.get("method") == "session.viewers"]
    assert viewers
    params = {k: v for k, v in viewers[-1]["params"].items() if k != "replay_epoch"}
    assert SessionViewers.model_validate(params).count >= 1
    await first.close()
    await second.close()
    await r.close()


@aio
async def test_an_exact_retry_of_a_lost_input_is_outcome_unknown(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    from indus.state import messages, sessions
    from indus.state import runtime_ledger as ledger

    sessions.create(store, "s-lost", "cli")
    messages.append(store, "s-lost", {"role": "user", "content": "deploy it"})
    epoch = ledger.begin_owner_epoch(store, "dead-owner").epoch
    ledger.admit(
        store,
        epoch=epoch,
        principal_id="attach:cli",
        session_id="s-lost",
        request_id="lost-1",
        payload={"text": "deploy it"},
    )
    assert ledger.claim_head(store, epoch=epoch, session_id="s-lost").kind == "claimed"
    r = await rig(scope, store, tmp_path)
    client = await r.client()
    result_of(await client.request("session.resume", {"session_id": "s-lost"}))
    retry = await client.request(
        "prompt.submit", {"session_id": "s-lost", "text": "deploy it", "request_id": "lost-1"}
    )
    assert error_of(retry) == errors.OUTCOME_UNKNOWN.code  # never "streaming" a lost turn
    await client.close()
    await r.close()


@aio
async def test_maintenance_run_inside_the_daemon(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    from indus.state import sessions

    sessions.create(store, "old-1", "cli")
    r = await rig(scope, store, tmp_path)
    client = await r.client()
    pruned = result_of(await client.request("maintenance.run", {"op": "prune", "ids": ["old-1"]}))
    assert pruned["op"] == "prune" and "refused" not in pruned
    assert pruned["report"]["deleted"] + pruned["report"]["removed"] >= 1
    assert sessions.get(store, "old-1") is None
    bad = result_of(await client.request("maintenance.run", {"op": "import"}))
    assert bad["refused"] == "bad_params" and bad["report"] == {}
    unknown_op = await client.request("maintenance.run", {"op": "vacuum"})
    assert error_of(unknown_op) == errors.INVALID_PARAMS.code
    await client.close()
    await r.close()


def test_request_settled_model_matches_the_book() -> None:
    from indus.service.request_redelivery import RequestSettlement

    event = {
        "type": "request.settled",
        "session_id": "s1",
        "seq": 3,
        "request_id": "srq-0123456789ab",
        "kind": "clarify",
        "outcome": RequestSettlement.UNDELIVERABLE.value,
        "reason": "not_shown",
    }
    assert RequestSettled.model_validate(event).outcome == "undeliverable"
    assert {s.value for s in RequestSettlement} == {"answered", "withdrawn", "undeliverable"}


@aio
async def test_the_attach_host_keeps_a_lost_input_when_the_daemon_says_outcome_unknown(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    from indus.service.remote.channel import Channel, ChannelLimits
    from indus.service.remote.host import OutcomeUnknown, RemoteSessionService
    from indus.service.remote.journal import Journal, new_request_id
    from indus.state import messages, sessions
    from indus.state import runtime_ledger as ledger
    from tests.unit.service.remote.wire import rig as wire_rig

    lost_id = new_request_id()
    sessions.create(store, "s-lost", "cli")
    messages.append(store, "s-lost", {"role": "user", "content": "deploy it"})
    epoch = ledger.begin_owner_epoch(store, "dead-owner").epoch
    ledger.admit(
        store,
        epoch=epoch,
        principal_id="attach:cli",
        session_id="s-lost",
        request_id=lost_id,
        payload={"text": "deploy it"},
    )
    assert ledger.claim_head(store, epoch=epoch, session_id="s-lost").kind == "claimed"
    r = await wire_rig(scope, store, tmp_path)
    channel = Channel(r.dial, limits=ChannelLimits(backoff_first=0.01, backoff_cap=0.05))
    svc = await RemoteSessionService(channel, Journal(scope.home)).start()
    try:
        await svc.resume("s-lost")
        svc.journal.prepare("s-lost", "deploy it", request_id=lost_id)
        with pytest.raises(OutcomeUnknown) as lost:
            await svc.retry(lost_id)  # the person retries a submission the daemon lost
        assert lost.value.request_id == lost_id
        kept = svc.journal.get(lost_id)
        assert kept is not None and kept.state == "unknown"  # kept for an explicit decision
    finally:
        await svc.aclose()
        await r.close()
