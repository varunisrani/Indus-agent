"""P04-12: spill files, previews, the turn budget and the retention sweep."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.state import messages, registry, sessions
from indus.tools import spill
from indus.tools.budget import Budget
from indus.tools.spill import (
    CLOSE_TAG,
    OPEN_TAG,
    HostSpill,
    enforce_turn_budget,
    names_in_texts,
    preview,
    retention_hours,
    retention_hours_for,
    spill_file_name,
    spill_if_needed,
    spilled_path,
    store_references,
    sweep,
    sweep_once,
    text_size,
)
from tests.fakes.clock import FakeClock


@pytest.fixture(autouse=True)
def _fresh_sweep_memory() -> Iterator[None]:
    spill.reset_sweep_memory()
    yield
    spill.reset_sweep_memory()


class MemorySink:
    def __init__(self, *, fail: bool = False) -> None:
        self.saved: dict[str, str] = {}
        self.fail = fail

    def save(self, name: str, text: str) -> str | None:
        if self.fail:
            return None
        self.saved[name] = text
        return f"/spill/{name}"


SMALL = Budget(per_result=100, per_turn=300, preview=40)


# -- single results -----------------------------------------------------------------------------


def test_under_threshold_unchanged() -> None:
    sink = MemorySink()
    assert spill_if_needed("x" * 100, "tool", "c1", budget=SMALL, sink=sink) == "x" * 100
    assert sink.saved == {}


def test_spill_file_content_equals_original(scope: Scope) -> None:
    sink = HostSpill(scope.home)
    original = "".join(f"line {i} ünïcode\n" for i in range(20_000))
    block = spill_if_needed(original, "web_extract", "call_9", sink=sink)
    path = spilled_path(block)
    assert path is not None
    saved = Path(path)
    assert saved == scope.home / "cache" / "spill" / "call_9.txt"
    assert saved.read_text(encoding="utf-8") == original
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600


def test_block_contents() -> None:
    sink = MemorySink()
    text = "a" * 2_000_000
    block = spill_if_needed(text, "tool", "c1", budget=SMALL, sink=sink)
    assert block.startswith(OPEN_TAG) and block.endswith(CLOSE_TAG)
    assert "2,000,000 characters" in block and "1.9 MB" in block
    assert "Saved in full at: /spill/c1.txt" in block
    assert "read_file" in block and "offset" in block and "limit" in block
    assert "First 40 characters:\n" + "a" * 40 in block
    small = spill_if_needed("b" * 3_000, "tool", "c2", budget=SMALL, sink=sink)
    assert "2.9 KB" in small


def test_preview_newline_rule() -> None:
    late = "x" * 30 + "\n" + "y" * 100  # newline past the midpoint of 40
    assert preview(late, 40) == ("x" * 30 + "\n", True)
    early = "x" * 10 + "\n" + "y" * 100  # newline before the midpoint: hard cut
    assert preview(early, 40) == (early[:40], True)
    assert preview("short", 40) == ("short", False)
    at_mid = "x" * 20 + "\n" + "y" * 100  # newline exactly at the midpoint: hard cut
    assert preview(at_mid, 40) == (at_mid[:40], True)


def test_read_file_never_spilled() -> None:
    sink = MemorySink()
    assert spill_if_needed("z" * 500, "read_file", "c", budget=SMALL, sink=sink) == "z" * 500


def test_registered_cap_respected() -> None:
    sink = MemorySink()
    out = spill_if_needed("q" * 60, "tool", "c", budget=SMALL, sink=sink, registered_cap=50)
    assert out.startswith(OPEN_TAG)


def test_nothing_writable_gives_inline_note() -> None:
    out = spill_if_needed("w" * 500, "tool", "c", budget=SMALL, sink=MemorySink(fail=True))
    assert out.startswith("w" * 40)
    assert "could not be saved" in out and "500 characters" in out
    out = spill_if_needed("w" * 500, "tool", "c", budget=SMALL, sink=None)
    assert "could not be saved" in out


def test_host_spill_unwritable_folder_returns_none(scope: Scope) -> None:
    blocker = scope.home / "cache"
    blocker.write_text("not a folder")
    assert HostSpill(scope.home).save("a.txt", "data") is None


def test_host_spill_redacts_when_given(scope: Scope) -> None:
    sink = HostSpill(scope.home, redact=lambda t: t.replace("hidden", "[masked]"))
    where = sink.save("r.txt", "a hidden value")
    assert where is not None and Path(where).read_text() == "a [masked] value"


@pytest.mark.parametrize(
    ("call_id", "check"),
    [
        ("call_abc-1.2", lambda n: n == "call_abc-1.2.txt"),
        ("../../etc/passwd", lambda n: "/" not in n and ".." not in n.split("_")[0]),
        ("a/b\\c", lambda n: "/" not in n and "\\" not in n),
        ("", lambda n: n == "result.txt"),
        (None, lambda n: n == "result.txt"),
        ("x" * 300, lambda n: len(n) < 130),
        ("...", lambda n: n.startswith("result_")),
    ],
)
def test_spill_file_names_are_safe(call_id: str | None, check: Any) -> None:
    name = spill_file_name(call_id)
    assert check(name), name
    assert name.endswith(".txt")
    assert spill_file_name(call_id) == name


def test_call_id_cannot_escape_the_spill_folder(scope: Scope) -> None:
    sink = HostSpill(scope.home)
    block = spill_if_needed("e" * 200_000, "tool", "../../outside", sink=sink)
    path = spilled_path(block)
    assert path is not None
    assert Path(path).parent == scope.home / "cache" / "spill"


# -- turn budget --------------------------------------------------------------------------------


def tool_msg(call_id: str, content: Any) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def test_turn_under_budget_unchanged() -> None:
    msgs = [tool_msg("a", "x" * 100), tool_msg("b", "y" * 100)]
    assert enforce_turn_budget(msgs, budget=SMALL, sink=MemorySink()) == msgs


def test_turn_budget_spills_largest_first() -> None:
    sink = MemorySink()
    budget = Budget(per_result=10_000, per_turn=3_000, preview=40)
    msgs = [
        tool_msg("small", "s" * 900),
        tool_msg("big", "b" * 2_500),
        tool_msg("mid", "m" * 1_500),
    ]
    out = enforce_turn_budget(msgs, budget=budget, sink=sink)
    assert list(sink.saved) == ["big.txt"]
    assert out[0]["content"] == "s" * 900 and out[2]["content"] == "m" * 1_500
    assert out[1]["content"].startswith(OPEN_TAG)
    assert msgs[1]["content"] == "b" * 2_500  # input not mutated
    assert sum(text_size(m["content"]) for m in out) <= 3_000


def test_turn_budget_keeps_going_until_under() -> None:
    sink = MemorySink()
    budget = Budget(per_result=10_000, per_turn=5_100, preview=40)
    msgs = [tool_msg(f"c{i}", str(i) * (3_000 + i)) for i in range(5)]
    out = enforce_turn_budget(msgs, budget=budget, sink=sink)
    assert sum(text_size(m["content"]) for m in out) <= 5_100
    assert list(sink.saved) == ["c4.txt", "c3.txt", "c2.txt", "c1.txt"]
    assert out[0]["content"] == msgs[0]["content"]


def test_already_spilled_results_are_not_candidates() -> None:
    sink = MemorySink()
    spilled = spill_if_needed("p" * 500, "tool", "old", budget=SMALL, sink=sink)
    sink.saved.clear()
    msgs = [tool_msg("old", spilled + "z" * 4_000), tool_msg("new", "n" * 2_000)]
    budget = Budget(per_result=10_000, per_turn=5_000, preview=40)
    out = enforce_turn_budget(msgs, budget=budget, sink=sink)
    assert list(sink.saved) == ["new.txt"]
    assert out[0]["content"] == msgs[0]["content"]


def test_multimodal_spills_only_its_largest_text_part() -> None:
    sink = MemorySink()
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 5_000}}
    parts = [{"type": "text", "text": "t" * 500}, image, {"type": "text", "text": "T" * 4_000}]
    budget = Budget(per_result=10_000, per_turn=3_000, preview=40)
    out = enforce_turn_budget([tool_msg("mm", parts)], budget=budget, sink=sink)
    new_parts = out[0]["content"]
    assert list(sink.saved) == ["mm_part2.txt"]
    assert new_parts[0] == parts[0] and new_parts[1] == image
    assert new_parts[2]["text"].startswith(OPEN_TAG)
    assert text_size(parts) == 4_500  # images do not count


# -- retention ----------------------------------------------------------------------------------


def test_retention_hours_floor_and_defaults() -> None:
    assert retention_hours(90) == 90 * 24
    assert retention_hours(0) == 24
    assert retention_hours(0.5) == 24
    assert retention_hours(None) == 90 * 24
    assert retention_hours("junk") == 90 * 24
    assert retention_hours(float("inf")) == 90 * 24
    assert retention_hours(float("nan")) == 90 * 24


def test_retention_from_scope_config(scope: Scope) -> None:
    class Config:
        def get(self, path: str, /) -> object:
            return {"sessions.retention_days": 3}.get(path)

    assert retention_hours_for(scope) == 90 * 24
    assert retention_hours_for(scope.with_config(Config())) == 72


def _aged(folder: Path, name: str, now: float, hours: float) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("old output")
    stamp = now - hours * 3600
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def store(scope: Scope) -> Iterator[registry.StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()
    registry.close_all()


def test_sweep_keeps_referenced_and_deletes_unreferenced_old(
    scope: Scope, fake_clock: FakeClock, store: registry.StoreHandle
) -> None:
    folder = scope.home / "cache" / "spill"
    sink = HostSpill(scope.home)
    block = spill_if_needed("r" * 200_000, "web_extract", "kept_call", sink=sink)
    sessions.create(store, "s1", "cli")
    messages.append(store, "s1", {"role": "tool", "tool_call_id": "kept_call", "content": block})
    fake_clock.advance(25 * 3600)
    now = fake_clock.now().timestamp()
    kept = folder / "kept_call.txt"
    os.utime(kept, (now - 25 * 3600, now - 25 * 3600))
    stale = _aged(folder, "stale_call.txt", now, 25)
    fresh = _aged(folder, "fresh_call.txt", now, 2)
    removed = sweep(scope.home, retention_h=24, now=now, referenced=lambda: store_references(store))
    assert removed == 1
    assert kept.exists() and fresh.exists() and not stale.exists()


def test_sweep_with_unknown_references_keeps_everything(
    scope: Scope, fake_clock: FakeClock
) -> None:
    now = fake_clock.now().timestamp()
    stale = _aged(scope.home / "cache" / "spill", "a.txt", now, 1_000)
    assert sweep(scope.home, retention_h=24, now=now, referenced=lambda: None) == 0
    assert stale.exists()


def test_sweep_missing_folder(scope: Scope, fake_clock: FakeClock) -> None:
    now = fake_clock.now().timestamp()
    assert sweep(scope.home, retention_h=24, now=now, referenced=set) == 0


def test_sweep_does_not_ask_for_references_when_nothing_is_old(
    scope: Scope, fake_clock: FakeClock
) -> None:
    now = fake_clock.now().timestamp()
    _aged(scope.home / "cache" / "spill", "young.txt", now, 1)

    def boom() -> set[str]:
        raise AssertionError("references read for nothing")

    assert sweep(scope.home, retention_h=24, now=now, referenced=boom) == 0


def test_sweep_once_per_home(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    now = fake_clock.now().timestamp()
    _aged(scope.home / "cache" / "spill", "a.txt", now, 100)
    assert sweep_once(scope.home, retention_h=24, now=now, referenced=set) == 1
    _aged(scope.home / "cache" / "spill", "b.txt", now, 100)
    assert sweep_once(scope.home, retention_h=24, now=now, referenced=set) is None
    other = tmp_path / "other_home"
    assert sweep_once(other, retention_h=24, now=now, referenced=set) == 0


def test_names_in_texts_finds_both_separators() -> None:
    texts = [
        "Saved in full at: /h/cache/spill/a_1.txt",
        '{"p": "C:\\\\h\\\\cache\\\\spill\\\\b.txt"}',
        "spill/c.txt at the start",
        "nothing here",
        None,
    ]
    assert names_in_texts(texts) == {"a_1.txt", "b.txt", "c.txt"}


def test_host_spill_for_sweeps_once_on_first_save(scope: Scope, fake_clock: FakeClock) -> None:
    now = fake_clock.now().timestamp()
    folder = scope.home / "cache" / "spill"
    stale = _aged(folder, "stale.txt", now, 91 * 24)
    asked: list[int] = []

    def refs() -> set[str]:
        asked.append(1)
        return set()

    sink = spill.host_spill_for(scope, refs)
    assert sink.save("first.txt", "a") is not None
    assert not stale.exists()
    again = _aged(folder, "stale_again.txt", now, 91 * 24)
    assert sink.save("second.txt", "b") is not None
    assert again.exists() and asked == [1]
