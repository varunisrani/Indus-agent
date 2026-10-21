"""P24-05: the group room store — limits, idempotent events, the control reserve, tombstones."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from typing import Any, cast

import pytest

from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.state.rooms import (
    DEFAULT_LIMITS,
    PROTOCOL_VERSION,
    ROOMS_FILE,
    RoomAuthorityConflict,
    RoomAuthoritySuperseded,
    RoomConflict,
    RoomCursorMoved,
    RoomError,
    RoomEventConflict,
    RoomFull,
    RoomLimits,
    RoomNotFound,
    RoomStore,
    user_event_id,
)
from tests.fakes.clock import FakeClock

DAY = 86_400.0
MEMBERS = [{"profile": "alice"}, {"profile": "bob"}]


@pytest.fixture
def store(scope: Scope) -> Iterator[RoomStore]:
    s = RoomStore(scope)
    yield s
    s.close()


def _small(scope: Scope, **limits: Any) -> RoomStore:
    return RoomStore(scope, scope.root / "small-rooms.db", limits=RoomLimits(**limits))


def _user(store: RoomStore, room: str, n: int, text: str = "hi", **kw: Any) -> Any:
    return store.append_event(
        room,
        f"e{n}",
        "message.user",
        {"kind": "user", "id": "person"},
        {"text": text},
        authority=store.gateway_id,
        epoch=kw.pop("epoch", 1),
        **kw,
    )


def test_default_limits_match_the_spec_table() -> None:
    lim = DEFAULT_LIMITS
    assert PROTOCOL_VERSION == 2
    assert (lim.max_id_chars, lim.max_name_chars, lim.max_kind_chars) == (128, 200, 64)
    assert (lim.max_members, lim.max_members_bytes) == (128, 128 * 1024)
    assert lim.max_event_bytes == 256 * 1024
    assert (lim.max_page_events, lim.max_page_bytes) == (500, 2 * 1024 * 1024)
    assert lim.max_active_rooms == 256
    assert (lim.max_tombstones, lim.tombstone_seconds) == (512, 90 * DAY)
    assert (lim.max_room_events, lim.max_room_bytes) == (50_000, 256 * 1024 * 1024)
    assert lim.max_gateway_bytes == 16 * 1024 * 1024
    assert (lim.reserve_events, lim.reserve_bytes) == (64, 1024 * 1024)


def test_the_store_is_one_file_under_the_install_root(scope: Scope, store: RoomStore) -> None:
    assert store.path == (scope.root / ROOMS_FILE).resolve()
    assert store.path.stat().st_mode & 0o077 == 0
    assert store.gateway_id.startswith("gw-")
    again = RoomStore(scope)
    try:
        assert again.gateway_id == store.gateway_id  # minted once, kept
    finally:
        again.close()


def test_create_is_idempotent_and_conflicts_on_other_settings(store: RoomStore) -> None:
    room = store.create_room("r1", "Planning", MEMBERS)
    assert (room.epoch, room.latest_seq, room.idempotent) == (1, 0, False)
    assert room.authority == store.gateway_id
    assert store.create_room("r1", "Planning", MEMBERS).idempotent
    with pytest.raises(RoomConflict):
        store.create_room("r1", "Other name", MEMBERS)
    with pytest.raises(RoomConflict):
        store.create_room("r1", "Planning", MEMBERS[:1])


@pytest.mark.parametrize(
    ("room_id", "ok"),
    [("a" * 128, True), ("a" * 129, False), ("", False), ("-lead", False), ("has space", False)],
)
def test_room_id_bound(store: RoomStore, room_id: str, ok: bool) -> None:
    if ok:
        store.create_room(room_id, "n", MEMBERS)
    else:
        with pytest.raises(RoomError):
            store.create_room(room_id, "n", MEMBERS)


def test_name_bound(store: RoomStore) -> None:
    store.create_room("r1", "x" * 200, MEMBERS)
    with pytest.raises(RoomError):
        store.create_room("r2", "x" * 201, MEMBERS)
    with pytest.raises(RoomError):
        store.create_room("r3", "   ", MEMBERS)


def test_member_count_and_size_bounds(store: RoomStore) -> None:
    store.create_room("r1", "n", [{"profile": f"p{i}"} for i in range(128)])
    with pytest.raises(RoomError, match="at most 128"):
        store.create_room("r2", "n", [{"profile": f"p{i}"} for i in range(129)])
    with pytest.raises(RoomError, match="members is larger"):
        store.create_room("r3", "n", [{"profile": "p", "note": "x" * (128 * 1024)}])
    with pytest.raises(RoomError, match="object"):
        store.create_room("r4", "n", cast(Any, ["alice"]))


def test_event_kind_bound_and_pattern(store: RoomStore) -> None:
    store.create_room("r1", "n", MEMBERS)
    gateway = {"kind": "gateway", "id": store.gateway_id}
    for bad in ("Message.user", "1abc", "a" * 65, "with space", ""):
        with pytest.raises(RoomError, match="kind"):
            store.append_event("r1", "x", bad, gateway, {}, authority=store.gateway_id, epoch=1)


def test_event_payload_bound(store: RoomStore) -> None:
    store.create_room("r1", "n", MEMBERS)
    _user(store, "r1", 1, "y" * (256 * 1024 - 100))
    with pytest.raises(RoomError, match="payload is larger"):
        _user(store, "r1", 2, "y" * (256 * 1024))


def test_actor_kinds_may_only_append_their_own_events(store: RoomStore) -> None:
    store.create_room("r1", "n", MEMBERS)
    gid = store.gateway_id
    with pytest.raises(RoomError, match="cannot append"):
        store.append_event(
            "r1", "x", "authority.claimed", {"kind": "member", "id": "bob"}, {},
            authority=gid, epoch=1,
        )  # fmt: skip
    with pytest.raises(RoomError, match="cannot append"):
        store.append_event(
            "r1", "x", "message.member", {"kind": "user", "id": "u"}, {}, authority=gid, epoch=1
        )
    with pytest.raises(RoomError, match="authority"):
        store.append_event(
            "r1", "x", "turn.started", {"kind": "gateway", "id": "gw-someone-else"}, {},
            authority=gid, epoch=1,
        )  # fmt: skip
    with pytest.raises(RoomError, match="unknown actor fields"):
        store.append_event(
            "r1", "x", "message.member", {"kind": "member", "id": "bob", "role": "x"}, {},
            authority=gid, epoch=1,
        )  # fmt: skip
    event = store.append_event(
        "r1", "m1", "message.member", {"kind": "member", "id": "bob", "profile": "bob"},
        {"text": "hello"}, authority=gid, epoch=1,
    )  # fmt: skip
    assert (event.seq, event.actor) == (1, {"kind": "member", "id": "bob", "profile": "bob"})


def test_duplicate_event_id_is_a_no_op(store: RoomStore) -> None:
    store.create_room("r1", "n", MEMBERS)
    first = _user(store, "r1", 1)
    again = _user(store, "r1", 1)
    assert (again.seq, again.idempotent, first.idempotent) == (first.seq, True, False)
    assert store.get_room("r1").latest_seq == 1
    page = store.read_events("r1")
    assert [e.event_id for e in page.events] == ["e1"]


def test_duplicate_event_id_with_other_content_is_refused(store: RoomStore) -> None:
    store.create_room("r1", "n", MEMBERS)
    _user(store, "r1", 1, "first")
    with pytest.raises(RoomEventConflict):
        _user(store, "r1", 1, "second")


def test_user_event_ids_live_in_their_own_namespace() -> None:
    key = user_event_id("client-retry-1")
    assert key.startswith("user:") and key == user_event_id("client-retry-1")
    assert key != user_event_id("client-retry-2")


def test_stale_authority_and_moved_cursor_are_refused(store: RoomStore) -> None:
    store.create_room("r1", "n", MEMBERS)
    with pytest.raises(RoomAuthorityConflict):
        _user(store, "r1", 1, epoch=2)
    with pytest.raises(RoomAuthorityConflict):
        store.append_event(
            "r1", "e9", "message.user", {"kind": "user", "id": "u"}, {}, authority="gw-x", epoch=1
        )
    _user(store, "r1", 1)
    with pytest.raises(RoomCursorMoved):
        _user(store, "r1", 2, expected_latest_seq=0)
    assert _user(store, "r1", 2, expected_latest_seq=1).seq == 2


def test_active_room_bound(scope: Scope) -> None:
    small = _small(scope, max_active_rooms=3)
    try:
        for n in range(3):
            small.create_room(f"r{n}", "n", MEMBERS)
        with pytest.raises(RoomFull, match="too many rooms"):
            small.create_room("r9", "n", MEMBERS)
        small.disband_room("r0", authority=small.gateway_id, epoch=1)
        small.create_room("r9", "n", MEMBERS)  # a disbanded room frees its place
    finally:
        small.close()


def test_events_per_room_bound_and_the_control_reserve(scope: Scope) -> None:
    small = _small(scope, max_room_events=5, reserve_events=2)
    try:
        small.create_room("r1", "n", MEMBERS)
        for n in range(5):
            _user(small, "r1", n)
        with pytest.raises(RoomFull, match="history limit"):
            _user(small, "r1", 99)
        gid = small.gateway_id
        small.request_stop("r1", "stop-1", authority=gid, epoch=1)
        small.request_stop("r1", "stop-2", authority=gid, epoch=1)
        with pytest.raises(RoomFull):  # the reserve is spent
            small.request_stop("r1", "stop-3", authority=gid, epoch=1)
    finally:
        small.close()


def test_a_full_room_still_accepts_a_stop_from_the_reserve(scope: Scope) -> None:
    small = _small(scope, max_room_bytes=2_000, reserve_bytes=600)
    try:
        small.create_room("r1", "n", MEMBERS)
        _user(small, "r1", 1, "z" * 1_900)
        with pytest.raises(RoomFull, match="storage limit"):
            _user(small, "r1", 2, "z" * 200)
        stop = small.request_stop("r1", "operator-stop", authority=small.gateway_id, epoch=1)
        assert stop.kind == "room.stop_requested"
        assert small.request_stop(
            "r1", "operator-stop", authority=small.gateway_id, epoch=1
        ).idempotent
        done = small.disband_room("r1", authority=small.gateway_id, epoch=1)
        assert not done.idempotent
    finally:
        small.close()


def test_bytes_per_room_bound(scope: Scope) -> None:
    small = _small(scope, max_room_bytes=1_000)
    try:
        small.create_room("r1", "n", MEMBERS)
        _user(small, "r1", 1, "a" * 800)
        with pytest.raises(RoomFull, match="storage limit"):
            _user(small, "r1", 2, "a" * 300)
    finally:
        small.close()


def test_gateway_budget_purges_disbanded_rooms_first(scope: Scope) -> None:
    small = _small(scope, max_gateway_bytes=3_000, reserve_bytes=0)
    gid = small.gateway_id
    try:
        small.create_room("old", "n", MEMBERS)
        _user(small, "old", 1, "o" * 1_500)
        small.disband_room("old", authority=gid, epoch=1)
        small.create_room("live", "n", MEMBERS)
        _user(small, "live", 1, "l" * 1_000)
        _user(small, "live", 2, "l" * 1_000)  # fits only once "old" is purged
        assert small.is_retired("old")
        with pytest.raises(RoomNotFound, match="expired"):
            small.get_room("old", include_disbanded=True)
        with pytest.raises(RoomFull, match="host is full"):
            _user(small, "live", 3, "l" * 1_200)
    finally:
        small.close()


def test_tombstones_are_pruned_after_90_days(
    scope: Scope, store: RoomStore, fake_clock: FakeClock
) -> None:
    gid = store.gateway_id
    store.create_room("r1", "n", MEMBERS)
    _user(store, "r1", 1)
    store.disband_room("r1", authority=gid, epoch=1)
    fake_clock.advance(90 * DAY - 1)
    assert store.prune_disbanded() == 0
    assert store.get_room("r1", include_disbanded=True).disbanded_at is not None
    fake_clock.advance(1)
    assert store.prune_disbanded() == 1
    with pytest.raises(RoomNotFound, match="expired"):
        store.read_events("r1", include_disbanded=True)
    assert store.is_retired("r1")
    with pytest.raises(RoomConflict):  # a retired id never comes back
        store.create_room("r1", "n", MEMBERS)
    replay = store.disband_room("r1", authority=gid, epoch=1)
    assert replay.idempotent and replay.purged


def test_more_than_512_tombstones_drop_the_oldest(scope: Scope, fake_clock: FakeClock) -> None:
    small = _small(scope, max_tombstones=3)
    gid = small.gateway_id
    try:
        for n in range(5):
            small.create_room(f"r{n}", "n", MEMBERS)
            small.disband_room(f"r{n}", authority=gid, epoch=1)
            fake_clock.advance(10)
        kept = {r.room_id for r in small.list_rooms(include_disbanded=True)}
        assert kept == {"r2", "r3", "r4"}
        assert all(small.is_retired(f"r{n}") for n in range(5))
    finally:
        small.close()


def test_disband_is_idempotent_and_needs_the_authority(store: RoomStore) -> None:
    store.create_room("r1", "n", MEMBERS)
    with pytest.raises(RoomAuthorityConflict):
        store.disband_room("r1", authority="gw-other", epoch=1)
    first = store.disband_room("r1", authority=store.gateway_id, epoch=1)
    again = store.disband_room("r1", authority=store.gateway_id, epoch=1)
    assert (first.idempotent, again.idempotent) == (False, True)
    with pytest.raises(RoomNotFound):
        _user(store, "r1", 1)
    page = store.read_events("r1", include_disbanded=True)
    assert [e.kind for e in page.events] == ["room.disbanded"]
    assert store.list_rooms() == []


def test_page_is_cut_by_bytes_without_splitting_an_event(scope: Scope) -> None:
    small = _small(scope, max_page_bytes=5_000)
    try:
        small.create_room("r1", "n", MEMBERS)
        for n in range(1, 8):
            _user(small, "r1", n, "p" * 1_000)
        page = small.read_events("r1", limit=100)
        assert 1 <= len(page.events) < 7
        encoded = json.dumps(page.to_dict(), ensure_ascii=False, separators=(",", ":"))
        assert len(encoded.encode()) <= 5_000
        assert page.has_more and page.cursor == page.events[-1].seq
        rest = small.read_events("r1", since_seq=page.cursor, limit=100)
        assert rest.events[0].seq == page.cursor + 1
    finally:
        small.close()


def test_page_limit_is_500_events(store: RoomStore) -> None:
    store.create_room("r1", "n", MEMBERS)
    with pytest.raises(RoomError, match="between 1 and 500"):
        store.read_events("r1", limit=501)
    for n in range(1, 4):
        _user(store, "r1", n)
    page = store.read_events("r1", limit=2)
    assert [e.seq for e in page.events] == [1, 2] and page.has_more
    last = store.read_events("r1", since_seq=2)
    assert [e.seq for e in last.events] == [3] and not last.has_more
    with pytest.raises(RoomError, match="ahead"):
        store.read_events("r1", since_seq=4)


def test_rename_logs_an_event_and_is_idempotent(store: RoomStore) -> None:
    store.create_room("r1", "Old", MEMBERS)
    renamed = store.rename_room("r1", "rn1", "New", authority=store.gateway_id, epoch=1)
    assert (renamed.name, renamed.revision, renamed.latest_seq) == ("New", 2, 1)
    assert store.rename_room("r1", "rn1", "New", authority=store.gateway_id, epoch=1).idempotent
    with pytest.raises(RoomEventConflict):
        store.rename_room("r1", "rn1", "Other", authority=store.gateway_id, epoch=1)


def test_claim_moves_the_epoch_and_fences_the_old_authority(store: RoomStore) -> None:
    gid = store.gateway_id
    store.create_room("r1", "n", MEMBERS)
    claimed = store.claim_authority(
        "r1", expected=gid, expected_epoch=1, new="gw-replacement", event_id="claim-1"
    )
    assert (claimed.authority, claimed.epoch) == ("gw-replacement", 2)
    with pytest.raises(RoomAuthorityConflict):
        _user(store, "r1", 1)  # the old authority can no longer append
    replay = store.claim_authority(
        "r1", expected=gid, expected_epoch=1, new="gw-replacement", event_id="claim-1"
    )
    assert replay.idempotent
    with pytest.raises(RoomAuthorityConflict):
        store.claim_authority(
            "r1", expected=gid, expected_epoch=1, new="gw-third", event_id="claim-2"
        )
    kinds = [e.kind for e in store.read_events("r1").events]
    assert kinds == ["authority.claimed"]


def test_a_repeated_claim_after_a_newer_one_reports_superseded(store: RoomStore) -> None:
    gid = store.gateway_id
    store.create_room("r1", "n", MEMBERS)
    store.claim_authority("r1", expected=gid, expected_epoch=1, new="gw-b", event_id="c1")
    store.claim_authority("r1", expected="gw-b", expected_epoch=2, new="gw-c", event_id="c2")
    with pytest.raises(RoomAuthoritySuperseded):
        store.claim_authority("r1", expected=gid, expected_epoch=1, new="gw-b", event_id="c1")


def test_demotion_adopts_only_a_newer_observed_lineage(store: RoomStore) -> None:
    store.create_room("r1", "n", MEMBERS)
    with pytest.raises(RoomAuthorityConflict, match="newer"):
        store.record_authority_lost("r1", observed="gw-new", observed_epoch=1)
    lost = store.record_authority_lost("r1", observed="gw-new", observed_epoch=2)
    assert (lost.authority, lost.epoch) == ("gw-new", 2)
    assert store.record_authority_lost("r1", observed="gw-new", observed_epoch=2).idempotent
    with pytest.raises(RoomAuthorityConflict):
        _user(store, "r1", 1)
    assert [e.kind for e in store.read_events("r1").events] == ["authority.lost"]


def test_list_rooms_pages_most_recent_first(store: RoomStore, fake_clock: FakeClock) -> None:
    for name in ("a", "b", "c"):
        store.create_room(name, name, MEMBERS)
        fake_clock.advance(1)
    _user(store, "a", 1)
    assert [r.room_id for r in store.list_rooms()] == ["a", "c", "b"]
    assert [r.room_id for r in store.list_rooms(limit=1, offset=1)] == ["c"]
    with pytest.raises(RoomError):
        store.list_rooms(limit=501)


# -- compare-step additions -----------------------------------------------------------------------


def test_the_actor_is_part_of_an_event_ids_content(store: RoomStore) -> None:
    store.create_room("r1", "n", MEMBERS)
    gid = store.gateway_id
    actor = {"kind": "member", "id": "bob"}
    store.append_event("r1", "m1", "message.member", actor, {"t": 1}, authority=gid, epoch=1)
    with pytest.raises(RoomEventConflict):
        store.append_event(
            "r1", "m1", "message.member", {"kind": "member", "id": "alice"}, {"t": 1},
            authority=gid, epoch=1,
        )  # fmt: skip


def test_the_log_survives_reopening_the_store(scope: Scope, store: RoomStore) -> None:
    store.create_room("r1", "n", MEMBERS)
    _user(store, "r1", 1, "kept")
    store.close()
    again = RoomStore(scope)
    try:
        page = again.read_events("r1")
        assert [e.payload for e in page.events] == [{"text": "kept"}]
        assert _user(again, "r1", 2).seq == 2
    finally:
        again.close()


def _real_clock(scope: Scope) -> Scope:
    """Writers that may wait on each other need a clock that moves by itself."""
    return dataclasses.replace(scope, clock=SystemClock())


def test_concurrent_writers_share_one_sequence(scope: Scope) -> None:
    import threading

    store = RoomStore(_real_clock(scope), scope.root / "shared-rooms.db")
    store.create_room("r1", "n", MEMBERS)
    other = RoomStore(_real_clock(scope), scope.root / "shared-rooms.db")
    errors: list[BaseException] = []

    def write(handle: RoomStore, prefix: str) -> None:
        try:
            for n in range(20):
                handle.append_event(
                    "r1", f"{prefix}{n}", "message.user", {"kind": "user", "id": "u"},
                    {"n": n}, authority=handle.gateway_id, epoch=1,
                )  # fmt: skip
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    try:
        threads = [
            threading.Thread(target=write, args=(store, "a")),
            threading.Thread(target=write, args=(other, "b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert errors == []
        page = store.read_events("r1", limit=100)
        assert [e.seq for e in page.events] == list(range(1, 41))
    finally:
        other.close()
        store.close()


def test_concurrent_claims_have_one_winner(scope: Scope) -> None:
    import threading

    store = RoomStore(_real_clock(scope), scope.root / "claim-rooms.db")
    store.create_room("r1", "n", MEMBERS)
    gid = store.gateway_id
    other = RoomStore(_real_clock(scope), scope.root / "claim-rooms.db")
    results: list[str] = []

    def claim(handle: RoomStore, new: str) -> None:
        try:
            handle.claim_authority(
                "r1", expected=gid, expected_epoch=1, new=new, event_id=f"claim-{new}"
            )
            results.append("won")
        except RoomAuthorityConflict:
            results.append("lost")

    try:
        threads = [
            threading.Thread(target=claim, args=(store, "gw-a")),
            threading.Thread(target=claim, args=(other, "gw-b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert sorted(results) == ["lost", "won"]
        assert store.get_room("r1").epoch == 2
        claim_event = store.authority_claim("r1")
        assert claim_event is not None and claim_event.epoch == 2
    finally:
        other.close()
        store.close()


def test_page_bytes_count_utf8_not_characters(scope: Scope) -> None:
    small = _small(scope, max_page_bytes=4_000)
    try:
        small.create_room("r1", "n", MEMBERS)
        for n in range(1, 6):
            _user(small, "r1", n, "é" * 500)  # 1,000 bytes of text each
        page = small.read_events("r1", limit=100)
        encoded = json.dumps(page.to_dict(), ensure_ascii=False, separators=(",", ":"))
        assert len(encoded.encode("utf-8")) <= 4_000 < len(encoded.encode("utf-8")) + 1_100
        assert len(page.events) == 3
    finally:
        small.close()
