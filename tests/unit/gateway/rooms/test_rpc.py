"""P24-06: the ``groups.*`` handler table over a real store and driver."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from indus.foundation.scope import Scope
from indus.gateway.rooms.driver import RoomDriver, TaskKey
from indus.gateway.rooms.rpc import METHODS, ConfirmRequired, GroupsRpc, GroupsRpcError
from indus.state.rooms import RoomAuthorityConflict, RoomStore
from tests.fakes.clock import FakeClock

MEMBERS = [{"profile": "alice"}, {"profile": "bob"}]


@pytest.fixture
def parts(scope: Scope) -> Iterator[tuple[RoomStore, RoomDriver, GroupsRpc]]:
    store = RoomStore(scope)
    driver = RoomDriver(store)
    yield store, driver, GroupsRpc(store, driver, process_generation="proc-test")
    store.close()


def test_capabilities_list_every_method(parts: tuple[RoomStore, RoomDriver, GroupsRpc]) -> None:
    _, _, rpc = parts
    caps = rpc.call("groups.capabilities")
    assert caps["methods"] == sorted(METHODS)
    assert caps["driver"] is True and caps["protocol"] == 2
    assert "not atomic" in caps["authority_transfer"]
    assert set(rpc.handlers()) == set(METHODS)


def test_create_send_log_round_trip(parts: tuple[RoomStore, RoomDriver, GroupsRpc]) -> None:
    _, _, rpc = parts
    rpc.call("groups.create", {"room_id": "r1", "name": "Plan", "members": MEMBERS})
    sent = rpc.call("groups.send", {"room_id": "r1", "client_event_id": "c1", "text": "hello"})
    again = rpc.call("groups.send", {"room_id": "r1", "client_event_id": "c1", "text": "hello"})
    assert sent["event"]["seq"] == again["event"]["seq"] == 1
    log = rpc.call("groups.log", {"room_id": "r1"})
    assert [e["kind"] for e in log["events"]] == ["message.user"]
    assert log["authority"]["epoch"] == 1
    listed = rpc.call("groups.list")
    assert [r["room_id"] for r in listed["rooms"]] == ["r1"]


def test_unknown_params_and_methods_are_refused(
    parts: tuple[RoomStore, RoomDriver, GroupsRpc],
) -> None:
    _, _, rpc = parts
    with pytest.raises(GroupsRpcError, match="unknown method"):
        rpc.call("groups.nope")
    with pytest.raises(GroupsRpcError, match="bad params at extra"):
        rpc.call("groups.list", {"extra": 1})
    with pytest.raises(GroupsRpcError, match="room_id"):
        rpc.call("groups.state", {})


def test_a_stop_directive_holds_the_member_and_cancels_its_queued_turn(
    parts: tuple[RoomStore, RoomDriver, GroupsRpc],
) -> None:
    _, driver, rpc = parts
    rpc.call("groups.create", {"room_id": "r1", "name": "Plan", "members": MEMBERS})
    bob = TaskKey("r1", "t-bob", "th", "turn-1")
    alice = TaskKey("r1", "t-alice", "th", "turn-2")
    driver.admit(bob, {"target": "bob", "prompt": "go", "source_seq": 1})
    driver.admit(alice, {"target": "alice", "prompt": "go", "source_seq": 1})
    out = rpc.call("groups.send", {"room_id": "r1", "client_event_id": "c", "text": "stop @bob"})
    assert (out["held"], out["cancelled"]) == (["bob"], ["t-bob"])
    assert driver.get_task(alice).status == "queued"
    kinds = [e["kind"] for e in rpc.call("groups.log", {"room_id": "r1"})["events"]]
    assert kinds == ["message.user", "member.held"]
    code = rpc.call(
        "groups.send", {"room_id": "r1", "client_event_id": "d", "text": "```\nstop @alice\n```"}
    )
    assert code["held"] == []


def test_stop_cancels_queued_and_stops_running(
    parts: tuple[RoomStore, RoomDriver, GroupsRpc],
) -> None:
    _, driver, rpc = parts
    rpc.call("groups.create", {"room_id": "r1", "name": "Plan", "members": MEMBERS})
    first = TaskKey("r1", "t1", "th", "turn-1")
    second = TaskKey("r1", "t2", "th", "turn-2")
    driver.admit(first, {"target": "bob", "prompt": "a", "source_seq": 1})
    driver.admit(second, {"target": "alice", "prompt": "b", "source_seq": 2})
    lease = driver.acquire_lease("r1", process_generation="proc-test", ttl_seconds=60)
    driver.start(first, lease, expected_cancel_gen=0)
    out = rpc.call("groups.stop", {"room_id": "r1", "cancel_id": "stop-1"})
    assert (out["cancelled"], out["stopping"]) == (["t2"], ["t1"])
    assert out["event"]["kind"] == "room.stop_requested"
    status = rpc.call("groups.status", {"room_id": "r1"})
    assert status["blocked"] is True and status["counts"] == {"stopping": 1, "cancelled": 1}


def test_retry_requeues_indeterminate_work_only_on_request(
    parts: tuple[RoomStore, RoomDriver, GroupsRpc], fake_clock: FakeClock
) -> None:
    _, driver, rpc = parts
    rpc.call("groups.create", {"room_id": "r1", "name": "Plan", "members": MEMBERS})
    key = TaskKey("r1", "t1", "th", "turn-1")
    driver.admit(key, {"target": "bob", "prompt": "a", "source_seq": 1})
    old = driver.acquire_lease("r1", process_generation="proc-dead", ttl_seconds=10)
    driver.start(key, old, expected_cancel_gen=0)
    fake_clock.advance(10)
    new = driver.acquire_lease("r1", process_generation="proc-test", ttl_seconds=60)
    driver.recover(new)
    status = rpc.call("groups.status", {"room_id": "r1"})
    assert status["pending_actions"] == [{"kind": "retry", "task_id": "t1"}]
    out = rpc.call("groups.retry", {"room_id": "r1", "task_id": "t1"})
    assert out["task"]["status"] == "queued"


def test_promote_needs_confirm_and_demote_needs_a_newer_lineage(
    parts: tuple[RoomStore, RoomDriver, GroupsRpc],
) -> None:
    store, _, rpc = parts
    rpc.call("groups.create", {"room_id": "r1", "name": "Plan", "members": MEMBERS})
    store.claim_authority(
        "r1", expected=store.gateway_id, expected_epoch=1, new="gw-old-host", event_id="c"
    )
    with pytest.raises(ConfirmRequired):
        rpc.call("groups.promote", {"room_id": "r1"})
    promoted = rpc.call("groups.promote", {"room_id": "r1", "confirm": True})
    assert (promoted["authority"], promoted["epoch"]) == (store.gateway_id, 3)
    again = rpc.call("groups.promote", {"room_id": "r1", "confirm": True})
    assert again["idempotent"] is True
    with pytest.raises(RoomAuthorityConflict, match="newer"):
        rpc.call(
            "groups.demote",
            {"room_id": "r1", "observed_gateway_id": "gw-other", "observed_epoch": 3},
        )
    demoted = rpc.call(
        "groups.demote", {"room_id": "r1", "observed_gateway_id": "gw-other", "observed_epoch": 4}
    )
    assert (demoted["authority"], demoted["epoch"]) == ("gw-other", 4)
    with pytest.raises(RoomAuthorityConflict):  # the demoted host's sends are refused
        rpc.call("groups.send", {"room_id": "r1", "client_event_id": "x", "text": "hi"})


def test_rename_and_disband(parts: tuple[RoomStore, RoomDriver, GroupsRpc]) -> None:
    _, _, rpc = parts
    rpc.call("groups.create", {"room_id": "r1", "name": "Plan", "members": MEMBERS})
    assert (
        rpc.call("groups.rename", {"room_id": "r1", "event_id": "n1", "name": "New"})["room"][
            "name"
        ]
        == "New"
    )
    done = rpc.call("groups.disband", {"room_id": "r1"})
    assert done["idempotent"] is False
    state = rpc.call("groups.state", {"room_id": "r1", "include_disbanded": True})
    assert "disbanded_at" in state["room"]


def test_state_shows_the_authority_claim_after_a_promotion(
    parts: tuple[RoomStore, RoomDriver, GroupsRpc],
) -> None:
    store, _, rpc = parts
    rpc.call("groups.create", {"room_id": "r1", "name": "Plan", "members": MEMBERS})
    assert "authority_claim" not in rpc.call("groups.state", {"room_id": "r1"})
    store.claim_authority(
        "r1", expected=store.gateway_id, expected_epoch=1, new="gw-other", event_id="c"
    )
    rpc.call("groups.promote", {"room_id": "r1", "confirm": True})
    state = rpc.call("groups.state", {"room_id": "r1"})
    assert state["authority_claim"]["epoch"] == state["room"]["epoch"] == 3
