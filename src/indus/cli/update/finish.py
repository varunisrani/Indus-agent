"""Finish an update in the new code: migrate, rebuild bundles and pages, restart the gateway
(P23-19).

``indus update --finish <request>`` is exec'd by the apply step (``apply.py``) from the new
environment, so everything here is the new release's own code. The checkout and environment have
already moved; nothing below can undo that, so every failure from here on is an *owed follow-up*:
reported, written to the receipt with how to retry, and the run still exits 0 (R04.24). The only
non-zero exit is the apply step's stash conflict (1): local edits that did not go back on top.

Phases:

- ``config_migration``: this profile's settings file brought to the current stamp; asked first
  (``-y`` accepts); without a terminal and without ``-y`` it is owed (``indus config migrate``);
- ``dist:<name>``: the web and TUI bundles a checkout already had are rebuilt by building into
  ``<dist>.next``, checking the entry file is there and renaming it over the old one; a failure
  keeps the old bundle;
- ``web_pages`` (wave 47): a checkout without its own ``apps/web/dist`` (a script install) gets the
  new release's prebuilt dashboard pages, as the installer's products stage does: ``<new python>
  -m indus.web.bundle fetch --repo <checkout> --target <checkout>/src/indus/web/static`` as a child
  process (the CLI layer never imports ``indus.web``, D-16.246). Exit 0 is ``ok``, 3 (HEAD on no
  release) ``skipped``, anything else owed with the same command as its retry; the pages left
  behind are then stamped with the old release, which ``web.bundle.locate`` treats as stale;
- ``gateway_restart``: this profile's gateway only (D-16.57). A systemd/launchd service is
  restarted through its manager; a gateway started by hand is asked over the control socket to
  stop for a restart (never killed), then started again detached on the new code;
  ``--no-gateway-restart`` defers it.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from indus.cli.update import REQUEST_SCHEMA, Console, Receipt, UpdatePaths, read_json
from indus.foundation.scope import Scope

__all__ = [
    "DistBuilder",
    "FinishContext",
    "GatewayRestarter",
    "RestartResult",
    "default_builders",
    "load_request",
    "run_finish",
    "stage_and_swap",
]

_GATEWAY_GONE_WAIT_S = 60.0
_GATEWAY_START_TIMEOUT_S = 90.0
_BUILD_TIMEOUT_S = 900.0
_FETCH_TIMEOUT_S = 300.0
_NOT_ON_RELEASE = 3
WEB_PAGES_PHASE = "web_pages"

type ChildRunner = Callable[..., subprocess.CompletedProcess[str]]


def load_request(path: Path) -> dict[str, Any] | None:
    """The apply step's request, or None when it is missing, unreadable or another schema."""
    data = read_json(path)
    if data is None or data.get("schema") != REQUEST_SCHEMA:
        return None
    for key in ("checkout", "root", "to_commit", "receipt"):
        if key not in data:
            return None
    return data


# -- bundles ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DistBuilder:
    """One bundle of a checkout: where it lives, which file proves a build, how to build it."""

    name: str
    directory: str
    entry: str
    command: Callable[[Path], list[str]]
    """``staging`` → argv run from the checkout root."""

    def target(self, checkout: Path) -> Path:
        return checkout / self.directory

    def present(self, checkout: Path) -> bool:
        return (self.target(checkout) / self.entry).is_file()


def default_builders(pnpm: str | None) -> list[DistBuilder]:
    """The web dashboard and TUI bundles (built with pnpm when a checkout has them)."""
    tool = pnpm or "pnpm"
    return [
        DistBuilder(
            "web",
            "apps/web/dist",
            "index.html",
            lambda staging: [
                tool, "--dir", "apps/web", "exec", "vite", "build",
                "--outDir", str(staging), "--emptyOutDir", "--logLevel", "warn",
            ],
        ),
        DistBuilder(
            "tui",
            "apps/tui/dist",
            "entry.js",
            lambda staging: [
                tool, "--dir", "apps/tui", "exec", "vite", "build", "--ssr", "src/entry.tsx",
                "--outDir", str(staging), "--emptyOutDir", "--logLevel", "warn",
            ],
        ),
    ]  # fmt: skip


def stage_and_swap(
    builder: DistBuilder,
    checkout: Path,
    *,
    run: ChildRunner,
    env: Mapping[str, str],
    log: Callable[[str], None],
) -> str:
    """Build into ``<dist>.next`` and rename it over ``<dist>``; '' on success, else why not.
    The old bundle stays in place on every failure."""
    target = builder.target(checkout)
    staging = target.with_name(target.name + ".next")
    previous = target.with_name(target.name + ".prev")
    shutil.rmtree(staging, ignore_errors=True)
    argv = builder.command(staging)
    try:
        done = run(
            argv,
            cwd=str(checkout),
            env=dict(env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_BUILD_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return f"could not run the build: {exc}"
    log(f"$ {' '.join(argv)}\n{done.stdout}{done.stderr}")
    if done.returncode != 0 or not (staging / builder.entry).is_file():
        shutil.rmtree(staging, ignore_errors=True)
        lines = (done.stderr or done.stdout or "").strip().splitlines()
        return lines[-1] if lines else f"the build produced no {builder.entry}"
    try:
        shutil.rmtree(previous, ignore_errors=True)
        if target.exists():
            os.replace(target, previous)
        os.replace(staging, target)
    except OSError as exc:
        with contextlib.suppress(OSError):
            if not target.exists() and previous.exists():
                os.replace(previous, target)
        shutil.rmtree(staging, ignore_errors=True)
        return f"could not put the new bundle in place: {exc}"
    shutil.rmtree(previous, ignore_errors=True)
    return ""


# -- the gateway -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class RestartResult:
    outcome: str  # ok | skipped | deferred | owed
    detail: str


class GatewayRestarter(Protocol):
    def describe(self) -> dict[str, Any]: ...

    def restart(self) -> RestartResult: ...


class _Manager(Protocol):
    kind: str
    tool: str

    def available(self) -> bool: ...

    def installed(self) -> bool: ...

    def restart(self) -> None: ...


@dataclass
class ProfileGateway:
    """This profile's gateway: its service (if installed) or a gateway started by hand."""

    home: Path
    manager: _Manager | None
    identify: Callable[[Path], dict[str, Any] | None]
    ask_stop: Callable[[Path], tuple[bool, str]]
    """Control-socket ``stop {restart: true, reason: update}`` → (asked, error)."""
    alive: Callable[[int, int | None], bool]
    start: Callable[[], str]
    """Start a detached gateway on the new code; '' on success, else why not."""
    record_stop: Callable[[Path, int, int | None], None]
    wait: Callable[[float], object]
    gone_wait_s: float = _GATEWAY_GONE_WAIT_S

    def _service(self) -> _Manager | None:
        if self.manager is not None and self.manager.installed():
            return self.manager
        return None

    def describe(self) -> dict[str, Any]:
        live = self.identify(self.home)
        service = self._service()
        if service is not None:
            how, managed = f"{service.kind} restart", service.kind
        elif live is not None:
            how, managed = "asked to restart over the control socket", "manual"
        else:
            how, managed = "not running", "none"
        return {
            "kind": "gateway",
            "profile_home": str(self.home),
            "running": live is not None,
            "pid": None if live is None else live.get("pid"),
            "managed_by": managed,
            "restart": how,
        }

    def restart(self) -> RestartResult:
        live = self.identify(self.home)
        service = self._service()
        if service is not None:
            if not service.available():
                return RestartResult(
                    "owed", f"the {service.kind} service is installed but {service.tool} is missing"
                )
            if live is not None:
                pid = int(live["pid"])
                start_time = live.get("start_time")
                self.record_stop(
                    self.home, pid, start_time if isinstance(start_time, int) else None
                )
            try:
                service.restart()
            except Exception as exc:  # the code is committed: a failed restart is owed
                return RestartResult("owed", f"{service.kind} restart failed: {exc}")
            return RestartResult("ok", f"restarted through {service.kind}")
        if live is None:
            return RestartResult("skipped", "no gateway is running for this profile")
        pid = int(live["pid"])
        start_time = live.get("start_time")
        asked, error = self.ask_stop(self.home)
        if not asked:
            return RestartResult(
                "owed", f"the gateway (pid {pid}) did not take the request: {error}"
            )
        waited = 0.0
        while self.alive(pid, start_time if isinstance(start_time, int) else None):
            if waited >= self.gone_wait_s:
                return RestartResult(
                    "owed",
                    f"asked the gateway (pid {pid}) to restart; it is still finishing its work "
                    "(it was not killed)",
                )
            self.wait(0.5)
            waited += 0.5
        failed = self.start()
        if failed:
            return RestartResult("owed", f"the gateway stopped but did not start again: {failed}")
        return RestartResult("ok", f"asked pid {pid} to restart; started again on the new code")


# -- the finish step -------------------------------------------------------------------------------


@dataclass
class FinishContext:
    scope: Scope
    console: Console
    gateway: GatewayRestarter
    builders: Sequence[DistBuilder]
    now: Callable[[], datetime]
    confirm: Callable[[str], bool | None]
    """Ask a yes/no question; None when there is no terminal (and no ``-y``)."""
    migrate: Callable[[bool], Any]
    """``dry_run`` → a ``MigrationReport`` (``from_version``, ``to_version``, ``steps``)."""
    run: ChildRunner = subprocess.run
    pnpm: str | None = None
    python: str = sys.executable
    """The new environment's interpreter (this process: the apply step exec'd it)."""
    extra_env: dict[str, str] = field(default_factory=dict[str, str])


def _migrate(ctx: FinishContext, receipt: Receipt) -> None:
    try:
        report = ctx.migrate(True)
    except Exception as exc:
        receipt.owe("config_migration", str(exc), retry="indus config migrate")
        ctx.console.warn(f"⚠ Config migration could not run: {exc}")
        return
    if report.from_version >= report.to_version and not report.steps:
        receipt.phase("config_migration", "skipped", "settings are current")
        return
    answer = ctx.confirm(
        f"Bring this profile's settings from version {report.from_version} to {report.to_version}?"
    )
    if answer is None:
        receipt.owe("config_migration", "not asked (no terminal)", retry="indus config migrate")
        return
    if not answer:
        receipt.owe("config_migration", "declined", retry="indus config migrate")
        return
    try:
        done = ctx.migrate(False)
    except Exception as exc:
        receipt.owe("config_migration", str(exc), retry="indus config migrate")
        ctx.console.warn(f"⚠ Config migration failed: {exc}")
        return
    receipt.phase("config_migration", "ok", getattr(done, "message", ""))


def _bundles(ctx: FinishContext, checkout: Path, receipt: Receipt) -> None:
    for builder in ctx.builders:
        phase = f"dist:{builder.name}"
        if not builder.present(checkout):
            receipt.phase(phase, "skipped", "not built in this checkout")
            continue
        if ctx.pnpm is None:
            receipt.owe(phase, "pnpm was not found", retry="rebuild after installing pnpm")
            ctx.console.warn(f"⚠ The {builder.name} bundle was not rebuilt: pnpm is missing.")
            continue
        ctx.console.say(f"→ Rebuilding the {builder.name} bundle...")
        env = {**ctx.scope.env.to_dict(), **ctx.extra_env}
        failed = stage_and_swap(
            builder, checkout, run=ctx.run, env=env, log=ctx.console.child_output
        )
        if failed:
            ctx.console.warn(
                f"⚠ The {builder.name} bundle was not rebuilt ({failed}); the previous one is kept."
            )
            receipt.owe(phase, failed, retry="the next `indus update` rebuilds it")
        else:
            receipt.phase(phase, "ok", "rebuilt")


def _last_line(*texts: str | None) -> str:
    for text in texts:
        lines = (text or "").strip().splitlines()
        if lines:
            return lines[-1].strip()
    return ""


def _web_pages(ctx: FinishContext, checkout: Path, receipt: Receipt) -> None:
    """Refresh the prebuilt dashboard pages of a script install (see the module notes)."""
    if (checkout / "apps" / "web" / "dist" / "index.html").is_file():
        receipt.phase(WEB_PAGES_PHASE, "skipped", "this checkout builds its own pages")
        return
    target = checkout / "src" / "indus" / "web" / "static"
    base = [ctx.python, "-m", "indus.web.bundle", "fetch", "--repo", str(checkout)]
    retry = " ".join(base)
    ctx.console.say("→ Fetching the dashboard pages of this release...")
    env = {**ctx.scope.env.to_dict(), **ctx.extra_env}
    try:
        done = ctx.run(
            [*base, "--target", str(target)],
            cwd=str(checkout),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_FETCH_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        receipt.owe(WEB_PAGES_PHASE, f"could not run the fetch: {exc}", retry=retry)
        ctx.console.warn(f"⚠ The dashboard pages were not refreshed: {exc}")
        return
    ctx.console.child_output(f"$ {' '.join(done.args)}\n{done.stdout}{done.stderr}")
    if done.returncode == 0:
        receipt.phase(WEB_PAGES_PHASE, "ok", _last_line(done.stdout))
    elif done.returncode == _NOT_ON_RELEASE:
        receipt.phase(WEB_PAGES_PHASE, "skipped", "this checkout is not on a release")
    else:
        why = _last_line(done.stderr, done.stdout) or f"exit {done.returncode}"
        receipt.owe(WEB_PAGES_PHASE, why, retry=retry)
        ctx.console.warn(f"⚠ The dashboard pages were not refreshed ({why}).")


def run_finish(request_path: Path, ctx: FinishContext, *, no_gateway_restart: bool) -> int:
    """The finish step (see the module notes). Exit 0, or 1 for a stash conflict."""
    data = load_request(request_path)
    if data is None:
        ctx.console.warn(f"✗ {request_path} is not an update request this version understands.")
        return 1
    with contextlib.suppress(OSError):
        request_path.unlink()
    paths = UpdatePaths(Path(str(data["checkout"])), Path(str(data["root"])))
    receipt = Receipt.from_json(data["receipt"])
    defer = no_gateway_restart or bool(data.get("no_gateway_restart"))
    _migrate(ctx, receipt)
    _bundles(ctx, paths.checkout, receipt)
    _web_pages(ctx, paths.checkout, receipt)
    if defer:
        receipt.phase(
            "gateway_restart", "deferred", "--no-gateway-restart: run `indus gateway restart`"
        )
    else:
        result = ctx.gateway.restart()
        if result.outcome == "owed":
            receipt.owe("gateway_restart", result.detail, retry="indus gateway restart")
            ctx.console.warn(f"⚠ Gateway: {result.detail}")
        else:
            receipt.phase("gateway_restart", result.outcome, result.detail)  # type: ignore[arg-type]
            if result.outcome == "ok":
                ctx.console.say(f"✓ Gateway: {result.detail}")
    with contextlib.suppress(OSError):
        paths.cache_file.unlink()
    conflict = data.get("stash_conflict")
    outcome = "ok_with_followups" if receipt.owed else "ok"
    if conflict:
        outcome = "local_changes_not_restored"
    written = receipt.write(paths.receipts, finished=ctx.now(), outcome=outcome)
    version = data.get("to_version") or str(data["to_commit"])[:10]
    ctx.console.say(f"✓ Updated to {version}.")
    for row in receipt.owed:
        ctx.console.say(f"  Owed: {row['followup']} — {row['detail']}")
    if written is not None:
        ctx.console.say(f"  Receipt: {written}")
    if conflict:
        ctx.console.warn(
            f"✗ Your local changes did not apply on top of the new code. They are kept in the "
            f"stash as {conflict}: git -C {paths.checkout} stash list"
        )
        return 1
    return 0


def interpreter() -> str:
    return sys.executable
