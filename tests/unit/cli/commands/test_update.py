"""P23-19: the ``indus update`` command surface — flags, refusals, ``--check``, ``--plan``,
``--set-channel``, ``--install-id`` — and the version line (I45)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from indus.cli.commands import update as update_mod
from indus.cli.update import REFUSALS, install_id
from indus.cli.version import update_line, version_lines
from indus.foundation import config
from indus.foundation.scope import Scope
from tests.fakes.clock import FakeClock
from tests.fakes.updater import World, scope_for
from tests.fixtures.update.make_repo import Upstream, git
from tests.unit.cli.commands.cmdrun import Ran, run_cli


@pytest.fixture
def upstream(tmp_path: Path) -> Upstream:
    return Upstream(tmp_path)


@pytest.fixture
def user_scope(tmp_path: Path, upstream: Upstream, fake_clock: FakeClock) -> Scope:
    return scope_for(tmp_path / "indus-root", upstream.home, fake_clock)


def cli(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    scope: Scope,
    world: World,
    *argv: str,
    tty: bool = True,
) -> Ran:
    monkeypatch.setattr(update_mod, "wiring", world.wiring)
    return run_cli(capsys, scope, "update", *argv, tty=tty)


def test_install_id_prints_id_and_path(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream.release("0.1.0")
    checkout = upstream.clone(tmp_path / "checkout")
    ran = cli(capsys, monkeypatch, user_scope, World(checkout), "--install-id", tty=False)
    assert ran.code == 0
    assert ran.out.split() == [install_id(checkout), str(checkout)]


def test_managed_install_refused_with_its_instruction(
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = tmp_path / "image"
    (tree / "src" / "indus").mkdir(parents=True)
    (tree / "pyproject.toml").write_text("", encoding="utf-8")
    (tree / ".install_method").write_text("docker\n", encoding="utf-8")
    world = World(tree)
    ran = cli(capsys, monkeypatch, user_scope, world, "-y")
    assert ran.code == 1 and REFUSALS["docker"] in ran.err
    ran = cli(capsys, monkeypatch, user_scope, world, "--check")
    assert ran.code == 1 and "Docker" in ran.err
    planned = cli(capsys, monkeypatch, user_scope, world, "--plan")
    plan = json.loads(planned.out)
    assert planned.code == 1 and plan["install_kind"] == "docker" and plan["action"] == "refused"
    (tree / ".install_method").write_text("nix\n", encoding="utf-8")
    ran = cli(capsys, monkeypatch, user_scope, world, "-y")
    assert ran.code == 1 and "Nix owns updates: run `nix flake update` and rebuild." in ran.err


def test_no_terminal_needs_yes_and_touches_nothing(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = upstream.release("0.1.0")
    upstream.release("0.2.0")
    checkout = upstream.clone(tmp_path / "checkout", at=first)
    world = World(checkout)
    ran = cli(capsys, monkeypatch, user_scope, world, tty=False)
    assert ran.code == 1 and "indus update -y" in ran.err
    assert world.runs == [] and git(checkout, "rev-parse", "HEAD", home=upstream.home) == first
    assert not (checkout / ".git" / "FETCH_HEAD").exists()  # nothing was fetched
    assert cli(capsys, monkeypatch, user_scope, world, "--backup", "--no-backup").code == 2
    assert cli(capsys, monkeypatch, user_scope, world, "--channel", "main", "--set-channel",
               "main").code == 2  # fmt: skip


def test_check_exit_codes_and_cache(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_clock: FakeClock,
) -> None:
    first = upstream.release("0.1.0")
    upstream.release("0.2.0")
    checkout = upstream.clone(tmp_path / "checkout", at=first)
    world = World(checkout)
    ran = cli(capsys, monkeypatch, user_scope, world, "--check", tty=False)
    assert ran.code == 10 and "0.2.0" in ran.out
    assert git(checkout, "rev-parse", "HEAD", home=upstream.home) == first  # installs nothing
    cache = user_scope.root / "cache" / "update-check.json"
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["available"] is True and data["target"] == "0.2.0" and data["notices"] is True
    now = fake_clock.now()
    line = update_line(user_scope.root, now=now)
    assert line == "Update available: 0.2.0 on stable (run `indus update`)"
    assert update_line(user_scope.root, now=now + timedelta(hours=25)) is None
    data["checked_at"] = datetime.now(UTC).isoformat()  # --version compares with the real clock
    cache.write_text(json.dumps(data), encoding="utf-8")
    lines = version_lines(checkout / "src" / "indus", root=user_scope.root)
    assert lines[1].startswith("Install method: ") and lines[-1].startswith("Update available")
    git(checkout, "checkout", "--quiet", "--detach", "v0.2.0", home=upstream.home)
    ran = cli(capsys, monkeypatch, user_scope, world, "--check", tty=False)
    assert ran.code == 0 and "already on 0.2.0" in ran.out
    assert update_line(user_scope.root, now=now) is None


def test_check_respects_passive_notice_setting(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_clock: FakeClock,
) -> None:
    first = upstream.release("0.1.0")
    upstream.release("0.2.0")
    checkout = upstream.clone(tmp_path / "checkout", at=first)
    config.save(user_scope, {"updates": {"check": False}})
    ran = cli(capsys, monkeypatch, user_scope, World(checkout), "--check", tty=False)
    assert ran.code == 10  # the explicit check still works
    assert update_line(user_scope.root, now=fake_clock.now()) is None


def test_set_channel_is_saved_per_install(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream.release("0.1.0")
    tip = upstream.commit("main moved on")
    checkout = upstream.clone(tmp_path / "checkout")
    world = World(checkout)
    ran = cli(capsys, monkeypatch, user_scope, world, "--set-channel", "canary", "--check")
    assert ran.code == 0 and "follows canary" in ran.out
    iid = install_id(checkout)
    view = config.load(user_scope)
    assert view.get(f"updates.installs.{iid}.channel") == "canary"
    # the saved choice is explicit: --plan now shows a move back to the release
    plan = json.loads(cli(capsys, monkeypatch, user_scope, world, "--plan").out)
    assert plan["channel"] == "canary" and plan["channel_source"] == "saved"
    assert plan["action"] == "rollback" and plan["from"]["commit"] == tip
    # a one-run channel wins and is not saved
    plan = json.loads(
        cli(capsys, monkeypatch, user_scope, world, "--plan", "--channel", "main").out
    )
    assert plan["channel"] == "main" and plan["action"] == "up_to_date"
    assert config.load(user_scope).get(f"updates.installs.{iid}.channel") == "canary"


def test_plan_names_the_gateway_and_its_restart(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = upstream.release("0.1.0")
    upstream.release("0.2.0")
    checkout = upstream.clone(tmp_path / "checkout", at=first)
    ran = cli(capsys, monkeypatch, user_scope, World(checkout), "--plan", tty=False)
    assert ran.code == 0
    plan = json.loads(ran.out)
    assert plan["install_kind"] == "source" and plan["action"] == "update"
    assert plan["to"]["version"] == "0.2.0" and plan["from"]["version"] == "0.1.0"
    assert plan["services"][0]["kind"] == "gateway"


def test_nothing_published_and_up_to_date_change_nothing(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream.write_tree("0.0.0")
    upstream.commit("no release yet")
    checkout = upstream.clone(tmp_path / "checkout")
    world = World(checkout)
    ran = cli(capsys, monkeypatch, user_scope, world, "-y")
    assert ran.code == 0 and "no stable release" in ran.out and "--channel main" in ran.out
    ran = cli(capsys, monkeypatch, user_scope, world, "-y", "--channel", "main")
    assert ran.code == 0 and "already on" in ran.out
    assert world.execs.calls == []


def test_unreachable_origin_fails_cleanly(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream.release("0.1.0")
    checkout = upstream.clone(tmp_path / "checkout", official=False)
    git(checkout, "remote", "set-url", "origin", str(tmp_path / "gone.git"), home=upstream.home)
    ran = cli(capsys, monkeypatch, user_scope, World(checkout), "--check")
    assert ran.code == 1 and "Could not read the main channel" in ran.err
