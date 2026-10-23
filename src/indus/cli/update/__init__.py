"""``indus update``: install kinds, receipts and the shared paths (P23-17..19).

Only a source checkout (a git clone, from the installer or by hand) is moved by ``indus update``.
Every other kind is refused with the one instruction that works for it (I21):

- ``docker``: pull a new image and recreate the container (the image never updates itself);
- ``nix``: Nix owns updates (``nix flake update``, then rebuild; P23-13);
- ``apt``: the Termux APT channel's package (P24-21); Termux's ``pkg`` upgrades it;
- ``desktop``: the desktop app updates its bundled runtime itself (Settings → About);
- ``package``: an installed package has no updater (we publish no wheels, D-16.14);
- ``dev``: a source tree without git history has nothing to move with.

The flow is split by who runs it (R04.24): ``channels`` decides where to go, ``apply`` moves the
checkout and builds the new environment in the *old* process, then execs the new code's
``indus update --finish <request>`` (``finish``) for migration, bundles and the gateway restart.
The request file is the only thing that crosses that seam, as plain JSON.

Every run records a receipt ``<root>/logs/update_receipts/<stamp>.json`` (one row per phase,
outcome ``ok`` / ``failed`` / ``skipped`` / ``owed`` / ``deferred`` / ``interrupted``) and mirrors
what it printed to ``<root>/logs/update.log``. A receipt never fails an update.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import click

from indus.cli.version import INSTALL_STAMP, install_dir, install_method
from indus.foundation.fsio import atomic_write_json

__all__ = [
    "EXIT_AVAILABLE",
    "EXIT_INTERRUPTED",
    "REFUSALS",
    "REQUEST_SCHEMA",
    "Console",
    "InstallKind",
    "Receipt",
    "UpdatePaths",
    "detect_kind",
    "install_id",
    "stamp_of",
]

type InstallKind = Literal["source", "docker", "nix", "apt", "desktop", "package", "dev"]
type Outcome = Literal["ok", "failed", "skipped", "owed", "deferred", "interrupted"]

EXIT_AVAILABLE = 10
"""``--check``: an update is available (0 = up to date or ahead)."""
EXIT_INTERRUPTED = 130
REQUEST_SCHEMA = 1
DESKTOP_ENV = "INDUS_DESKTOP_BUNDLED"
"""Set by the desktop app for the runtime it ships (its updater owns that runtime)."""

REFUSALS: dict[str, str] = {
    "docker": (
        "This is a Docker install: images never update themselves. Pull the new image "
        "(docker compose pull, or docker pull <image>:stable) and recreate the container; "
        "your data volume is kept."
    ),
    "nix": "Nix owns updates: run `nix flake update` and rebuild.",
    "apt": (
        "This copy came from the Termux APT channel, so Termux's package manager updates it: "
        "run `pkg upgrade indus-agent`. The suite named in "
        "$PREFIX/etc/apt/sources.list.d/indus-agent.list (indus-stable or indus-canary) "
        "decides the channel."
    ),
    "desktop": (
        "This runtime belongs to the desktop app, which updates it itself: "
        "use Settings → About → Update in the app."
    ),
    "package": (
        "This copy was installed as a package, which `indus update` cannot move. Reinstall with "
        "the installer (install.sh / install.ps1), Docker or Nix."
    ),
    "dev": (
        "This source tree is not a git checkout, so there is nothing to update it with. "
        "Clone the repository, or reinstall with the installer."
    ),
}

_SOURCE_STAMPS = frozenset({"git", "source", "installer", "sh", "ps1"})
_MANAGED_STAMPS = frozenset({"docker", "nix", "apt", "desktop", "package", "dev"})
"""Stamps that name their kind as is. ``apt`` is written only by the Termux build
(``scripts/termux/build.sh``), so it always means the Termux channel."""


def detect_kind(
    package_dir: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    exists: Callable[[str], bool] = os.path.exists,
) -> InstallKind:
    """How this copy was installed, in the updater's words (see the module notes)."""
    values = env if env is not None else {}
    if values.get(DESKTOP_ENV, "").strip().lower() in {"1", "true", "yes"}:
        return "desktop"
    method = install_method(package_dir, exists=exists)
    root = install_dir(package_dir)
    if method == "docker" and (root / ".git").exists() and not (root / INSTALL_STAMP).exists():
        # A clone inside some container (a dev container) is still a source checkout; our own
        # image carries no git history.
        return "source"
    if method in _SOURCE_STAMPS:
        return "source" if (root / ".git").exists() else "dev"
    if method in _MANAGED_STAMPS:
        return method  # type: ignore[return-value]
    return "package"


def install_id(checkout: Path) -> str:
    """The first 16 hex digits of SHA-256 over the checkout's real path (stable per install)."""
    real = os.path.realpath(checkout)
    return hashlib.sha256(real.encode("utf-8")).hexdigest()[:16]


def stamp_of(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


@dataclass(frozen=True)
class UpdatePaths:
    """Where an update keeps its files. ``root`` is the root home (shared by every profile)."""

    checkout: Path
    root: Path

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def receipts(self) -> Path:
        return self.logs / "update_receipts"

    @property
    def log_file(self) -> Path:
        return self.logs / "update.log"

    @property
    def cache_file(self) -> Path:
        return self.root / "cache" / "update-check.json"

    @property
    def lock_file(self) -> Path:
        return self.root / ".update.lock"

    @property
    def requests(self) -> Path:
        return self.root / "cache" / "update-requests"

    @property
    def venv(self) -> Path:
        return self.checkout / ".venv"

    @property
    def venv_next(self) -> Path:
        return self.checkout / ".venv-next"

    @property
    def venv_prev(self) -> Path:
        return self.checkout / ".venv-prev"


class Console:
    """Prints for the person and appends the same lines (plus child output) to ``update.log``."""

    def __init__(self, log_file: Path | None, *, now: Callable[[], datetime]) -> None:
        self.log_file = log_file
        self._now = now

    def _log(self, text: str) -> None:
        if self.log_file is None:
            return
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            stamp = self._now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(self.log_file, "a", encoding="utf-8") as out:
                for line in text.splitlines() or [""]:
                    out.write(f"{stamp} {line}\n")
        except OSError:
            self.log_file = None  # the log never stops an update

    def say(self, line: str = "") -> None:
        click.echo(line)
        self._log(line)

    def warn(self, line: str) -> None:
        click.echo(line, err=True)
        self._log(line)

    def child_output(self, text: str) -> None:
        """Output of a child (git, uv): logged in full, never printed (failures print a tail)."""
        if text.strip():
            self._log(text.rstrip("\n"))


@dataclass
class Receipt:
    """One update run: what it was asked, each phase's outcome and what is owed afterwards."""

    started_at: str
    install_id: str
    channel: str = ""
    from_commit: str = ""
    to_commit: str = ""
    to_version: str = ""
    phases: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    owed: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    outcome: str = ""
    finished_at: str = ""
    path: Path | None = None

    def phase(self, name: str, outcome: Outcome, detail: str = "") -> None:
        row = {"phase": name, "outcome": outcome}
        if detail:
            row["detail"] = detail
        self.phases.append(row)

    def owe(self, name: str, detail: str, *, retry: str = "") -> None:
        row = {"followup": name, "detail": detail}
        if retry:
            row["retry"] = retry
        self.owed.append(row)
        self.phase(name, "owed", detail)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "install_id": self.install_id,
            "channel": self.channel,
            "from_commit": self.from_commit,
            "to_commit": self.to_commit,
            "to_version": self.to_version,
            "outcome": self.outcome,
            "phases": list(self.phases),
            "owed": list(self.owed),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Receipt:
        receipt = cls(
            started_at=str(data.get("started_at", "")),
            install_id=str(data.get("install_id", "")),
            channel=str(data.get("channel", "")),
            from_commit=str(data.get("from_commit", "")),
            to_commit=str(data.get("to_commit", "")),
            to_version=str(data.get("to_version", "")),
        )
        receipt.phases = [dict(p) for p in data.get("phases", []) if isinstance(p, dict)]
        receipt.owed = [dict(p) for p in data.get("owed", []) if isinstance(p, dict)]
        return receipt

    def write(self, directory: Path, *, finished: datetime, outcome: str) -> Path | None:
        """Write the receipt once (atomic); a failure to write is only a warning."""
        self.outcome = outcome
        self.finished_at = finished.astimezone(UTC).isoformat()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            base = self.started_at.replace(":", "").replace("-", "")[:15] or "update"
            target = directory / f"{base}Z.json"
            counter = 2
            while target.exists() and target != self.path:
                target = directory / f"{base}Z-{counter}.json"
                counter += 1
            self.path = atomic_write_json(target, self.to_json())
        except OSError as exc:
            click.echo(f"⚠ Could not write the update receipt: {exc}", err=True)
            return None
        return self.path


def read_json(path: Path) -> dict[str, Any] | None:
    with contextlib.suppress(OSError, ValueError):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data  # type: ignore[return-value]
    return None
