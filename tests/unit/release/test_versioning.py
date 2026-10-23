"""P23-21: versions, next versions, attempt refs and the channel record (R10.26, R10.27)."""

from __future__ import annotations

import random
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from indus.foundation import versions as grammar
from indus.foundation.versions import (
    Version,
    attempt_ref,
    marker_ref,
    parse_attempt_ref,
    parse_marker_ref,
    parse_tag,
    parse_version,
    tag_record_text,
)
from scripts.release import versioning
from scripts.release.versioning import (
    ChannelEntry,
    RecordConflict,
    newest_published_stable,
    next_attempt,
    next_canary,
    next_stable,
    open_attempts,
    read_channel_record,
    update_channel_record,
)
from tests.fakes.release.fake_github import FakeGitHub

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 21, 6, 41, 0, tzinfo=UTC)


def v(text: str) -> Version:
    found = parse_version(text)
    assert found is not None, text
    return found


def release(tag: str, *, draft: bool = False, prerelease: bool = False) -> dict[str, object]:
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease}


# -- grammar ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "v1.2",
        "1.2.3",
        "v1.2.3-rc.1",
        "v1.2.3+canary.20261021T064100Z",
        "v01.2.3",
        "v1.02.3",
        "v1000.0.0",
        "v2026.10.21",
        "v1.2.3-canary.20261341T000000Z",
        "v1.2.3-canary.20261021T246100Z",
        "v1.2.3-canary.2026-10-21",
        "v1.2.3-nightly.20261021T064100Z",
        "rc.1-v1.2.3",
        " v1.2.3",
        "v1.2.3\n",
    ],
)
def test_malformed_tags_are_rejected(text: str) -> None:
    assert parse_tag(text) is None


def test_well_formed_tags_round_trip() -> None:
    for text in ("v0.0.1", "v1.2.3", "v999.0.12", "v1.2.4-canary.20261021T064100Z"):
        found = parse_tag(text)
        assert found is not None and found.tag == text
    assert v("1.2.4-canary.20261021T064100Z").channel == "canary"
    assert v("1.2.4").channel == "stable"
    assert parse_tag(None) is None and parse_version(3) is None


def test_channel_words_are_exactly_three() -> None:
    assert grammar.CHANNELS == ("stable", "canary", "main")
    assert grammar.RELEASE_CHANNELS == ("stable", "canary")


def test_attempt_and_marker_refs_round_trip() -> None:
    ref = attempt_ref(v("1.2.3"), 2)
    assert ref == "rc.2-v1.2.3"
    parsed = parse_attempt_ref(ref)
    assert parsed is not None and parsed.version == v("1.2.3") and parsed.attempt == 2
    assert marker_ref(v("1.2.3"), 2) == "abandoned-rc.2-v1.2.3"
    assert parse_marker_ref("abandoned-rc.2-v1.2.3") == parsed
    canary = attempt_ref(v("1.2.4-canary.20261021T064100Z"), 1)
    assert parse_attempt_ref(canary) is not None
    for bad in ("rc.0-v1.2.3", "rc.1-1.2.3", "v1.2.3-rc.1", "rc.1-v1.2", "abandoned-v1.2.3"):
        assert parse_attempt_ref(bad) is None and parse_marker_ref(bad) is None
    with pytest.raises(ValueError):
        attempt_ref(v("1.2.3"), 0)


def test_tag_record_strips_a_signature() -> None:
    message = '{"schema": 1}\n-----BEGIN PGP SIGNATURE-----\nabc\n-----END PGP SIGNATURE-----\n'
    assert tag_record_text(message) == '{"schema": 1}'
    assert tag_record_text('{"schema": 1}\n') == '{"schema": 1}'


# -- next versions ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("published", "kind", "expected"),
    [
        (None, "patch", "0.0.1"),
        (None, "minor", "0.1.0"),
        (None, "major", "1.0.0"),
        ("0.4.2", "patch", "0.4.3"),
        ("0.4.2", "minor", "0.5.0"),
        ("0.9.9", "major", "1.0.0"),
        ("1.0.0", "patch", "1.0.1"),
    ],
)
def test_next_versions_table(published: str | None, kind: str, expected: str) -> None:
    assert str(next_stable(None if published is None else v(published), kind)) == expected


def test_next_stable_reads_only_published_stable_releases() -> None:
    rows = [
        release("v0.3.0"),
        release("v0.5.0", draft=True),
        release("v0.4.0", prerelease=True),
        release("v0.4.1-canary.20261020T000000Z", prerelease=True),
        release("rc.1-v0.6.0", draft=True),
        release("channels"),
        release("v0.3.1"),
        {"tag_name": "v0.9.0"},  # no draft/prerelease flags: not provably published
    ]
    newest = newest_published_stable(rows)
    assert newest == v("0.3.1")
    assert str(next_stable(newest, "minor")) == "0.4.0"
    with pytest.raises(ValueError):
        next_stable(newest, "huge")


def test_next_canary_is_the_next_patch_with_a_utc_stamp() -> None:
    canary = next_canary(v("1.2.3"), NOW)
    assert str(canary) == "1.2.4-canary.20261021T064100Z"
    local = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert next_canary(v("1.2.3"), local) == canary
    assert str(next_canary(None, NOW)) == "0.0.1-canary.20261021T064100Z"


def test_canary_ordering_property() -> None:
    """Over many random stamps and stables: last stable < canary < its own stable, and canaries
    order by time (R10.26). Seeded, so a failure reproduces."""
    rng = random.Random(2310)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    for _ in range(500):
        stable = Version(rng.randrange(0, 3), rng.randrange(0, 40), rng.randrange(0, 40))
        a = start + timedelta(seconds=rng.randrange(0, 10**9))
        b = start + timedelta(seconds=rng.randrange(0, 10**9))
        ca, cb = next_canary(stable, a), next_canary(stable, b)
        assert stable < ca < ca.stable() and stable < cb < cb.stable()
        assert (ca < cb) == (a.replace(microsecond=0) < b.replace(microsecond=0))
        assert ca.stable() == next_stable(stable, "patch")
        assert next_stable(stable, "minor") > ca and next_stable(stable, "major") > ca
        assert sorted([ca.stable(), cb, ca, stable], key=lambda x: x.key)[0] == stable


# -- attempts --------------------------------------------------------------------------------------


def test_next_attempt_counts_markers_as_spent() -> None:
    refs = ["rc.1-v0.2.0", "abandoned-rc.1-v0.2.0", "abandoned-rc.2-v0.2.0", "rc.4-v0.1.0"]
    assert next_attempt(v("0.2.0"), refs) == 3
    assert next_attempt(v("0.3.0"), refs) == 1


def test_open_attempts_ignore_cleared_and_published() -> None:
    refs = [
        "rc.1-v0.1.0",
        "v0.1.0",  # published
        "rc.1-v0.2.0",
        "abandoned-rc.1-v0.2.0",  # abandoned
        "rc.2-v0.2.0",  # open
        "rc.1-v0.3.0",  # open, newer
        "v0.2.0-not-a-tag",
    ]
    assert [a.name for a in open_attempts(refs)] == ["rc.2-v0.2.0", "rc.1-v0.3.0"]


# -- the channel record ----------------------------------------------------------------------------


def entry(version: str, commit: str = "a" * 40) -> ChannelEntry:
    return ChannelEntry(version, f"v{version}", commit, {"product": "sha256:" + "b" * 64})


def test_first_write_and_idempotent_rewrite() -> None:
    gh = FakeGitHub()
    record, digest = read_channel_record(gh)
    assert record.stable is None and digest is None
    updated = update_channel_record(gh, "stable", entry("0.1.0"), expected_digest=digest)
    assert updated.stable is not None and updated.stable.version == "0.1.0"
    again, digest2 = read_channel_record(gh)
    assert again == updated
    writes = gh.asset_writes
    update_channel_record(gh, "stable", entry("0.1.0"), expected_digest=digest2)
    assert gh.asset_writes == writes  # same entry: nothing written


def test_record_update_refused_when_the_previous_digest_changed() -> None:
    gh = FakeGitHub()
    _record, digest = read_channel_record(gh)
    update_channel_record(
        gh, "canary", entry("0.1.1-canary.20261021T064100Z"), expected_digest=digest
    )
    with pytest.raises(RecordConflict, match="changed since it was read"):
        update_channel_record(gh, "stable", entry("0.1.0"), expected_digest=digest)
    record, _ = read_channel_record(gh)
    assert record.stable is None and record.canary is not None


def test_record_never_moves_backwards_or_across_channels() -> None:
    gh = FakeGitHub()
    _r, d = read_channel_record(gh)
    update_channel_record(gh, "stable", entry("0.2.0"), expected_digest=d)
    _r, d = read_channel_record(gh)
    with pytest.raises(RecordConflict, match="backwards"):
        update_channel_record(gh, "stable", entry("0.1.9"), expected_digest=d)
    with pytest.raises(ValueError, match="not a canary version"):
        update_channel_record(gh, "canary", entry("0.2.1"), expected_digest=d)
    with pytest.raises(ValueError):
        versioning.ChannelRecord.from_bytes(b'{"schema": 2}')


# -- one grammar -----------------------------------------------------------------------------------

_GRAMMAR_FRAGMENTS = (r"\{8\}T", r"rc\\\.", r"-canary\\\.")
_CALLERS = (
    "src/indus/cli/update/channels.py",
    "scripts/release/versioning.py",
    "scripts/release/release.py",
    "scripts/release/publish.py",
    "scripts/termux/repo.py",
)


def _python_files() -> list[Path]:
    found: list[Path] = []
    for top in ("src", "scripts", "tooling"):
        found.extend(p for p in (ROOT / top).rglob("*.py") if "__pycache__" not in p.parts)
    return found


def test_grammar_is_defined_once_and_imported_by_every_caller() -> None:
    home = ROOT / "src/indus/foundation/versions.py"
    for path in _python_files():
        if path == home:
            continue
        text = path.read_text(encoding="utf-8")
        for fragment in _GRAMMAR_FRAGMENTS:
            assert not re.search(fragment, text), f"{path} defines version grammar ({fragment})"
    for rel in _CALLERS:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "indus.foundation.versions" in text or "scripts.release.versioning" in text, rel


def test_one_word_per_channel_in_release_and_update_files() -> None:
    files = [
        *(ROOT / "src/indus/cli/update").rglob("*.py"),
        ROOT / "src/indus/cli/commands/update.py",
        ROOT / "src/indus/foundation/versions.py",
        *(ROOT / "scripts/release").rglob("*.py"),
        ROOT / ".github/workflows/release.yml",
        ROOT / ".github/workflows/canary.yml",
    ]
    for path in files:
        assert "nightly" not in path.read_text(encoding="utf-8").lower(), path
