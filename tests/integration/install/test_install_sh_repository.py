"""install.sh repository stage (P23-04, R10.14): real git over a local origin, fake git/curl
wrappers for failures, injected sleeps."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tests.fakes.install.fake_tools import (
    INSTALL_SH,
    OFFICIAL_URL,
    Sandbox,
    make_origin,
    push_commit,
)
from tests.integration.install.stage_protocol import parse_stage_frame

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def box(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def _dir(box: Sandbox) -> Path:
    return box.indus_home / "indus-agent"


def _stage(box: Sandbox, *extra: str) -> tuple[int, dict[str, Any], str]:
    result = box.run(
        "--stage",
        "repository",
        "--json",
        "--non-interactive",
        "--indus-home",
        str(box.indus_home),
        *extra,
    )
    frame = parse_stage_frame(result.stdout)
    assert frame is not None, result.stdout + result.stderr
    return result.returncode, frame, result.stdout + result.stderr


def _head(box: Sandbox, path: Path) -> str:
    return box.git("rev-parse", "HEAD", cwd=path)


def _tag_commit(box: Sandbox, bare: Path, tag: str) -> str:
    return box.git("rev-parse", f"{tag}^{{commit}}", cwd=bare)


def _no_staging_left(box: Sandbox) -> bool:
    return not list(box.indus_home.glob(".indus-clone-*"))


# -- fresh installs -------------------------------------------------------------------------------


def test_fresh_install_takes_the_newest_stable_release(box: Sandbox) -> None:
    bare = make_origin(box, tags=("v0.9.0", "v0.10.0", "v0.10.1-canary.20261020T000000Z"))
    code, frame, out = _stage(box)
    assert code == 0 and frame["ok"], out
    assert _head(box, _dir(box)) == _tag_commit(box, bare, "v0.10.0")
    assert box.git("config", "--get", "remote.origin.url", cwd=_dir(box)) == OFFICIAL_URL
    assert _no_staging_left(box)


def test_tag_lookup_falls_back_in_order(box: Sandbox) -> None:
    bare = make_origin(box, tags=("v0.1.0", "v0.2.0"))
    # 1. the API answers: nothing else is asked.
    box.rule(
        tool="curl", match=["api.github.com"], stdout='{\n  "tag_name": "v0.1.0",\n  "x": 1\n}\n'
    )
    code, _, out = _stage(box)
    assert code == 0, out
    assert _head(box, _dir(box)) == _tag_commit(box, bare, "v0.1.0")
    curls = box.calls("curl")
    assert len(curls) == 1 and any("api.github.com" in a for a in curls[0])
    assert not [c for c in box.calls("git") if "ls-remote" in c]


def test_tag_lookup_uses_the_redirect_when_the_api_fails(box: Sandbox) -> None:
    bare = make_origin(box, tags=("v0.1.0", "v0.2.0"))
    box.rule(tool="curl", match=["api.github.com"], exit=22)
    box.rule(
        tool="curl",
        match=["github.com/indusagi/indus-agent/releases/latest"],
        stdout="HTTP/2 302\r\nlocation: https://github.com/indusagi/indus-agent/releases/tag/v0.1.0\r\n\r\n",
    )
    code, _, out = _stage(box)
    assert code == 0, out
    assert _head(box, _dir(box)) == _tag_commit(box, bare, "v0.1.0")
    assert [("api" in " ".join(c)) for c in box.calls("curl")] == [True, False]
    assert not [c for c in box.calls("git") if "ls-remote" in c]


def test_tag_lookup_ends_with_the_remote_tags(box: Sandbox) -> None:
    bare = make_origin(box, tags=("v0.9.0", "v0.10.0", "v1000.0.0", "v0.11.0-rc1"))
    code, _, out = _stage(box)  # both curl lookups fail (no rule: exit 7)
    assert code == 0, out
    assert _head(box, _dir(box)) == _tag_commit(box, bare, "v0.10.0")
    assert len(box.calls("curl")) == 2
    assert [c for c in box.calls("git") if "ls-remote" in c]


def test_no_release_fails_with_the_main_hint(box: Sandbox) -> None:
    make_origin(box, tags=())
    code, frame, _ = _stage(box)
    assert code == 1
    assert "--channel main" in str(frame["reason"])
    assert not _dir(box).exists()


def test_channel_main_installs_the_branch_tip(box: Sandbox) -> None:
    bare = make_origin(box, tags=("v0.1.0",))
    code, _, out = _stage(box, "--channel", "main")
    assert code == 0, out
    assert _head(box, _dir(box)) == box.git("rev-parse", "main", cwd=bare)
    assert box.calls("curl") == []


def test_canary_takes_the_newest_by_semver(box: Sandbox) -> None:
    bare = make_origin(
        box,
        tags=("v0.2.0", "v0.2.1-canary.20261020T000000Z", "v0.2.1-canary.20261021T000000Z"),
    )
    code, _, out = _stage(box, "--channel", "canary")
    assert code == 0, out
    assert _head(box, _dir(box)) == _tag_commit(box, bare, "v0.2.1-canary.20261021T000000Z")


def test_canary_offers_a_newer_stable(box: Sandbox) -> None:
    bare = make_origin(box, tags=("v0.2.1-canary.20261021T000000Z", "v0.2.1"))
    code, _, out = _stage(box, "--channel", "canary")
    assert code == 0, out
    assert _head(box, _dir(box)) == _tag_commit(box, bare, "v0.2.1")


def test_clone_failure_midway_leaves_no_target(box: Sandbox) -> None:
    make_origin(box)
    box.rule(tool="git", match=["clone"], exit=128, partial=True, stderr="fatal: early EOF\n")
    code, frame, _ = _stage(box, "--channel", "main")
    assert code == 1
    assert frame["reason"] == "git clone failed; nothing was installed: fatal: early EOF"
    assert not _dir(box).exists()
    assert _no_staging_left(box)
    clones = [c for c in box.calls("git") if "clone" in c]
    assert len(clones) == 4 and "--no-checkout" in clones[3]
    assert all("--filter=blob:none" in c for c in clones)
    assert box.sleeps() == ["5", "10"]


def test_clone_retries_then_publishes(box: Sandbox) -> None:
    bare = make_origin(box)
    box.rule(tool="git", match=["clone"], times=2, exit=128, partial=True, stderr="fatal: reset\n")
    code, _, out = _stage(box, "--channel", "main")
    assert code == 0, out
    assert _head(box, _dir(box)) == box.git("rev-parse", "main", cwd=bare)
    assert not (_dir(box) / "half-written").exists()
    assert box.sleeps() == ["5", "10"]


def test_deferred_checkout_after_three_failed_clones(box: Sandbox) -> None:
    make_origin(box)
    box.rule(tool="git", match=["clone"], times=3, exit=128, stderr="fatal: throttled\n")
    code, _, out = _stage(box, "--channel", "main")
    assert code == 0, out
    assert (_dir(box) / "README.md").read_text() == "tip of main\n"


def test_pin_must_be_on_the_branch(box: Sandbox) -> None:
    bare = make_origin(box)
    work = box.root / "origin-work"
    box.git("switch", "-q", "-c", "side", cwd=work)
    side = push_commit(box, bare, "side work")
    box.git("push", "-q", str(bare), "side", cwd=work)
    box.git("switch", "-q", "main", cwd=work)
    code, frame, _ = _stage(box, "--commit", side)
    assert code == 1
    assert "is not on branch main" in str(frame["reason"])
    assert not _dir(box).exists() and _no_staging_left(box)


def test_pin_on_the_branch_is_checked_out(box: Sandbox) -> None:
    bare = make_origin(box, tags=("v0.1.0",))
    pin = _tag_commit(box, bare, "v0.1.0")
    code, _, out = _stage(box, "--commit", pin)
    assert code == 0, out
    assert _head(box, _dir(box)) == pin


# -- what is already there ------------------------------------------------------------------------


def test_broken_checkout_is_moved_aside(box: Sandbox) -> None:
    make_origin(box)
    _dir(box).mkdir(parents=True)
    box.git("init", "-q", str(_dir(box)))
    (_dir(box) / "keep.txt").write_text("mine\n")
    code, _, out = _stage(box, "--channel", "main")
    assert code == 0, out
    (aside,) = box.indus_home.glob("indus-agent.broken-*")
    assert (aside / "keep.txt").read_text() == "mine\n"
    assert (_dir(box) / "README.md").exists()


def test_non_checkout_dir_is_refused(box: Sandbox) -> None:
    make_origin(box)
    _dir(box).mkdir(parents=True)
    (_dir(box) / "notes.txt").write_text("not ours\n")
    code, frame, _ = _stage(box, "--channel", "main")
    assert code == 1
    assert "is not an Indus Agent checkout" in str(frame["reason"])
    assert (_dir(box) / "notes.txt").read_text() == "not ours\n"
    assert box.calls("git") == [] or not [c for c in box.calls("git") if "clone" in c]


def test_symlink_target_is_refused(box: Sandbox) -> None:
    make_origin(box)
    elsewhere = box.root / "elsewhere"
    elsewhere.mkdir()
    box.indus_home.mkdir(parents=True)
    _dir(box).symlink_to(elsewhere)
    code, frame, _ = _stage(box, "--channel", "main")
    assert code == 1 and "is not an Indus Agent checkout" in str(frame["reason"])
    assert _dir(box).is_symlink() and list(elsewhere.iterdir()) == []


def test_empty_dir_is_taken_over(box: Sandbox) -> None:
    make_origin(box)
    _dir(box).mkdir(parents=True)
    code, _, out = _stage(box, "--channel", "main")
    assert code == 0, out
    assert (_dir(box) / ".git").exists()


def _clone_existing(box: Sandbox, ref: str | None = None) -> Path:
    target = _dir(box)
    box.git("clone", "-q", OFFICIAL_URL, str(target))
    if ref:
        box.git("checkout", "-q", "--detach", ref, cwd=target)
    return target


def _install_id(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]


def _save_channel(box: Sandbox, path: Path, channel: str) -> None:
    (box.indus_home / "config.yaml").write_text(
        "_config_version: 1\n"
        "# a comment: with a colon\n"
        "updates:\n"
        "  check: true\n"
        "  installs:\n"
        f"    '{_install_id(path)}':\n"
        f"      channel: {channel}  # chosen\n"
        "model:\n"
        "  channel: stable\n"
    )


def test_diverged_commit_is_saved_before_the_reset(box: Sandbox) -> None:
    bare = make_origin(box)
    checkout = _clone_existing(box)
    (checkout / "local.txt").write_text("my commit\n")
    box.git("add", "local.txt", cwd=checkout)
    box.git("commit", "-q", "-m", "local work", cwd=checkout)
    local = _head(box, checkout)
    (checkout / "README.md").write_text("edited but not committed\n")
    (checkout / "scratch.txt").write_text("untracked\n")
    tip = push_commit(box, bare, "origin moved on")
    code, _, out = _stage(box, "--channel", "main")
    assert code == 0, out
    assert _head(box, checkout) == tip
    refs = box.git(
        "for-each-ref", "--format=%(refname)", "refs/indus/install-backups/", cwd=checkout
    )
    (backup,) = refs.splitlines()
    box.git("merge-base", "--is-ancestor", local, backup, cwd=checkout)  # raises if unreachable
    stashes = box.git("stash", "list", cwd=checkout)
    assert "indus-install-autostash-" in stashes
    assert not (checkout / "scratch.txt").exists()


def test_unmerged_index_is_cleared_before_the_stash(box: Sandbox) -> None:
    make_origin(box)
    checkout = _clone_existing(box)
    box.git("switch", "-q", "-c", "other", cwd=checkout)
    (checkout / "README.md").write_text("theirs\n")
    box.git("commit", "-q", "-am", "theirs", cwd=checkout)
    box.git("switch", "-q", "main", cwd=checkout)
    (checkout / "README.md").write_text("ours\n")
    box.git("commit", "-q", "-am", "ours", cwd=checkout)
    merged = box.run_git_allow_fail(["merge", "other"], cwd=checkout)
    assert merged != 0
    assert box.git("ls-files", "--unmerged", cwd=checkout)
    code, _, out = _stage(box, "--channel", "main")
    assert code == 0, out
    assert box.git("ls-files", "--unmerged", cwd=checkout) == ""
    assert "indus-install-autostash-" in box.git("stash", "list", cwd=checkout)


def test_saved_channel_is_honoured(box: Sandbox) -> None:
    bare = make_origin(box, tags=("v0.1.0", "v0.2.0"))
    checkout = _clone_existing(box, "v0.1.0")
    _save_channel(box, checkout, "main")
    code, _, out = _stage(box)
    assert code == 0, out
    assert _head(box, checkout) == box.git("rev-parse", "main", cwd=bare)
    assert box.calls("curl") == []  # main needs no release lookup


def test_default_channel_moves_a_release_checkout_forward(box: Sandbox) -> None:
    bare = make_origin(box, tags=("v0.1.0", "v0.2.0"))
    checkout = _clone_existing(box, "v0.1.0")
    code, _, out = _stage(box)
    assert code == 0, out
    assert _head(box, checkout) == _tag_commit(box, bare, "v0.2.0")


def test_explicit_channel_beats_the_saved_one(box: Sandbox) -> None:
    bare = make_origin(box, tags=("v0.1.0", "v0.2.0"))
    checkout = _clone_existing(box, "v0.1.0")
    _save_channel(box, checkout, "main")
    code, _, out = _stage(box, "--channel", "stable")
    assert code == 0, out
    assert _head(box, checkout) == _tag_commit(box, bare, "v0.2.0")


def test_a_release_never_moves_a_checkout_backwards(box: Sandbox) -> None:
    make_origin(box, tags=("v0.1.0",))
    checkout = _clone_existing(box)  # main's tip, ahead of v0.1.0
    before = _head(box, checkout)
    code, _, out = _stage(box)
    assert code == 0, out
    assert _head(box, checkout) == before
    assert "already has v0.1.0" in out


def test_unreachable_release_keeps_an_existing_checkout(box: Sandbox) -> None:
    make_origin(box, tags=("v0.1.0",))
    checkout = _clone_existing(box, "v0.1.0")
    before = _head(box, checkout)
    box.rule(tool="git", match=["ls-remote"], exit=128, stderr="fatal: unable to access\n")
    code, frame, out = _stage(box)
    assert code == 0 and frame["ok"], out
    assert _head(box, checkout) == before
    assert "keeping" in out
    code, frame, _ = _stage(box, "--channel", "stable")
    assert code == 1 and "--channel main" in str(frame["reason"])


def test_saved_channel_reader_ignores_other_keys(box: Sandbox) -> None:
    make_origin(box)
    checkout = _clone_existing(box)
    _save_channel(box, checkout, "canary")
    script = (
        f'source "{INSTALL_SH}"; '
        f'ins_parse_args --indus-home "{box.indus_home}"; ins_saved_channel "{checkout}"'
    )
    out = box.run_bash(script)
    assert out == "canary"
    (box.indus_home / "config.yaml").write_text(
        json.dumps({"updates": {"installs": {_install_id(checkout): {"channel": "weekly"}}}})
    )
    assert box.run_bash(script) == ""
