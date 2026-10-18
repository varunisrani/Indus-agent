"""Credential pool data model (P07-07)."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from indus.providers import bundled
from indus.providers.auth import pool_model
from indus.providers.auth.pool_model import (
    DEFAULT_STRATEGY,
    Pool,
    PoolEntry,
    PoolTargetError,
    check_strategy,
    edit_pool,
    env_entries,
    load_pool,
    note_request,
    pending_requests,
    secret_name,
    strategy_for,
    suppress_env,
)
from indus.providers.auth.store import AuthStore
from tests.fakes.oauth_server import fake_token
from tests.fakes.secret_store import MemorySecretStore

NOW = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
OPENROUTER = bundled.by_name()["openrouter"]
ZAI = bundled.by_name()["zai"]


@pytest.fixture(autouse=True)
def no_pending() -> Iterator[None]:
    pool_model.reset_pending_requests()
    yield
    pool_model.reset_pending_requests()


def label_of(entry: PoolEntry | None) -> str | None:
    return entry.label if entry is not None else None


def _pool(*labels: str) -> Pool:
    return Pool(
        "p", [PoolEntry(id=f"id{i}", label=lab, priority=i) for i, lab in enumerate(labels)]
    )


def test_priority_insert_and_renumber() -> None:
    pool = _pool("a", "b", "c")
    pool.add(PoolEntry(id="z", label="z"), priority=0)
    assert [e.label for e in pool.entries] == ["z", "a", "b", "c"]
    pool.add(PoolEntry(id="m", label="m"), priority=2)
    pool.add(PoolEntry(id="e", label="e"))
    pool.add(PoolEntry(id="big", label="big"), priority=99)
    assert [e.label for e in pool.entries] == ["z", "a", "m", "b", "c", "e", "big"]
    assert [e.priority for e in pool.entries] == list(range(7))
    moved = pool.set_priority("big", 0)
    assert moved.priority == 0 and pool.entries[0].label == "big"
    pool.set_priority("1", 50)
    assert pool.entries[-1].label == "big"
    pool.set_priority("big", -3)
    assert pool.entries[0].label == "big"
    assert [e.priority for e in pool.entries] == list(range(7))


def test_remove_by_index_id_and_label() -> None:
    pool = _pool("a", "b", "c", "d")
    assert pool.remove("2")[1].label == "b"
    assert pool.remove("id3")[1].label == "d"
    assert pool.remove("c") == (2, pool_entry_like("c"))
    assert [e.label for e in pool.entries] == ["a"]
    with pytest.raises(PoolTargetError, match="no #5"):
        pool.remove("5")
    with pytest.raises(PoolTargetError, match="'zz'"):
        pool.remove("zz")


def pool_entry_like(label: str) -> PoolEntry:
    return PoolEntry(id="id2", label=label, priority=1)


def test_ambiguous_label_refused() -> None:
    pool = _pool("same", "same")
    with pytest.raises(PoolTargetError, match="#1, #2"):
        pool.remove("same")
    assert len(pool) == 2


def test_duplicate_id_refused() -> None:
    pool = _pool("a")
    with pytest.raises(ValueError):
        pool.add(PoolEntry(id="id0", label="again"))


def test_reset_clears_status() -> None:
    pool = _pool("a", "b")
    pool.mark_exhausted("a", "rate_limit", NOW + timedelta(minutes=5))
    pool.mark_exhausted("b", "billing", None)
    assert [e.status for e in pool.entries] == ["exhausted", "exhausted"]
    assert pool.reset("b")[0].label == "b"
    assert pool.entries[1].status == "ok" and pool.entries[1].last_error_class is None
    pool.reset()
    assert all(e.status == "ok" and e.cooldown_until is None for e in pool.entries)


def test_peek_leaves_state_unchanged() -> None:
    pool = _pool("a", "b", "c")
    pool.mark_exhausted("a", "rate_limit", NOW - timedelta(seconds=1))
    pool.mark_exhausted("b", "rate_limit", NOW + timedelta(hours=1))
    pool.entries[2].request_count = 0
    before = copy.deepcopy(pool.rows())
    assert label_of(pool.peek("fill_first", NOW)) == "a"  # cooldown over: available, not reset
    assert label_of(pool.peek("least_used", NOW)) == "a"
    assert pool.rows() == before
    pool.mark_exhausted("a", "rate_limit", NOW + timedelta(hours=1))
    pool.mark_exhausted("c", "auth", None)
    assert pool.peek("round_robin", NOW) is None


def test_least_used_peek_picks_lowest_count() -> None:
    pool = _pool("a", "b")
    pool.entries[0].request_count = 5
    pool.entries[1].request_count = 2
    assert label_of(pool.peek("least_used", NOW)) == "b"
    assert label_of(pool.peek("fill_first", NOW)) == "a"


def test_strategy_validation_and_default(caplog: pytest.LogCaptureFixture) -> None:
    assert check_strategy("Round-Robin") == "round_robin"
    with pytest.raises(ValueError, match="fill_first, round_robin, least_used, random"):
        check_strategy("fastest")
    assert strategy_for({}, "p") == DEFAULT_STRATEGY
    assert strategy_for({"credential_pool_strategies": {"p": "least_used"}}, "p") == "least_used"
    assert strategy_for({"credential_pool_strategies": {"p": "bogus"}}, "p") == DEFAULT_STRATEGY
    assert "not a pool strategy" in caplog.text
    assert strategy_for(None, "p") == DEFAULT_STRATEGY


def test_row_round_trip_keeps_unknown_fields() -> None:
    row = {
        "id": "x1",
        "label": "l",
        "auth_type": "oauth",
        "source": "pkce",
        "priority": 3,
        "status": "exhausted",
        "cooldown_until": NOW.isoformat(),
        "future_field": 1,
    }
    entry = PoolEntry.from_row(row)
    assert entry.to_row() == {**row, "request_count": 0}
    with pytest.raises(ValueError):
        PoolEntry(id="x", label="x", source="portal")


def test_secret_names_are_env_shaped_and_distinct() -> None:
    assert secret_name("openrouter", "ab12cd34") == "AUTH_OPENROUTER_AB12CD34"
    assert secret_name("kimi-coding", "ab12cd34") == "AUTH_KIMI_CODING_AB12CD34"
    assert secret_name("a", "x1") != secret_name("a", "x2")


def test_env_seeded_entries_listed() -> None:
    secrets = MemorySecretStore({"ZAI_API_KEY": fake_token("zai")})
    env = {"GLM_API_KEY": fake_token("glm"), "OPENROUTER_API_KEY": "sk-or-"}
    rows = env_entries(ZAI, secrets, env)
    assert [(e.label, e.source, e.env_var, e.auth_type) for e in rows] == [
        ("ZAI_API_KEY", "env", "ZAI_API_KEY", "api_key"),
        ("GLM_API_KEY", "env", "GLM_API_KEY", "api_key"),
    ]
    assert env_entries(OPENROUTER, secrets, env) == []  # placeholder ignored


def test_env_rows_merge_persist_and_drop(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "h")
    env_rows = env_entries(OPENROUTER, None, {"OPENROUTER_API_KEY": fake_token("or")})
    pool, inherited = load_pool(store, "openrouter", env_rows)
    assert not inherited and [e.source for e in pool.entries] == ["env"]
    assert not store.path.exists()  # loading writes nothing
    edit_pool(
        store, "openrouter", lambda p, _d: p.add(PoolEntry(id="m1", label="mine"), 0), env_rows
    )
    stored = store.read()["pool"]["openrouter"]
    assert [r["label"] for r in stored] == ["mine", "OPENROUTER_API_KEY"]
    edit_pool(store, "openrouter", lambda p, _d: None, [])  # variable gone
    assert [r["label"] for r in store.read()["pool"]["openrouter"]] == ["mine"]


def test_suppressed_env_row_stays_hidden(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "h")
    env_rows = env_entries(OPENROUTER, None, {"OPENROUTER_API_KEY": fake_token("or")})

    def drop(pool: Pool, doc: dict[str, object]) -> None:
        _, entry = pool.remove("1")
        suppress_env(doc, "openrouter", str(entry.env_var))  # type: ignore[arg-type]

    edit_pool(store, "openrouter", drop, env_rows)  # pyright: ignore[reportArgumentType]
    pool, _ = load_pool(store, "openrouter", env_rows)
    assert pool.entries == []


def test_request_counts_in_memory_until_next_write(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "h")
    edit_pool(store, "p", lambda pool, _d: pool.add(PoolEntry(id="a1", label="a")))
    written = store.path.stat().st_mtime_ns
    note_request(store, "p", "a1")
    note_request(store, "p", "a1")
    assert store.path.stat().st_mtime_ns == written
    assert store.read()["pool"]["p"][0]["request_count"] == 0
    pool, _ = load_pool(store, "p")
    assert pool.entries[0].request_count == 2
    assert pending_requests(store, "p") == {"a1": 2}
    edit_pool(store, "p", lambda pool, _d: None)
    assert store.read()["pool"]["p"][0]["request_count"] == 2
    assert pending_requests(store, "p") == {}


def test_failed_edit_keeps_pending_counts(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "h")
    edit_pool(store, "p", lambda pool, _d: pool.add(PoolEntry(id="a1", label="a")))
    note_request(store, "p", "a1")

    def boom(pool: Pool, _doc: dict[str, object]) -> None:
        raise PoolTargetError("nope")

    with pytest.raises(PoolTargetError):
        edit_pool(store, "p", boom)  # pyright: ignore[reportArgumentType]
    assert pending_requests(store, "p") == {"a1": 1}
