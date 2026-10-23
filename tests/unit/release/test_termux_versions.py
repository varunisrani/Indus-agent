"""P24-21: release tag → Debian version for the Termux APT channel, and dpkg ordering."""

from __future__ import annotations

import random
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from indus.foundation.versions import Version
from scripts.release.versioning import next_canary, next_stable
from scripts.termux import repo
from scripts.termux.repo import RepoError, channel_of, compare_versions, deb_version, suite_for

ROOT = Path(__file__).resolve().parents[3]
STAMP = "20261021T064100Z"


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v0.0.1", "0.0.1-1"),
        ("v1.2.3", "1.2.3-1"),
        ("v10.20.30", "10.20.30-1"),
        ("v999.0.12", "999.0.12-1"),
        (f"v1.2.4-canary.{STAMP}", f"1.2.4~canary.{STAMP}-1"),
        ("v0.1.0-canary.20240101T000000Z", "0.1.0~canary.20240101T000000Z-1"),
    ],
)
def test_version_table(tag: str, expected: str) -> None:
    assert deb_version(tag) == expected
    assert repo.main(["version", tag]) == repo.EXIT_OK


@pytest.mark.parametrize(
    "tag",
    [
        "1.2.3",  # no v
        "v1.2",
        "v1.2.3.4",
        "v01.2.3",
        "v1.02.3",
        "v1000.0.0",  # a calendar label, never ours
        f"v1.2.3+canary.{STAMP}",  # the reference's spelling; ours is -canary (W49-c-1)
        f"v1.2.3-nightly.{STAMP}",
        "v1.2.3-canary.20261341T000000Z",  # month 13
        "v1.2.3-canary.20261021T246100Z",  # hour 24
        "v1.2.3-canary.2026-10-21",
        "v1.2.3-rc.1",
        "rc.1-v1.2.3",  # an attempt ref, not a tag
        " v1.2.3",
        "v1.2.3\n",
        "",
    ],
)
def test_malformed_tags_are_refused(tag: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(RepoError, match="malformed release tag"):
        deb_version(tag)
    with pytest.raises(RepoError):
        channel_of(tag)
    assert repo.main(["version", tag]) == repo.EXIT_REFUSED
    out, err = capsys.readouterr()
    assert out == "" and "malformed release tag" in err


def test_channel_follows_the_tag(capsys: pytest.CaptureFixture[str]) -> None:
    assert channel_of("v1.2.3") == "stable" and suite_for("stable") == "indus-stable"
    assert channel_of(f"v1.2.4-canary.{STAMP}") == "canary"
    assert suite_for("canary") == "indus-canary"
    with pytest.raises(RepoError):
        suite_for("main")  # a source channel has no published packages
    assert repo.main(["channel", f"v1.2.4-canary.{STAMP}"]) == 0
    assert capsys.readouterr().out == "canary\n"


@pytest.mark.parametrize(
    ("left", "right", "order"),
    [
        ("1.2.3-1", "1.2.3-1", 0),
        ("1.2.3-1", "1.2.4-1", -1),
        ("1.2.10-1", "1.2.9-1", 1),  # numbers compare as numbers
        ("1.2.3~canary.1-1", "1.2.3-1", -1),  # ~ sorts before the end of the version
        ("1.2.3~a-1", "1.2.3~b-1", -1),
        ("1.2.3~~-1", "1.2.3~-1", -1),
        ("1.2.3a-1", "1.2.3-1", 1),  # a letter sorts after the end
        ("1.2.3+x-1", "1.2.3a-1", 1),  # non-letters sort after letters
        ("1.0-1", "1.0-2", -1),  # the revision breaks ties
        ("1:0.1-1", "9.9-1", 1),  # the epoch wins
        ("1.0", "1.0-0", 0),
        ("1.01-1", "1.1-1", 0),  # leading zeros do not count
    ],
)
def test_dpkg_ordering(left: str, right: str, order: int) -> None:
    assert compare_versions(left, right) == order
    assert compare_versions(right, left) == -order


def test_canary_sorts_between_stables() -> None:
    """A canary is the next patch's prerelease: above the last stable, below its own stable, and
    canaries order by time — in dpkg order exactly as in our own grammar (R10.26)."""
    rng = random.Random(2124)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    for _ in range(300):
        stable = Version(rng.randrange(0, 3), rng.randrange(0, 40), rng.randrange(0, 40))
        a = next_canary(stable, start + timedelta(seconds=rng.randrange(0, 10**9)))
        b = next_canary(stable, start + timedelta(seconds=rng.randrange(0, 10**9)))
        last, own = deb_version(stable.tag), deb_version(next_stable(stable, "patch").tag)
        da, db = deb_version(a.tag), deb_version(b.tag)
        assert compare_versions(last, da) < 0 < compare_versions(own, da)
        expected = (a > b) - (a < b)
        assert compare_versions(da, db) == expected


@pytest.mark.skipif(shutil.which("dpkg") is None, reason="dpkg is not installed")
def test_our_ordering_agrees_with_dpkg() -> None:
    versions = [
        "0.9.9-1",
        "1.0.0-1",
        f"1.0.1~canary.{STAMP}-1",
        "1.0.1~canary.20261022T000000Z-1",
        "1.0.1-1",
        "1.0.1a-1",
        "1:0.0.1-1",
        "1.0.10-1",
    ]
    for left in versions:
        for right in versions:
            dpkg = subprocess.run(
                ["dpkg", "--compare-versions", left, "lt", right], check=False, timeout=30
            )
            assert (dpkg.returncode == 0) == (compare_versions(left, right) < 0), (left, right)


def test_the_mapping_imports_the_one_grammar() -> None:
    """R10.27: the tag grammar lives in indus.foundation.versions; repo.py imports it."""
    text = (ROOT / "scripts/termux/repo.py").read_text(encoding="utf-8")
    assert "from indus.foundation.versions import" in text
    assert "{8}T" not in text and "nightly" not in text.lower()
