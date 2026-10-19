"""Hub state on disk: the install lock, the taps list, the audit log and the shared path checks.

Everything lives under ``<home>/skills/.hub/`` (a dot folder, so the skill scanner never walks it):

- ``lock.json`` — ``{"version": 1, "installed": {name: entry}}``. One entry per hub-installed
  skill: ``name, source, identifier, install_path`` (relative to ``<home>/skills``, posix),
  ``content_hash`` (the installed tree), ``upstream_hash`` (the bundle as fetched), ``trust``
  (``builtin`` | ``trusted`` | ``community``), ``verdict``, ``scanner_version``, ``findings``,
  ``files`` and ``ts``. P09 readers (usage telemetry, curator ownership, inline-shell gate) read it
  through ``hub_names`` and ``trust_of`` here.
- ``taps.json`` — ``{"taps": [{repo, path, trust}]}``.
- ``audit.log`` — one space-separated line per install, block, update and removal.
- ``quarantine/`` — fetched bundles waiting for their scan.

This module imports nothing from the rest of the hub so the P09 modules can use it cheaply.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from indus.foundation.fsio import FileLock, atomic_write_json

__all__ = [
    "AUDIT_FILE",
    "HUB_DIR",
    "LOCK_FILE",
    "QUARANTINE_DIR",
    "TAPS_FILE",
    "TRUST_LEVELS",
    "HubLock",
    "Taps",
    "Trust",
    "append_audit",
    "bundle_hash",
    "hub_dir",
    "hub_names",
    "is_redirect",
    "safe_install_path",
    "safe_rel_path",
    "safe_skill_name",
    "trust_of",
    "tree_hash",
]

log = logging.getLogger(__name__)

HUB_DIR = ".hub"
LOCK_FILE = "lock.json"
TAPS_FILE = "taps.json"
AUDIT_FILE = "audit.log"
QUARANTINE_DIR = "quarantine"
LOCK_VERSION = 1
STATE_LOCK_TIMEOUT_S = 10.0

type Trust = Literal["builtin", "trusted", "community"]
TRUST_LEVELS: tuple[Trust, ...] = ("builtin", "trusted", "community")


def hub_dir(skills_root: Path) -> Path:
    return skills_root / HUB_DIR


# -- path checks --------------------------------------------------------------------------------


def safe_rel_path(raw: str, *, nested: bool = True, what: str = "path") -> str:
    """``raw`` as a clean relative posix path, or ``ValueError``.

    Refused: empty, absolute, ``..`` anywhere, a backslash-only trick, a ``:`` in any part (drive
    letters and alternate data streams) and, with ``nested=False``, more than one part.
    """
    if not isinstance(raw, str) or not raw.strip():  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError(f"unsafe {what}: empty")
    text = raw.strip().replace("\\", "/")
    if text.startswith("/"):
        raise ValueError(f"unsafe {what}: {raw!r} is absolute")
    parts = [p for p in PurePosixPath(text).parts if p not in ("", ".")]
    if not parts:
        raise ValueError(f"unsafe {what}: {raw!r}")
    if any(p == ".." for p in parts):
        raise ValueError(f"unsafe {what}: {raw!r} leaves its folder")
    if any(":" in p for p in parts):
        raise ValueError(f"unsafe {what}: {raw!r} has a ':'")
    if not nested and len(parts) != 1:
        raise ValueError(f"unsafe {what}: {raw!r} must be a single name")
    return "/".join(parts)


def safe_skill_name(raw: str) -> str:
    return safe_rel_path(raw, nested=False, what="skill name")


def safe_install_path(install_path: str, name: str) -> str:
    """A lock ``install_path``: relative, clean, and ending in the skill's own name."""
    clean = safe_rel_path(install_path, what="install path")
    if clean.rsplit("/", 1)[-1] != safe_skill_name(name):
        raise ValueError(f"unsafe install path: {install_path!r} does not end in {name!r}")
    return clean


def is_redirect(path: Path) -> bool:
    """A symlink, or a Windows junction: either could aim a later delete outside the root."""
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction is not None and junction())


# -- hashing ------------------------------------------------------------------------------------


def bundle_hash(files: Mapping[str, str | bytes]) -> str:
    """One hash for a set of files: sorted posix paths, each followed by its bytes."""
    digest = hashlib.sha256()
    normalized = {key.replace("\\", "/"): value for key, value in files.items()}
    for rel in sorted(normalized):
        content = normalized[rel]
        data = content.encode("utf-8") if isinstance(content, str) else content
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
    return "sha256:" + digest.hexdigest()


def tree_hash(folder: Path) -> str:
    """``bundle_hash`` of every regular file under ``folder`` (symlinks are not followed)."""
    files: dict[str, bytes] = {}
    for current, dirs, names in os.walk(folder, followlinks=False):
        dirs.sort()
        here = Path(current)
        for name in sorted(names):
            path = here / name
            if path.is_symlink() or not path.is_file():
                continue
            files[path.relative_to(folder).as_posix()] = path.read_bytes()
    return bundle_hash(files)


# -- JSON state files ---------------------------------------------------------------------------


class _StateFile:
    """A JSON file under the hub folder, rewritten atomically under a cross-process lock."""

    name = ""

    def __init__(self, skills_root: Path) -> None:
        self.skills_root = skills_root
        self.path = hub_dir(skills_root) / self.name

    def _empty(self) -> dict[str, Any]:
        raise NotImplementedError

    def read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty()
        except (OSError, ValueError):
            log.warning("%s is unreadable; treating it as empty", self.path)
            return self._empty()
        return cast(dict[str, Any], data) if isinstance(data, dict) else self._empty()

    def _update(self, change: Callable[[dict[str, Any]], bool]) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.path.with_name(self.path.name + ".lock"), timeout=STATE_LOCK_TIMEOUT_S):
            data = self.read()
            changed = change(data)
            if changed:
                atomic_write_json(self.path, data)
            return changed


class HubLock(_StateFile):
    """``.hub/lock.json``: what the hub installed, from where, and what the scan said."""

    name = LOCK_FILE

    def _empty(self) -> dict[str, Any]:
        return {"version": LOCK_VERSION, "installed": {}}

    def installed(self) -> dict[str, dict[str, Any]]:
        rows = self.read().get("installed")
        if not isinstance(rows, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for key, value in cast(dict[str, Any], rows).items():
            if isinstance(value, dict):
                out[str(key)] = {**cast(dict[str, Any], value), "name": str(key)}
        return out

    def get(self, name: str) -> dict[str, Any] | None:
        return self.installed().get(name)

    def record(self, entry: Mapping[str, Any]) -> None:
        name = safe_skill_name(str(entry["name"]))
        row = dict(entry)
        row["name"] = name
        row["install_path"] = safe_install_path(str(entry["install_path"]), name)

        def put(data: dict[str, Any]) -> bool:
            data["version"] = LOCK_VERSION
            installed = data.get("installed")
            if not isinstance(installed, dict):
                installed = data["installed"] = {}
            cast(dict[str, Any], installed)[name] = row
            return True

        self._update(put)

    def forget(self, name: str) -> bool:
        def drop(data: dict[str, Any]) -> bool:
            installed = data.get("installed")
            if not isinstance(installed, dict) or name not in installed:
                return False
            del cast(dict[str, Any], installed)[name]
            return True

        return self._update(drop)

    def install_dir(self, entry: Mapping[str, Any]) -> Path:
        """The folder an entry names, refusing anything that could escape ``<home>/skills``.

        Every part on the way is checked for a symlink/junction before resolving, and the skills
        root itself is never an answer (deleting it would take every skill with it).
        """
        rel = safe_install_path(str(entry.get("install_path", "")), str(entry.get("name", "")))
        target = self.skills_root
        for part in rel.split("/"):
            target = target / part
            if is_redirect(target):
                raise ValueError(f"unsafe install path: {rel!r} passes through a link")
        root = self.skills_root.resolve()
        resolved = target.resolve()
        if resolved == root or not resolved.is_relative_to(root):
            raise ValueError(f"unsafe install path: {rel!r}")
        return resolved


def hub_names(skills_root: Path) -> set[str]:
    """Names the hub installed into ``skills_root`` (empty when there is no lock)."""
    return set(HubLock(skills_root).installed())


def trust_of(skills_root: Path, name: str) -> Trust | None:
    """The trust a hub skill was installed with; ``community`` for a row without a known one,
    ``None`` when the hub did not install ``name``."""
    entry = HubLock(skills_root).get(name)
    if entry is None:
        return None
    trust = entry.get("trust")
    return cast(Trust, trust) if trust in TRUST_LEVELS else "community"


class Taps(_StateFile):
    """``.hub/taps.json``: extra GitHub repos searched for skills."""

    name = TAPS_FILE

    def _empty(self) -> dict[str, Any]:
        return {"taps": []}

    def list(self) -> list[dict[str, str]]:
        rows = self.read().get("taps")
        out: list[dict[str, str]] = []
        for row in cast(list[Any], rows) if isinstance(rows, list) else []:
            if isinstance(row, dict) and isinstance(row.get("repo"), str):  # pyright: ignore[reportUnknownMemberType]
                item = cast(dict[str, Any], row)
                trust = item.get("trust")
                out.append(
                    {
                        "repo": str(item["repo"]),
                        "path": str(item.get("path") or "skills/"),
                        "trust": trust if trust in TRUST_LEVELS else "community",
                    }
                )
        return out

    def add(self, repo: str, path: str = "skills/") -> bool:
        def put(data: dict[str, Any]) -> bool:
            rows = data.get("taps")
            if not isinstance(rows, list):
                rows = data["taps"] = []
            listed = cast(list[Any], rows)
            if any(isinstance(r, dict) and r.get("repo") == repo for r in listed):  # pyright: ignore[reportUnknownMemberType]
                return False
            listed.append({"repo": repo, "path": path, "trust": "community"})
            return True

        return self._update(put)

    def remove(self, repo: str) -> bool:
        def drop(data: dict[str, Any]) -> bool:
            rows = data.get("taps")
            listed = cast(list[Any], rows) if isinstance(rows, list) else []
            kept = [r for r in listed if not (isinstance(r, dict) and r.get("repo") == repo)]  # pyright: ignore[reportUnknownMemberType]
            data["taps"] = kept
            return len(kept) != len(listed)

        return self._update(drop)

    def set_trust(self, repo: str, trust: Trust) -> bool:
        def change(data: dict[str, Any]) -> bool:
            rows = data.get("taps")
            for row in cast(list[Any], rows) if isinstance(rows, list) else []:
                if isinstance(row, dict) and row.get("repo") == repo:  # pyright: ignore[reportUnknownMemberType]
                    cast(dict[str, Any], row)["trust"] = trust
                    return True
            return False

        return self._update(change)


def append_audit(
    skills_root: Path,
    action: str,
    name: str,
    *,
    source: str,
    trust: str,
    verdict: str,
    detail: str = "",
    now: datetime | None = None,
) -> None:
    """One line in ``.hub/audit.log``; a failure is logged, never raised."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    fields: Iterable[str] = (stamp, action, name, f"{source}:{trust}", verdict, detail)
    line = " ".join(f.replace("\n", " ") for f in fields if f) + "\n"
    path = hub_dir(skills_root) / AUDIT_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        log.warning("could not write the hub audit log at %s", path, exc_info=True)
