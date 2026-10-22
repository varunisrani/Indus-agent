# pyright: strict
"""Sweeping old Indus containers of this profile (P11-07).

Persistent containers are left running for reuse and a crash can leave any container behind, so
once per process — the first time a Docker environment opens, when
``terminal.docker_orphan_reaper`` is on — the containers labelled ``indus-agent=1`` for this
profile are checked:

- stopped (exited, created, dead) and finished more than 24 h ago → removed with a plain ``rm``
  (the daemon refuses to remove a running container, which re-checks that nobody restarted it
  meanwhile); an unknown finish time keeps it;
- running session containers whose session is gone and whose task id has no live background job
  → removed (``rm -f``); the profile-wide container (task ``shared``) is never removed while it
  runs, it is there to be reused.

Containers without our label or of another profile are never touched: the listing filters on both
labels and ``inspect`` checks them again before anything is removed. The sweep runs as a detached
task with a 30 s budget on the scope's clock; when the budget runs out it stops and logs what was
left. Whether a session is gone and whether a task has live jobs are questions only the host can
answer (``set_liveness``); until it does, every session counts as alive, so running containers are
kept.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from indus.backends.docker import liveness
from indus.backends.docker.environment import (
    LABEL_AGENT,
    LABEL_PROFILE,
    LABEL_TASK,
    SHARED_TASK,
)
from indus.backends.docker.liveness import Alive, set_liveness
from indus.backends.docker.probe import DockerCli
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope

__all__ = [
    "BUDGET_S",
    "STOPPED_AFTER_S",
    "ReapReport",
    "parse_finished_at",
    "reap_orphans",
    "reset_for_tests",
    "set_liveness",
    "start_reaper_once",
]

log = logging.getLogger(__name__)

BUDGET_S = 30.0
STOPPED_AFTER_S = 24 * 3600.0
LIST_TIMEOUT_S = 10.0
INSPECT_TIMEOUT_S = 10.0
RM_TIMEOUT_S = 30.0
STOPPED_STATES = frozenset({"exited", "created", "dead"})
_FRACTION = re.compile(r"(\.\d{6})\d+")

_lock = threading.Lock()
_started: set[tuple[str, str]] = set()
_tasks: set[asyncio.Future[Any]] = set()


def reset_for_tests() -> None:
    with _lock:
        _started.clear()
    liveness.reset()


@dataclass
class ReapReport:
    removed: list[str] = field(default_factory=lambda: [])
    kept: list[str] = field(default_factory=lambda: [])
    budget_exceeded: bool = False


def parse_finished_at(raw: str) -> datetime | None:
    """Docker's RFC 3339 time (nanoseconds allowed); None for the zero value or nonsense."""
    text = raw.strip()
    if not text or text.startswith("0001-01-01"):
        return None
    text = _FRACTION.sub(r"\1", text).replace("Z", "+00:00")
    try:
        found = datetime.fromisoformat(text)
    except ValueError:
        return None
    return found if found.tzinfo is not None else found.replace(tzinfo=UTC)


def _kept(cid: str, keep: Collection[str]) -> bool:
    return any(k and (k.startswith(cid) or cid.startswith(k)) for k in keep)


async def reap_orphans(
    cli: DockerCli,
    *,
    profile_label: str,
    clock: Clock,
    keep: Collection[str] = (),
    session_alive: Alive | None = None,
    jobs_alive: Alive | None = None,
    budget_s: float = BUDGET_S,
    stopped_after_s: float = STOPPED_AFTER_S,
) -> ReapReport:
    """One sweep (see the module notes). Never raises for a docker failure."""
    installed_session, installed_jobs = liveness.current()
    session_ok = session_alive or installed_session
    jobs_ok = jobs_alive or installed_jobs
    report = ReapReport()
    began = clock.monotonic()
    listing = await cli.call(
        "ps",
        "-a",
        "--filter",
        f"label={LABEL_AGENT}=1",
        "--filter",
        f"label={LABEL_PROFILE}={profile_label}",
        "--format",
        "{{.ID}}\t{{.State}}",
        timeout=LIST_TIMEOUT_S,
    )
    if not listing.ok:
        log.debug("orphan sweep: listing failed: %s", listing.text)
        return report
    rows = [line.strip().partition("\t") for line in listing.stdout.splitlines() if line.strip()]
    for index, (cid, _, state) in enumerate(rows):
        if clock.monotonic() - began > budget_s:
            report.budget_exceeded = True
            log.warning(
                "orphan container sweep stopped after %.0f s; %d container(s) left for next time",
                budget_s,
                len(rows) - index,
            )
            break
        if not cid or _kept(cid, keep):
            report.kept.append(cid)
            continue
        removed = await _reap_one(
            cli,
            cid,
            state.strip().lower(),
            profile_label,
            clock,
            session_ok,
            jobs_ok,
            stopped_after_s,
        )
        if removed:
            report.removed.append(cid)
        else:
            report.kept.append(cid)
    if report.removed:
        log.info("removed %d old sandbox container(s)", len(report.removed))
    return report


async def _reap_one(
    cli: DockerCli,
    cid: str,
    state: str,
    profile_label: str,
    clock: Clock,
    session_alive: Alive,
    jobs_alive: Alive,
    stopped_after_s: float,
) -> bool:
    template = "\t".join(
        [
            "{{.State.FinishedAt}}",
            f'{{{{index .Config.Labels "{LABEL_AGENT}"}}}}',
            f'{{{{index .Config.Labels "{LABEL_PROFILE}"}}}}',
            f'{{{{index .Config.Labels "{LABEL_TASK}"}}}}',
        ]
    )
    seen = await cli.call("inspect", "--format", template, cid, timeout=INSPECT_TIMEOUT_S)
    if not seen.ok:
        return False
    fields = seen.stdout.strip("\n").split("\t")
    if len(fields) != 4:
        return False
    finished, agent, profile, task = (f.strip() for f in fields)
    if agent != "1" or profile != profile_label:
        return False  # not ours after all: never touched
    if state in STOPPED_STATES:
        ended = parse_finished_at(finished)
        if ended is None or (clock.now() - ended).total_seconds() < stopped_after_s:
            return False
        result = await cli.call("rm", cid, timeout=RM_TIMEOUT_S)
    elif state == "running":
        if task == SHARED_TASK or session_alive(task) or jobs_alive(task):
            return False
        result = await cli.call("rm", "-f", cid, timeout=RM_TIMEOUT_S)
    else:
        return False
    if not result.ok:
        log.debug("orphan sweep: could not remove %s: %s", cid[:12], result.text)
    return result.ok


def start_reaper_once(
    cli: DockerCli,
    scope: Scope,
    *,
    profile_label: str,
    keep: Collection[str] = (),
) -> asyncio.Future[ReapReport] | None:
    """Schedule the sweep for this client and profile, once per process; None when it already
    ran here."""
    key = (cli.binary, profile_label)
    with _lock:
        if key in _started:
            return None
        _started.add(key)
    task = asyncio.ensure_future(
        reap_orphans(cli, profile_label=profile_label, clock=scope.clock, keep=tuple(keep))
    )
    _tasks.add(task)

    def done(finished: asyncio.Future[Any]) -> None:
        _tasks.discard(finished)
        if not finished.cancelled() and finished.exception() is not None:
            log.warning("orphan container sweep failed", exc_info=finished.exception())

    task.add_done_callback(done)
    return task
