"""``indus skills snapshot export|import``: carry the hub skills of a profile elsewhere (P24-45).

Export writes ``{format: "indus-skills-snapshot", version: 1, exported_at, skills: [{name,
source, identifier, category, content_hash, upstream_hash}], taps: [{repo, path, trust?}]}`` from
the hub lock and the taps file. Nothing else (skills the person wrote, settings, secrets) goes in.

Import restores the taps (new ones as ``community``; a recorded ``trusted`` is not carried over)
and installs every entry again through the normal hub pipeline: fetch, quarantine, scan, trust
policy, confirm. ``force`` lifts a ``caution`` block only — a skill whose upstream content now
scans ``dangerous`` is refused whatever the flags. An entry whose upstream content differs from
the snapshot's hash is installed with a note (``drift``). An entry already installed is left as it
is unless ``force``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, cast

from indus.foundation.scope import Scope
from indus.learning.skills.hub.install import InstallOptions, Preview, install, skills_root
from indus.learning.skills.hub.lock import HubLock, Taps
from indus.learning.skills.hub.sources import HubError, SourceSet

__all__ = [
    "FORMAT",
    "VERSION",
    "ImportRow",
    "export_snapshot",
    "import_snapshot",
    "read_snapshot",
]

FORMAT = "indus-skills-snapshot"
VERSION = 1


def export_snapshot(scope: Scope, *, now: datetime | None = None) -> dict[str, Any]:
    root = skills_root(scope)
    rows: list[dict[str, Any]] = []
    for name, entry in sorted(HubLock(root).installed().items()):
        install_path = str(entry.get("install_path") or name)
        parent = str(PurePosixPath(install_path).parent)
        rows.append(
            {
                "name": name,
                "source": str(entry.get("source") or ""),
                "identifier": str(entry.get("identifier") or ""),
                "category": "" if parent == "." else parent,
                "content_hash": str(entry.get("content_hash") or ""),
                "upstream_hash": str(entry.get("upstream_hash") or ""),
            }
        )
    taps = [
        {k: v for k, v in tap.items() if k in ("repo", "path", "trust")}
        for tap in Taps(root).list()
    ]
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": (now or scope.clock.now()).isoformat(),
        "skills": rows,
        "taps": taps,
    }


def read_snapshot(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError:
        raise HubError("the snapshot is not JSON") from None
    if not isinstance(data, dict):
        raise HubError("the snapshot is not a JSON object")
    doc = cast(dict[str, Any], data)
    if doc.get("format") not in (FORMAT, None):
        raise HubError(f"not a skills snapshot (format {doc.get('format')!r})")
    version = doc.get("version", VERSION)
    if not isinstance(version, int) or version > VERSION:
        raise HubError(f"snapshot version {version!r} is newer than this Indus reads")
    if not isinstance(doc.get("skills", []), list) or not isinstance(doc.get("taps", []), list):
        raise HubError("the snapshot's skills and taps must be lists")
    return doc


@dataclass(frozen=True)
class ImportRow:
    name: str
    identifier: str
    status: str
    message: str = ""
    drift: bool = False

    @property
    def ok(self) -> bool:
        return self.status in ("installed", "kept", "skipped")


async def import_snapshot(
    scope: Scope,
    sources: SourceSet,
    snapshot: Mapping[str, Any],
    *,
    force: bool = False,
    confirm: Callable[[Preview], bool] | None = None,
    say: Callable[[str], None] = lambda _line: None,
) -> list[ImportRow]:
    """Restore the taps, then install each entry through the normal pipeline (module notes)."""
    root = skills_root(scope)
    taps = Taps(root)
    for raw in cast(list[object], snapshot.get("taps") or []):
        if isinstance(raw, Mapping):
            tap = cast(Mapping[str, Any], raw)
            repo = str(tap.get("repo") or "").strip()
            if repo and taps.add(repo, str(tap.get("path") or "skills/")):
                say(f"tap added: {repo}")
    lock = HubLock(root)
    rows: list[ImportRow] = []
    for raw in cast(list[object], snapshot.get("skills") or []):
        if not isinstance(raw, Mapping):
            continue
        entry = cast(Mapping[str, Any], raw)
        name = str(entry.get("name") or "").strip()
        identifier = str(entry.get("identifier") or "").strip()
        if not identifier:
            rows.append(ImportRow(name or "?", "", "skipped", "no identifier in the snapshot"))
            continue
        if name and lock.get(name) is not None and not force:
            rows.append(ImportRow(name, identifier, "kept", "already installed"))
            continue
        options = InstallOptions(
            category=str(entry.get("category") or ""),
            name=name,
            force=force,
            replace=force,
            source_id=str(entry.get("source") or "") or None,
            confirm=confirm,
            say=say,
        )
        result = await install(scope, sources, identifier, options)
        if not result.ok:
            rows.append(ImportRow(result.name or name, identifier, result.status, result.message))
            continue
        recorded = str(entry.get("upstream_hash") or "")
        now_hash = str((result.entry or {}).get("upstream_hash") or "")
        drift = bool(recorded and now_hash and recorded != now_hash)
        note = "upstream content changed since the snapshot" if drift else ""
        rows.append(ImportRow(result.name, identifier, "installed", note, drift))
    return rows
