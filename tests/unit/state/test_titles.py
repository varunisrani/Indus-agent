"""Session titles (P02-13): sanitising, provenance, ancestor yield, ``#N`` numbering."""

from __future__ import annotations

import random
import unicodedata

import pytest

from indus.state import titles
from indus.state.registry import StoreHandle
from indus.state.titles import TitleTaken
from tests.unit.state._helpers_a import chain, column, seed, sql
from tests.unit.state.conftest import AutoClock


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("safe\u202etxt.exe", "safetxt.exe"),  # right-to-left override
        ("zero\u200bwidth\u200djoin", "zerowidthjoin"),
        ("bell\x07 rings", "bell rings"),
        ("\ufeffbom first", "bom first"),
        ("iso\u2066late\u2069d", "isolated"),
        ("note\ufff9a\ufffab\ufffb \ufffcobj", "noteab obj"),
        ("lone \ud800 half", "lone half"),
        ("  many \t\n spaces\r\n  ", "many spaces"),
        ("line\u2028sep", "line sep"),
        ("c1\x85next\x9bcsi", "c1 nextcsi"),
        ("x" * 300, "x" * 100),
        ("word " * 30, ("word " * 20).strip()),
        ("     ", None),
        ("\u200b\u202e", None),
        ("", None),
        (None, None),
        ("Plain Title #2", "Plain Title #2"),
        ("emoji \U0001f600 ok", "emoji \U0001f600 ok"),
    ],
)
def test_sanitize_table(raw: str | None, clean: str | None) -> None:
    assert titles.sanitize(raw) == clean


_POOLS = ((0x00, 0x7F), (0x80, 0x24F), (0x2000, 0x206F), (0xD800, 0xDFFF), (0x4E00, 0x4E80))


@pytest.mark.parametrize("seed_value", range(60))
def test_sanitize_is_idempotent_on_generated_text(seed_value: int) -> None:
    # Seeded table instead of hypothesis (not installed): mixed controls, format marks, surrogates.
    rng = random.Random(seed_value)
    for _ in range(20):
        size = rng.randint(0, 160)
        text = "".join(chr(rng.randint(*rng.choice(_POOLS))) for _ in range(size))
        once = titles.sanitize(text)
        assert titles.sanitize(once) == once
        if once is not None:
            assert 0 < len(once) <= titles.MAX_TITLE_LENGTH
            assert once == once.strip() and "  " not in once
            for char in once:
                assert unicodedata.category(char) not in ("Cc", "Cs")
                assert char not in "\u200b\u200d\u202e\u2066\ufeff"


# -- provenance -----------------------------------------------------------------------------------


def test_llm_title_does_not_replace_a_user_title(store: StoreHandle) -> None:
    seed(store, "s1")
    assert titles.set_title(store, "s1", "Mine")
    assert titles.set_title(store, "s1", "Generated", source="llm") is False
    assert titles.set_title(store, "s1", "Derived", source="derived") is False
    assert (column(store, "s1", "title"), column(store, "s1", "title_source")) == ("Mine", "user")


def test_llm_title_replaces_a_derived_one_once(store: StoreHandle) -> None:
    seed(store, "s1")
    assert titles.set_title(store, "s1", "from first message", source="derived")
    assert titles.set_title(store, "s1", "Better", source="llm")
    assert titles.set_title(store, "s1", "Again", source="llm") is False
    assert titles.set_title(store, "s1", "Lower", source="derived") is False
    assert (column(store, "s1", "title"), column(store, "s1", "title_source")) == ("Better", "llm")
    assert titles.set_title(store, "s1", "Person", source="user")
    assert column(store, "s1", "title_source") == "user"


def test_null_provenance_ranks_as_user(store: StoreHandle) -> None:
    seed(store, "s1")
    sql(store, "UPDATE sessions SET title = 'Legacy' WHERE id = 's1'")
    assert titles.set_title(store, "s1", "Generated", source="llm") is False
    assert column(store, "s1", "title") == "Legacy"


def test_clearing_and_automatic_empty_writes(store: StoreHandle) -> None:
    seed(store, "s1")
    titles.set_title(store, "s1", "Named", source="derived")
    assert titles.set_title(store, "s1", "   ", source="llm") is False
    assert column(store, "s1", "title") == "Named"
    assert titles.set_title(store, "s1", "")
    assert (column(store, "s1", "title"), column(store, "s1", "title_source")) == (None, None)


def test_missing_row_and_unknown_source(store: StoreHandle) -> None:
    assert titles.set_title(store, "nope", "x") is False
    seed(store, "s1")
    with pytest.raises(ValueError):
        titles.set_title(store, "s1", "x", source="robot")  # type: ignore[arg-type]


def test_title_is_stored_sanitised(store: StoreHandle) -> None:
    seed(store, "s1")
    titles.set_title(store, "s1", " my\u202e  chat\n")
    assert titles.get_title(store, "s1") == "my chat"
    assert titles.get_title(store, "nope") is None


# -- uniqueness -----------------------------------------------------------------------------------


def test_title_held_by_own_compression_root_moves_to_the_tip(store: StoreHandle) -> None:
    chain(store, ["root", "mid", "tip"])
    titles.set_title(store, "root", "Project chat")
    assert titles.set_title(store, "tip", "Project chat", source="llm")
    assert column(store, "root", "title") is None and column(store, "root", "title_source") is None
    assert (column(store, "tip", "title"), column(store, "tip", "title_source")) == (
        "Project chat",
        "llm",
    )


def test_title_held_by_an_unrelated_session_is_taken(store: StoreHandle) -> None:
    seed(store, "a")
    seed(store, "b")
    titles.set_title(store, "a", "Shared")
    with pytest.raises(TitleTaken) as caught:
        titles.set_title(store, "b", "Shared")
    assert caught.value.code == "store.title_taken" and caught.value.holder == "a"
    assert column(store, "b", "title") is None
    assert titles.set_title(store, "a", "Shared")  # rewriting one's own title is fine


def test_descendant_does_not_yield_to_its_ancestor(store: StoreHandle) -> None:
    chain(store, ["root", "tip"])
    titles.set_title(store, "tip", "Held by tip")
    with pytest.raises(TitleTaken):
        titles.set_title(store, "root", "Held by tip")


def test_branch_parent_does_not_yield(store: StoreHandle) -> None:
    seed(store, "parent")
    seed(store, "fork", parent_session_id="parent", model_config={"_branched_from": "parent"})
    titles.set_title(store, "parent", "Original")
    with pytest.raises(TitleTaken):
        titles.set_title(store, "fork", "Original")


def test_copy_title_keeps_provenance(store: StoreHandle) -> None:
    chain(store, ["root", "tip"])
    titles.set_title(store, "root", "Auto", source="llm")
    assert store.write(lambda conn: titles.copy_title_in(conn, "root", "tip"))
    assert (column(store, "tip", "title"), column(store, "tip", "title_source")) == ("Auto", "llm")
    assert column(store, "root", "title") is None
    seed(store, "untitled")
    assert store.write(lambda conn: titles.copy_title_in(conn, "untitled", "tip")) is False


def test_copy_of_a_legacy_title_counts_as_user(store: StoreHandle) -> None:
    chain(store, ["root", "tip"])
    sql(store, "UPDATE sessions SET title = 'Old' WHERE id = 'root'")
    store.write(lambda conn: titles.copy_title_in(conn, "root", "tip"))
    assert column(store, "tip", "title_source") == "user"


# -- numbering ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("existing", "base", "expected"),
    [
        (["name"], "name", "name #2"),
        (["x", "x #3"], "x", "x #4"),
        (["x", "x #3"], "x #3", "x #4"),
        (["x #2"], "x", "x #3"),
        ([], "fresh", "fresh"),
        (["50%", "50% #2", "50x #9"], "50%", "50% #3"),
        (["a_b", "aXb #7"], "a_b", "a_b #2"),
        (["t", "t #2 #5"], "t", "t #2"),
        (["v", "v #10", "v #9"], "v", "v #11"),
    ],
)
def test_next_lineage_title(
    store: StoreHandle, existing: list[str], base: str, expected: str
) -> None:
    for index, title in enumerate(existing):
        seed(store, f"s{index}")
        titles.set_title(store, f"s{index}", title)
    assert titles.next_lineage_title(store, base) == expected


def test_resolve_prefers_the_newest_numbered_continuation(
    store: StoreHandle, fake_clock: AutoClock
) -> None:
    seed(store, "first")
    titles.set_title(store, "first", "Work")
    assert titles.resolve_title(store, "Work") == "first"
    fake_clock.advance(10)
    seed(store, "third")
    titles.set_title(store, "third", "Work #3")
    fake_clock.advance(10)
    seed(store, "second")
    titles.set_title(store, "second", "Work #2")
    assert titles.resolve_title(store, "Work") == "second"
    assert titles.resolve_title(store, " Work ") == "second"
    assert titles.resolve_title(store, "Wor") is None
    assert titles.resolve_title(store, "") is None
    seed(store, "pct")
    titles.set_title(store, "pct", "W%rk #4")
    assert titles.resolve_title(store, "W%rk") == "pct"
