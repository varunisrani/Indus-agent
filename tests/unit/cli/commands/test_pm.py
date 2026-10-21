"""P24-24: the `indus pm` command group (output, exit codes, non-TTY rules)."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from indus.cli.commands import pm as pm_cli
from indus.foundation.scope import Scope
from indus.pm.core import Pm
from indus.pm.lock import ANY, Lock, Pin
from tests.fakes.pm_origin import MIRROR, FakeOrigin
from tests.unit.cli.commands._helpers_d import Result, run
from tests.unit.pm.conftest import TARGET, publish_extra, publish_tool, standard_packages


@pytest.fixture
def origin() -> FakeOrigin:
    return FakeOrigin()


@pytest.fixture
def wired(scope: Scope, origin: FakeOrigin, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Pm]]:
    """Every pm the commands build points at the fake origin and the test package list."""
    made: list[Pm] = []

    def manager(local_dirs: Sequence[Path] = ()) -> Pm:
        pm = Pm(
            scope.root,
            env={"PATH": "/usr/bin"},
            target=TARGET,
            packages=standard_packages(),
            mirror=MIRROR,
            local_dirs=tuple(local_dirs),
            transport=origin.transport(),
            clock=scope.clock,
        )
        made.append(pm)
        return pm

    monkeypatch.setattr(pm_cli, "_manager", manager)
    yield made
    for pm in made:
        pm.close()


def _lock(scope: Scope, origin: FakeOrigin) -> None:
    Lock(
        {
            "tool": Pin("1.0", {TARGET: publish_tool(origin)}),
            "extra": Pin("2.0", {ANY: publish_extra(origin)}),
        }
    ).write(scope.root / "pm" / "lock.json")


def _pm(scope: Scope, *argv: str, tty: bool = False, input: str | None = None) -> Result:
    return run(scope, "pm", *argv, tty=tty, input=input)


def test_lock_asks_first_and_needs_yes_without_a_terminal(
    scope: Scope, origin: FakeOrigin, wired: list[Pm]
) -> None:
    refused = _pm(scope, "lock")
    assert refused.code == 1 and "--yes" in refused.err
    assert origin.seen == []
    declined = _pm(scope, "lock", tty=True, input="n\n")
    assert declined.code == 1 and "Nothing was changed." in declined.out
    origin.add("tool/1.0/tool-linux-x64.tar.gz", b"t")
    origin.add("tool/1.0/tool-darwin-arm64.tar.gz", b"m")
    origin.add("extra/extra-2.0", b"e")
    done = _pm(scope, "lock", "--yes")
    assert done.code == 0, done.out + done.err
    assert "ok     pin tool: 1.0 (2 targets)" in done.out
    assert set(Lock.read(scope.root / "pm" / "lock.json").packages) == {"tool", "extra"}


def test_install_doctor_env_and_status(scope: Scope, origin: FakeOrigin, wired: list[Pm]) -> None:
    assert _pm(scope, "install").code == 1  # nothing locked
    _lock(scope, origin)
    out = _pm(scope, "install")
    assert out.code == 0, out.out + out.err
    assert "ok     install tool: 1.0" in out.out
    doctor = _pm(scope, "doctor")
    assert doctor.code == 0 and "The store is clean." in doctor.out
    env = json.loads(_pm(scope, "env", "tool").out)
    assert len(env["PATH"]) == 1 and env["PATH"][0].endswith("/bin")
    status = json.loads(_pm(scope, "status", "--json").out)
    assert status["receipt"]["operation"] == "install"
    assert "Last: install ok (exit 0)" in _pm(scope, "status").out


def test_doctor_exits_1_on_a_tampered_entry_and_repair_fixes_it(
    scope: Scope, origin: FakeOrigin, wired: list[Pm]
) -> None:
    _lock(scope, origin)
    assert _pm(scope, "install").code == 0
    entries = [p for p in (scope.root / "pm" / "store").iterdir() if p.name.startswith("tool-")]
    (entries[0] / "README").write_text("tampered")
    doctor = _pm(scope, "doctor")
    assert doctor.code == 1
    assert "BAD  tool: tampered" in doctor.out
    assert "indus pm repair" in doctor.err
    repaired = _pm(scope, "repair")
    assert repaired.code == 0 and "ok     repair tool" in repaired.out
    assert _pm(scope, "doctor").code == 0


def test_without_must_name_a_default_package(
    scope: Scope, origin: FakeOrigin, wired: list[Pm]
) -> None:
    _lock(scope, origin)
    out = _pm(scope, "install", "--without", "tool")
    assert out.code == 2 and "tool is required" in out.err


def test_gc_bundle_and_offline_install(
    scope: Scope, origin: FakeOrigin, wired: list[Pm], tmp_path: Path
) -> None:
    _lock(scope, origin)
    assert _pm(scope, "install").code == 0
    stray = scope.root / "pm" / "store" / "old-1-0000000000000000"
    stray.mkdir()
    gc = _pm(scope, "gc")
    assert gc.code == 0 and "gc: removed 1, kept 2 entries." in gc.out
    out = tmp_path / "bundle"
    bundled = _pm(scope, "bundle", "--out", str(out))
    assert bundled.code == 0 and "indus pm install --from" in bundled.out
    assert _pm(scope, "bundle", "--out", str(out)).code == 2  # not empty
    assert _pm(scope, "install", "--from", str(tmp_path / "missing")).code == 2


def test_update_check_and_apply(scope: Scope, origin: FakeOrigin, wired: list[Pm]) -> None:
    from indus.pm import commands as ops

    _lock(scope, origin)
    now = scope.clock.now()
    origin.json_docs[ops.GITHUB_RELEASES.format(repo="example/tool")] = [
        {"tag_name": "1.1", "published_at": f"{now.year - 1}-06-01T00:00:00Z"}
    ]
    publish_tool(origin, "1.1")
    origin.add("tool/1.1/tool-darwin-arm64.tar.gz", b"m")
    check = _pm(scope, "update", "--check")
    assert check.code == 1 and "tool   1.0 -> 1.1" in check.out
    assert "extra  moves only with the product" in check.out
    refused = _pm(scope, "update")
    assert refused.code == 1 and "--yes" in refused.err
    applied = _pm(scope, "update", "--yes")
    assert applied.code == 0, applied.out + applied.err
    assert Lock.read(scope.root / "pm" / "lock.json").packages["tool"].version == "1.1"
    again = _pm(scope, "update", "--check")
    assert again.code == 0 and "Everything is up to date." in again.out


def test_fresh_profile_commands_without_a_terminal(scope: Scope, wired: list[Pm]) -> None:
    assert _pm(scope, "doctor").code == 0
    assert json.loads(_pm(scope, "status", "--json").out) == {"schema": 1, "receipt": None}
    assert json.loads(_pm(scope, "env").out) == {}
    assert _pm(scope, "update").code == 1
    assert "nothing is locked yet" in _pm(scope, "update").err
