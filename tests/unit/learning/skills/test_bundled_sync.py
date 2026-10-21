"""Bundled skill seeding and sync (P09-21, trap R07.28)."""

from __future__ import annotations

import os
import stat
import sys
import tomllib
from pathlib import Path

import pytest

from indus.learning.skills import bundled_sync as bs
from indus.learning.skills.bundled_sync import (
    OPT_OUT_MARKER,
    bundled_source,
    is_opted_out,
    list_modified,
    manifest_path,
    opt_in,
    read_manifest,
    remove_pristine,
    reset,
    set_opt_out,
    sync_bundled,
    tree_hash,
)

REPO = Path(__file__).resolve().parents[4]


def _skill(root: Path, rel: str, body: str = "body\n", extra: dict[str, str] | None = None) -> Path:
    folder = root / rel
    folder.mkdir(parents=True, exist_ok=True)
    name = Path(rel).name
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name}\n---\n{body}", encoding="utf-8"
    )
    for sub, text in (extra or {}).items():
        target = folder / sub
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return folder


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "shipped"
    _skill(root, "dev/alpha", extra={"references/notes.md": "alpha notes\n"})
    _skill(root, "dev/beta")
    _skill(root, "gamma")
    return root


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "profile-home"
    path.mkdir()
    return path


def _md(home: Path, rel: str) -> Path:
    return home / "skills" / rel / "SKILL.md"


# -- seeding -------------------------------------------------------------------------------------


def test_seed_copies_packages_and_records_origins(source: Path, home: Path) -> None:
    report = sync_bundled(home, source=source)
    assert sorted(report.added) == ["alpha", "beta", "gamma"]
    assert report.total == 3
    assert _md(home, "dev/alpha").is_file()
    assert (home / "skills/dev/alpha/references/notes.md").read_text() == "alpha notes\n"
    assert _md(home, "gamma").is_file()
    rows = read_manifest(home)
    assert rows == {
        "alpha": tree_hash(source / "dev/alpha"),
        "beta": tree_hash(source / "dev/beta"),
        "gamma": tree_hash(source / "gamma"),
    }
    lines = manifest_path(home).read_text().splitlines()
    assert lines == sorted(lines)
    assert all(len(line.split(":")[1]) == 64 for line in lines)


def test_second_sync_changes_nothing(source: Path, home: Path) -> None:
    sync_bundled(home, source=source)
    before = manifest_path(home).read_bytes()
    report = sync_bundled(home, source=source)
    assert report.added == [] and report.updated == []
    assert sorted(report.unchanged) == ["alpha", "beta", "gamma"]
    assert manifest_path(home).read_bytes() == before


def test_edited_skill_kept_and_pristine_refreshed(source: Path, home: Path) -> None:
    sync_bundled(home, source=source)
    _md(home, "dev/alpha").write_text("my own version\n", encoding="utf-8")
    _skill(source, "dev/alpha", body="upstream v2\n")
    _skill(source, "dev/beta", body="upstream v2\n")
    report = sync_bundled(home, source=source)
    assert report.user_modified == ["alpha"]
    assert report.updated == ["beta"]
    assert _md(home, "dev/alpha").read_text() == "my own version\n"
    assert "upstream v2" in _md(home, "dev/beta").read_text()
    assert read_manifest(home)["beta"] == tree_hash(source / "dev/beta")
    # The edited copy keeps its old origin row, so it is never refreshed again.
    _skill(source, "dev/alpha", body="upstream v3\n")
    again = sync_bundled(home, source=source)
    assert again.user_modified == ["alpha"]
    assert _md(home, "dev/alpha").read_text() == "my own version\n"
    assert list_modified(home) == ["alpha"]


def test_refresh_removes_files_dropped_upstream(source: Path, home: Path) -> None:
    sync_bundled(home, source=source)
    (source / "dev/alpha/references/notes.md").unlink()
    sync_bundled(home, source=source)
    assert not (home / "skills/dev/alpha/references/notes.md").exists()
    assert not list((home / "skills/dev").glob(".alpha*"))


def test_deleted_skill_stays_deleted(source: Path, home: Path) -> None:
    sync_bundled(home, source=source)
    bs._remove_tree(home / "skills/dev/beta", home / "skills")
    _skill(source, "dev/beta", body="upstream v2\n")
    report = sync_bundled(home, source=source)
    assert report.left_deleted == ["beta"]
    assert not (home / "skills/dev/beta").exists()
    assert "beta" in read_manifest(home)


def test_dropped_upstream_row_lifecycle(source: Path, home: Path) -> None:
    sync_bundled(home, source=source)
    bs._remove_tree(source / "gamma", source)
    report = sync_bundled(home, source=source)
    assert report.cleaned == [] and "gamma" in read_manifest(home)  # live copy keeps the row
    archive = home / "skills/.archive/gamma"
    archive.parent.mkdir(parents=True)
    os.rename(home / "skills/gamma", archive)
    report = sync_bundled(home, source=source)
    assert report.cleaned == [] and "gamma" in read_manifest(home)  # archived copy keeps it
    bs._remove_tree(archive, home / "skills")
    report = sync_bundled(home, source=source)
    assert report.cleaned == ["gamma"]
    assert "gamma" not in read_manifest(home)


def test_runtime_caches_do_not_mark_modified(source: Path, home: Path) -> None:
    (source / "dev/alpha/scripts").mkdir()
    (source / "dev/alpha/scripts/tool.py").write_text("print('hi')\n", encoding="utf-8")
    (source / "dev/alpha/__pycache__").mkdir()
    (source / "dev/alpha/__pycache__/tool.cpython-313.pyc").write_bytes(b"\x00shipped")
    sync_bundled(home, source=source)
    copy = home / "skills/dev/alpha"
    assert not (copy / "__pycache__").exists()  # caches are never copied
    for cache in ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"):
        (copy / "scripts" / cache).mkdir()
        (copy / "scripts" / cache / "junk").write_bytes(b"\x01")
    (copy / "scripts/tool.pyc").write_bytes(b"\x02")
    assert tree_hash(copy) == read_manifest(home)["alpha"]
    _skill(source, "dev/alpha", body="upstream v2\n")
    report = sync_bundled(home, source=source)
    assert report.updated == ["alpha"]
    assert list_modified(home) == []


def test_helper_script_run_leaves_skill_pristine(source: Path, home: Path) -> None:
    helper = source / "dev/beta/scripts/helper.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("import json\nVALUE = 1\n", encoding="utf-8")
    sync_bundled(home, source=source)
    copy = home / "skills/dev/beta/scripts"
    cache = copy / "__pycache__"
    cache.mkdir()
    (cache / "helper.cpython-313.pyc").write_bytes(b"compiled")
    assert list_modified(home) == []


def test_existing_local_skill_kept(source: Path, home: Path) -> None:
    _skill(home / "skills", "dev/alpha", body="hand written\n")
    report = sync_bundled(home, source=source)
    assert report.kept_local == ["alpha"]
    assert "alpha" not in read_manifest(home)
    assert "hand written" in _md(home, "dev/alpha").read_text()


def test_existing_identical_skill_adopted(source: Path, home: Path) -> None:
    _skill(home / "skills", "dev/alpha", extra={"references/notes.md": "alpha notes\n"})
    report = sync_bundled(home, source=source)
    assert "alpha" in report.unchanged
    assert read_manifest(home)["alpha"] == tree_hash(source / "dev/alpha")


def test_interrupted_swap_recovered(source: Path, home: Path) -> None:
    sync_bundled(home, source=source)
    dest = home / "skills/dev/beta"
    retired = home / "skills/dev/.beta.sync-old"
    staged = home / "skills/dev/.beta.sync-new"
    os.rename(dest, retired)  # crash after moving the old copy aside
    staged.mkdir()
    report = sync_bundled(home, source=source)
    assert "beta" in report.unchanged and report.left_deleted == []
    assert _md(home, "dev/beta").is_file()
    assert not retired.exists() and not staged.exists()


def test_copy_failure_is_not_recorded(
    source: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = bs._copy_package

    def flaky(src: Path, target: Path) -> None:
        if src.name == "beta":
            raise OSError("disk full")
        real(src, target)

    monkeypatch.setattr(bs, "_copy_package", flaky)
    report = sync_bundled(home, source=source)
    assert report.failed and report.failed[0].startswith("beta:")
    assert "beta" not in read_manifest(home)
    assert not (home / "skills/dev/beta").exists()
    assert not list((home / "skills/dev").glob(".beta*"))
    monkeypatch.setattr(bs, "_copy_package", real)
    assert sync_bundled(home, source=source).added == ["beta"]


def test_missing_source_touches_nothing(source: Path, home: Path, tmp_path: Path) -> None:
    sync_bundled(home, source=source)
    before = manifest_path(home).read_bytes()
    report = sync_bundled(home, source=tmp_path / "nowhere")
    assert report.failed and report.total == 0
    assert manifest_path(home).read_bytes() == before


def test_malformed_manifest_lines_ignored(source: Path, home: Path) -> None:
    (home / "skills").mkdir()
    manifest_path(home).write_text("alpha\n\n:abc\nbeta:\n", encoding="utf-8")
    assert read_manifest(home) == {}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_read_only_source_copy_is_removable(source: Path, home: Path) -> None:
    for current, _dirs, files in os.walk(source):
        for name in files:
            os.chmod(os.path.join(current, name), stat.S_IRUSR)
    try:
        sync_bundled(home, source=source)
        copy = home / "skills/dev/alpha/SKILL.md"
        assert os.stat(copy).st_mode & stat.S_IWUSR
        assert reset(home, "alpha", restore=True, source=source).ok
    finally:
        for current, _dirs, files in os.walk(source):
            for name in files:
                os.chmod(os.path.join(current, name), stat.S_IRUSR | stat.S_IWUSR)


def test_remove_tree_refuses_outside_skills(home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "precious"
    outside.mkdir()
    (home / "skills").mkdir()
    with pytest.raises(ValueError):
        bs._remove_tree(outside, home / "skills")
    with pytest.raises(ValueError):
        bs._remove_tree(home / "skills", home / "skills")
    assert outside.exists()


# -- opt-out -------------------------------------------------------------------------------------


def test_opt_out_marker_stops_seeding(source: Path, home: Path) -> None:
    assert set_opt_out(home, True) is True
    assert set_opt_out(home, True) is False
    assert is_opted_out(home) and (home / OPT_OUT_MARKER).is_file()
    report = sync_bundled(home, source=source)
    assert report.opted_out and report.added == []
    assert not (home / "skills").exists()


def test_opt_out_keeps_rows_and_copies(source: Path, home: Path) -> None:
    sync_bundled(home, source=source)
    rows = read_manifest(home)
    set_opt_out(home, True)
    bs._remove_tree(source / "gamma", source)
    bs._remove_tree(home / "skills/gamma", home / "skills")
    sync_bundled(home, source=source)
    assert read_manifest(home) == rows  # no cleaning while opted out


def test_remove_pristine_keeps_edited(source: Path, home: Path) -> None:
    sync_bundled(home, source=source)
    _md(home, "dev/alpha").write_text("mine\n", encoding="utf-8")
    dry = remove_pristine(home, dry_run=True)
    assert sorted(dry.removed) == ["beta", "gamma"]
    assert (home / "skills/gamma").exists()
    report = remove_pristine(home)
    assert sorted(report.removed) == ["beta", "gamma"]
    assert report.kept == [("alpha", "edited since it was copied")]
    assert not (home / "skills/gamma").exists() and not (home / "skills/dev/beta").exists()
    assert _md(home, "dev/alpha").read_text() == "mine\n"
    assert set(read_manifest(home)) == {"alpha"}


def test_opt_in_with_seed_reseeds(source: Path, home: Path) -> None:
    sync_bundled(home, source=source)
    set_opt_out(home, True)
    remove_pristine(home)
    assert opt_in(home) is None and not is_opted_out(home)
    set_opt_out(home, True)
    report = opt_in(home, seed=True, source=source)
    assert report is not None and sorted(report.added) == ["alpha", "beta", "gamma"]


# -- reset ---------------------------------------------------------------------------------------


def test_reset_clears_row_and_next_sync_rebaselines(source: Path, home: Path) -> None:
    sync_bundled(home, source=source)
    result = reset(home, "beta", source=source)
    assert result.ok and result.action == "untracked"
    assert "beta" not in read_manifest(home)
    assert _md(home, "dev/beta").is_file()  # the copy stays
    report = sync_bundled(home, source=source)
    assert "beta" in report.unchanged and "beta" in read_manifest(home)


def test_reset_edited_copy_becomes_users_own(source: Path, home: Path) -> None:
    sync_bundled(home, source=source)
    _md(home, "dev/beta").write_text("mine\n", encoding="utf-8")
    reset(home, "beta", source=source)
    report = sync_bundled(home, source=source)
    assert report.kept_local == ["beta"]
    assert _md(home, "dev/beta").read_text() == "mine\n"


def test_reset_restore_recopies(source: Path, home: Path) -> None:
    sync_bundled(home, source=source)
    _md(home, "dev/alpha").write_text("mine\n", encoding="utf-8")
    (home / "skills/dev/alpha/references/extra.md").write_text("x", encoding="utf-8")
    result = reset(home, "alpha", restore=True, source=source)
    assert result.ok and result.action == "restored"
    assert tree_hash(home / "skills/dev/alpha") == tree_hash(source / "dev/alpha")
    assert read_manifest(home)["alpha"] == tree_hash(source / "dev/alpha")
    assert list_modified(home) == []


def test_reset_restore_after_user_delete(source: Path, home: Path) -> None:
    sync_bundled(home, source=source)
    bs._remove_tree(home / "skills/gamma", home / "skills")
    set_opt_out(home, True)
    result = reset(home, "gamma", restore=True, source=source)
    assert result.ok and _md(home, "gamma").is_file()


def test_reset_refusals(source: Path, home: Path) -> None:
    sync_bundled(home, source=source)
    assert reset(home, "nope", source=source).action == "not_tracked"
    bs._remove_tree(source / "gamma", source)
    refused = reset(home, "gamma", restore=True, source=source)
    assert not refused.ok and refused.action == "not_shipped"
    assert "gamma" in read_manifest(home) and _md(home, "gamma").is_file()


def test_reset_restore_delete_failure_changes_nothing(
    source: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync_bundled(home, source=source)
    _md(home, "dev/alpha").write_text("mine\n", encoding="utf-8")
    rows = read_manifest(home)

    def boom(path: Path, root: Path) -> None:
        raise OSError("busy")

    monkeypatch.setattr(bs, "_remove_tree", boom)
    result = reset(home, "alpha", restore=True, source=source)
    assert not result.ok and result.action == "failed"
    assert read_manifest(home) == rows
    assert _md(home, "dev/alpha").read_text() == "mine\n"


# -- source --------------------------------------------------------------------------------------


def test_bundled_source_default_and_override(tmp_path: Path) -> None:
    assert bundled_source() == REPO / "skills"
    assert bundled_source({"INDUS_BUNDLED_SKILLS": str(tmp_path)}) == tmp_path
    assert bundled_source({"INDUS_BUNDLED_SKILLS": "  "}) == REPO / "skills"


def test_duplicate_shipped_name_keeps_first(tmp_path: Path) -> None:
    root = tmp_path / "shipped"
    _skill(root, "a/same")
    _skill(root, "b/same")
    _skill(root, "c/.hidden/inner")
    _skill(root, "a/same/references/nested")  # inside a package: not a package of its own
    found = bs.discover(root)
    assert [(s.name, s.rel.as_posix()) for s in found] == [("same", "a/same")]


def test_wheel_ships_the_skills_where_bundled_source_looks() -> None:
    """pyproject force-includes ``skills/`` as ``indus/_bundled_skills`` (integration); other
    package data (P23-01's locales) may sit beside it."""
    with (REPO / "pyproject.toml").open("rb") as fh:
        wheel = tomllib.load(fh)["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["force-include"][bs.SKILLS_DIR] == "indus/_bundled_skills"
    module = Path(bs.__file__).resolve()
    packaged = module.parents[2] / "_bundled_skills"
    assert packaged.relative_to(module.parents[3]).as_posix() == "indus/_bundled_skills"


def test_seed_first_run_only_once_and_never_when_opted_out(source: Path, home: Path) -> None:
    env = {bs.SOURCE_ENV: str(source)}
    report = bs.seed_first_run(home, env=env)
    assert report is not None and sorted(report.added) == ["alpha", "beta", "gamma"]
    _md(home, "gamma").unlink()
    assert bs.seed_first_run(home, env=env) is None  # seeded before: left to `skills sync`
    assert not _md(home, "gamma").exists()
    other = home.parent / "opted-out"
    other.mkdir()
    bs.set_opt_out(other, True)
    assert bs.seed_first_run(other, env=env) is None
    assert not (other / "skills" / ".bundled_manifest").exists()
    missing = home.parent / "no-source"
    missing.mkdir()
    gone = bs.seed_first_run(missing, env={bs.SOURCE_ENV: str(home.parent / "nowhere")})
    assert gone is not None and gone.failed and not bs.manifest_path(missing).exists()
