"""Preflight: refuse a run that cannot succeed before it spends anything (P14-08, R07.16).

``structural`` (every job, before the wake gate): the script resolves inside
``<home>/scripts/``; the workdir is an existing absolute folder. ``check`` (after the wake gate):
the delivery targets have deliverers; for agent jobs also the provider key of the job's route
(skipped for an unpinned job when ``fallback_providers`` lists anything), the attached skills
(``PreflightProbes.skills``, filled by the service layer) and the MCP servers named in the job's
toolsets (``PreflightProbes.mcp``; a reconnecting server passes, R07.16).

Every check fails open on its own internal error: preflight blocks only on a clear verdict.
``AlertBook`` remembers, per job, the reason it last alerted for, so a block alerts once per
distinct reason and a healthy run clears it. ``cron.preflight: false`` turns everything off.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from indus.core.fallback import read_chain
from indus.cron import settings, targets
from indus.cron.jobroute import RouteChoice, route_choice
from indus.cron.ports import JobSpec
from indus.cron.script import ScriptRefused, resolve_script
from indus.foundation.errors import KeyHostMismatch, NoUsableProvider
from indus.foundation.fsio import atomic_write_json
from indus.foundation.home import secure_dir
from indus.foundation.scope import Scope
from indus.providers.route import resolve

__all__ = [
    "GATEWAY_REASON",
    "AlertBook",
    "McpState",
    "PreflightProbes",
    "check",
    "key_problem",
    "platforms_of",
    "structural",
]

log = logging.getLogger(__name__)

ALERTS_FILE = "preflight_alerts.json"
LOCAL_TARGETS = frozenset({"local", "origin"})
GATEWAY_REASON = (
    "delivery to {platform!r} needs the gateway: platform delivery is not available in this "
    "build yet. Deliver to local (or origin) instead, or wait for gateway support."
)

McpState = Literal["ok", "reconnecting", "empty"]


@dataclass(frozen=True)
class PreflightProbes:
    """Checks only another layer can answer; None skips that check."""

    key: Callable[[Scope, RouteChoice], str | None] | None = None
    skills: Callable[[Sequence[str]], str | None] | None = None
    mcp: Callable[[str], McpState | None] | None = None
    deliverers: Callable[[], frozenset[str]] | None = None


def _guarded(name: str, check: Callable[[], str | None]) -> str | None:
    try:
        return check()
    except Exception:
        log.debug("preflight check %s failed internally; not blocking", name, exc_info=True)
        return None


def structural(scope: Scope, job: JobSpec) -> str | None:
    """Script and workdir problems (see the module notes)."""
    if job.no_agent and not job.script:
        return "this job runs a script only, but it names no script"
    for value in (job.script, job.monitor_script):
        if value:
            try:
                resolve_script(scope.home, value)
            except ScriptRefused as exc:
                return str(exc)
    if job.workdir:
        folder = Path(job.workdir)
        if not folder.is_absolute():
            return f"the job's workdir must be an absolute path (got {job.workdir!r})"
        if not folder.is_dir():
            return f"the job's workdir does not exist any more: {job.workdir}"
    return None


def platforms_of(value: str | None) -> list[str]:
    """The platform names of a target list (``a, b:chat, c:chat:thread``), read with the delivery
    target parser (``cron.targets``); tokens it cannot read are left out."""
    if not (value or "").strip():
        return []
    return [target.platform for target in targets.parse_targets(value or "").targets]


def _delivery_problem(job: JobSpec, probes: PreflightProbes) -> str | None:
    known = probes.deliverers() if probes.deliverers is not None else frozenset({"local"})
    for lane in (job.deliver, job.failure_deliver):
        if not (lane or "").strip():
            continue
        parsed = targets.parse_targets(lane or "")
        if parsed.problems:
            problem = parsed.problems[0]
            return f"the delivery target {problem.token!r} cannot be used: {problem.message}"
        for target in parsed.targets:
            if target.platform in LOCAL_TARGETS or target.platform in known:
                continue
            return GATEWAY_REASON.format(platform=target.platform)
    return None


def key_problem(scope: Scope, route: RouteChoice) -> str | None:
    """The reason the job's route has no usable key (None when it resolves)."""
    try:
        resolve(scope, model=route.model, provider=route.provider, base_url=route.base_url)
    except (NoUsableProvider, KeyHostMismatch) as exc:
        return (
            f"no usable credential for this job's model: {exc.message} "
            f"(profile {scope.profile_id!r}, keys read from {scope.home})"
        )
    return None


def _key_check(scope: Scope, job: JobSpec, probes: PreflightProbes) -> str | None:
    route = route_choice(job, scope)
    if not route.pinned and read_chain(scope.config):
        return None  # the fallback chain may rescue a missing primary key
    probe = probes.key or key_problem
    return probe(scope, route)


def _mcp_servers(scope: Scope) -> frozenset[str]:
    raw = settings.config_value(scope, "mcp_servers")
    if not isinstance(raw, dict):
        return frozenset()
    return frozenset(str(k) for k in cast(dict[str, Any], raw))


def _mcp_problem(scope: Scope, job: JobSpec, probes: PreflightProbes) -> str | None:
    if probes.mcp is None or not job.enabled_toolsets:
        return None
    servers = _mcp_servers(scope)
    empty = [
        name for name in job.enabled_toolsets if name in servers and probes.mcp(name) == "empty"
    ]
    if not empty:
        return None
    return (
        "MCP server(s) " + ", ".join(sorted(empty)) + " named in this job's toolsets gave no "
        "tools for this profile. If a server is only unreachable for now this clears by itself "
        "on a later run; otherwise fix the server or drop it from the job's toolsets."
    )


def check(scope: Scope, job: JobSpec, probes: PreflightProbes) -> str | None:
    """The first blocking reason after the wake gate, or None (see the module notes)."""
    found = _guarded("delivery", lambda: _delivery_problem(job, probes))
    if found or job.no_agent:
        return found
    for name, run in (
        ("provider_key", lambda: _key_check(scope, job, probes)),
        ("skills", lambda: probes.skills(job.skills) if probes.skills and job.skills else None),
        ("mcp", lambda: _mcp_problem(scope, job, probes)),
    ):
        found = _guarded(name, run)
        if found:
            return found
    return None


class AlertBook:
    """Per job, the preflight reason last alerted (``<home>/cron/preflight_alerts.json``)."""

    _lock = threading.Lock()

    def __init__(self, home: Path) -> None:
        self.path = settings.cron_dir(home) / ALERTS_FILE

    def _read(self) -> dict[str, str]:
        try:
            raw: object = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        items = cast(dict[object, object], raw).items()
        return {str(k): str(v) for k, v in items if isinstance(v, str)}

    def _write(self, book: dict[str, str]) -> None:
        try:
            secure_dir(self.path.parent)
            atomic_write_json(self.path, book, sort_keys=True, mode=0o600)
        except OSError as exc:
            log.warning("could not save the preflight alert record: %s", exc)

    def should_alert(self, job_id: str, reason: str) -> bool:
        """True the first time ``reason`` blocks ``job_id`` (records it)."""
        with self._lock:
            book = self._read()
            if book.get(job_id) == reason:
                return False
            book[job_id] = reason
            self._write(book)
            return True

    def clear(self, job_id: str) -> None:
        with self._lock:
            book = self._read()
            if job_id in book:
                del book[job_id]
                self._write(book)
