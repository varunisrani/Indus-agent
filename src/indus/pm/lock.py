"""The pm lock and facts files (P24-23).

``lock.json`` (machine-written by ``indus pm lock`` / ``update``) pins every package::

    {"schema": 1, "packages": {name: {"version": "...",
                                      "artifacts": {target: {"url": "...", "sha256": "..."}}}}}

A package's ``any`` row serves every target without a row of its own.

``facts.json`` records what this install actually holds::

    {"schema": 1, "declined": [...],
     "packages": {name: {"version", "target", "url", "sha256", "entry", "digest", "exe",
                         "path", "installed_at"}}}

``entry`` is the store folder name, ``digest`` the content digest of that folder when it was
published, ``exe`` and ``path`` are relative to the entry (so a store copied to another root still
works). A facts file that cannot be read is treated as empty and its bytes are kept beside it as
``facts.json.corrupt`` before anything overwrites it; a lock that cannot be read is an error.

``StatCache`` keeps a parsed file until its stat signature changes, so a satisfied ``ensure``
costs a ``stat`` per file and no read.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from indus.foundation.fsio import atomic_write_json

__all__ = [
    "ANY",
    "SCHEMA",
    "TARGETS",
    "Artifact",
    "Fact",
    "Facts",
    "Lock",
    "LockInvalid",
    "Pin",
    "PmError",
    "StatCache",
    "valid_sha256",
]

SCHEMA = 1
ANY = "any"
TARGETS: tuple[str, ...] = (
    ANY,
    "darwin-arm64",
    "darwin-x64",
    "linux-arm64",
    "linux-arm64-bionic",
    "linux-arm64-musl",
    "linux-x64",
    "linux-x64-musl",
    "win32-arm64",
    "win32-x64",
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


class PmError(Exception):
    """A pm operation that cannot go on; ``hint`` says what to run next."""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return f"{self.message} ({self.hint})" if self.hint else self.message


class LockInvalid(PmError):
    """The lock file exists but is not a lock this build understands."""


def valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


# -- lock ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Artifact:
    url: str
    sha256: str

    def to_json(self) -> dict[str, str]:
        return {"url": self.url, "sha256": self.sha256}


@dataclass(frozen=True)
class Pin:
    version: str
    artifacts: Mapping[str, Artifact]

    def artifact(self, target: str) -> Artifact | None:
        """This target's artifact, else the package's ``any`` artifact, else None."""
        found = self.artifacts.get(target)
        return found if found is not None else self.artifacts.get(ANY)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "artifacts": {t: a.to_json() for t, a in sorted(self.artifacts.items())},
        }


def _pin_from(name: str, raw: object, where: Path) -> Pin:
    def bad(why: str) -> LockInvalid:
        return LockInvalid(
            f"{where}: package {name!r} {why}", hint="run `indus pm lock` to write it again"
        )

    if not isinstance(raw, Mapping):
        raise bad("is not an object")
    row = cast(Mapping[str, object], raw)
    version = row.get("version")
    artifacts = row.get("artifacts")
    if not isinstance(version, str) or not version:
        raise bad("has no version")
    if not isinstance(artifacts, Mapping):
        raise bad("has no artifacts")
    pins: dict[str, Artifact] = {}
    for target, value in cast(Mapping[object, object], artifacts).items():
        if target not in TARGETS:
            raise bad(f"names an unknown target {target!r}")
        if not isinstance(value, Mapping):
            raise bad(f"has a broken {target} artifact")
        art = cast(Mapping[str, object], value)
        url, sha = art.get("url"), art.get("sha256")
        if not isinstance(url, str) or not url or not valid_sha256(sha):
            raise bad(f"has a broken {target} artifact (url and a full sha256 are needed)")
        pins[str(target)] = Artifact(url, cast(str, sha))
    return Pin(version, pins)


@dataclass
class Lock:
    packages: dict[str, Pin] = field(default_factory=dict[str, Pin])

    @classmethod
    def from_json(cls, data: object, where: Path) -> Lock:
        if not isinstance(data, Mapping):
            raise LockInvalid(f"{where} is not a pm lock", hint="run `indus pm lock`")
        top = cast(Mapping[str, object], data)
        if top.get("schema") != SCHEMA:
            raise LockInvalid(
                f"{where} has lock schema {top.get('schema')!r}; this build reads {SCHEMA}",
                hint="run `indus pm lock`",
            )
        packages = top.get("packages")
        if not isinstance(packages, Mapping):
            raise LockInvalid(f"{where} has no packages table", hint="run `indus pm lock`")
        rows = cast(Mapping[object, object], packages)
        return cls({str(n): _pin_from(str(n), raw, where) for n, raw in rows.items()})

    @classmethod
    def read(cls, path: Path) -> Lock:
        """The lock at ``path``; an empty lock when the file does not exist."""
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls()
        except OSError as exc:
            raise LockInvalid(f"{path} cannot be read: {exc.strerror or exc}") from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise LockInvalid(f"{path} is not valid JSON", hint="run `indus pm lock`") from exc
        return cls.from_json(data, path)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "packages": {n: p.to_json() for n, p in sorted(self.packages.items())},
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, self.to_json(), sort_keys=True, mode=0o644)


# -- facts -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Fact:
    version: str
    target: str
    url: str
    sha256: str
    entry: str
    digest: str
    exe: str | None
    path: tuple[str, ...]
    installed_at: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "target": self.target,
            "url": self.url,
            "sha256": self.sha256,
            "entry": self.entry,
            "digest": self.digest,
            "exe": self.exe,
            "path": list(self.path),
            "installed_at": self.installed_at,
        }

    @classmethod
    def from_json(cls, raw: object) -> Fact | None:
        if not isinstance(raw, Mapping):
            return None
        row = cast(Mapping[str, object], raw)
        texts = {k: row.get(k) for k in ("version", "target", "url", "sha256", "entry", "digest")}
        if not all(isinstance(v, str) and v for v in texts.values()):
            return None
        entry = cast(str, texts["entry"])
        if entry in (".", "..") or "/" in entry or "\\" in entry:
            return None
        exe = row.get("exe")
        path = row.get("path")
        dirs = cast(list[object], path) if isinstance(path, list) else []
        stamp = row.get("installed_at")
        return cls(
            version=cast(str, texts["version"]),
            target=cast(str, texts["target"]),
            url=cast(str, texts["url"]),
            sha256=cast(str, texts["sha256"]),
            entry=entry,
            digest=cast(str, texts["digest"]),
            exe=exe if isinstance(exe, str) and exe else None,
            path=tuple(str(d) for d in dirs if isinstance(d, str)),
            installed_at=stamp if isinstance(stamp, str) else "",
        )


@dataclass
class Facts:
    packages: dict[str, Fact] = field(default_factory=dict[str, Fact])
    declined: list[str] = field(default_factory=list[str])

    @classmethod
    def read(cls, path: Path) -> Facts:
        """The facts at ``path``. Missing → empty. Unreadable → empty, and the bytes are kept as
        ``<name>.corrupt`` so the next write does not lose the evidence."""
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return cls()
        except OSError:
            return cls()
        try:
            top = json.loads(data.decode("utf-8"))
            if not isinstance(top, Mapping):
                raise ValueError("not an object")
            fields = cast(Mapping[str, object], top)
            if fields.get("schema") != SCHEMA or not isinstance(fields.get("packages"), Mapping):
                raise ValueError("wrong schema")
        except ValueError:
            if data.strip():
                with contextlib.suppress(OSError):
                    path.with_name(path.name + ".corrupt").write_bytes(data)
            return cls()
        rows = cast(Mapping[object, object], fields["packages"])
        packages: dict[str, Fact] = {}
        for name, raw in rows.items():
            fact = Fact.from_json(raw)
            if fact is not None:
                packages[str(name)] = fact
        declined_raw = fields.get("declined")
        declined = (
            [str(d) for d in cast(list[object], declined_raw) if isinstance(d, str)]
            if isinstance(declined_raw, list)
            else []
        )
        return cls(packages, declined)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "declined": sorted(set(self.declined)),
            "packages": {n: f.to_json() for n, f in sorted(self.packages.items())},
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, self.to_json(), sort_keys=True, mode=0o644)

    def entries_in_use(self) -> set[str]:
        return {f.entry for f in self.packages.values()}

    def blobs_in_use(self) -> set[str]:
        return {f.sha256 for f in self.packages.values()}


# -- stat-keyed cache ------------------------------------------------------------------------------


class StatCache[T]:
    """Keeps ``load(path)`` until the file's (inode, size, mtime) changes; a missing file is a
    signature of its own. Only ``os.stat`` runs while the file stays the same."""

    def __init__(self, load: Callable[[Path], T]) -> None:
        self._load = load
        self._seen: dict[Path, tuple[tuple[int, int, int] | None, T]] = {}

    @staticmethod
    def _signature(path: Path) -> tuple[int, int, int] | None:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return None
        return (st.st_ino, st.st_size, st.st_mtime_ns)

    def get(self, path: Path) -> T:
        signature = self._signature(path)
        held = self._seen.get(path)
        if held is not None and held[0] == signature:
            return held[1]
        value = self._load(path)
        self._seen[path] = (signature, value)
        return value

    def put(self, path: Path, value: T) -> None:
        """Remember ``value`` as what ``path`` holds now (right after writing it)."""
        self._seen[path] = (self._signature(path), value)

    def forget(self, path: Path) -> None:
        self._seen.pop(path, None)
