"""P24-24: the operations behind `indus pm` (lock, install, doctor, repair, gc, bundle, status,
update)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from indus.pm import commands as ops
from indus.pm.core import Pm, parse_definitions
from indus.pm.lock import ANY, Lock, Pin, PmError
from tests.fakes.clock import FakeClock
from tests.fakes.pm_origin import FakeOrigin, definitions_text, sha256, tar_gz
from tests.unit.pm.conftest import TARGET, extra_rows, publish_tool, tool_archive, tool_rows


class NoNetwork(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"network used: {request.url}")


# -- lock ------------------------------------------------------------------------------------------


def test_lock_pins_every_target_and_collapses_shared_urls(
    make_pm: Callable[..., Pm], origin: FakeOrigin
) -> None:
    pm = make_pm()
    linux = origin.add("tool/1.0/tool-linux-x64.tar.gz", tool_archive())
    mac_data = tar_gz({"bin/tool": b"mac"})
    origin.add("tool/1.0/tool-darwin-arm64.tar.gz", mac_data)
    extra = origin.add("extra/extra-2.0", b"extra")
    op = ops.lock_packages(pm)
    assert op.exit_code == 0
    lock = Lock.read(pm.paths.lock)
    assert lock.packages["tool"].version == "1.0"
    assert lock.packages["tool"].artifacts[TARGET].url == linux
    assert lock.packages["tool"].artifacts["darwin-arm64"].sha256 == sha256(mac_data)
    assert set(lock.packages["extra"].artifacts) == {ANY}
    assert lock.packages["extra"].artifacts[ANY].url == extra
    origin.seen.clear()
    assert ops.lock_packages(pm).exit_code == 0
    assert origin.seen == []  # unchanged URLs keep their hashes


def test_a_target_that_fails_to_resolve_fails_its_package_only(
    make_pm: Callable[..., Pm], origin: FakeOrigin
) -> None:
    pm = make_pm()
    origin.add("extra/extra-2.0", b"extra")
    op = ops.lock_packages(pm)
    assert op.exit_code == 1
    assert [(s.name, s.ok) for s in op.steps] == [("pin tool", False), ("pin extra", True)]
    assert set(Lock.read(pm.paths.lock).packages) == {"extra"}
    assert ops.read_receipt(pm)["outcome"] == "partial"  # type: ignore[index]


# -- install ---------------------------------------------------------------------------------------


def test_install_takes_required_and_default_packages(locked_pm: Pm) -> None:
    op, warnings = ops.install(locked_pm)
    assert op.exit_code == 0 and warnings == []
    assert sorted(locked_pm.facts().packages) == ["extra", "tool"]


def test_without_declines_a_default_now_and_later(locked_pm: Pm) -> None:
    ops.install(locked_pm, without=["extra"])
    assert sorted(locked_pm.facts().packages) == ["tool"]
    assert locked_pm.facts().declined == ["extra"]
    ops.install(locked_pm)
    assert "extra" not in locked_pm.facts().packages
    ops.install(locked_pm, ["extra"])
    assert "extra" in locked_pm.facts().packages
    assert locked_pm.facts().declined == []


def test_without_takes_only_default_packages(locked_pm: Pm) -> None:
    with pytest.raises(ops.UsageProblem, match="tool is required"):
        ops.install(locked_pm, without=["tool"])
    with pytest.raises(PmError, match="unknown package"):
        ops.install(locked_pm, without=["nope"])


def test_a_failing_default_warns_and_does_not_fail(locked_pm: Pm, origin: FakeOrigin) -> None:
    pin = locked_pm.pin("extra").artifacts[ANY]
    origin.status[pin.url] = 404
    origin.mirror_blobs.clear()
    op, warnings = ops.install(locked_pm)
    assert op.exit_code == 0
    assert warnings and "extra was not installed" in warnings[0]
    assert ops.read_receipt(locked_pm)["outcome"] == "partial"  # type: ignore[index]


def test_install_needs_a_lock(make_pm: Callable[..., Pm]) -> None:
    with pytest.raises(PmError, match="nothing is locked yet"):
        ops.install(make_pm())


# -- doctor and repair -----------------------------------------------------------------------------


def test_doctor_reports_a_tampered_store_entry(locked_pm: Pm) -> None:
    ops.install(locked_pm)
    assert {r.name: r.state for r in ops.doctor(locked_pm)} == {"extra": "ok", "tool": "ok"}
    runner = locked_pm.ensure("tool")
    (runner.entry / "README").write_text("changed behind pm's back")
    rows = {r.name: r for r in ops.doctor(locked_pm)}
    assert rows["tool"].state == "tampered" and rows["tool"].bad
    assert rows["extra"].state == "ok"


def test_doctor_states(locked_pm: Pm, origin: FakeOrigin) -> None:
    rows = {r.name: (r.state, r.bad) for r in ops.doctor(locked_pm)}
    assert rows == {"extra": ("not installed", False), "tool": ("not installed", True)}
    ops.install(locked_pm)
    runner = locked_pm.ensure("tool")
    assert runner.executable is not None
    runner.executable.unlink()
    assert {r.name: r.state for r in ops.doctor(locked_pm)}["tool"] == "tampered"
    ops.repair(locked_pm)
    lock = Lock.read(locked_pm.paths.lock)
    lock.packages["tool"] = Pin("1.1", {TARGET: publish_tool(origin, "1.1")})
    lock.write(locked_pm.paths.lock)
    rows2 = {r.name: r for r in ops.doctor(locked_pm)}
    assert (rows2["tool"].state, rows2["tool"].detail) == ("outdated", "1.0 installed, 1.1 locked")


def test_repair_rebuilds_without_changing_the_graph(locked_pm: Pm) -> None:
    ops.install(locked_pm)
    runner = locked_pm.ensure("tool")
    lock_before = locked_pm.paths.lock.read_bytes()
    (runner.entry / "bin" / "tool").write_text("broken")
    op = ops.repair(locked_pm)
    assert [(s.name, s.ok) for s in op.steps] == [("repair tool", True)]
    assert (runner.entry / "bin" / "tool").read_bytes().startswith(b"#!/bin/sh")
    assert {r.state for r in ops.doctor(locked_pm)} == {"ok"}
    assert locked_pm.paths.lock.read_bytes() == lock_before
    assert sorted(locked_pm.facts().packages) == ["extra", "tool"]
    assert ops.repair(locked_pm).steps == []


# -- gc --------------------------------------------------------------------------------------------


def test_gc_keeps_everything_the_facts_reference(locked_pm: Pm) -> None:
    ops.install(locked_pm)
    store = locked_pm.paths.store
    downloads = locked_pm.paths.downloads
    (store / "old-0.9-0123456789abcdef").mkdir()
    (store / ".staging-1-dead").mkdir()
    (downloads / ("f" * 64)).write_bytes(b"stale")
    (downloads / (("e" * 64) + ".part")).write_bytes(b"half")
    kept_entries = {p.name for p in locked_pm.store.entries()} - {"old-0.9-0123456789abcdef"}
    op = ops.gc(locked_pm)
    removed = sorted(s.name for s in op.steps if s.name.startswith("remove"))
    assert removed == [
        "remove .staging-1-dead",
        f"remove downloads/{'e' * 64}.part",
        f"remove downloads/{'f' * 64}",
        "remove old-0.9-0123456789abcdef",
    ]
    assert {p.name for p in locked_pm.store.entries()} == kept_entries
    assert {p.name for p in downloads.iterdir()} == locked_pm.facts().blobs_in_use()
    assert {r.state for r in ops.doctor(locked_pm)} == {"ok"}


# -- bundle ----------------------------------------------------------------------------------------


def test_a_bundle_installs_offline_in_a_second_root(
    locked_pm: Pm, make_pm: Callable[..., Pm], tmp_path: Path
) -> None:
    ops.install(locked_pm)
    out = tmp_path / "bundle"
    op = ops.bundle(locked_pm, out)
    assert {s.name for s in op.steps} == {"bundle tool", "bundle extra"}
    assert (out / "lock.json").is_file() and (out / "facts.json").is_file()
    assert (out / ops.BUNDLE_FILE).is_file()
    for fact in locked_pm.facts().packages.values():
        assert (out / "blobs" / fact.sha256).is_file()
        assert (out / "store" / fact.entry).is_dir()
    second = tmp_path / "second-root"
    second.mkdir()
    offline = make_pm(root=second, transport=NoNetwork(), mirror="", local_dirs=(out,))
    assert ops.adopt_bundle_lock(offline, out)
    install_op, _ = ops.install(offline)
    assert install_op.exit_code == 0
    runner = offline.ensure("tool")
    assert runner.entry.is_relative_to(second)
    assert {r.state for r in ops.doctor(offline)} == {"ok"}


def test_bundle_refuses_a_used_folder_and_a_damaged_store(locked_pm: Pm, tmp_path: Path) -> None:
    used = tmp_path / "used"
    used.mkdir()
    (used / "x").write_text("x")
    with pytest.raises(ops.UsageProblem, match="not empty"):
        ops.bundle(locked_pm, used)
    with pytest.raises(PmError, match="installed nothing"):
        ops.bundle(locked_pm, tmp_path / "empty")
    ops.install(locked_pm)
    (locked_pm.ensure("tool").entry / "README").write_text("x")
    with pytest.raises(PmError, match="repair"):
        ops.bundle(locked_pm, tmp_path / "fresh")


# -- status ----------------------------------------------------------------------------------------


def test_status_json_shape(locked_pm: Pm, fake_clock: FakeClock) -> None:
    assert ops.status(locked_pm) == {"schema": 1, "receipt": None}
    ops.install(locked_pm, ["tool"])
    assert ops.status(locked_pm) == {
        "schema": 1,
        "receipt": {
            "schema": 1,
            "operation": "install",
            "started_at": "2026-01-01T09:00:00+00:00",
            "finished_at": "2026-01-01T09:00:00+00:00",
            "outcome": "ok",
            "exit_code": 0,
            "steps": [{"name": "install tool", "ok": True, "detail": "1.0"}],
        },
    }


# -- update ----------------------------------------------------------------------------------------


def _releases(fake_clock: FakeClock, *rows: tuple[str, int, bool]) -> list[dict[str, object]]:
    now = fake_clock.now()
    return [
        {
            "tag_name": tag,
            "published_at": (now - timedelta(days=age)).isoformat().replace("+00:00", "Z"),
            "draft": False,
            "prerelease": pre,
        }
        for tag, age, pre in rows
    ]


def test_update_takes_the_newest_release_at_least_14_days_old(
    locked_pm: Pm, origin: FakeOrigin, fake_clock: FakeClock
) -> None:
    origin.json_docs[ops.GITHUB_RELEASES.format(repo="example/tool")] = _releases(
        fake_clock, ("1.3", 3, False), ("1.2-rc", 15, True), ("1.1", 20, False), ("1.0", 90, False)
    )
    rows = {r.name: r for r in ops.check_updates(locked_pm)}
    assert (rows["tool"].locked, rows["tool"].newest, rows["tool"].changed) == ("1.0", "1.1", True)
    assert rows["extra"].newest is None and rows["extra"].reason == "moves only with the product"
    assert Lock.read(locked_pm.paths.lock).packages["tool"].version == "1.0"  # check writes nothing


def test_apply_updates_repins_and_reinstalls(
    locked_pm: Pm, origin: FakeOrigin, fake_clock: FakeClock
) -> None:
    ops.install(locked_pm)
    origin.json_docs[ops.GITHUB_RELEASES.format(repo="example/tool")] = _releases(
        fake_clock, ("1.1", 20, False)
    )
    publish_tool(origin, "1.1")
    origin.add("tool/1.1/tool-darwin-arm64.tar.gz", b"mac")
    rows = ops.check_updates(locked_pm)
    op = ops.apply_updates(locked_pm, rows)
    assert op.exit_code == 0
    assert Lock.read(locked_pm.paths.lock).packages["tool"].version == "1.1"
    assert locked_pm.facts().packages["tool"].version == "1.1"


def test_node_releases_take_the_newest_lts(
    make_pm: Callable[..., Pm], origin: FakeOrigin, fake_clock: FakeClock
) -> None:
    pm = make_pm(parse_definitions(definitions_text({"node": {**tool_rows(), "releases": "node"}})))
    day = fake_clock.now().date()
    origin.json_docs[ops.NODE_INDEX] = [
        {"version": "v24.0.0", "date": str(day - timedelta(days=30)), "lts": False},
        {"version": "v22.12.0", "date": str(day - timedelta(days=5)), "lts": "Jod"},
        {"version": "v22.11.0", "date": str(day - timedelta(days=40)), "lts": "Jod"},
    ]
    assert ops.newest_release(pm, pm.package("node"), fake_clock.now()) == "22.11.0"


def test_a_failed_lookup_is_reported(locked_pm: Pm, origin: FakeOrigin) -> None:
    rows = {r.name: r for r in ops.check_updates(locked_pm, ["tool"])}
    assert rows["tool"].reason.startswith("lookup failed")


def test_update_needs_a_lock(make_pm: Callable[..., Pm]) -> None:
    with pytest.raises(PmError, match="indus pm lock"):
        ops.check_updates(make_pm())


def test_env_exports_only_installed_packages(locked_pm: Pm) -> None:
    assert ops.env_exports(locked_pm) == {}
    ops.install(locked_pm, ["tool"])
    runner = locked_pm.ensure("tool")
    assert ops.env_exports(locked_pm) == {"PATH": [str(runner.entry / "bin")]}
    assert extra_rows()["kind"] == "default"
