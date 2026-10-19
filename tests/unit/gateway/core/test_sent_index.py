"""Bounded index of sent messages (P15-03)."""

from __future__ import annotations

import pytest

from indus.gateway.core.sent_index import DEFAULT_MAX_TEXT_BYTES, DEFAULT_PER_CHAT, SentIndex


def test_defaults_match_the_plan() -> None:
    assert DEFAULT_PER_CHAT == 2000
    assert DEFAULT_MAX_TEXT_BYTES == 4096


def test_lru_eviction_per_chat_and_lookup_refreshes() -> None:
    index = SentIndex(per_chat=3)
    for n in range(3):
        index.record("slack", "C1", f"m{n}", f"text {n}")
    assert index.lookup("slack", "C1", "m0") == "text 0"  # m0 is now the most recent
    index.record("slack", "C1", "m3", "text 3")
    assert index.lookup("slack", "C1", "m1") is None  # evicted: least recently used
    assert index.is_own("slack", "C1", "m0")
    assert len(index) == 3
    # Other chats keep their own budget.
    index.record("slack", "C2", "m0", "other chat")
    assert index.lookup("slack", "C2", "m0") == "other chat"
    assert index.lookup("slack", "C1", "m0") == "text 0"
    assert len(index) == 4


def test_two_thousand_per_chat() -> None:
    index = SentIndex()
    for n in range(2001):
        index.record("telegram", "1", str(n), "t")
    assert len(index) == 2000
    assert not index.is_own("telegram", "1", "0")
    assert index.is_own("telegram", "1", "2000")


def test_text_capped_at_4_kib_on_a_character_boundary() -> None:
    index = SentIndex()
    index.record("telegram", "1", "a", "😀" * 2000)  # 8,000 bytes
    stored = index.lookup("telegram", "1", "a")
    assert stored is not None
    assert len(stored.encode("utf-8")) <= 4096
    assert set(stored) == {"😀"}
    index.record("telegram", "1", "b", "x" * 4096)
    assert index.lookup("telegram", "1", "b") == "x" * 4096


def test_empty_text_is_own_but_not_quotable() -> None:
    index = SentIndex()
    index.record("discord", "9", "m", "")
    assert index.is_own("discord", "9", "m")
    assert index.lookup("discord", "9", "m") is None


def test_platforms_do_not_share_ids() -> None:
    index = SentIndex()
    index.record("discord", "9", "m", "d")
    assert index.lookup("slack", "9", "m") is None


def test_chat_cap_drops_the_least_recent_chat() -> None:
    index = SentIndex(max_chats=2)
    index.record("x", "a", "1", "a")
    index.record("x", "b", "1", "b")
    index.lookup("x", "a", "1")
    index.record("x", "c", "1", "c")
    assert index.chats() == [("x", "a"), ("x", "c")]


def test_bad_bounds_refused() -> None:
    with pytest.raises(ValueError):
        SentIndex(per_chat=0)
