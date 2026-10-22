"""P23-19: the finish step — config migration, bundles by stage-and-swap, gateway restart,
receipts. Runs ``run_finish`` with fakes for the service manager, the control socket and builds."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from indus.cli.update import Console, Receipt
from indus.cli.update.finish import (
    DistBuilder,
    FinishContext,
    ProfileGateway,
    RestartResult,
    run_finish,
    stage_and_swap,
)
from indus.foundation.scope import Scope
from indus.service.gateway_manager import Completed, SystemdUser
from indus.service.gateway_units import UnitSpec
from tests.fakes.updater import FakeGateway

# -- helpers ---------------------------------------------------------------------------------------


@dataclass
class Report:
    from_version: int
    to_version: int
    steps: list[str] = field(default_factory=list[str])
    message: str = ""


def request_file(tmp_path: Path, root: Path, checkout: Path, **extra: Any) -> Path:
    receipt = Receipt(started_at="2026-01-01T09:00:00+00:00", install_id="abcd", channel="stable")
    receipt.phase("snapshot", "skipped", "--no-backup")
    receipt.phase("move", "ok", "a → b")
    receipt.phase("venv", "ok")
    data = {
        "schema": 1,
        "checkout": str(checkout),
        "root": str(root),
        "profile_home": str(root),
        "channel": "stable",
        "from_commit": "a" * 40,
        "to_commit": "b" * 40,
        "to_version": "0.2.0",
        "assume_yes": False,
        "no_gateway_restart": False,
        "stash_conflict": None,
        "receipt": receipt.to_json(),
        **extra,
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def context(
    scope: Scope,
    *,
    gateway: Any = None,
    builders: list[DistBuilder] | None = None,
    run: Any = None,
    answer: bool | None = True,
    report: Report | None = None,
    pnpm: str | None = "pnpm",
) -> FinishContext:
    def migrate(_dry: bool) -> Report:
        return report or Report(1, 1)

    ctx = FinishContext(
        scope=scope,
        console=Console(scope.root / "logs" / "update.log", now=scope.clock.now),
        gateway=gateway or FakeGateway(),
        builders=builders or [],
        now=scope.clock.now,
        confirm=lambda _q: answer,
        migrate=migrate,
        run=run or (lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")),
        pnpm=pnpm,
    )
    return ctx


def final_receipt(root: Path) -> dict[str, Any]:
    [path] = sorted((root / "logs" / "update_receipts").glob("*.json"))
    return json.loads(path.read_text(encoding="utf-8"))


def outcomes(receipt: dict[str, Any]) -> dict[str, str]:
    return {r["phase"]: r["outcome"] for r in receipt["phases"]}


def web_builder() -> tuple[DistBuilder, list[list[str]]]:
    calls: list[list[str]] = []

    def command(staging: Path) -> list[str]:
        return ["pnpm", "build-into", str(staging)]

    builder = DistBuilder("web", "apps/web/dist", "index.html", command)
    return builder, calls


def fake_build(calls: list[list[str]], *, fail: bool):
    def run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        staging = Path(argv[-1])
        if fail:
            staging.mkdir(parents=True, exist_ok=True)  # half a build, then a failure
            (staging / "partial.js").write_text("x", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 1, "", "error: Cannot find module 'vite'")
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "index.html").write_text("<html>new</html>", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "built", "")

    return run


# -- receipts and bundles --------------------------------------------------------------------------


def test_receipt_lists_every_phase(scope: Scope, tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    builder, calls = web_builder()
    (checkout / "apps/web/dist").mkdir(parents=True)
    (checkout / "apps/web/dist/index.html").write_text("<html>old</html>", encoding="utf-8")
    tui = DistBuilder("tui", "apps/tui/dist", "entry.js", lambda s: ["pnpm", str(s)])
    gateway = FakeGateway(RestartResult("ok", "restarted through systemd"))
    ctx = context(
        scope, gateway=gateway, builders=[builder, tui], run=fake_build(calls, fail=False),
        report=Report(0, 1),
    )  # fmt: skip
    path = request_file(tmp_path, scope.root, checkout)
    assert run_finish(path, ctx, no_gateway_restart=False) == 0
    assert not path.exists()
    receipt = final_receipt(scope.root)
    assert receipt["outcome"] == "ok" and receipt["owed"] == []
    assert outcomes(receipt) == {
        "snapshot": "skipped", "move": "ok", "venv": "ok", "config_migration": "ok",
        "dist:web": "ok", "dist:tui": "skipped", "web_pages": "skipped", "gateway_restart": "ok",
    }  # fmt: skip
    assert (checkout / "apps/web/dist/index.html").read_text(encoding="utf-8") == "<html>new</html>"
    assert not (checkout / "apps/web/dist.next").exists()
    assert not (checkout / "apps/web/dist.prev").exists()
    assert gateway.restarts == 1


def test_dist_build_failure_keeps_the_old_bundle_and_exits_0(
    scope: Scope, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "apps/web/dist").mkdir(parents=True)
    (checkout / "apps/web/dist/index.html").write_text("<html>old</html>", encoding="utf-8")
    builder, calls = web_builder()
    ctx = context(scope, builders=[builder], run=fake_build(calls, fail=True))
    path = request_file(tmp_path, scope.root, checkout)
    assert run_finish(path, ctx, no_gateway_restart=False) == 0
    assert (checkout / "apps/web/dist/index.html").read_text(encoding="utf-8") == "<html>old</html>"
    assert not (checkout / "apps/web/dist.next").exists()
    receipt = final_receipt(scope.root)
    assert receipt["outcome"] == "ok_with_followups"
    [owed] = receipt["owed"]
    assert owed["followup"] == "dist:web" and "vite" in owed["detail"]
    assert "Owed: dist:web" in capsys.readouterr().out


def test_missing_pnpm_owes_the_rebuild(scope: Scope, tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "apps/web/dist").mkdir(parents=True)
    (checkout / "apps/web/dist/index.html").write_text("old", encoding="utf-8")
    builder, _calls = web_builder()
    ctx = context(scope, builders=[builder], pnpm=None)
    assert (
        run_finish(request_file(tmp_path, scope.root, checkout), ctx, no_gateway_restart=True) == 0
    )
    assert outcomes(final_receipt(scope.root))["dist:web"] == "owed"


def test_stage_and_swap_never_leaves_a_half_bundle(tmp_path: Path) -> None:
    checkout = tmp_path / "c"
    builder, calls = web_builder()
    target = checkout / "apps/web/dist"
    target.mkdir(parents=True)
    (target / "index.html").write_text("old", encoding="utf-8")

    def no_entry(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        Path(argv[-1]).mkdir(parents=True)
        return subprocess.CompletedProcess(argv, 0, "", "")

    problem = stage_and_swap(builder, checkout, run=no_entry, env={}, log=lambda _t: None)
    assert "index.html" in problem and (target / "index.html").read_text() == "old"
    assert (
        stage_and_swap(
            builder, checkout, run=fake_build(calls, fail=False), env={}, log=lambda _t: None
        )
        == ""
    )
    assert (target / "index.html").read_text() == "<html>new</html>"


# -- the fetched dashboard pages (wave 47, f) ------------------------------------------------------


def fetch_runner(code: int, out: str = "", err: str = "") -> tuple[Any, list[list[str]]]:
    calls: list[list[str]] = []

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        assert kw.get("timeout"), "the fetch is time-limited"
        return subprocess.CompletedProcess(argv, code, out, err)

    return run, calls


def test_a_script_install_fetches_the_pages_of_the_new_release(
    scope: Scope, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    builder, _ = web_builder()
    run, calls = fetch_runner(0, "Installed the dashboard pages of v0.2.0 in x\n")
    ctx = context(scope, builders=[builder], run=run, pnpm=None)
    ctx.python = "/new/venv/bin/python"
    path = request_file(tmp_path, scope.root, checkout)
    assert run_finish(path, ctx, no_gateway_restart=True) == 0
    # A child process: the CLI layer never imports indus.web (D-16.246).
    assert calls == [
        [
            "/new/venv/bin/python", "-m", "indus.web.bundle", "fetch",
            "--repo", str(checkout), "--target", str(checkout / "src/indus/web/static"),
        ]
    ]  # fmt: skip
    receipt = final_receipt(scope.root)
    assert outcomes(receipt)["web_pages"] == "ok" and receipt["owed"] == []
    assert outcomes(receipt)["dist:web"] == "skipped"


def test_pages_off_a_release_are_skipped(scope: Scope, tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    run, calls = fetch_runner(3, "", "No prebuilt dashboard pages: not a release.\n")
    ctx = context(scope, run=run)
    assert (
        run_finish(request_file(tmp_path, scope.root, checkout), ctx, no_gateway_restart=True) == 0
    )
    receipt = final_receipt(scope.root)
    assert len(calls) == 1 and outcomes(receipt)["web_pages"] == "skipped"
    assert receipt["owed"] == []


def test_a_failed_page_fetch_is_owed_with_its_retry(
    scope: Scope, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    run, _calls = fetch_runner(1, "", "Could not fetch the dashboard pages: download failed\n")
    ctx = context(scope, run=run)
    ctx.python = "/new/py"
    assert (
        run_finish(request_file(tmp_path, scope.root, checkout), ctx, no_gateway_restart=True) == 0
    )
    receipt = final_receipt(scope.root)
    assert receipt["outcome"] == "ok_with_followups"
    [owed] = receipt["owed"]
    assert owed["followup"] == "web_pages" and "download failed" in owed["detail"]
    assert owed["retry"] == f"/new/py -m indus.web.bundle fetch --repo {checkout}"
    assert "Owed: web_pages" in capsys.readouterr().out


def test_a_page_fetch_that_cannot_start_is_owed(scope: Scope, tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    def broken(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 1.0)

    ctx = context(scope, run=broken)
    assert (
        run_finish(request_file(tmp_path, scope.root, checkout), ctx, no_gateway_restart=True) == 0
    )
    assert outcomes(final_receipt(scope.root))["web_pages"] == "owed"


def test_a_checkout_with_its_own_build_fetches_nothing(scope: Scope, tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "apps/web/dist").mkdir(parents=True)
    (checkout / "apps/web/dist/index.html").write_text("old", encoding="utf-8")
    run, calls = fetch_runner(0)
    ctx = context(scope, run=run, pnpm=None)
    assert (
        run_finish(request_file(tmp_path, scope.root, checkout), ctx, no_gateway_restart=True) == 0
    )
    assert calls == []
    assert outcomes(final_receipt(scope.root))["web_pages"] == "skipped"


# -- config migration ------------------------------------------------------------------------------


@pytest.mark.parametrize(("answer", "expected"), [(True, "ok"), (False, "owed"), (None, "owed")])
def test_config_migration_is_asked_first(
    scope: Scope, tmp_path: Path, answer: bool | None, expected: str
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    ctx = context(scope, answer=answer, report=Report(0, 1))
    assert (
        run_finish(request_file(tmp_path, scope.root, checkout), ctx, no_gateway_restart=True) == 0
    )
    receipt = final_receipt(scope.root)
    assert outcomes(receipt)["config_migration"] == expected
    if expected == "owed":
        assert receipt["owed"][0]["retry"] == "indus config migrate"


def test_bad_request_is_refused(scope: Scope, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema": 99}', encoding="utf-8")
    assert run_finish(bad, context(scope), no_gateway_restart=False) == 1
    assert run_finish(tmp_path / "missing.json", context(scope), no_gateway_restart=False) == 1


# -- the gateway -----------------------------------------------------------------------------------


@dataclass
class Control:
    """A fake control socket and process table for a gateway started by hand."""

    live: dict[str, Any] | None = None
    exits_after: int = 2
    accepts: bool = True
    asked: list[Path] = field(default_factory=list[Path])
    started: int = 0
    stops: list[tuple[int, int | None]] = field(default_factory=list[tuple[int, int | None]])
    polls: int = 0

    def identify(self, _home: Path) -> dict[str, Any] | None:
        return self.live

    def ask_stop(self, home: Path) -> tuple[bool, str]:
        self.asked.append(home)
        return (True, "") if self.accepts else (False, "forbidden")

    def alive(self, _pid: int, _start: int | None) -> bool:
        self.polls += 1
        return self.polls <= self.exits_after

    def start(self) -> str:
        self.started += 1
        return ""

    def record(self, _home: Path, pid: int, start: int | None) -> None:
        self.stops.append((pid, start))


def gateway(home: Path, control: Control, manager: Any = None) -> ProfileGateway:
    return ProfileGateway(
        home=home,
        manager=manager,
        identify=control.identify,
        ask_stop=control.ask_stop,
        alive=control.alive,
        start=control.start,
        record_stop=control.record,
        wait=lambda _s: None,
        gone_wait_s=5.0,
    )


def systemd(scope: Scope, tmp_path: Path) -> tuple[SystemdUser, list[list[str]]]:
    calls: list[list[str]] = []
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool = bin_dir / "systemctl"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    env = {
        "HOME": str(tmp_path / "user"),
        "PATH": str(bin_dir),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
    }
    spec = UnitSpec.build(scope.home, env=env, setting=lambda _k, d: d)

    def runner(args: Sequence[str]) -> Completed:
        calls.append(list(args))
        return Completed(0, "", "")

    manager = SystemdUser(spec, env, runner)
    return manager, calls


def test_systemd_service_is_restarted_through_its_manager(scope: Scope, tmp_path: Path) -> None:
    manager, calls = systemd(scope, tmp_path)
    control = Control(live={"pid": 4242, "start_time": 7})
    profile = gateway(scope.home, control, manager)
    assert profile.describe()["managed_by"] == "manual"  # not installed yet
    manager.path.parent.mkdir(parents=True, exist_ok=True)
    manager.path.write_text(manager.definition(), encoding="utf-8")
    described = profile.describe()
    assert described["managed_by"] == "systemd" and described["running"] is True
    result = profile.restart()
    assert result.outcome == "ok" and "systemd" in result.detail
    assert any("restart" in c for c in calls)
    assert control.stops == [(4242, 7)]  # the planned-stop record comes first
    assert control.asked == [] and control.started == 0  # never via the socket, never killed


def test_manual_gateway_is_asked_over_the_control_socket(scope: Scope) -> None:
    control = Control(live={"pid": 4242, "start_time": 7}, exits_after=3)
    profile = gateway(scope.home, control)
    assert profile.describe()["managed_by"] == "manual"
    result = profile.restart()
    assert result.outcome == "ok" and "4242" in result.detail
    assert control.asked == [scope.home] and control.started == 1


def test_manual_gateway_that_keeps_working_is_owed_not_killed(scope: Scope) -> None:
    control = Control(live={"pid": 4242}, exits_after=10**6)
    result = gateway(scope.home, control).restart()
    assert result.outcome == "owed" and "not killed" in result.detail
    assert control.started == 0
    refused = gateway(scope.home, Control(live={"pid": 1}, accepts=False)).restart()
    assert refused.outcome == "owed" and "forbidden" in refused.detail


def test_no_gateway_running_is_skipped(scope: Scope) -> None:
    result = gateway(scope.home, Control()).restart()
    assert result.outcome == "skipped"


def test_no_gateway_restart_defers(scope: Scope, tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    fake = FakeGateway(RestartResult("ok", "restarted"))
    ctx = context(scope, gateway=fake)
    assert (
        run_finish(request_file(tmp_path, scope.root, checkout), ctx, no_gateway_restart=True) == 0
    )
    assert fake.restarts == 0
    assert outcomes(final_receipt(scope.root))["gateway_restart"] == "deferred"
    # the request can carry the choice too (the apply half's flag)
    fake2 = FakeGateway(RestartResult("ok", "restarted"))
    ctx2 = context(scope, gateway=fake2)
    path = request_file(tmp_path, scope.root, checkout, no_gateway_restart=True)
    assert run_finish(path, ctx2, no_gateway_restart=False) == 0 and fake2.restarts == 0


def test_owed_gateway_restart_is_reported_with_exit_0(scope: Scope, tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    ctx = context(scope, gateway=FakeGateway(RestartResult("owed", "still draining")))
    assert (
        run_finish(request_file(tmp_path, scope.root, checkout), ctx, no_gateway_restart=False) == 0
    )
    receipt = final_receipt(scope.root)
    assert receipt["owed"][0]["followup"] == "gateway_restart"
    assert receipt["owed"][0]["retry"] == "indus gateway restart"
