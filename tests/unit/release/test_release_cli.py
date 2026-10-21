"""P23-22: ``scripts/release/release.py`` — checks, the claim, the draft, abandon (R10.29).

A real bare repository stands in for GitHub's git side and ``FakeGitHub`` for its API; nothing
reaches the network. Agents only run ``--dry-run`` for real; the live path is exercised here
against the fakes only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.release import release
from scripts.release.claims import WARNING_BEGIN, Claim
from tests.fakes.release.fake_github import FakeGitHub
from tests.unit.release._repo import Remote

NOW = datetime(2026, 10, 21, 6, 41, 0, tzinfo=UTC)


@pytest.fixture
def remote(tmp_path: Path) -> Remote:
    made = Remote(tmp_path)
    made.credit("maintainer", "The Maintainer")
    return made


def cut(
    remote: Remote,
    capsys: pytest.CaptureFixture[str],
    *argv: str,
    github: FakeGitHub | None = None,
    ask: bool | None = None,
) -> tuple[int, str, str]:
    capsys.readouterr()
    code = release.main(
        list(argv),
        root=remote.clone,
        github=github,
        env=remote.env,
        now=lambda: NOW,
        ask=(lambda _q: bool(ask)),
        interactive=ask is not None,
    )
    out = capsys.readouterr()
    return code, out.out, out.err


def test_dry_run_prints_the_plan_and_creates_nothing(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    remote.commit("feat: first feature")
    sha = remote.commit("fix: a bug")
    gh = FakeGitHub()
    code, out, err = cut(remote, capsys, "--commit", "HEAD", "--dry-run", github=gh)
    assert code == 0, out + err
    assert "dry run" in out and "attempt ref: rc.1-v0.0.1" in out and sha in out
    assert "## Features" in out and "## Fixes" in out and "The Maintainer" in out
    assert remote.remote_tags() == {} and remote.git("tag", "--list") == ""
    assert gh.calls == [] and gh.dispatches == []


def test_real_run_pushes_the_claim_and_drafts_once(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = remote.commit("feat: something")
    gh = FakeGitHub()
    code, out, err = cut(remote, capsys, "--commit", sha, "--lanes", "ci,docker", github=gh)
    assert code == 0, out + err
    assert "rc.1-v0.0.1" in remote.remote_tags()
    [draft] = gh.releases
    assert draft["tag_name"] == "rc.1-v0.0.1" and draft["draft"] is True
    assert draft["name"] == "v0.0.1" and draft["prerelease"] is False
    assert draft["body"].count(WARNING_BEGIN) == 2 and "Something" in draft["body"]
    assert gh.dispatches == [
        {"workflow": "release.yml", "ref": "main", "inputs": {"attempt_ref": "rc.1-v0.0.1"}}
    ]


def test_claim_message_fields(remote: Remote, capsys: pytest.CaptureFixture[str]) -> None:
    sha = remote.commit("feat: something")
    code, _out, err = cut(
        remote, capsys, "--commit", sha, "--autopublish", "--lanes", "ci,nix", github=FakeGitHub()
    )
    assert code == 0, err
    message = remote.message("rc.1-v0.0.1")
    assert message == {
        "schema": 1, "kind": "stable", "version": "0.0.1", "attempt": 1, "commit": sha,
        "autopublish": True, "lanes": ["ci", "nix"], "epoch": int(NOW.timestamp()),
    }  # fmt: skip
    tagger = remote.git("for-each-ref", "--format=%(taggerdate:unix)", "refs/tags/rc.1-v0.0.1")
    assert int(tagger) == message["epoch"]  # the epoch IS the tagger time
    assert Claim.from_json(message).lanes == frozenset({"ci", "nix"})


def test_open_attempt_refuses_without_a_terminal(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = remote.commit("feat: one")
    gh = FakeGitHub()
    assert cut(remote, capsys, "--commit", sha, github=gh)[0] == 0
    sha2 = remote.commit("feat: two")
    code, out, err = cut(remote, capsys, "--commit", sha2, github=gh)
    assert code == 1 and "rc.1-v0.0.1 is still open" in out and "abandon" in out
    assert "Not released" in err
    # on a terminal the default answer is no
    assert cut(remote, capsys, "--commit", sha2, github=gh, ask=False)[0] == 1
    assert len(gh.releases) == 1
    # yes: abandon the old attempt, then cut the next one of the same version
    code, out, err = cut(remote, capsys, "--commit", sha2, github=gh, ask=True)
    assert code == 0, out + err
    tags = remote.remote_tags()
    assert {"rc.1-v0.0.1", "abandoned-rc.1-v0.0.1", "rc.2-v0.0.1"} <= set(tags)
    assert [r["tag_name"] for r in gh.releases] == ["rc.2-v0.0.1"]  # the old draft is gone


def test_non_descendant_commit_is_refused(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    older = remote.commit("feat: one")
    released = remote.commit("feat: two")
    remote.tag("v0.1.0", released, "receipt")
    gh = FakeGitHub()
    gh.create_release(tag="v0.1.0", name="v0.1.0", body="", draft=False, prerelease=False,
                      target=released)  # fmt: skip
    code, out, _err = cut(remote, capsys, "--commit", older, github=gh)
    assert code == 1 and "does not descend from v0.1.0" in out
    newer = remote.commit("feat: three")
    code, out, err = cut(remote, capsys, "--commit", newer, "--bump", "minor", github=gh)
    assert code == 0, out + err
    assert "rc.1-v0.2.0" in remote.remote_tags()


def test_commit_not_on_main_is_refused(remote: Remote, capsys: pytest.CaptureFixture[str]) -> None:
    remote.commit("feat: on main")
    side = remote.commit("feat: local only", push=False)
    code, out, _err = cut(remote, capsys, "--commit", side, "--dry-run")
    assert code == 1 and "is not on origin/main" in out
    code, out, _err = cut(remote, capsys, "--commit", "no-such-ref", "--dry-run")
    assert code == 1 and "is not a commit" in out


def test_unmapped_author_fails_a_release_and_warns_a_canary(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = remote.commit("feat: something new", author="newcomer")
    code, out, _err = cut(remote, capsys, "--commit", sha, "--dry-run")
    assert code == 1 and "not in the contributor map" in out
    assert "newcomer" not in out and "example.invalid" not in out  # no address is printed
    code, out, _err = cut(remote, capsys, "--commit", sha, "--dry-run", "--canary")
    assert code == 0 and "  ! 1 commit author(s)" in out
    assert "rc.1-v0.0.1-canary.20261021T064100Z" in out
    remote.credit("newcomer", "A Newcomer")
    code, out, _err = cut(remote, capsys, "--commit", sha, "--dry-run")
    assert code == 0 and "A Newcomer" in out


def test_abandon_writes_the_marker_ref(remote: Remote, capsys: pytest.CaptureFixture[str]) -> None:
    sha = remote.commit("feat: one")
    gh = FakeGitHub()
    assert cut(remote, capsys, "--commit", sha, github=gh)[0] == 0
    code, out, _err = cut(remote, capsys, "abandon", "--version", "0.0.1", "--dry-run")
    assert code == 0 and "Would write abandoned-rc.1-v0.0.1" in out
    assert "abandoned-rc.1-v0.0.1" not in remote.remote_tags()
    code, out, err = cut(remote, capsys, "abandon", "--version", "0.0.1", github=gh)
    assert code == 0, out + err
    tags = remote.remote_tags()
    assert "abandoned-rc.1-v0.0.1" in tags and "rc.1-v0.0.1" in tags  # the attempt ref stays
    assert gh.releases == []
    assert cut(remote, capsys, "abandon", "--version", "0.0.1", github=gh)[0] == 1  # nothing open
    code, out, err = cut(remote, capsys, "--commit", sha, github=gh)
    assert code == 0, out + err
    assert "rc.2-v0.0.1" in remote.remote_tags()  # the abandoned number is spent


def test_if_new_skips_a_released_commit(remote: Remote, capsys: pytest.CaptureFixture[str]) -> None:
    sha = remote.commit("feat: one")
    remote.tag("v0.0.1", sha, "receipt")
    code, out, _err = cut(remote, capsys, "--commit", sha, "--canary", "--if-new", "--dry-run")
    assert code == 0 and "Nothing to release" in out and "already released" in out
    newer = remote.commit("feat: two")
    gh = FakeGitHub()
    assert cut(remote, capsys, "--commit", newer, "--canary", "--if-new", github=gh)[0] == 0
    # the next day, the canary of the same commit is still in flight: nothing to do, no error
    code, out, _err = cut(remote, capsys, "--commit", newer, "--canary", "--if-new", github=gh)
    assert code == 0 and "in flight" in out and len(gh.releases) == 1


def test_usage_errors(remote: Remote, capsys: pytest.CaptureFixture[str]) -> None:
    remote.commit("feat: one")
    assert cut(remote, capsys, "--commit", "HEAD", "--lanes", "ci,bogus", "--dry-run")[0] == 2
    assert cut(remote, capsys, "--dry-run")[0] == 2  # --commit is required
    assert cut(remote, capsys, "abandon", "--version", "1.2", "--dry-run")[0] == 2
