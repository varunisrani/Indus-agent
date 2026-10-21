"""The operations behind ``indus pm`` (P24-24); the click layer is ``indus.cli.commands.pm``.

Each operation that changes something writes a receipt, ``<root>/pm/receipt.json`` (the last one
only), shaped::

    {"schema": 1, "operation": "install", "started_at": "...", "finished_at": "...",
     "outcome": "ok" | "partial" | "failed", "exit_code": 0, "steps": [{"name", "ok", "detail"}]}

``status`` reads it back. Network work goes through the pm fetcher only (timeouts, resumable
ranges, sha256 checks).
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from indus.foundation.fsio import FileLock, atomic_write_json
from indus.pm.core import INSTALL_LOCK_TIMEOUT_S, Package, Pm
from indus.pm.lock import ANY, SCHEMA, Artifact, Facts, Lock, Pin, PmError
from indus.pm.store import STAGING_PREFIX, tree_digest

__all__ = [
    "AGE_DAYS",
    "DoctorRow",
    "Operation",
    "Step",
    "UpdateRow",
    "UsageProblem",
    "adopt_bundle_lock",
    "apply_updates",
    "bundle",
    "check_updates",
    "default_selection",
    "doctor",
    "env_exports",
    "gc",
    "install",
    "lock_packages",
    "newest_release",
    "operation",
    "read_receipt",
    "repair",
    "status",
]

AGE_DAYS = 14
BUNDLE_FILE = "bundle.json"
GITHUB_RELEASES = "https://api.github.com/repos/{repo}/releases?per_page=50"
NODE_INDEX = "https://nodejs.org/dist/index.json"


class UsageProblem(PmError):
    """The arguments do not make sense (exit 2)."""


@dataclass(frozen=True)
class Step:
    name: str
    ok: bool
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class Operation:
    """One receipt in the making."""

    pm: Pm
    name: str
    started_at: str
    steps: list[Step] = field(default_factory=list[Step])
    exit_code: int = 0

    def add(self, name: str, ok: bool, detail: str = "") -> Step:
        step = Step(name, ok, detail)
        self.steps.append(step)
        return step

    def outcome(self) -> str:
        if self.exit_code == 0:
            return "ok" if all(s.ok for s in self.steps) else "partial"
        return "partial" if any(s.ok for s in self.steps) else "failed"


def _now(pm: Pm) -> str:
    return pm.clock.now().isoformat(timespec="seconds")


@contextmanager
def operation(pm: Pm, name: str) -> Iterator[Operation]:
    """Record a receipt for the work done inside, also when it raises."""
    op = Operation(pm, name, _now(pm))
    try:
        yield op
    except BaseException as exc:
        if isinstance(exc, PmError):
            op.add(name, False, str(exc))
        op.exit_code = op.exit_code or 1
        raise
    finally:
        receipt = {
            "schema": SCHEMA,
            "operation": op.name,
            "started_at": op.started_at,
            "finished_at": _now(pm),
            "outcome": op.outcome(),
            "exit_code": op.exit_code,
            "steps": [s.to_json() for s in op.steps],
        }
        pm.paths.base.mkdir(parents=True, exist_ok=True)
        atomic_write_json(pm.paths.receipt, receipt, mode=0o644)


def read_receipt(pm: Pm) -> dict[str, Any] | None:
    try:
        data = json.loads(pm.paths.receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast(dict[str, Any], data) if isinstance(data, dict) else None


def status(pm: Pm) -> dict[str, Any]:
    """``{"schema": 1, "receipt": <the last receipt or null>}``."""
    return {"schema": SCHEMA, "receipt": read_receipt(pm)}


# -- lock ------------------------------------------------------------------------------------------


def _pin_package(pm: Pm, package: Package, version: str, current: Pin | None) -> Pin:
    """Pins for every target ``package`` ships, reusing a hash when the URL did not change."""
    known: dict[str, str] = {}
    if current is not None:
        known = {a.url: a.sha256 for a in current.artifacts.values()}
    urls = {target: package.url_for(version, target) for target in package.targets}
    hashes: dict[str, str] = {}
    for url in dict.fromkeys(urls.values()):
        hashes[url] = known.get(url) or pm.fetcher().hash_url(url)
    if len(set(urls.values())) == 1:
        url = next(iter(urls.values()))
        return Pin(version, {ANY: Artifact(url, hashes[url])})
    return Pin(version, {t: Artifact(u, hashes[u]) for t, u in urls.items()})


def _write_pins(pm: Pm, pins: dict[str, Pin]) -> None:
    with FileLock(pm.paths.install_lock_ready(), timeout=INSTALL_LOCK_TIMEOUT_S):
        on_disk = pm.paths.lock.exists()
        lock = Lock.read(pm.paths.lock) if on_disk else Lock(dict(pm.lock().packages))
        lock.packages.update(pins)
        lock.write(pm.paths.lock)


def lock_packages(pm: Pm, names: Sequence[str] = ()) -> Operation:
    """Pin our package list (or ``names``) at the definitions' versions."""
    selected = [pm.package(n) for n in names] if names else list(pm.definitions.values())
    with operation(pm, "lock") as op:
        lock = pm.lock()
        pins: dict[str, Pin] = {}
        for package in selected:
            try:
                pin = _pin_package(pm, package, package.version, lock.packages.get(package.name))
            except PmError as exc:
                op.add(f"pin {package.name}", False, str(exc))
                continue
            pins[package.name] = pin
            op.add(f"pin {package.name}", True, f"{pin.version} ({len(pin.artifacts)} targets)")
        if pins:
            _write_pins(pm, pins)
        op.exit_code = 0 if len(pins) == len(selected) else 1
    return op


# -- install ---------------------------------------------------------------------------------------


def default_selection(pm: Pm, facts: Facts, without: Sequence[str] = ()) -> list[Package]:
    """Required packages plus the default ones not declined, that ship for this machine."""
    declined = set(facts.declined) | set(without)
    target = pm.machine_target()
    chosen: list[Package] = []
    for package in pm.definitions.values():
        if not package.ships_for(target):
            continue
        if package.kind == "required" or (
            package.kind == "default" and package.name not in declined
        ):
            chosen.append(package)
    return chosen


def install(
    pm: Pm, names: Sequence[str] = (), *, without: Sequence[str] = ()
) -> tuple[Operation, list[str]]:
    """Install ``names`` (or the default selection). Returns the receipt and warnings for default
    packages that failed (those never fail the run)."""
    if not pm.lock().packages:
        raise PmError("nothing is locked yet", hint="run `indus pm lock`")
    for name in without:
        package = pm.package(name)
        if package.kind != "default":
            raise UsageProblem(
                f"--without takes a default package; {name} is {package.kind}"
                + (" (always installed)" if package.kind == "required" else "")
            )
    if names and without:
        raise UsageProblem("--without shapes the default selection; do not name packages too")
    for name in names:
        pm.package(name)
    warnings: list[str] = []
    with operation(pm, "install") as op:
        if names or without:
            with FileLock(pm.paths.install_lock_ready(), timeout=INSTALL_LOCK_TIMEOUT_S):
                facts = pm.fresh_facts()
                declined = (set(facts.declined) | set(without)) - set(names)
                if declined != set(facts.declined):
                    facts.declined = sorted(declined)
                    pm.save_facts(facts)
        selection = [pm.package(n) for n in names] or default_selection(pm, pm.fresh_facts())
        failed_hard = False
        for package in selection:
            try:
                runner = pm.ensure(package.name)
            except PmError as exc:
                op.add(f"install {package.name}", False, str(exc))
                if package.kind == "default" and not names:
                    warnings.append(
                        f"optional default {package.name} was not installed ({exc.message}); "
                        f"`indus pm install {package.name}` tries again"
                    )
                else:
                    failed_hard = True
                continue
            op.add(f"install {package.name}", True, runner.version)
        op.exit_code = 1 if failed_hard else 0
    return op, warnings


def env_exports(pm: Pm, names: Sequence[str] = ()) -> dict[str, list[str]]:
    """Exports of installed packages only; never the caller's environment."""
    chosen = list(names) or sorted(pm.facts().packages)
    return pm.exports(chosen)


# -- doctor ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DoctorRow:
    name: str
    state: str
    detail: str
    bad: bool


def doctor(pm: Pm) -> list[DoctorRow]:
    """Every locked or installed package: ``ok``, ``not installed``, ``outdated``,
    ``missing executable``, ``tampered``, ``not for this machine``, ``not locked``."""
    lock = pm.lock()
    facts = pm.facts()
    target = pm.machine_target()
    rows: list[DoctorRow] = []
    for name in sorted(set(lock.packages) | set(facts.packages)):
        package = pm.definitions.get(name)
        soft = package is None or package.kind != "required"
        pin = lock.packages.get(name)
        fact = facts.packages.get(name)
        artifact = pin.artifact(target) if pin is not None else None
        if pin is not None and artifact is None:
            rows.append(DoctorRow(name, "not for this machine", target, False))
            continue
        if fact is None:
            rows.append(DoctorRow(name, "not installed", "", not soft))
            continue
        entry = pm.store.entry(fact.entry)
        if not entry.is_dir():
            rows.append(DoctorRow(name, "not installed", f"{fact.entry} is missing", True))
            continue
        if tree_digest(entry) != fact.digest:
            rows.append(DoctorRow(name, "tampered", f"{fact.entry} differs from its digest", True))
            continue
        if fact.exe and not (entry / fact.exe).is_file():
            rows.append(DoctorRow(name, "missing executable", fact.exe, True))
            continue
        if pin is None:
            rows.append(DoctorRow(name, "not locked", fact.version, False))
            continue
        assert artifact is not None
        if (fact.version, fact.sha256) != (pin.version, artifact.sha256):
            detail = f"{fact.version} installed, {pin.version} locked"
            rows.append(DoctorRow(name, "outdated", detail, not soft))
            continue
        rows.append(DoctorRow(name, "ok", fact.version, False))
    return rows


# -- repair ----------------------------------------------------------------------------------------


def repair(pm: Pm) -> Operation:
    """Re-realise recorded packages whose entry is missing, tampered or lacks its executable, at
    their recorded version and hash; the package set and the lock stay as they are."""
    with (
        operation(pm, "repair") as op,
        FileLock(pm.paths.install_lock_ready(), timeout=INSTALL_LOCK_TIMEOUT_S),
    ):
        facts = pm.fresh_facts()
        for name, fact in sorted(facts.packages.items()):
            entry = pm.store.entry(fact.entry)
            broken = (
                not entry.is_dir()
                or tree_digest(entry) != fact.digest
                or (fact.exe is not None and not (entry / fact.exe).is_file())
            )
            if not broken:
                continue
            package = pm.definitions.get(name)
            if package is None:
                op.add(f"repair {name}", False, "no longer a known package")
                continue
            try:
                entry_name, digest = pm.realise(
                    package, fact.version, fact.url, fact.sha256, fact.target
                )
            except PmError as exc:
                op.add(f"repair {name}", False, str(exc))
                continue
            facts.packages[name] = replace(fact, entry=entry_name, digest=digest)
            pm.save_facts(facts)
            op.add(f"repair {name}", True, fact.version)
        op.exit_code = 0 if all(s.ok for s in op.steps) else 1
    return op


# -- gc --------------------------------------------------------------------------------------------


def gc(pm: Pm) -> Operation:
    """Remove store entries, staging folders, partial downloads and archives the facts do not
    reference; everything referenced stays."""
    with (
        operation(pm, "gc") as op,
        FileLock(pm.paths.install_lock_ready(), timeout=INSTALL_LOCK_TIMEOUT_S),
    ):
        facts = pm.fresh_facts()
        keep_entries = facts.entries_in_use()
        keep_blobs = facts.blobs_in_use()
        if pm.paths.store.is_dir():
            for item in sorted(pm.paths.store.iterdir()):
                if item.name in keep_entries:
                    continue
                if item.name.startswith(".") and not item.name.startswith(STAGING_PREFIX):
                    continue
                pm.store.remove(item)
                op.add(f"remove {item.name}", True)
        if pm.paths.downloads.is_dir():
            for item in sorted(pm.paths.downloads.iterdir()):
                if item.name in keep_blobs:
                    continue
                pm.store.remove(item)
                op.add(f"remove downloads/{item.name}", True)
        op.add("kept", True, f"{len(keep_entries)} entries")
    return op


# -- bundle ----------------------------------------------------------------------------------------


def bundle(pm: Pm, out: Path) -> Operation:
    """Stage lock, facts, the recorded store entries and their archives into ``out`` (which must
    be missing or empty) for an offline ``indus pm install --from``."""
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise UsageProblem(f"{out} is not empty; pick a new folder")
    with operation(pm, "bundle") as op:
        facts = pm.fresh_facts()
        if not facts.packages:
            raise PmError("pm has installed nothing to bundle", hint="run `indus pm install`")
        for name, fact in sorted(facts.packages.items()):
            entry = pm.store.entry(fact.entry)
            if not entry.is_dir() or tree_digest(entry) != fact.digest:
                raise PmError(f"{name}: its store entry is damaged", hint="run `indus pm repair`")
        (out / "store").mkdir(parents=True, exist_ok=True)
        (out / "blobs").mkdir(parents=True, exist_ok=True)
        for name, fact in sorted(facts.packages.items()):
            blob = pm.fetcher().fetch(fact.url, fact.sha256)
            shutil.copyfile(blob, out / "blobs" / fact.sha256)
            shutil.copytree(pm.store.entry(fact.entry), out / "store" / fact.entry, symlinks=True)
            op.add(f"bundle {name}", True, fact.version)
        Facts(dict(facts.packages), list(facts.declined)).write(out / "facts.json")
        lock_source = pm.lock_path()
        if lock_source.is_file():
            shutil.copyfile(lock_source, out / "lock.json")
        manifest = {
            "schema": SCHEMA,
            "created_at": _now(pm),
            "target": pm.machine_target(),
            "packages": {
                n: {"version": f.version, "sha256": f.sha256} for n, f in facts.packages.items()
            },
        }
        atomic_write_json(out / BUNDLE_FILE, manifest, sort_keys=True, mode=0o644)
    return op


def adopt_bundle_lock(pm: Pm, folder: Path) -> bool:
    """Use the bundle's lock when this root has none (an offline machine cannot run `lock`)."""
    source = folder / "lock.json"
    if pm.paths.lock.exists() or not source.is_file():
        return False
    lock = Lock.read(source)
    lock.write(pm.paths.lock)
    return True


# -- update ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class UpdateRow:
    name: str
    locked: str | None
    newest: str | None
    reason: str = ""

    @property
    def changed(self) -> bool:
        return self.newest is not None and self.newest != self.locked


def _parse_time(text: object) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp


def newest_release(pm: Pm, package: Package, now: datetime) -> str | None:
    """The newest upstream version released at least ``AGE_DAYS`` before ``now`` (None: the
    package does not move on its own)."""
    cutoff = now - timedelta(days=AGE_DAYS)
    source = package.releases
    if source.startswith("github:"):
        rows = pm.fetcher().get_json(GITHUB_RELEASES.format(repo=source.removeprefix("github:")))
        best: tuple[datetime, str] | None = None
        for raw in cast(list[object], rows) if isinstance(rows, list) else []:
            if not isinstance(raw, dict):
                continue
            row = cast(dict[str, object], raw)
            if row.get("draft") or row.get("prerelease"):
                continue
            tag = row.get("tag_name")
            when = _parse_time(row.get("published_at"))
            if not isinstance(tag, str) or when is None or when > cutoff:
                continue
            if package.tag_prefix and not tag.startswith(package.tag_prefix):
                continue
            version = tag.removeprefix(package.tag_prefix)
            if best is None or when > best[0]:
                best = (when, version)
        return best[1] if best else None
    if source == "node":
        rows = pm.fetcher().get_json(NODE_INDEX)
        best = None
        for raw in cast(list[object], rows) if isinstance(rows, list) else []:
            if not isinstance(raw, dict):
                continue
            row = cast(dict[str, object], raw)
            version, lts = row.get("version"), row.get("lts")
            when = _parse_time(row.get("date"))
            if not isinstance(version, str) or not lts or when is None or when > cutoff:
                continue
            if best is None or when > best[0]:
                best = (when, version.removeprefix("v"))
        return best[1] if best else None
    return None


def check_updates(pm: Pm, names: Sequence[str] = ()) -> list[UpdateRow]:
    """The newest release (at least ``AGE_DAYS`` old) of each locked package, or of ``names``.
    Reads upstream listings only; writes nothing."""
    lock = pm.lock()
    if not lock.packages:
        raise PmError("nothing is locked yet", hint="run `indus pm lock`")
    selected = (
        [pm.package(n) for n in names]
        if names
        else [pm.package(n) for n in sorted(lock.packages) if n in pm.definitions]
    )
    now = pm.clock.now()
    rows: list[UpdateRow] = []
    for package in selected:
        locked = lock.packages.get(package.name)
        current = locked.version if locked else None
        try:
            newest = newest_release(pm, package, now)
        except PmError as exc:
            rows.append(UpdateRow(package.name, current, None, f"lookup failed: {exc.message}"))
            continue
        reason = "" if newest is not None else "moves only with the product"
        rows.append(UpdateRow(package.name, current, newest, reason))
    return rows


def apply_updates(pm: Pm, rows: Sequence[UpdateRow]) -> Operation:
    """Re-pin the changed rows and reinstall the ones that are installed."""
    lock = pm.lock()
    with operation(pm, "update") as op:
        pins: dict[str, Pin] = {}
        for row in rows:
            if not row.changed or row.newest is None:
                continue
            package = pm.package(row.name)
            try:
                pins[row.name] = _pin_package(pm, package, row.newest, lock.packages.get(row.name))
            except PmError as exc:
                op.add(f"pin {row.name}", False, str(exc))
                continue
            op.add(f"pin {row.name}", True, f"{row.locked or '-'} -> {row.newest}")
        if pins:
            _write_pins(pm, pins)
        installed = pm.fresh_facts().packages
        for name in pins:
            if name not in installed:
                continue
            try:
                pm.ensure(name)
                op.add(f"install {name}", True, pins[name].version)
            except PmError as exc:
                op.add(f"install {name}", False, str(exc))
        op.exit_code = 0 if all(s.ok for s in op.steps) else 1
    return op
