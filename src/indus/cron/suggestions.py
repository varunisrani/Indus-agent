"""Suggested jobs: offered, never scheduled until a person accepts (P24-46).

One store per profile, ``<home>/cron/suggestions.json`` behind ``<home>/cron/suggestions.lock``.
Each record holds a dedup key, a title, a description, where it came from (``catalogue``,
``skill``, ``usage`` for the self-improvement review, ``integration`` for a connected service),
the job spec it would create, and its status: ``pending``, ``accepted`` (with the job id) or
``dismissed``.

- ``offer`` adds a pending suggestion unless a record with its dedup key exists in any status:
  once accepted or dismissed, a key is latched and never offered again, whatever later refreshes
  find. At most ``MAX_PENDING`` (20) wait at once.
- ``refresh`` offers the starter catalogue and the skills' blueprints that fill from their
  defaults alone (dedup key ``blueprint:<key>``).
- ``accept`` takes a list number (1 = first pending) or an id and creates the job through the
  normal job creation (``CronBackend.create``) with the stored spec, under the store lock: a second
  accept, from this process or another, finds nothing pending. The prompt goes through the cron
  prompt check first; when creation fails the suggestion stays pending and the reason is raised.
- ``dismiss`` latches the key.

Nothing in this module creates a job by itself. ``handle_suggestions`` is the ``/suggestions``
command for every surface (``list``, ``accept N`` — also ``add``, ``schedule``, ``yes`` —,
``dismiss N`` — also ``no``, ``reject`` —, ``refresh``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import secrets
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, cast, get_args

from indus.cron import ports
from indus.cron import tool as checks
from indus.cron.blueprints import Blueprint, BlueprintError, fill, list_blueprints
from indus.cron.errors import CronStoreBusy
from indus.foundation.fsio import FileLock, LockTimeout, atomic_write_json
from indus.foundation.profiles import mkdir_in_profile
from indus.foundation.scope import Scope

__all__ = [
    "MAX_PENDING",
    "SOURCES",
    "Suggestion",
    "SuggestionNotFound",
    "SuggestionRefused",
    "SuggestionStore",
    "handle_suggestions",
    "offer",
    "pending_lines",
]

log = logging.getLogger(__name__)

type SuggestionSource = Literal["catalogue", "skill", "usage", "integration"]
type Status = Literal["pending", "accepted", "dismissed"]
SOURCES: frozenset[str] = frozenset(get_args(SuggestionSource.__value__))
MAX_PENDING = 20
FILE_NAME = "suggestions.json"
LOCK_NAME = "suggestions.lock"
LOCK_TIMEOUT_S = 30.0
FORMAT_VERSION = 1


class SuggestionNotFound(LookupError):
    """No pending suggestion has this number or id."""


class SuggestionRefused(ValueError):
    """Accepting did not create a job; the suggestion stays pending."""


@dataclass
class Suggestion:
    """One offered job (see the module notes)."""

    id: str
    dedup_key: str
    title: str
    description: str
    source: SuggestionSource
    spec: dict[str, Any]
    created_at: str
    status: Status = "pending"
    job_id: str | None = None
    resolved_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        extra = data.pop("extra")
        return {**extra, **data}

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Suggestion | None:
        known = {
            "id", "dedup_key", "title", "description", "source", "spec", "created_at", "status",
            "job_id", "resolved_at",
        }  # fmt: skip
        ident, key, spec = raw.get("id"), raw.get("dedup_key"), raw.get("spec")
        if not isinstance(ident, str) or not isinstance(key, str) or not isinstance(spec, dict):
            return None
        status = raw.get("status")
        source = raw.get("source")
        return cls(
            id=ident,
            dedup_key=key,
            title=str(raw.get("title") or "(untitled)"),
            description=str(raw.get("description") or ""),
            source=cast(SuggestionSource, source if source in SOURCES else "integration"),
            spec=cast(dict[str, Any], spec),
            created_at=str(raw.get("created_at") or ""),
            status=cast(
                Status, status if status in ("pending", "accepted", "dismissed") else "pending"
            ),
            job_id=raw.get("job_id") if isinstance(raw.get("job_id"), str) else None,
            resolved_at=raw.get("resolved_at") if isinstance(raw.get("resolved_at"), str) else None,
            extra={k: v for k, v in raw.items() if k not in known},
        )


class SuggestionStore:
    """The suggestions of one profile; every change is one locked read-modify-write."""

    def __init__(self, scope: Scope, *, lock_timeout: float = LOCK_TIMEOUT_S) -> None:
        self.scope = scope
        self.dir = scope.home / "cron"
        self.path = self.dir / FILE_NAME
        self.lock_path = self.dir / LOCK_NAME
        self.lock_timeout = lock_timeout

    # -- file -------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self) -> Iterator[list[Suggestion]]:
        mkdir_in_profile(self.scope.home, "cron")
        lock = FileLock(self.lock_path, timeout=self.lock_timeout, clock=self.scope.clock)
        try:
            lock.acquire()
        except LockTimeout as exc:
            raise CronStoreBusy(
                f"the suggestions file {self.path} is locked by another process",
                hint="try again in a moment",
            ) from exc
        try:
            records = self._read()
            before = [r.to_json() for r in records]
            yield records
            after = [r.to_json() for r in records]
            if after != before:
                atomic_write_json(self.path, {"version": FORMAT_VERSION, "suggestions": after})
        finally:
            lock.release()

    def _read(self) -> list[Suggestion]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            log.warning("the suggestions file %s does not read; starting empty", self.path)
            return []
        rows = data.get("suggestions") if isinstance(data, dict) else None
        out: list[Suggestion] = []
        for raw in cast(list[object], rows if isinstance(rows, list) else []):
            if isinstance(raw, Mapping):
                found = Suggestion.from_json(cast(Mapping[str, Any], raw))
                if found is not None:
                    out.append(found)
        return out

    def _now(self) -> str:
        return self.scope.clock.now().isoformat()

    # -- reading ----------------------------------------------------------------------------------

    def all(self) -> list[Suggestion]:
        return self._read()

    def pending(self) -> list[Suggestion]:
        return [s for s in self._read() if s.status == "pending"]

    # -- changes ----------------------------------------------------------------------------------

    def offer(
        self,
        *,
        dedup_key: str,
        title: str,
        spec: Mapping[str, Any],
        source: SuggestionSource,
        description: str = "",
    ) -> Suggestion | None:
        """Add a pending suggestion; None when its key is known (latched) or the list is full."""
        key = dedup_key.strip()
        if not key:
            raise ValueError("a suggestion needs a dedup key")
        if source not in SOURCES:
            raise ValueError(f"unknown suggestion source {source!r}")
        with self._locked() as records:
            return self._offer_locked(records, key, title, dict(spec), source, description)

    def _offer_locked(
        self,
        records: list[Suggestion],
        key: str,
        title: str,
        spec: dict[str, Any],
        source: SuggestionSource,
        description: str,
    ) -> Suggestion | None:
        if any(r.dedup_key == key for r in records):
            return None
        if sum(1 for r in records if r.status == "pending") >= MAX_PENDING:
            return None
        made = Suggestion(
            id=secrets.token_hex(4),
            dedup_key=key,
            title=title.strip() or "(untitled)",
            description=description.strip(),
            source=source,
            spec=spec,
            created_at=self._now(),
        )
        records.append(made)
        return made

    def refresh(self, blueprints: list[Blueprint] | None = None) -> list[Suggestion]:
        """Offer every blueprint that fills from its defaults alone; the ones added."""
        offers: list[tuple[str, str, dict[str, Any], SuggestionSource, str]] = []
        for bp in list_blueprints(self.scope) if blueprints is None else blueprints:
            if not bp.fills_from_defaults():
                continue
            try:
                spec = fill(bp)
            except BlueprintError as exc:
                log.warning("blueprint %s does not fill from its defaults: %s", bp.key, exc)
                continue
            offers.append((f"blueprint:{bp.key}", bp.title, spec, bp.source, bp.description))
        added: list[Suggestion] = []
        with self._locked() as records:
            for key, title, spec, source, description in offers:
                made = self._offer_locked(records, key, title, spec, source, description)
                if made is not None:
                    added.append(made)
        return added

    @staticmethod
    def _find(records: list[Suggestion], ref: str) -> Suggestion:
        text = ref.strip()
        pending = [r for r in records if r.status == "pending"]
        if text.isdigit() and 1 <= int(text) <= len(pending) and len(text) < 4:
            return pending[int(text) - 1]
        for record in pending:
            if record.id == text.lower():
                return record
        raise SuggestionNotFound(text)

    def accept(
        self,
        ref: str,
        backend: ports.CronBackend,
        *,
        origin: Mapping[str, Any] | None = None,
    ) -> tuple[Suggestion, Any]:
        """Create the suggested job (exactly once) and latch the key; see the module notes."""
        with self._locked() as records:
            record = self._find(records, ref)
            spec = dict(record.spec)
            problem = checks.prompt_problem(spec.get("prompt")) or checks.deliver_problem(
                spec.get("deliver")
            )
            if problem:
                raise SuggestionRefused(problem)
            if origin is not None and "origin" not in spec:
                spec["origin"] = dict(origin)
            try:
                job = backend.create(spec)
            except (ValueError, CronStoreBusy, ports.StoreUnavailable) as exc:
                raise SuggestionRefused(str(exc)) from exc
            record.status = "accepted"
            record.job_id = str(ports.field(job, "id"))
            record.resolved_at = self._now()
            return record, job

    def dismiss(self, ref: str) -> Suggestion:
        """Latch the key so the suggestion never returns."""
        with self._locked() as records:
            record = self._find(records, ref)
            record.status = "dismissed"
            record.resolved_at = self._now()
            return record


def offer(
    scope: Scope,
    *,
    dedup_key: str,
    title: str,
    spec: Mapping[str, Any],
    source: SuggestionSource,
    description: str = "",
) -> Suggestion | None:
    """Offer one suggestion to ``scope``'s store (the review fork and integrations call this)."""
    return SuggestionStore(scope).offer(
        dedup_key=dedup_key, title=title, spec=spec, source=source, description=description
    )


# -- the /suggestions command -------------------------------------------------------------------

USAGE = (
    "Usage: /suggestions [list] | accept <n|id> | dismiss <n|id> | refresh\n"
    "  list      the suggested jobs waiting for a yes or no\n"
    "  accept    create suggestion n as a scheduled job\n"
    "  dismiss   never suggest it again\n"
    "  refresh   add the starter jobs and the ones your skills suggest"
)


_ALIASES = {"add": "accept", "schedule": "accept", "yes": "accept", "no": "dismiss",
            "reject": "dismiss"}  # fmt: skip


def pending_lines(records: list[Suggestion]) -> list[str]:
    if not records:
        return [
            "No suggested jobs right now.",
            "/suggestions refresh adds the starter set and the jobs your skills suggest.",
        ]
    lines = ["Suggested jobs (/suggestions accept N or dismiss N):"]
    for number, record in enumerate(records, 1):
        schedule = record.spec.get("schedule", "?")
        lines.append(f"  {number}. {record.title}  [{schedule}]  ({record.source}, {record.id})")
        if record.description:
            lines.append(f"     {record.description}")
    return lines


def _manage(surface: str) -> str:
    if surface in ("cli", "tui"):
        return "/cron lists, pauses and removes it."
    return "Ask me any time to list, pause or remove it."


def handle_suggestions(
    scope: Scope,
    args: str,
    *,
    backend: Callable[[], ports.CronBackend],
    surface: str = "cli",
    origin: Mapping[str, Any] | None = None,
) -> str:
    """Run one ``/suggestions`` line and return what to show."""
    words = args.split()
    verb = words[0].lower() if words else "list"
    verb = _ALIASES.get(verb, verb)
    ref = " ".join(words[1:]).strip()
    store = SuggestionStore(scope)
    try:
        if verb == "list" and not ref:
            return "\n".join(pending_lines(store.pending()))
        if verb == "refresh" and not ref:
            added = store.refresh()
            if not added:
                return (
                    "Nothing new to suggest (each one was offered before, or the list is full). "
                    "/suggestions shows what is waiting."
                )
            names = ", ".join(s.title for s in added)
            return f"Added {len(added)} suggestion(s): {names}. /suggestions lists them."
        if verb in ("accept", "dismiss"):
            if not ref:
                return f"Usage: /suggestions {verb} <number or id>"
            if verb == "dismiss":
                record = store.dismiss(ref)
                return f"Dismissed '{record.title}'; it will not be suggested again."
            record, job = store.accept(ref, backend(), origin=origin)
            shown = ports.field(job, "schedule_display") or record.spec.get("schedule", "")
            return (
                f"Scheduled '{record.title}' ({shown}) as job {record.job_id}. {_manage(surface)}"
            )
    except SuggestionNotFound:
        return f"No waiting suggestion is {ref!r}; /suggestions lists them."
    except SuggestionRefused as exc:
        return f"The job was not created: {exc}. The suggestion is still waiting."
    except (CronStoreBusy, ports.StoreUnavailable) as exc:
        return f"Suggestions are unavailable right now: {exc}"
    return USAGE


def store_path(scope: Scope) -> Path:
    return scope.home / "cron" / FILE_NAME
