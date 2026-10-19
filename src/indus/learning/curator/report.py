"""Per-run curator reports: ``<home>/logs/curator/<stamp>/{run.json,REPORT.md}``.

``stamp`` is ``YYYYMMDD-HHMMSS`` (UTC) with ``-2``, ``-3``… when a run in the same second already
wrote one. Reports are best-effort: a failure to write one is logged and never stops a run.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from indus.foundation.fsio import atomic_write_text
from indus.foundation.scope import Scope

__all__ = ["NOT_RESOLVED", "render_markdown", "reports_root", "write_report"]

log = logging.getLogger(__name__)

NOT_RESOLVED = "(not resolved)"
_LIST_CAP = 50


def reports_root(scope: Scope) -> Path:
    return scope.home / "logs" / "curator"


def _run_dir(root: Path, started: datetime) -> Path:
    stamp = started.strftime("%Y%m%d-%H%M%S")
    candidate, n = root / stamp, 1
    while candidate.exists():
        n += 1
        candidate = root / f"{stamp}-{n}"
    return candidate


def _seconds(value: object) -> str:
    total = int(value) if isinstance(value, int | float) else 0
    minutes, seconds = divmod(total, 60)
    return f"{minutes} min {seconds} s" if minutes else f"{seconds} s"


def _capped(lines: list[str], items: Sequence[Any], render: Any) -> None:
    for item in items[:_LIST_CAP]:
        lines.append(render(item))
    if len(items) > _LIST_CAP:
        lines.append(f"- and {len(items) - _LIST_CAP} more (see run.json)")


def render_markdown(payload: Mapping[str, Any]) -> str:
    auto: Mapping[str, Any] = payload.get("transitions") or {}
    cons: Mapping[str, Any] = payload.get("consolidation") or {}
    mode = " (dry run: nothing was changed)" if payload.get("dry_run") else ""
    lines = [
        f"# Curator run, {payload.get('started_at', '')}{mode}",
        "",
        f"- model: {cons.get('model') or NOT_RESOLVED} via {cons.get('provider') or NOT_RESOLVED}",
        f"- duration: {_seconds(payload.get('duration_s'))}",
        f"- trigger: {payload.get('trigger', 'manual')}",
    ]
    if payload.get("backup"):
        lines.append(f"- snapshot taken first: {payload['backup']}")
    lines += [
        "",
        "## Lifecycle (by inactivity, no model)",
        "",
        f"- skills checked: {auto.get('checked', 0)}",
        f"- marked stale: {auto.get('marked_stale', 0)}",
        f"- archived: {auto.get('archived', 0)}",
        f"- active again: {auto.get('reactivated', 0)}",
        f"- clock started (first sight): {auto.get('seeded', 0)}",
        f"- skipped, pinned: {auto.get('pinned', 0)}",
        f"- skipped, in use elsewhere: {auto.get('referenced', 0)}",
    ]
    if auto.get("protection_unknown"):
        lines.append("- a reference source failed, so nothing was archived this run")
    changes: Sequence[Mapping[str, Any]] = payload.get("state_changes") or []
    if changes:
        lines += ["", "### State changes", ""]
        _capped(lines, changes, lambda c: f"- `{c['name']}`: {c['from']} → {c['to']}")
    lines += ["", "## Consolidation", ""]
    if not cons.get("ran"):
        lines.append(f"- {cons.get('summary') or 'not run'}")
    else:
        counts: Mapping[str, int] = cons.get("tool_calls") or {}
        listed = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"
        lines.append(f"- tool calls: {sum(counts.values())} ({listed})")
        if cons.get("error"):
            lines.append(f"- stopped with an error: {cons['error']}")
        merged: Sequence[Mapping[str, Any]] = cons.get("consolidated") or []
        pruned: Sequence[Mapping[str, Any]] = cons.get("pruned") or []
        added: Sequence[str] = cons.get("added") or []
        if merged:
            lines += ["", "### Folded into other skills", ""]
            _capped(lines, merged, lambda e: f"- `{e['name']}` → `{e['into']}` ({e['source']})")
        if pruned:
            lines += ["", "### Archived without a successor", ""]
            _capped(lines, pruned, lambda e: f"- `{e['name']}`")
        if added:
            lines += ["", "### New skills", ""]
            _capped(lines, added, lambda n: f"- `{n}`")
        rewrites: Sequence[Mapping[str, Any]] = cons.get("reference_rewrites") or []
        if rewrites:
            lines += ["", "### References followed", ""]
            for rw in rewrites:
                if rw.get("error"):
                    lines.append(f"- {rw['source']}: failed ({rw['error']})")
                for change in rw.get("changes") or []:
                    lines.append(f"- {rw['source']}: {change}")
        final = str(cons.get("final") or "").strip()
        if final:
            lines += ["", "### The model's closing notes", "", final]
    lines += [
        "",
        "## Undo",
        "",
        "- `indus curator restore <name>` brings an archived skill back.",
        "- `indus curator rollback` restores the whole skills folder from the newest snapshot.",
        "- `run.json` next to this file has the full record.",
        "",
    ]
    return "\n".join(lines)


def write_report(scope: Scope, started: datetime, payload: Mapping[str, Any]) -> Path | None:
    """Write both files; returns the run folder, or None when it could not be written."""
    try:
        folder = _run_dir(reports_root(scope), started)
        folder.mkdir(parents=True, exist_ok=False)
        atomic_write_text(
            folder / "run.json", json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        )
        atomic_write_text(folder / "REPORT.md", render_markdown(payload))
        return folder
    except Exception:
        log.warning("curator report not written", exc_info=True)
        return None
