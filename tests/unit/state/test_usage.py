"""Usage accounting: session totals, per-model rows, side calls, lineage reads, routes."""

from __future__ import annotations

import pytest

from indus.state import usage
from indus.state.registry import StoreHandle
from indus.state.usage import UsageDelta
from tests.unit.state._helpers_b import add_session, now, session
from tests.unit.state.conftest import AutoClock


def call(model: str = "m-1", provider: str = "prov", **counts: object) -> UsageDelta:
    fields: dict[str, object] = {"input_tokens": 100, "output_tokens": 10, **counts}
    return UsageDelta(
        model=model,
        billing_provider=provider,
        billing_base_url="https://" + provider + ".example.test",
        **fields,  # type: ignore[arg-type]
    )


@pytest.fixture
def sid(store: StoreHandle) -> str:
    return add_session(store, "s1", model="m-1", billing_provider="prov")


def test_incremental_deltas_sum(store: StoreHandle, sid: str) -> None:
    for _ in range(3):
        assert usage.record(store, sid, call(cache_read_tokens=5, estimated_cost_usd=0.01))
    meta = session(store, sid)
    assert meta is not None
    assert (meta["input_tokens"], meta["output_tokens"], meta["cache_read_tokens"]) == (
        300,
        30,
        15,
    )
    assert meta["api_call_count"] == 3
    assert meta["estimated_cost_usd"] == pytest.approx(0.03)
    assert meta["actual_cost_usd"] is None
    rows = usage.by_model(store, sid)
    assert len(rows) == 1
    assert (rows[0].model, rows[0].task, rows[0].input_tokens, rows[0].api_calls) == (
        "m-1",
        "",
        300,
        3,
    )


def test_absolute_replaces(store: StoreHandle, sid: str) -> None:
    usage.record(store, sid, call(actual_cost_usd=0.5))
    usage.record(
        store,
        sid,
        UsageDelta(input_tokens=1000, output_tokens=200, api_calls=7, estimated_cost_usd=0.2),
        mode="absolute",
    )
    meta = session(store, sid)
    assert meta is not None
    assert (meta["input_tokens"], meta["output_tokens"], meta["api_call_count"]) == (1000, 200, 7)
    assert meta["estimated_cost_usd"] == pytest.approx(0.2)
    assert meta["actual_cost_usd"] == pytest.approx(0.5)  # not given: kept
    assert [r.api_calls for r in usage.by_model(store, sid)] == [1]  # absolute is not per route
    with pytest.raises(ValueError):
        usage.record(store, sid, call(), mode="sideways")  # type: ignore[arg-type]


def test_auxiliary_call_only_lands_per_model(store: StoreHandle, sid: str) -> None:
    usage.record(store, sid, call())
    before = session(store, sid)
    usage.record(store, sid, call("m-small", "other", input_tokens=40), task="title")
    usage.record(store, sid, UsageDelta(input_tokens=5, api_calls=0), task="vision")
    assert session(store, sid) == before
    rows = {(r.task, r.model, r.billing_provider): r for r in usage.by_model(store, sid)}
    assert set(rows) == {
        ("", "m-1", "prov"),
        ("title", "m-small", "other"),
        ("vision", "unknown", ""),
    }
    assert rows[("title", "m-small", "other")].input_tokens == 40
    assert rows[("vision", "unknown", "")].api_calls == 0  # as reported
    assert rows[("title", "m-small", "other")].api_calls == 1  # one delta = one call
    assert usage.totals(store, sid).input_tokens == 100


def test_lineage_of_two_segments_sums(store: StoreHandle) -> None:
    t = now(store)
    add_session(store, "root", started_at=t - 100, ended_at=t - 50, end_reason="compression")
    add_session(store, "tip", parent_session_id="root", started_at=t - 50)
    usage.record(store, "root", call(estimated_cost_usd=0.1))
    usage.record(store, "tip", call(input_tokens=1, estimated_cost_usd=0.2, actual_cost_usd=0.05))
    usage.record(store, "tip", call(input_tokens=2), task="compression")
    for sid in ("root", "tip"):
        found = usage.totals(store, sid)
        assert (found.input_tokens, found.output_tokens, found.api_calls) == (101, 20, 2)
        assert found.estimated_cost_usd == pytest.approx(0.3)
        assert found.actual_cost_usd == pytest.approx(0.05)
        assert found.cost_usd == pytest.approx(0.15)  # actual on the tip, estimate on the root
    tasks = sorted((r.task, r.input_tokens) for r in usage.by_model(store, "root"))
    assert tasks == [("", 101), ("compression", 2)]


def test_store_totals_prefer_actual_cost(store: StoreHandle) -> None:
    add_session(store, "a")
    add_session(store, "b")
    add_session(store, "old", archived=1)
    usage.record(store, "a", call(estimated_cost_usd=1.0, actual_cost_usd=0.25))
    usage.record(store, "b", call(estimated_cost_usd=0.5))
    usage.record(store, "b", call(input_tokens=7, estimated_cost_usd=0.1), task="vision")
    usage.record(store, "old", call(estimated_cost_usd=9.0))
    found = usage.store_totals(store)
    assert found.cost_usd == pytest.approx(0.25 + 0.5 + 0.1)
    assert found.input_tokens == 100 + 100 + 7
    assert found.actual_cost_usd == pytest.approx(0.25)
    assert usage.store_totals(store, include_archived=True).cost_usd == pytest.approx(9.85)


def test_route_switch_is_reflected(store: StoreHandle, sid: str, fake_clock: AutoClock) -> None:
    usage.record(store, sid, call())
    assert usage.recent_route(store, sid) == usage.Route("m-1", "prov", "https://prov.example.test")
    fake_clock.advance(1)
    assert usage.set_route(
        store, sid, billing_provider="alt", billing_base_url="https://alt", model="m-2"
    )
    meta = session(store, sid)
    assert meta is not None and (meta["model"], meta["billing_provider"]) == ("m-2", "alt")
    usage.record(store, sid, call("m-2", "alt"))
    assert usage.recent_route(store, sid) == usage.Route("m-2", "alt", "https://alt.example.test")
    fake_clock.advance(1)
    usage.record(store, sid, call())  # back to the first route
    assert usage.recent_route(store, sid) == usage.Route("m-1", "prov", "https://prov.example.test")
    usage.record(store, sid, call("m-3", "x"), task="title")  # side calls never count as the route
    assert usage.recent_route(store, sid) is not None
    assert usage.recent_route(store, sid).model == "m-1"  # type: ignore[union-attr]
    assert not usage.set_route(store, "nope", billing_provider="a", billing_base_url="b")


def test_recent_route_reads_the_lineage(store: StoreHandle) -> None:
    t = now(store)
    add_session(store, "root", started_at=t - 100, ended_at=t - 50, end_reason="compression")
    add_session(store, "tip", parent_session_id="root", started_at=t - 50)
    assert usage.recent_route(store, "tip") is None
    usage.record(store, "root", call())
    assert usage.recent_route(store, "tip") == usage.Route(
        "m-1", "prov", "https://prov.example.test"
    )


def test_first_accounted_route_replaces_the_requested_one(store: StoreHandle) -> None:
    add_session(store, "s2", model="wanted", billing_provider="primary")
    usage.record(store, "s2", call("fallback-model", "backup"))
    meta = session(store, "s2")
    assert meta is not None
    assert (meta["model"], meta["billing_provider"]) == ("fallback-model", "backup")
    usage.record(store, "s2", call("third", "again"))  # later calls only fill empty columns
    meta = session(store, "s2")
    assert meta is not None and meta["model"] == "fallback-model"


def test_empty_delta_does_not_fill_the_route(store: StoreHandle) -> None:
    add_session(store, "s3")
    usage.record(store, "s3", UsageDelta(model="m", billing_provider="p", api_calls=0))
    meta = session(store, "s3")
    assert meta is not None and (meta["model"], meta["billing_provider"]) == (None, None)
    assert usage.by_model(store, "s3") == []
    usage.record(store, "s3", UsageDelta(output_tokens=1, model="m", billing_provider="p"))
    meta = session(store, "s3")
    assert meta is not None and (meta["model"], meta["billing_provider"]) == ("m", "p")


def test_unknown_session_is_not_invented(store: StoreHandle) -> None:
    assert not usage.record(store, "ghost", call())
    assert not usage.record(store, "ghost", call(), task="title")
    assert session(store, "ghost") is None
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM model_usage").fetchone()[0] == 0
