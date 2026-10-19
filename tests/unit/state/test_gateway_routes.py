"""P15-07: the gateway routing index (key → session, origin, overrides, markers)."""

from __future__ import annotations

import itertools

import pytest

from indus.state import gateway_routes as routes
from indus.state.registry import StoreHandle
from tests.unit.state._helpers_d import DAY, advance, now_of

KEY = "@default:telegram:dm:100"
ORIGIN = {"platform": "telegram", "chat_id": "100", "chat_type": "dm", "user_id": "100"}


def _ids() -> itertools.count[int]:
    return itertools.count(1)


def _create(store: StoreHandle, key: str = KEY, sid: str = "s1") -> routes.RouteEntry:
    entry, created = routes.get_or_create(
        store,
        key,
        new_session_id=lambda: sid,
        platform="telegram",
        chat_type="dm",
        origin=ORIGIN,
        display_name="Ada",
    )
    assert created
    return entry


def test_round_trip(store: StoreHandle) -> None:
    assert routes.get(store, KEY) is None
    entry = _create(store)
    assert entry.session_key == KEY and entry.session_id == "s1"
    assert entry.origin == ORIGIN and entry.display_name == "Ada"
    assert entry.platform == "telegram" and entry.chat_type == "dm"
    assert not entry.resume_pending and entry.active_turn_token is None
    assert entry.model_override is None and entry.prev_session_id is None
    assert entry.created_at == entry.updated_at == now_of(store)
    assert routes.get(store, KEY) == entry
    assert routes.list_routes(store) == [entry]
    assert routes.list_routes(store, platform="slack") == []
    assert routes.routes_for_session(store, "s1") == [entry]


def test_get_or_create_is_idempotent_and_refreshes_origin(store: StoreHandle) -> None:
    _create(store)
    calls: list[int] = []
    advance(store, 5)
    origin = {**ORIGIN, "user_name": "ada2"}
    again, created = routes.get_or_create(
        store,
        KEY,
        new_session_id=lambda: calls.append(1) or "other",  # type: ignore[func-returns-value]
        platform="telegram",
        chat_type="dm",
        origin=origin,
    )
    assert not created and calls == []
    assert again.session_id == "s1" and again.origin == origin
    assert again.display_name == "Ada"  # None keeps the stored name
    assert again.updated_at == again.created_at + 5


def test_idle_thirty_days_keeps_the_session(store: StoreHandle) -> None:
    _create(store)
    advance(store, 30 * DAY)
    entry, created = routes.get_or_create(
        store,
        KEY,
        new_session_id=lambda: "fresh",
        platform="telegram",
        chat_type="dm",
        origin=ORIGIN,
    )
    assert not created and entry.session_id == "s1"


def test_rotate_keeps_previous_and_clears_turn_state(store: StoreHandle) -> None:
    _create(store)
    assert routes.begin_turn(store, KEY, "tok")
    assert routes.mark_resume_pending(store, KEY, "restart_timeout")
    rotated = routes.rotate(store, KEY, "s2")
    assert rotated is not None
    assert rotated.session_id == "s2" and rotated.prev_session_id == "s1"
    assert rotated.active_turn_token is None and not rotated.resume_pending
    assert rotated.resume_reason is None
    assert routes.rotate(store, "missing", "s3") is None
    with pytest.raises(ValueError):
        routes.rotate(store, KEY, "")


def test_model_override_never_stores_credentials(store: StoreHandle) -> None:
    _create(store)
    secret = "k" + "-" + "x" * 24
    assert routes.set_model_override(
        store, KEY, {"model": "m1", "provider": "p", "api_key": secret, "base_url": " ", "token": 1}
    )
    entry = routes.get(store, KEY)
    assert entry is not None and entry.model_override == {"model": "m1", "provider": "p"}
    with store.read() as conn:
        raw = conn.execute("SELECT model_override_json FROM gateway_routes").fetchone()[0]
    assert secret not in raw
    assert routes.set_model_override(store, KEY, None)
    entry = routes.get(store, KEY)
    assert entry is not None and entry.model_override is None
    assert routes.sanitize_model_override({"api_key": "x"}) is None
    assert not routes.set_model_override(store, "missing", {"model": "m"})


def test_resume_flags(store: StoreHandle) -> None:
    _create(store)
    assert routes.mark_resume_pending(store, KEY, "restart_timeout")
    entry = routes.get(store, KEY)
    assert entry is not None and entry.resume_pending and entry.resume_reason == "restart_timeout"
    assert routes.clear_resume_pending(store, KEY)
    entry = routes.get(store, KEY)
    assert entry is not None and not entry.resume_pending and entry.resume_reason is None
    assert not routes.mark_resume_pending(store, "missing", "x")


def test_active_turn_marker_is_compare_and_clear(store: StoreHandle) -> None:
    _create(store)
    assert routes.begin_turn(store, KEY, "gen-1")
    entry = routes.get(store, KEY)
    assert entry is not None and entry.active_turn_token == "gen-1"
    assert entry.active_turn_started_at == now_of(store)
    # A successor takes over; the displaced turn cannot clear it.
    assert routes.begin_turn(store, KEY, "gen-2")
    assert not routes.end_turn(store, KEY, "gen-1")
    entry = routes.get(store, KEY)
    assert entry is not None and entry.active_turn_token == "gen-2"
    assert routes.end_turn(store, KEY, "gen-2")
    entry = routes.get(store, KEY)
    assert entry is not None and entry.active_turn_token is None
    assert entry.active_turn_started_at is None
    with pytest.raises(ValueError):
        routes.begin_turn(store, KEY, "")


def test_several_keys_one_session_and_delete(store: StoreHandle) -> None:
    _create(store, KEY, "shared")
    _create(store, "@default:slack:dm:D1", "shared")
    assert [e.session_key for e in routes.routes_for_session(store, "shared")] == [
        "@default:slack:dm:D1",
        KEY,
    ]
    assert routes.set_display_name(store, KEY, "Renamed")
    assert routes.delete(store, KEY)
    assert not routes.delete(store, KEY)
    assert routes.get(store, KEY) is None


def test_refuses_empty_key_and_empty_new_id(store: StoreHandle) -> None:
    with pytest.raises(ValueError):
        _create(store, "")
    with pytest.raises(ValueError):
        _create(store, KEY, "")
    assert routes.get(store, KEY) is None


def test_corrupt_json_columns_read_as_empty(store: StoreHandle) -> None:
    _create(store)
    store.write(
        lambda c: c.execute(
            "UPDATE gateway_routes SET origin_json = 'nope', model_override_json = '[1]'"
        )
    )
    entry = routes.get(store, KEY)
    assert entry is not None and entry.origin == {} and entry.model_override is None


def test_deleting_a_session_drops_its_routes(store: StoreHandle) -> None:
    """A route never points at a deleted session (P15 integration): the row goes, and a route
    whose previous session was deleted forgets it; the chat's next message starts afresh."""
    from indus.state import deletion
    from tests.unit.state._helpers_d import add_session

    for sid in ("s1", "s2", "s3"):
        add_session(store, sid)
    _create(store, KEY, "s1")
    _create(store, "@default:telegram:dm:200", "s2")
    routes.rotate(store, "@default:telegram:dm:200", "s3")  # prev = s2
    assert deletion.delete_session(store, "s1")
    assert deletion.delete_session(store, "s2")
    assert routes.get(store, KEY) is None
    kept = routes.get(store, "@default:telegram:dm:200")
    assert kept is not None and kept.session_id == "s3" and kept.prev_session_id is None
    fresh = _create(store, KEY, "s9")
    assert fresh.session_id == "s9"
