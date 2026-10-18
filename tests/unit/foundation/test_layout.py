"""foundation.layout: home skeleton, persona seed, scratch dir, prune and socket dir."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from indus.foundation import errors, layout
from indus.foundation.scope import Scope, make_scope
from tests.fakes.clock import FakeClock

HOUR = 3600.0


@pytest.fixture(autouse=True)
def fresh_memo() -> Iterator[None]:
    layout.reset_memo()
    yield
    layout.reset_memo()


def _scope(home: Path) -> Scope:
    return make_scope(home)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_fresh_home_gets_every_dir_owner_only_and_a_persona(tmp_path: Path) -> None:
    home = tmp_path / "fresh"
    assert layout.ensure_home(_scope(home)) == home
    assert _mode(home) == 0o700
    for rel in layout.SKELETON_DIRS:
        assert (home / rel).is_dir(), rel
        assert _mode(home / rel) == 0o700, rel
    persona = home / layout.PERSONA_FILE
    assert persona.read_text(encoding="utf-8") == layout.DEFAULT_PERSONA
    assert _mode(persona) == 0o600


def test_customised_persona_survives_later_runs(tmp_path: Path) -> None:
    home = tmp_path / "h"
    layout.ensure_home(_scope(home))
    persona = home / layout.PERSONA_FILE
    persona.write_text("I am a pirate.\n", encoding="utf-8")
    layout.reset_memo()
    layout.ensure_home(_scope(home))
    assert persona.read_text(encoding="utf-8") == "I am a pirate.\n"


def test_empty_persona_is_the_users_choice(tmp_path: Path) -> None:
    home = tmp_path / "h"
    home.mkdir()
    (home / layout.PERSONA_FILE).write_text("", encoding="utf-8")
    assert layout.seed_persona(home) is False
    assert (home / layout.PERSONA_FILE).read_text(encoding="utf-8") == ""


def test_dangling_persona_link_replaced_and_target_not_created(tmp_path: Path) -> None:
    home = tmp_path / "h"
    home.mkdir()
    target = tmp_path / "elsewhere" / "persona.md"
    (home / layout.PERSONA_FILE).symlink_to(target)
    layout.ensure_home(_scope(home))
    persona = home / layout.PERSONA_FILE
    assert not persona.is_symlink()
    assert persona.read_text(encoding="utf-8") == layout.DEFAULT_PERSONA
    assert not target.exists()
    assert not target.parent.exists()


def test_cyclic_persona_link_replaced(tmp_path: Path) -> None:
    home = tmp_path / "h"
    home.mkdir()
    persona = home / layout.PERSONA_FILE
    persona.symlink_to(persona)
    assert layout.seed_persona(home) is True
    assert not persona.is_symlink()
    assert persona.read_text(encoding="utf-8") == layout.DEFAULT_PERSONA


def test_live_persona_link_is_kept(tmp_path: Path) -> None:
    home = tmp_path / "h"
    home.mkdir()
    target = tmp_path / "shared.md"
    target.write_text("shared persona\n", encoding="utf-8")
    (home / layout.PERSONA_FILE).symlink_to(target)
    assert layout.seed_persona(home) is False
    assert (home / layout.PERSONA_FILE).is_symlink()
    assert target.read_text(encoding="utf-8") == "shared persona\n"


def test_tombstoned_profile_refused_even_after_a_memo_hit(tmp_path: Path) -> None:
    root = tmp_path / "root"
    work = root / "profiles" / "work"
    work.mkdir(parents=True)
    scope = _scope(work)
    layout.ensure_home(scope)
    (root / "profiles" / ".deleted").mkdir()
    (root / "profiles" / ".deleted" / "work").write_text("2026-01-01T00:00:00Z")
    with pytest.raises(errors.ProfileError) as info:
        layout.ensure_home(scope)
    assert info.value.code == "profile.tombstoned"


def test_missing_named_profile_refused_and_not_created(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    gone = root / "profiles" / "gone"
    with pytest.raises(errors.ProfileError) as info:
        layout.ensure_home(_scope(gone))
    assert info.value.code == "profile.not_found"
    assert not gone.exists()


def test_memo_skips_work_but_notices_a_removed_home(tmp_path: Path) -> None:
    import shutil

    home = tmp_path / "h"
    layout.ensure_home(_scope(home))
    shutil.rmtree(home / "skills")
    layout.ensure_home(_scope(home))  # memo hit: not re-checked
    assert not (home / "skills").exists()
    shutil.rmtree(home)
    layout.ensure_home(_scope(home))
    assert (home / "skills").is_dir()


def test_scratch_env_keeps_user_values(tmp_path: Path) -> None:
    home = tmp_path / "h"
    env = layout.scratch_env(home, {"TMP": "/custom", "TEMP": ""})
    scratch = str(home / "cache" / "scratch")
    assert env == {"TMPDIR": scratch, "TEMP": scratch}


def test_scratch_dir_is_created_owner_only(tmp_path: Path) -> None:
    home = tmp_path / "h"
    path = layout.scratch_dir(_scope(home))
    assert path == home / "cache" / "scratch"
    assert _mode(path) == 0o700


def _aged(path: Path, clock: FakeClock, hours: float) -> None:
    when = (clock.now() - timedelta(hours=hours)).timestamp()
    os.utime(path, (when, when), follow_symlinks=False)


def _make_scratch(tmp_path: Path) -> tuple[Scope, Path]:
    home = tmp_path / "h"
    scope = _scope(home)
    return scope, layout.scratch_dir(scope)


def test_prune_removes_entries_idle_over_a_day(tmp_path: Path, fake_clock: FakeClock) -> None:
    scope, scratch = _make_scratch(tmp_path)
    old_dir = scratch / "old-run"
    (old_dir / "deep").mkdir(parents=True)
    (old_dir / "deep" / "f.txt").write_text("x")
    for p in (old_dir / "deep" / "f.txt", old_dir / "deep", old_dir):
        _aged(p, fake_clock, 25)
    young = scratch / "young.txt"
    young.write_text("y")
    _aged(young, fake_clock, 23)
    busy = scratch / "busy-run"
    busy.mkdir()
    (busy / "new.txt").write_text("z")
    _aged(busy / "new.txt", fake_clock, 1)
    _aged(busy, fake_clock, 30)
    old_file = scratch / "old.bin"
    old_file.write_text("o")
    _aged(old_file, fake_clock, 48)

    assert layout.prune_scratch(scope, fake_clock) == 2
    assert not old_dir.exists()
    assert not old_file.exists()
    assert young.exists()
    assert busy.exists()


def test_prune_ignores_fresh_mtimes_of_hard_linked_files(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    scope, scratch = _make_scratch(tmp_path)
    shared = tmp_path / "pack.bin"
    shared.write_text("p")
    entry = scratch / "clone"
    entry.mkdir()
    os.link(shared, entry / "pack.bin")
    _aged(shared, fake_clock, 0)
    _aged(entry, fake_clock, 30)
    assert layout.prune_scratch(scope, fake_clock) == 1
    assert not entry.exists()
    assert shared.read_text() == "p"


def test_prune_runs_at_most_once_an_hour(tmp_path: Path, fake_clock: FakeClock) -> None:
    scope, scratch = _make_scratch(tmp_path)
    assert layout.prune_scratch(scope, fake_clock) == 0
    stale = scratch / "stale.txt"
    stale.write_text("s")
    _aged(stale, fake_clock, 25)
    fake_clock.advance(HOUR - 1)
    assert layout.prune_scratch(scope, fake_clock) == 0
    assert stale.exists()
    fake_clock.advance(2)
    assert layout.prune_scratch(scope, fake_clock) == 1
    assert not stale.exists()
    assert (scratch / ".last_prune").exists()


def test_prune_does_not_follow_links(tmp_path: Path, fake_clock: FakeClock) -> None:
    scope, scratch = _make_scratch(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("k")
    link = scratch / "link"
    link.symlink_to(outside)
    _aged(link, fake_clock, 30)
    assert layout.prune_scratch(scope, fake_clock) == 1
    assert not os.path.lexists(link)
    assert (outside / "keep.txt").exists()


def test_socket_dir_falls_back_for_long_scratch_paths(tmp_path: Path) -> None:
    temp_root = tmp_path / "t"
    long_home = tmp_path / ("x" * max(1, 60 - len(str(tmp_path / "cache" / "scratch"))))
    long_scope = _scope(long_home)
    assert len(str(long_home / "cache" / "scratch")) > layout.SOCKET_PATH_BUDGET
    assert layout.socket_dir(long_scope, temp_root=temp_root) == temp_root

    short_scope = _scope(Path("/h"))
    assert layout.socket_dir(short_scope) == Path("/h/cache/scratch")


def test_profile_home_problem_shapes(tmp_path: Path) -> None:
    assert layout.profile_home_problem(tmp_path / "plain") is None
    named = tmp_path / "profiles" / "x"
    assert layout.profile_home_problem(named) == "profile.not_found"
    named.mkdir(parents=True)
    assert layout.profile_home_problem(named) is None
