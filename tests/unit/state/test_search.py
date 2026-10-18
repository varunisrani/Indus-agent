"""P02-15: query sanitising, routing between the indexes and LIKE, visibility, context."""

from __future__ import annotations

import logging
import random
import sqlite3
from collections.abc import Iterator

import pytest

from indus.foundation.scope import Scope
from indus.state import fts, registry, search
from indus.state.registry import StoreHandle
from indus.state.search import ContextLine, Hit, or_relaxed, sanitize
from tests.unit.state._helpers_c import BASE_TIME, add_message, add_session, parts


@pytest.fixture(autouse=True)
def _fresh_capabilities() -> Iterator[None]:
    fts.reset_capabilities()
    yield
    fts.reset_capabilities()


@pytest.fixture
def routes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> Iterator[list[str]]:
    """Every search logs its route (threshold 0); returns the routes seen so far."""
    monkeypatch.setattr(search, "_slow_ms", lambda store: 0.0)
    caplog.set_level(logging.INFO, logger="indus.state.search")
    seen: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(str(record.args[0]) if isinstance(record.args, tuple) else "?")

    handler = Grab()
    logging.getLogger("indus.state.search").addHandler(handler)
    yield seen
    logging.getLogger("indus.state.search").removeHandler(handler)


def _ids(hits: list[Hit]) -> list[int]:
    return [hit.message_id for hit in hits]


# -- sanitiser ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("hello world", "hello world"),
        ('"exact phrase" rest', '"exact phrase" rest'),
        ('unbalanced "quote here', "unbalanced quote here"),
        ('a"b"c', 'a "b" c'),
        ("foo-bar", '"foo-bar"'),
        ("v1.2", '"v1.2"'),
        ("my-app.config.ts", '"my-app.config.ts"'),
        ("foo-bar*", '"foo-bar"*'),
        ("-leading. trailing-", "leading trailing"),
        ("alpha OR", "alpha"),
        ("OR alpha", "alpha"),
        ("NOT alpha", "alpha"),
        ("alpha AND OR beta", "alpha AND beta"),
        ("alpha NOT NOT beta", "alpha NOT beta"),
        ("or and not", "or and not"),
        ("pre*", "pre*"),
        ("*lead", "lead"),
        ("mid*dle", "mid dle"),
        ("a**", "a*"),
        ("(group) ^start col:value", "group start col value"),
        ("TODO: fix it", "TODO fix it"),
        ("x+y {z} [w] a|b c&d", "x y z w a b c d"),
        ('""', ""),
        ("!!! ??? %%%", ""),
        ("-- .. **", ""),
        ("漓江 山水", "漓江 山水"),
        ("tab\there\x00nul", "tab here nul"),
        ("bad\ud800half", "bad�half"),
    ],
)
def test_sanitiser_table(raw: str, clean: str) -> None:
    assert sanitize(raw) == clean


def test_sanitiser_caps_the_input() -> None:
    assert len(sanitize("w" * 5000)) == search.MAX_QUERY_CHARS
    assert sanitize("a " * 1500 + "tailword").count("tailword") == 0


@pytest.mark.parametrize(
    ("query", "relaxed"),
    [
        ("red green", "red OR green"),
        ("red AND green blue", "red OR green OR blue"),
        ('"two words" more', '"two words" OR more'),
        ("single", None),
        ('"one phrase"', None),
        ("red OR green", None),
        ("red NOT green", None),
    ],
)
def test_or_relaxation_table(query: str, relaxed: str | None) -> None:
    assert or_relaxed(query) == relaxed


_PIECES = [
    *"abcxyz019 _-.*\"'():^+,;!?@#$&|~[]<>=/\\%{}",
    "AND",
    "OR",
    "NOT",
    "NEAR",
    "漓江",
    "é",
    "\x00",
    "\t",
    "  ",
    "word",
    "a-b.c",
]


def _random_queries(seed: int, count: int) -> list[str]:
    rng = random.Random(seed)
    return ["".join(rng.choice(_PIECES) for _ in range(rng.randint(0, 24))) for _ in range(count)]


@pytest.mark.parametrize("seed", range(8))
def test_no_sanitised_query_upsets_fts5(store: StoreHandle, seed: int) -> None:
    """Property check (hypothesis is not installed): 60 seeded-random queries per seed."""
    add_session(store, "s1")
    add_message(store, "s1", "user", "word a-b.c x y z 漓江")
    with store.read() as conn:
        for raw in _random_queries(seed, 60):
            query = sanitize(raw)
            if not query:
                continue
            conn.execute(f"SELECT rowid FROM {fts.BASE} WHERE {fts.BASE} MATCH ?", (query,))
            relaxed = or_relaxed(query)
            if relaxed is not None:
                conn.execute(f"SELECT rowid FROM {fts.BASE} WHERE {fts.BASE} MATCH ?", (relaxed,))
            quoted = search._quoted_terms(query)  # pyright: ignore[reportPrivateUsage]
            if quoted:
                conn.execute(
                    f"SELECT rowid FROM {fts.TRIGRAM} WHERE {fts.TRIGRAM} MATCH ?", (quoted,)
                )
        for raw in _random_queries(seed + 100, 20):
            search.search(store, raw)


# -- routes -------------------------------------------------------------------------------------


def test_exact_phrase_is_found(store: StoreHandle, routes: list[str]) -> None:
    add_session(store, "s1")
    hit = add_message(store, "s1", "user", "the exact phrase lives here")
    add_message(store, "s1", "user", "phrase first then exact")
    found = search.search(store, '"exact phrase"')
    assert _ids(found) == [hit]
    assert "[[" in found[0].snippet and "]]" in found[0].snippet
    assert routes == ["words"]


def test_substring_is_found_through_the_trigram_index(
    store: StoreHandle, routes: list[str]
) -> None:
    add_session(store, "s1")
    hit = add_message(store, "s1", "assistant", "we call the httpclient wrapper")
    assert _ids(search.search(store, "ttpcl")) == [hit]
    assert routes == ["trigram"]
    assert search.search(store, "tp") == []  # too short for trigrams, no word either


def test_zero_hit_multi_word_query_is_relaxed_to_any_word(
    store: StoreHandle, routes: list[str]
) -> None:
    add_session(store, "s1")
    first = add_message(store, "s1", "user", "Priya moved the retro to Friday afternoon")
    second = add_message(store, "s1", "assistant", "retro notes are posted")
    found = search.search(store, "when did priya schedule the retro")
    assert set(_ids(found)) == {first, second}
    assert routes[-1] == "words_any"
    assert _ids(search.search(store, "retro NOT friday")) == [second]
    assert search.search(store, "retro NOT posted NOT friday") == []
    assert search.search(store, "zygote quantum") == []


def test_cjk_short_terms_use_like_and_long_terms_use_trigram(
    store: StoreHandle, routes: list[str]
) -> None:
    add_session(store, "s1")
    hit = add_message(store, "s1", "user", "我们下个月去桂林旅游")
    other = add_message(store, "s1", "user", "漓江的风景很美")
    assert _ids(search.search(store, "桂林")) == [hit]
    assert routes[-1] == "like_cjk"
    assert sorted(_ids(search.search(store, "桂林 漓江"))) == [hit, other]  # OR per term
    assert _ids(search.search(store, "桂林旅")) == [hit]
    assert routes[-1] == "trigram"
    marked = search.search(store, "桂林")[0].snippet
    assert "[[桂林]]" in marked


def test_cjk_in_an_internal_source_is_found_by_like(store: StoreHandle) -> None:
    add_session(store, "job", "cron")
    hit = add_message(store, "job", "user", "桂林山水甲天下")
    assert _ids(search.search(store, "桂林山", sources=["cron"])) == [hit]


def test_tool_role_search_reaches_past_the_indexed_prefix(
    store: StoreHandle, routes: list[str]
) -> None:
    add_session(store, "s1")
    body = "start " + "x" * 12_000 + " needlepoint " + "y" * 100
    hit = add_message(store, "s1", "tool", body, tool_name="terminal")
    found = search.search(store, "needlepoint", role="tool")
    assert _ids(found) == [hit]
    assert "[[needlepoint]]" in found[0].snippet
    assert found[0].snippet.startswith(search.ELLIPSIS)
    assert found[0].tool_name == "terminal"
    assert routes[-1] == "like_tool"
    assert search.search(store, "needlepoint") == []


def test_rewound_rows_hidden_unless_asked_and_archived_rows_found(store: StoreHandle) -> None:
    add_session(store, "s1")
    live = add_message(store, "s1", "user", "marmalade live")
    rewound = add_message(store, "s1", "user", "marmalade rewound", active=0)
    archived = add_message(store, "s1", "user", "marmalade archived", active=0, compacted=1)
    assert sorted(_ids(search.search(store, "marmalade"))) == [live, archived]
    everything = search.search(store, "marmalade", include_inactive=True)
    assert sorted(_ids(everything)) == [live, rewound, archived]


def test_filters_apply_on_every_route(store: StoreHandle) -> None:
    add_session(store, "a", "cli", started_at=BASE_TIME)
    add_session(store, "b", "telegram", started_at=BASE_TIME + 100)
    add_session(store, "c", "cron", started_at=BASE_TIME + 200)
    in_a = add_message(store, "a", "user", "pumpkin soup")
    in_b = add_message(store, "b", "assistant", "pumpkin pie")
    in_c = add_message(store, "c", "user", "pumpkin bread")
    for _ in range(2):  # words route, then the stale (LIKE) route
        assert _ids(search.search(store, "pumpkin", sources=["telegram"])) == [in_b]
        assert sorted(_ids(search.search(store, "pumpkin", exclude_sources=["cron"]))) == [
            in_a,
            in_b,
        ]
        assert _ids(search.search(store, "pumpkin", role="assistant")) == [in_b]
        assert sorted(_ids(search.search(store, "pumpkin", started_after=BASE_TIME + 100))) == [
            in_b,
            in_c,
        ]
        assert _ids(search.search(store, "pumpkin", started_before=BASE_TIME + 100)) == [in_a]
        many = [f"src{n}" for n in range(1000)] + ["cli"]
        assert _ids(search.search(store, "pumpkin", sources=many)) == [in_a]
        store.write(lambda conn: fts.detach_in(conn, "test", now=1.0))


def test_sort_orders_and_paging(store: StoreHandle) -> None:
    add_session(store, "s1")
    old = add_message(store, "s1", "user", "lantern one", timestamp=BASE_TIME + 1)
    new = add_message(store, "s1", "user", "lantern two", timestamp=BASE_TIME + 3)
    mid = add_message(store, "s1", "user", "lantern three", timestamp=BASE_TIME + 2)
    assert _ids(search.search(store, "lantern", sort="newest")) == [new, mid, old]
    assert _ids(search.search(store, "lantern", sort="oldest")) == [old, mid, new]
    assert _ids(search.search(store, "lantern", sort="oldest", limit=1, offset=1)) == [mid]
    assert len(search.search(store, "lantern")) == 3
    hit = search.search(store, "lantern", sort="oldest")[0]
    assert (hit.session_id, hit.role, hit.source, hit.model) == ("s1", "user", "cli", "test-model")
    assert hit.timestamp == BASE_TIME + 1 and hit.session_started == BASE_TIME
    with pytest.raises(ValueError):
        search.search(store, "lantern", sort="best")  # type: ignore[arg-type]
    assert search.search(store, "lantern", limit=0) == []
    assert search.search(store, "   ") == []
    assert search.search(store, "!!!") == []


def test_context_neighbours_are_batched_and_truncated(store: StoreHandle) -> None:
    add_session(store, "s1")
    add_session(store, "s2")
    before = "b" * 500
    first = add_message(store, "s1", "user", before)
    add_message(store, "s1", "user", "skipped rewound neighbour", active=0)
    target = add_message(store, "s1", "assistant", "the compass points north")
    add_message(store, "s1", "user", parts([{"type": "text", "text": "after the compass"}]))
    lone = add_message(store, "s2", "user", "compass alone")
    found = {hit.message_id: hit for hit in search.search(store, "compass", context=True)}
    assert found[target].context == [
        ContextLine("user", "b" * search.CONTEXT_CHARS),
        ContextLine("user", "after the compass"),
    ]
    assert found[lone].context == []
    assert first not in found
    assert all(hit.context is None for hit in search.search(store, "compass"))


def test_multimodal_rows_match_on_their_text_only(store: StoreHandle) -> None:
    add_session(store, "s1")
    rich = add_message(
        store,
        "s1",
        "user",
        parts(
            [{"type": "text", "text": "photo of a heron"}, {"type": "image_url", "image_url": {}}]
        ),
    )
    assert _ids(search.search(store, "heron")) == [rich]
    assert search.search(store, "image_url") == []
    store.write(lambda conn: fts.detach_in(conn, "test", now=1.0))
    assert _ids(search.search(store, "heron")) == [rich]
    assert search.search(store, "image_url") == []
    assert search.search(store, "type") == []


# -- fallbacks ----------------------------------------------------------------------------------


def test_stale_index_uses_like_with_a_boolean_subset(store: StoreHandle, routes: list[str]) -> None:
    add_session(store, "s1")
    alpha = add_message(store, "s1", "user", "alpha only")
    beta = add_message(store, "s1", "user", "beta only")
    both = add_message(store, "s1", "user", "alpha and beta")
    odd = add_message(store, "s1", "user", "snakeXcase")
    store.write(lambda conn: fts.detach_in(conn, "test", now=1.0))
    assert sorted(_ids(search.search(store, "alpha OR beta"))) == [alpha, beta, both]
    assert _ids(search.search(store, "alpha NOT beta")) == [alpha]
    assert _ids(search.search(store, "alpha beta")) == [both]
    assert _ids(search.search(store, '"and beta"')) == [both]
    assert search.search(store, "NOT alpha") != []  # leading NOT dropped by the sanitiser
    assert search.search(store, "snake_case") == []  # LIKE wildcards are escaped
    assert odd not in _ids(search.search(store, "snake_case"))
    assert set(routes) == {"like_stale"}
    marked = search.search(store, "alpha", sort="oldest")[0].snippet
    assert marked == "[[alpha]] only"


def test_store_without_fts_searches_with_like(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fts, "_probe", lambda spec: False)
    with registry.open_unshared(scope) as handle:
        add_session(handle, "s1")
        hit = add_message(handle, "s1", "user", "kept without an index")
        assert _ids(search.search(handle, "index")) == [hit]
        assert _ids(search.search(handle, "witho")) == [hit]


def test_damaged_index_during_search_fails_open(store: StoreHandle, routes: list[str]) -> None:
    add_session(store, "s1")
    hits = [add_message(store, "s1", "user", f"hello number {n}") for n in range(30)]
    store.write(
        lambda conn: conn.execute(
            f"UPDATE {fts.BASE}_data SET block = X'DEADBEEFDEADBEEFDEADBEEFDEADBEEF'"
        )
    )
    found = search.search(store, "hello", sort="oldest", limit=50)
    assert _ids(found) == hits
    assert routes[-1] == "like_after_index_damage"
    with store.read() as conn:
        assert fts.is_stale(conn)
    later = add_message(store, "s1", "user", "hello after detach")
    assert later in _ids(search.search(store, "hello", limit=50))
    assert routes[-1] == "like_stale"


def test_slow_searches_log_their_route(
    store: StoreHandle, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.INFO, logger="indus.state.search")
    add_session(store, "s1")
    add_message(store, "s1", "user", "quiet search")
    search.search(store, "quiet")
    assert "slow session search" not in caplog.text  # fake clock: 0 ms < 1000 ms
    monkeypatch.setattr(search, "_slow_ms", lambda handle: 0.0)
    search.search(store, "quiet")
    assert "route=words" in caplog.text


class _Config:
    def __init__(self, value: object) -> None:
        self.value = value

    def get(self, key: str, default: object = None) -> object:
        return self.value if key == "sessions.search_slow_ms" else default


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 1000.0),
        (0, 0.0),
        (250, 250.0),
        ("40", 40.0),
        ("lots", 1000.0),
        (-5, 1000.0),
        (True, 1000.0),
    ],
)
def test_slow_threshold_from_config(store: StoreHandle, value: object, expected: float) -> None:
    from dataclasses import replace

    store.scope = replace(store.scope, config=_Config(value))
    assert search._slow_ms(store) == expected  # pyright: ignore[reportPrivateUsage]


def test_corruption_outside_the_index_is_raised(
    store: StoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_session(store, "s1")
    add_message(store, "s1", "user", "anything")

    def broken(plan: object, table: str, match: str) -> None:
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(search, "_match", broken)
    with pytest.raises(sqlite3.DatabaseError):
        search.search(store, "anything")
