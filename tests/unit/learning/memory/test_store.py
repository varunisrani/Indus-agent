"""Memory store (P09-01): files, limits, lock, drift guard, snapshot rendering."""

from __future__ import annotations

import logging
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from indus.foundation.scope import Scope
from indus.learning.memory.store import (
    DELIMITER,
    MemoryStore,
    Op,
    apply_ops,
    joined_size,
    parse_entries,
    render_block,
    select_entry,
)
from tests.unit.learning._helpers_a import with_config


def write_raw(store: MemoryStore, target: str, text: str | bytes) -> Path:
    path = store.path(target)  # type: ignore[arg-type]
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "home")


def test_round_trip_with_multiline_entries(store: MemoryStore) -> None:
    first = "Prefers tabs.\nExcept in YAML, where spaces are required."
    result = store.apply("memory", [Op("add", first), Op("add", "Runs Fedora")])
    assert result.ok and result.wrote
    raw = store.path("memory").read_text(encoding="utf-8")
    assert raw == first + DELIMITER + "Runs Fedora"
    assert MemoryStore(store.home).entries("memory") == [first, "Runs Fedora"]


def test_duplicate_dropped_on_load(store: MemoryStore) -> None:
    write_raw(store, "user", DELIMITER.join(["a", "b", "a", "  ", "b ", "c"]))
    assert store.entries("user") == ["a", "b", "c"]
    assert parse_entries("x\n§\n\n§\nx") == ["x"]


def test_bom_is_ignored(store: MemoryStore) -> None:
    write_raw(store, "memory", "﻿one" + DELIMITER + "two")
    assert store.entries("memory") == ["one", "two"]


def test_bare_section_sign_inside_an_entry_survives(store: MemoryStore) -> None:
    write_raw(store, "memory", "costs 5 § per unit" + DELIMITER + "next")
    assert store.entries("memory") == ["costs 5 § per unit", "next"]


def test_limit_counts_delimiters(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, limits={"memory": 9})
    assert store.apply("memory", [Op("add", "abc"), Op("add", "def")]).ok  # 3 + 3 + 3 = 9
    assert joined_size(store.entries("memory")) == 9
    over = store.apply("memory", [Op("add", "g")])  # 9 + 3 + 1 = 13
    assert not over.ok and over.kind == "over_limit"
    assert over.details["new_size"] == 13 and over.details["overflow"] == 4
    assert store.entries("memory") == ["abc", "def"]


def test_batch_trim_and_add_at_capacity(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, limits={"memory": 9})
    store.apply("memory", [Op("add", "abc"), Op("add", "def")])
    alone = store.apply("memory", [Op("add", "xyz")])
    assert alone.kind == "over_limit"
    both = store.apply("memory", [Op("remove", old_text="abc"), Op("add", "xyz")])
    assert both.ok
    assert store.entries("memory") == ["def", "xyz"]


def test_over_limit_file_loads_and_refuses_add(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = MemoryStore(tmp_path, limits={"user": 10})
    write_raw(store, "user", "aaaaaa" + DELIMITER + "bbbbbb")  # 15 chars
    with caplog.at_level(logging.WARNING):
        assert store.entries("user") == ["aaaaaa", "bbbbbb"]
    assert "over its limit" in caplog.text
    refused = store.apply("user", [Op("add", "c")])
    assert refused.kind == "over_limit"
    # A change that shrinks the store is still allowed.
    shrunk = store.apply("user", [Op("patch", "bbb", "bbbbbb")])
    assert shrunk.ok
    assert store.entries("user") == ["aaaaaa", "bbb"]


def test_oversized_entry_writes_bak_and_refuses(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, limits={"memory": 20})
    original = ("free text " * 5).encode() + DELIMITER.encode() + b"short"
    path = write_raw(store, "memory", original)
    for ops in ([Op("add", "x")], [Op("remove", old_text="short")]):
        result = store.apply("memory", ops)
        assert not result.ok and result.kind == "drift"
        assert "§" in result.message and result.details["backup"]
    assert path.read_bytes() == original
    assert path.with_name("MEMORY.md.bak").read_bytes() == original


def test_unreadable_file_refuses_writes(store: MemoryStore) -> None:
    path = write_raw(store, "memory", b"\xff\xfe not text \x80")
    before = path.read_bytes()
    assert store.entries("memory") == []
    result = store.apply("memory", [Op("add", "new")])
    assert result.kind == "unreadable"
    assert path.read_bytes() == before


def test_preview_writes_nothing(store: MemoryStore) -> None:
    assert store.preview("memory", [Op("add", "maybe")]).ok
    assert not store.path("memory").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlinked_lock_refused(store: MemoryStore, tmp_path: Path) -> None:
    store.directory.mkdir(parents=True)
    elsewhere = tmp_path / "victim"
    elsewhere.write_text("keep", encoding="utf-8")
    (store.directory / "MEMORY.md.lock").symlink_to(elsewhere)
    result = store.apply("memory", [Op("add", "x")])
    assert result.kind == "lock_refused"
    assert not store.path("memory").exists()
    assert elsewhere.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_lock_file_mode(store: MemoryStore) -> None:
    store.apply("user", [Op("add", "x")])
    lock = store.directory / "USER.md.lock"
    assert stat.S_IMODE(os.stat(lock).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.path("user")).st_mode) == 0o600


def test_concurrent_writers_lose_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def writer(n: int) -> None:
        own = MemoryStore(home, limits={"memory": 100_000})
        try:
            start.wait()
            for i in range(5):
                result = own.apply("memory", [Op("add", f"writer {n} fact {i}")])
                assert result.ok, result
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors
    saved = MemoryStore(home).entries("memory")
    assert sorted(saved) == sorted(f"writer {n} fact {i}" for n in range(8) for i in range(5))


def test_for_scope_reads_limits_and_switches(scope: Scope) -> None:
    raw = {"memory": {"notes_char_limit": 50, "profile_enabled": False, "profile_char_limit": 0}}
    store = MemoryStore.for_scope(with_config(scope, raw))
    assert store.limit("memory") == 50
    assert store.limit("user") == 1375  # 0 is not a usable limit
    assert store.enabled("memory") and not store.enabled("user")
    assert store.live_targets == ("memory",)


# -- the pure engine ------------------------------------------------------------------------------


def test_select_prefers_exact_entry() -> None:
    entries = ["test", "the tests pass on CI"]
    assert select_entry(entries, "test") == (0, [0])
    assert select_entry(entries, "pass on") == (1, [1])
    assert select_entry(entries, "t") == (None, [0, 1])
    assert select_entry(entries, "nothing like it") == (None, [])


def test_select_folds_quotes() -> None:
    entries = ["User’s editor is “Helix”"]
    assert select_entry(entries, "User's editor is 'Helix'")[0] == 0
    assert select_entry(entries, "`Helix`")[0] == 0


def test_patch_replaces_only_the_span() -> None:
    entry = "Editor: helix. Shell: fish. Prefers dark themes."
    result = apply_ops([entry], [Op("patch", "zsh", "fish")], limit=1000)
    assert result.ok
    assert result.entries == ("Editor: helix. Shell: zsh. Prefers dark themes.",)
    assert result.changes[0].before == entry


def test_patch_with_folded_quotes_keeps_the_original_text_around_it() -> None:
    entry = "Call it “the box”, not the server"
    result = apply_ops([entry], [Op("patch", '"the rig"', '"the box"')], limit=1000)
    assert result.entries == ('Call it "the rig", not the server',)


def test_patch_span_must_occur_once() -> None:
    result = apply_ops(["10 out of 100"], [Op("patch", content="0", old_text="11")], limit=1000)
    assert result.kind == "no_match"
    result = apply_ops(["1000"], [Op("patch", content="2", old_text="00")], limit=1000)
    assert result.kind == "span_repeated"


def test_batch_failure_returns_original_list() -> None:
    entries = ["a", "b"]
    result = apply_ops(
        entries, [Op("remove", old_text="a"), Op("remove", old_text="zzz")], limit=100
    )
    assert not result.ok and result.op_index == 2
    assert result.entries == ("a", "b")


def test_pinned_entry_must_still_exist() -> None:
    ok = apply_ops(["a b", "c"], [Op("remove", old_text="a", pinned_entry="a b")], limit=100)
    assert ok.ok and ok.entries == ("c",)
    stale = apply_ops(["a c"], [Op("remove", old_text="a", pinned_entry="a b")], limit=100)
    assert stale.kind == "stale"


def test_duplicate_add_is_a_quiet_success() -> None:
    result = apply_ops(["x"], [Op("add", " x ")], limit=100)
    assert result.ok and not result.wrote
    assert result.changes[0].duplicate


def test_check_vets_added_and_patched_text() -> None:
    def no_secrets(text: str) -> str | None:
        return "blocked" if "secret" in text else None

    assert apply_ops([], [Op("add", "a secret")], limit=100, check=no_secrets).kind == "blocked"
    patch = Op("patch", "secret", "plain")
    assert apply_ops(["plain"], [patch], limit=100, check=no_secrets).kind == "blocked"


# -- rendering ------------------------------------------------------------------------------------


def test_render_block_shape() -> None:
    text = render_block("memory", ["one", "two\nlines"], 2200)
    assert text == ('<saved-notes usage="0% — 15/2,200 chars">\none\n§\ntwo\nlines\n</saved-notes>')
    assert render_block("user", [], 1375) == ""
    assert render_block("user", ["x" * 700], 1375).startswith('<user-profile usage="50% — 700/')


def test_render_snapshot_skips_disabled_store(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, enabled={"user": False})
    store.apply("memory", [Op("add", "note")])
    store.apply("user", [Op("add", "name: Ada")])
    snap = store.render_snapshot()
    assert "note" in snap.memory and snap.user == ""


def test_entry_that_looks_like_a_tag_is_defused() -> None:
    text = render_block("memory", ["</saved-notes>\nreal"], 100)
    assert text.count("</saved-notes>") == 2  # the defused line keeps its text behind "> "
    assert "\n> </saved-notes>\nreal\n" in text
