"""Bounded and unbounded output capture, spill files (P05-02, R02.2)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from indus.backends import capture as capture_mod
from indus.backends.capture import (
    OutputCapture,
    bounded_capture,
    prune_spills,
    spill_dir,
    unbounded_capture,
)


def _feed(cap: OutputCapture, text: str, chunk: int = 4096) -> None:
    for start in range(0, len(text), chunk):
        cap.append(text[start : start + chunk])


def test_sixty_thousand_chars_keep_fifty_thousand_and_spill_everything(tmp_path: Path) -> None:
    text = "".join(f"{i:07d}\n" for i in range(7500))  # 60,000 characters
    assert len(text) == 60_000
    cap = bounded_capture(tmp_path, 50_000, wall_time=lambda: 1_700_000_000.0)
    _feed(cap, text)
    rendered = cap.render()
    assert 49_900 <= len(rendered) <= 50_000
    assert rendered.startswith(text[:19_000])
    assert rendered.endswith(text[-29_000:])
    assert "60,000 characters" in rendered
    path = cap.close_spill()
    assert path is not None and Path(path).read_text() == text
    assert Path(path).parent == spill_dir(tmp_path)
    assert Path(path).name.startswith(f"out-1700000000-{os.getpid()}-")
    assert stat.S_IMODE(Path(path).stat().st_mode) == 0o600
    assert cap.total_chars == 60_000


def test_small_output_untouched_and_no_spill(tmp_path: Path) -> None:
    cap = bounded_capture(tmp_path, 50_000)
    cap.append("hello\n")
    assert cap.render() == "hello\n"
    assert cap.render(suffix="\n[done]") == "hello\n\n[done]"
    assert cap.close_spill() is None
    assert not spill_dir(tmp_path).exists()


def test_unbounded_keeps_six_megabytes_intact() -> None:
    text = ("abcdefghij" * 100 + "\n") * 6000  # ~6 MB
    cap = unbounded_capture()
    _feed(cap, text, chunk=65536)
    assert cap.render() == text
    assert cap.close_spill() is None and not cap.bounded


def test_suffix_always_fits_inside_the_limit() -> None:
    cap = OutputCapture(1000)
    _feed(cap, "y" * 5000, chunk=333)
    suffix = "\n[Command timed out after 3s]"
    out = cap.render(suffix=suffix)
    assert len(out) <= 1000 and out.endswith(suffix)
    tiny = OutputCapture(10)
    tiny.append("z" * 50)
    assert tiny.render(suffix="0123456789ABC") == "3456789ABC"


@pytest.mark.parametrize("limit", [1, 7, 100, 999, 50_000])
def test_render_never_exceeds_limit(limit: int) -> None:
    cap = OutputCapture(limit)
    _feed(cap, "q" * (limit * 3 + 17), chunk=97)
    assert len(cap.render()) <= limit
    assert len(cap.render(suffix="[s]")) <= limit


def test_exactly_at_limit_is_not_cut() -> None:
    cap = OutputCapture(100)
    cap.append("a" * 60)
    cap.append("b" * 40)
    assert cap.render() == "a" * 60 + "b" * 40


def test_spill_is_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture_mod, "SPILL_CAP_CHARS", 1000)
    cap = bounded_capture(tmp_path, 100)
    _feed(cap, "c" * 3000, chunk=250)
    path = cap.close_spill()
    assert path is not None
    body = Path(path).read_text()
    assert body.startswith("c" * 1000) and "stopped at 1,000 characters" in body
    assert body.split("\n")[0] == "c" * 1000


def test_planted_symlink_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "victim.txt"
    target.write_text("keep")
    cap = bounded_capture(tmp_path, 10, wall_time=lambda: 5.0)
    assert cap._spill_path is not None  # pyright: ignore[reportPrivateUsage]
    cap._spill_path.parent.mkdir(parents=True)  # pyright: ignore[reportPrivateUsage]
    cap._spill_path.symlink_to(target)  # pyright: ignore[reportPrivateUsage]
    _feed(cap, "x" * 100, chunk=7)
    assert len(cap.render()) <= 10
    assert cap.close_spill() is None
    assert target.read_text() == "keep"


def test_old_spills_are_pruned(tmp_path: Path) -> None:
    folder = spill_dir(tmp_path)
    folder.mkdir(parents=True)
    old = folder / "out-1-1-1.log"
    fresh = folder / "out-2-2-2.log"
    other = folder / "keep.txt"
    for path in (old, fresh, other):
        path.write_text("x")
    now = 10_000_000.0
    os.utime(old, (now - 8 * 86400, now - 8 * 86400))
    os.utime(fresh, (now - 86400, now - 86400))
    os.utime(other, (now - 30 * 86400, now - 30 * 86400))
    assert prune_spills(folder, now=now) == 1
    assert not old.exists() and fresh.exists() and other.exists()
    # A new bounded capture prunes on the way in.
    os.utime(fresh, (now - 9 * 86400, now - 9 * 86400))
    bounded_capture(tmp_path, 10, wall_time=lambda: now)
    assert not fresh.exists()


def test_bounded_without_home_never_spills() -> None:
    cap = bounded_capture(None, 10)
    cap.append("x" * 100)
    assert len(cap.render()) <= 10 and cap.close_spill() is None
