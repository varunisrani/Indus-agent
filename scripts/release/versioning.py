"""Next versions, attempt numbers and the channel record (P23-21).

The grammar itself (versions, tags, attempt refs, channel words) lives in
``indus.foundation.versions`` so the updater and these scripts read one definition; this module
adds what only the release tooling needs:

- ``next_stable`` / ``next_canary``: the next version from the newest *published* stable release
  (a draft or prerelease never counts). With nothing published the line starts from ``0.0.0``;
- ``next_attempt`` / ``open_attempts``: attempt refs number tries of one version; an abandon marker
  clears an attempt but never frees its number, and only a final tag spends a version;
- ``ChannelRecord``: the ``channels.json`` asset on the fixed ``channels`` release. It names the
  newest stable and canary (version, tag, commit, image digests). ``update_channel_record`` is a
  compare-and-swap: it re-reads the asset and replaces it only when the asset's SHA-256 still
  equals the digest the caller read, and it never moves a channel to an older version.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from indus.foundation.versions import (
    CANARY,
    RELEASE_CHANNELS,
    SOURCE_VERSION,
    STABLE,
    AttemptRef,
    Version,
    canary_stamp,
    parse_attempt_ref,
    parse_marker_ref,
    parse_tag,
    parse_version,
)

__all__ = [
    "BUMPS",
    "CHANNELS_ASSET",
    "CHANNELS_TAG",
    "ChannelEntry",
    "ChannelRecord",
    "RecordConflict",
    "ReleaseAssets",
    "bump",
    "digest_of",
    "newest_published_stable",
    "next_attempt",
    "next_canary",
    "next_stable",
    "open_attempts",
    "read_channel_record",
    "update_channel_record",
]

BUMPS = ("patch", "minor", "major")
CHANNELS_TAG = "channels"
"""The fixed GitHub release that carries the channel record (never a version)."""
CHANNELS_ASSET = "channels.json"


def bump(version: Version, kind: str) -> Version:
    """The next plain version after ``version``'s core."""
    if kind == "major":
        return Version(version.major + 1, 0, 0)
    if kind == "minor":
        return Version(version.major, version.minor + 1, 0)
    if kind == "patch":
        return Version(version.major, version.minor, version.patch + 1)
    raise ValueError(f"unknown bump {kind!r} (use one of {', '.join(BUMPS)})")


def _start() -> Version:
    start = parse_version(SOURCE_VERSION)
    assert start is not None
    return start


def newest_published_stable(releases: Iterable[Mapping[str, Any]]) -> Version | None:
    """The newest published stable among GitHub release rows (``tag_name``, ``draft``,
    ``prerelease``); drafts, prereleases and anything not a ``vX.Y.Z`` tag do not count."""
    found: list[Version] = []
    for row in releases:
        if row.get("draft") is not False or row.get("prerelease") is not False:
            continue
        version = parse_tag(row.get("tag_name"))
        if version is not None and not version.is_canary:
            found.append(version)
    return max(found, default=None)


def next_stable(published: Version | None, kind: str = "patch") -> Version:
    return bump(published or _start(), kind)


def next_canary(published: Version | None, now: datetime) -> Version:
    """The canary of the patch after the newest stable, stamped with ``now`` (UTC)."""
    base = bump(published or _start(), "patch")
    return Version(base.major, base.minor, base.patch, canary_stamp(now))


def next_attempt(version: Version, refs: Iterable[str]) -> int:
    """One more than the highest attempt of ``version`` among ``refs`` (markers included: an
    abandoned attempt keeps its number)."""
    numbers = [
        parsed.attempt
        for ref in refs
        if (parsed := parse_attempt_ref(ref) or parse_marker_ref(ref)) is not None
        and parsed.version == version
    ]
    return max(numbers, default=0) + 1


def open_attempts(refs: Sequence[str]) -> list[AttemptRef]:
    """Attempts with neither an abandon marker nor a final tag, oldest version first.

    ``refs`` are the remote's tag names (attempt refs, markers and final tags together)."""
    names = set(refs)
    cleared = {marker for ref in names if (marker := parse_marker_ref(ref)) is not None}
    published = {version for ref in names if (version := parse_tag(ref)) is not None}
    found = [
        attempt
        for ref in names
        if (attempt := parse_attempt_ref(ref)) is not None
        and attempt not in cleared
        and attempt.version not in published
    ]
    return sorted(found, key=lambda a: (a.version.key, a.attempt))


# -- the channel record ----------------------------------------------------------------------------


class RecordConflict(RuntimeError):
    """The channel record changed between read and write, or a write would move it backwards."""


@dataclass(frozen=True)
class ChannelEntry:
    version: str
    tag: str
    commit: str
    images: dict[str, str] = field(default_factory=dict[str, str])
    published_at: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "tag": self.tag,
            "commit": self.commit,
            "images": dict(sorted(self.images.items())),
            "published_at": self.published_at,
        }


@dataclass(frozen=True)
class ChannelRecord:
    """``{"schema": 1, "stable": entry | null, "canary": entry | null}``."""

    stable: ChannelEntry | None = None
    canary: ChannelEntry | None = None

    def get(self, channel: str) -> ChannelEntry | None:
        return self.stable if channel == STABLE else self.canary if channel == CANARY else None

    def with_entry(self, channel: str, entry: ChannelEntry) -> ChannelRecord:
        if channel == STABLE:
            return ChannelRecord(entry, self.canary)
        if channel == CANARY:
            return ChannelRecord(self.stable, entry)
        raise ValueError(f"{channel!r} has no channel record (use one of {RELEASE_CHANNELS})")

    def to_bytes(self) -> bytes:
        body = {
            "schema": 1,
            STABLE: None if self.stable is None else self.stable.to_json(),
            CANARY: None if self.canary is None else self.canary.to_json(),
        }
        return (json.dumps(body, indent=2, sort_keys=True) + "\n").encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> ChannelRecord:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or data.get("schema") != 1:
            raise ValueError("the channel record is not schema 1")
        entries: dict[str, ChannelEntry | None] = {}
        for channel in RELEASE_CHANNELS:
            row = data.get(channel)
            if row is None:
                entries[channel] = None
                continue
            if not isinstance(row, dict) or parse_version(row.get("version")) is None:
                raise ValueError(f"the channel record's {channel} entry is malformed")
            images = row.get("images") or {}
            entries[channel] = ChannelEntry(
                version=str(row["version"]),
                tag=str(row.get("tag", "")),
                commit=str(row.get("commit", "")),
                images={str(k): str(v) for k, v in dict(images).items()},
                published_at=str(row.get("published_at", "")),
            )
        return cls(entries[STABLE], entries[CANARY])


class ReleaseAssets(Protocol):
    """The two GitHub calls the record needs (the real client and the test fake share it)."""

    def read_asset(self, release_tag: str, name: str) -> bytes | None: ...

    def replace_asset(self, release_tag: str, name: str, data: bytes) -> None: ...


def digest_of(raw: bytes | None) -> str | None:
    return None if raw is None else hashlib.sha256(raw).hexdigest()


def read_channel_record(assets: ReleaseAssets) -> tuple[ChannelRecord, str | None]:
    """The current record and the digest a later update must present (None: no asset yet)."""
    raw = assets.read_asset(CHANNELS_TAG, CHANNELS_ASSET)
    record = ChannelRecord() if raw is None else ChannelRecord.from_bytes(raw)
    return record, digest_of(raw)


def update_channel_record(
    assets: ReleaseAssets,
    channel: str,
    entry: ChannelEntry,
    *,
    expected_digest: str | None,
) -> ChannelRecord:
    """Set ``channel`` to ``entry`` when the asset still has ``expected_digest``.

    Raises ``RecordConflict`` when the asset changed since it was read, or when ``entry`` is
    older than what the channel already names. Writing the same entry again is a no-op."""
    raw = assets.read_asset(CHANNELS_TAG, CHANNELS_ASSET)
    if digest_of(raw) != expected_digest:
        raise RecordConflict(
            "the channel record changed since it was read; read it again and retry"
        )
    record = ChannelRecord() if raw is None else ChannelRecord.from_bytes(raw)
    current = record.get(channel)
    new_version = parse_version(entry.version)
    if new_version is None:
        raise ValueError(f"{entry.version!r} is not a release version")
    if new_version.channel != channel:
        raise ValueError(f"{entry.version} is not a {channel} version")
    if current is not None:
        if current.to_json() == entry.to_json():
            return record
        old = parse_version(current.version)
        if old is not None and new_version <= old:
            raise RecordConflict(
                f"the {channel} channel already names {current.version}; "
                f"{entry.version} would move it backwards"
            )
    updated = record.with_entry(channel, entry)
    assets.replace_asset(CHANNELS_TAG, CHANNELS_ASSET, updated.to_bytes())
    return updated
