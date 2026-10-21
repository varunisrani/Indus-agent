"""P23-17: install kinds, channels, the never-backwards rule, store schema and the plan.

Real ``git`` in temp repositories (``tests/fixtures/update/make_repo.py``); nothing reaches the
network: the official ``origin`` URL is rewritten to a local bare repository.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from indus.cli.update import REFUSALS, detect_kind, install_id
from indus.cli.update.channels import (
    ChannelChoice,
    Git,
    build_plan,
    choose_channel,
    current_version,
    decide,
    fetch,
    is_official,
    live_store_schema,
    resolve_target,
    saved_channel,
    saved_extras,
    store_refusal,
)
from tests.fixtures.update.make_repo import Upstream, env, git

STAMP_A = "20261020T064100Z"
STAMP_B = "20261021T064100Z"


@pytest.fixture
def upstream(tmp_path: Path) -> Upstream:
    return Upstream(tmp_path)


def checkout_git(up: Upstream, dest: Path, *, at: str | None = None, official: bool = True) -> Git:
    up.clone(dest, at=at, official=official)
    handle = Git(dest, env(up.home))
    fetch(handle)
    return handle


def stable() -> ChannelChoice:
    return ChannelChoice("stable", "default")


# -- install kinds ---------------------------------------------------------------------------------


def _tree(root: Path) -> Path:
    pkg = root / "src" / "indus"
    pkg.mkdir(parents=True)
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    return pkg


def test_managed_installs_are_refused(tmp_path: Path) -> None:
    never = lambda _p: False  # noqa: E731
    pkg = _tree(tmp_path / "checkout")
    assert detect_kind(pkg, exists=never) == "dev"
    (tmp_path / "checkout" / ".git").mkdir()
    assert detect_kind(pkg, exists=never) == "source"
    # a clone inside some container is still a source checkout
    assert detect_kind(pkg, exists=lambda p: p == "/.dockerenv") == "source"
    assert detect_kind(pkg, env={"INDUS_DESKTOP_BUNDLED": "1"}, exists=never) == "desktop"
    for word in ("docker", "nix", "desktop"):
        (tmp_path / "checkout" / ".install_method").write_text(word + "\n", encoding="utf-8")
        assert detect_kind(pkg, exists=never) == word
        assert REFUSALS[word]
    (tmp_path / "checkout" / ".install_method").write_text("installer\n", encoding="utf-8")
    assert detect_kind(pkg, exists=never) == "source"
    image = _tree(tmp_path / "image")  # our image: no git history, container marker
    assert detect_kind(image, exists=lambda p: p == "/.dockerenv") == "docker"
    wheel = tmp_path / "site-packages" / "indus"
    wheel.mkdir(parents=True)
    assert detect_kind(wheel, exists=never) == "package"
    assert "pull" in REFUSALS["docker"].lower() and "nix" in REFUSALS["nix"].lower()


def test_install_id_is_the_real_path_hash(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert install_id(real) == install_id(link)
    assert len(install_id(real)) == 16 and int(install_id(real), 16) >= 0


# -- channels --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "official"),
    [
        ("https://github.com/indusagi/indus-agent.git", True),
        ("https://github.com/indusagi/indus-agent", True),
        ("git@github.com:indusagi/indus-agent.git", True),
        ("https://github.com/IndusAgi/Indus-Agent/", True),
        ("https://github.com/someone/indus-agent.git", False),
        ("https://example.com/indusagi/indus-agent.git", False),
        ("/srv/mirror/indus-agent.git", False),
        ("", False),
    ],
)
def test_official_repository_detection(url: str, official: bool) -> None:
    assert is_official(url) is official


def test_saved_channel_and_one_run_channel() -> None:
    assert choose_channel(None, None, official=True) == ChannelChoice("stable", "default")
    assert choose_channel(None, "canary", official=True) == ChannelChoice("canary", "saved")
    assert choose_channel("main", "canary", official=True) == ChannelChoice("main", "flag")
    assert choose_channel(None, "nightly", official=True).source == "default"
    with pytest.raises(ValueError):
        choose_channel("nightly", None, official=True)
    installs = {"abc": {"channel": "canary", "extras": ["web", "Bad Extra", 3]}}
    assert saved_channel(installs, "abc") == "canary"
    assert saved_channel(installs, "other") is None
    assert saved_channel("not a mapping", "abc") is None
    assert saved_extras(installs, "abc") == ["web"]


def test_fork_defaults_to_main(upstream: Upstream, tmp_path: Path) -> None:
    upstream.release("0.1.0")
    tip = upstream.commit("work after the release")
    handle = checkout_git(upstream, tmp_path / "fork", official=False)
    choice = choose_channel(None, None, official=is_official(handle.remote_url()))
    assert choice == ChannelChoice("main", "default")
    target = resolve_target(handle, "main")
    assert target is not None and target.commit == tip and target.version is None


def test_stable_picks_the_newest_release_tag(upstream: Upstream, tmp_path: Path) -> None:
    first = upstream.release("0.1.0")
    second = upstream.release("0.2.0")
    upstream.release(f"0.2.1-canary.{STAMP_A}")
    upstream.commit("later work")
    for bad in ("v0.3", "v01.4.0", "v9.9.9-rc.1", "v1000.0.0", f"v0.9.0-canary.{STAMP_A}x"):
        upstream.tag(bad)
    handle = checkout_git(upstream, tmp_path / "user", at=first)
    assert is_official(handle.remote_url())
    target = resolve_target(handle, "stable")
    assert target is not None
    assert (str(target.version), target.tag, target.commit) == ("0.2.0", "v0.2.0", second)
    decision = decide(handle, stable(), target)
    assert decision.action == "update" and decision.moves
    assert str(current_version(handle, decision.head)) == "0.1.0"


def test_no_release_yet(upstream: Upstream, tmp_path: Path) -> None:
    upstream.commit("first")
    handle = checkout_git(upstream, tmp_path / "user")
    decision = decide(handle, stable(), resolve_target(handle, "stable"))
    assert decision.action == "none" and "no stable release" in decision.reason


def test_checkout_ahead_of_the_release_is_not_moved(upstream: Upstream, tmp_path: Path) -> None:
    upstream.release("0.1.0")
    tip = upstream.commit("main moved on")
    handle = checkout_git(upstream, tmp_path / "user")
    assert handle.head() == tip
    decision = decide(handle, stable(), resolve_target(handle, "stable"))
    assert decision.action == "ahead" and not decision.moves


def test_explicit_channel_may_move_back_and_says_so(upstream: Upstream, tmp_path: Path) -> None:
    released = upstream.release("0.1.0")
    upstream.commit("main moved on")
    handle = checkout_git(upstream, tmp_path / "user")
    decision = decide(handle, ChannelChoice("stable", "saved"), resolve_target(handle, "stable"))
    assert decision.action == "rollback" and decision.target is not None
    assert decision.target.commit == released
    up_to_date = decide(handle, ChannelChoice("main", "flag"), resolve_target(handle, "main"))
    assert up_to_date.action == "up_to_date"


def test_canary_user_is_offered_a_newer_stable(upstream: Upstream, tmp_path: Path) -> None:
    upstream.release("0.1.0")
    canary_sha = upstream.release(f"0.1.1-canary.{STAMP_A}")
    handle = checkout_git(upstream, tmp_path / "user")
    target = resolve_target(handle, "canary")
    assert target is not None and target.commit == canary_sha and target.version is not None
    assert target.version.is_canary
    stable_sha = upstream.release("0.1.1")
    upstream.release(f"0.1.2-canary.{STAMP_B}")
    fetch(handle)
    newest = resolve_target(handle, "canary")
    assert newest is not None and str(newest.version) == f"0.1.2-canary.{STAMP_B}"
    # the canary of 0.1.2 is still below a 0.1.2 stable, which wins once published
    upstream.release("0.1.2")
    fetch(handle)
    lead = resolve_target(handle, "canary")
    assert lead is not None and str(lead.version) == "0.1.2" and lead.tag == "v0.1.2"
    stable_target = resolve_target(handle, "stable")
    assert stable_target is not None and stable_target.commit != stable_sha  # 0.1.2 now


# -- store schema ----------------------------------------------------------------------------------


def _store(path: Path, version: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
    conn.commit()
    conn.close()


def test_target_with_an_older_store_schema_is_refused(upstream: Upstream, tmp_path: Path) -> None:
    upstream.release("0.1.0", schema=1)
    upstream.release("0.2.0", schema=2)
    handle = checkout_git(upstream, tmp_path / "user")
    root = tmp_path / "indus-root"
    assert live_store_schema(root) is None
    _store(root / "state.db", 1)
    _store(root / "profiles" / "work" / "state.db", 2)
    assert live_store_schema(root) == 2
    newest = resolve_target(handle, "stable")
    assert newest is not None and store_refusal(handle, root, newest) is None
    older = resolve_target(handle, "stable")
    assert older is not None
    git(handle.root, "tag", "-d", "v0.2.0", home=upstream.home)  # pretend 0.1.0 is the target
    old_target = resolve_target(handle, "stable")
    assert old_target is not None and str(old_target.version) == "0.1.0"
    message = store_refusal(handle, root, old_target)
    assert message is not None and "schema 1" in message and "schema 2" in message


# -- plan ------------------------------------------------------------------------------------------


def test_plan_json_shape(upstream: Upstream, tmp_path: Path) -> None:
    first = upstream.release("0.1.0")
    upstream.release("0.2.0")
    handle = checkout_git(upstream, tmp_path / "user", at=first)
    target = resolve_target(handle, "stable")
    decision = decide(handle, stable(), target)
    service = {"kind": "gateway", "running": False, "managed_by": "none", "restart": "not running"}
    plan = build_plan(
        kind="source",
        checkout=handle.root,
        iid="0123456789abcdef",
        choice=stable(),
        decision=decision,
        current=current_version(handle, decision.head),
        services=[service],
    )
    assert set(plan) == {
        "schema", "install_kind", "install_id", "install_dir", "channel", "channel_source",
        "action", "reason", "from", "to", "services",
    }  # fmt: skip
    assert plan["action"] == "update" and plan["from"] == {"commit": first, "version": "0.1.0"}
    assert plan["to"]["version"] == "0.2.0" and plan["to"]["tag"] == "v0.2.0"
    assert plan["services"] == [service]
    refused = build_plan(
        kind="docker", checkout=handle.root, iid="x", choice=None, decision=None, current=None,
        services=[], refusal=REFUSALS["docker"],
    )  # fmt: skip
    assert refused["action"] == "refused" and refused["to"] is None
