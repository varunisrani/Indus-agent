"""The runtime ledger: the durable FIFO of inputs a daemon runs for each session (P17-01).

Every input a surface hands the daemon is an *admission*: one row keyed by (principal, target
session, request id) with a digest of its payload. Its ``state`` walks

    queued → started → terminal          (outcome completed / failed / cancelled / interrupted)
    queued → terminal                    (cancelled, replaced, or steered into the running turn)
    started → unknown → terminal         (the owner died mid-turn; an operator discards it)

Rules (research 03 §3.18):

- **Exact retry.** Admitting the same key with the same payload returns the row that exists, in
  whatever state; another payload under that key is ``admission_conflict``. At most
  ``QUEUE_LIMIT`` rows of a session are ``queued`` (``queue_full``), paused FIFO included.
- **Owner epoch.** ``begin_owner_epoch`` bumps one epoch row when a daemon starts (the caller holds
  the profile ownership lock). In the same write every row left ``started`` becomes ``unknown`` and
  is never replayed (R03.21); a steer transfer bound to such a claim becomes ``interrupted``. Every
  write names the caller's epoch; an outdated owner is refused ``stale_epoch``.
- **Claims and generations.** ``claim_head`` starts the session's oldest queued row under a fresh
  generation from the per-session counter (shared with the P17-03 turn lease). A session with an
  ``unknown`` row is blocked: nothing later starts until ``resolve_unknown`` (generation checked).
  ``settle`` is fenced by epoch and generation, so a displaced turn cannot settle its successor.
- **Steer transfers.** ``steer_into`` hands a queued row to the running claim (terminal, outcome
  ``steering``, bound to the claim's generation); the claim's ``settle`` turns it ``steered`` (the
  text reached a completed turn), ``interrupted`` (reached a turn that did not complete) or puts it
  back in its FIFO place (never delivered). ``revert_steer`` puts it back early.
- **Replacement.** ``admit(..., replaces=id)`` cancels a still-queued row and admits its successor
  at the tail in one transaction; the successor's exact retry answers only for that same row.
- **Retirement.** Rows that leave the table (``prune``, ``retire``, the session's deletion) leave a
  marker keyed like the row. An exact retry of a retired input answers with the retired receipt;
  any other payload under its key is a conflict; a deleted (tombstoned) session never takes fresh
  input (``retired``, R03.22).

Refusals are ``LedgerRefusal``: a bounded ``reason`` (the code is ``runtime.<reason>``), the
``field`` that failed and a ``detail`` cut to one printable line, credential shapes masked and
absolute paths shortened to their last part (``clean_text``; a settle's detail too). Every write
goes through the store's writer (``BEGIN IMMEDIATE``, time-budgeted retry; R03.8), so each
callback is idempotent.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from indus.foundation.errors import IndusError
from indus.foundation.redact import redact_text
from indus.state.registry import StoreHandle
from indus.state.sessions import is_tombstoned_in
from indus.state.writer import ROUTINE

__all__ = [
    "ANY_HEAD",
    "MAX_PAYLOAD_BYTES",
    "MAX_TEXT",
    "QUEUE_LIMIT",
    "RETENTION_SECONDS",
    "Admission",
    "AdmissionRow",
    "AdmissionState",
    "Claim",
    "ClaimKind",
    "LedgerRefusal",
    "OwnerStart",
    "Outcome",
    "RefusalReason",
    "SettleOutcome",
    "admit",
    "begin_owner_epoch",
    "cancel",
    "claim_head",
    "clean_text",
    "current_epoch",
    "find",
    "generation_in",
    "generation_of",
    "get",
    "next_generation_in",
    "payload_digest",
    "payload_text",
    "pending",
    "prune",
    "queue_position",
    "resolve_unknown",
    "retire",
    "retire_sessions_in",
    "revert_steer",
    "sessions_with_queued",
    "settle",
    "steer_into",
]

type AdmissionState = Literal["queued", "started", "terminal", "unknown"]
type Outcome = Literal[
    "completed",
    "failed",
    "cancelled",
    "interrupted",
    "steering",
    "steered",
    "discarded",
    "replaced",
]
type SettleOutcome = Literal["completed", "failed", "cancelled", "interrupted"]
type RefusalReason = Literal[
    "invalid_params",
    "not_found",
    "stale_epoch",
    "admission_conflict",
    "queue_full",
    "stale_generation",
    "unknown_execution",
    "admission_started",
    "session_idle",
    "retired",
]
type ClaimKind = Literal["claimed", "idle", "running", "blocked", "moved"]

QUEUE_LIMIT: Final = 32
MAX_TEXT: Final = 1024
MAX_PAYLOAD_BYTES: Final = 1024 * 1024
MAX_FINAL_TEXT: Final = 256 * 1024
RETENTION_SECONDS: Final = 7 * 86400.0
_FIELD_CHARS: Final = 80
_DETAIL_CHARS: Final = 160
_SETTLE_OUTCOMES: Final = frozenset({"completed", "failed", "cancelled", "interrupted"})
_STEERING: Final = "steering"
_COLUMNS: Final = (
    "seq, admission_id, principal_id, session_id, request_id, payload_json, digest, state, "
    "outcome, owner_epoch, generation, replaces, final_text, detail, created_at, updated_at"
)


_ABS_PATH: Final = re.compile(r"(?<![\w.~:/\\-])(?:[A-Za-z]:)?(?:[\\/][^\s\\/'\"`<>|]+){2,}[\\/]?")
"""An absolute path of two parts or more (POSIX or Windows); URLs are left alone."""


def _short_path(match: re.Match[str]) -> str:
    parts = [p for p in re.split(r"[\\/]", match.group(0)) if p]
    return "\u2026/" + parts[-1]


def clean_text(value: object, limit: int) -> str | None:
    """One printable line of at most ``limit`` characters (client text can reach logs):
    credential shapes masked, absolute paths cut to ``…/<last part>``."""
    if value is None:
        return None
    joined = " ".join(str(value).split())
    text = "".join(ch for ch in joined if ch.isprintable())
    text = _ABS_PATH.sub(_short_path, redact_text(text))
    return text[:limit] or None


class LedgerRefusal(IndusError):
    """The ledger refused a request: ``reason`` is the bounded code callers compare."""

    default_code = "runtime.refused"

    def __init__(
        self, reason: RefusalReason, *, field: str | None = None, detail: object = None
    ) -> None:
        self.reason: RefusalReason = reason
        self.field: str | None = clean_text(field, _FIELD_CHARS)
        self.detail_text: str | None = clean_text(detail, _DETAIL_CHARS)
        message = reason if self.field is None else f"{reason} ({self.field})"
        if self.detail_text:
            message = f"{message}: {self.detail_text}"
        super().__init__(f"runtime.{reason}", message, detail=self.detail_text)


@dataclass(frozen=True)
class AdmissionRow:
    admission_id: str
    seq: int
    principal_id: str
    session_id: str
    request_id: str
    payload: dict[str, Any]
    digest: str
    state: AdmissionState
    outcome: Outcome | None
    owner_epoch: int
    generation: int | None
    replaces: str | None
    final_text: str | None
    detail: str | None
    created_at: float
    updated_at: float
    retired: bool = False

    @property
    def terminal(self) -> bool:
        return self.state == "terminal"


@dataclass(frozen=True)
class Admission:
    """What ``admit`` answers: the row, and whether this call created it."""

    row: AdmissionRow
    created: bool


@dataclass(frozen=True)
class Claim:
    """``claimed``: ``row`` was started now. ``idle``: nothing queued. ``running``: ``row`` is the
    started claim. ``blocked``: ``row`` is the unknown turn the FIFO waits behind. ``moved``: the
    head is no longer the one the caller expected."""

    kind: ClaimKind
    row: AdmissionRow | None = None


@dataclass(frozen=True)
class OwnerStart:
    epoch: int
    unknown: tuple[str, ...]
    """Admission ids that were ``started`` under an older epoch and are now ``unknown``."""
    interrupted_steers: tuple[str, ...]


# -- helpers --------------------------------------------------------------------------------------


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT:
        raise LedgerRefusal("invalid_params", field=field)
    return value


def _encode(payload: object) -> str:
    if not isinstance(payload, Mapping):
        raise LedgerRefusal("invalid_params", field="payload", detail="not a JSON object")
    try:
        encoded = json.dumps(
            dict(cast(Mapping[str, Any], payload)),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise LedgerRefusal("invalid_params", field="payload", detail=exc) from exc
    if len(encoded.encode("utf-8", "surrogatepass")) > MAX_PAYLOAD_BYTES:
        raise LedgerRefusal("invalid_params", field="payload", detail="larger than 1 MiB")
    return encoded


def payload_digest(session_id: str, payload: Mapping[str, Any]) -> str:
    """The digest an admission is fingerprinted by (target and canonical payload)."""
    blob = json.dumps({"target": session_id, "input": json.loads(_encode(payload))}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8", "surrogatepass")).hexdigest()


def _now(store: StoreHandle) -> float:
    return store.scope.clock.now().timestamp()


def _row(raw: Sequence[Any]) -> AdmissionRow:
    try:
        payload = json.loads(str(raw[5]))
    except ValueError:
        payload = {}
    return AdmissionRow(
        seq=int(raw[0]),
        admission_id=str(raw[1]),
        principal_id=str(raw[2]),
        session_id=str(raw[3]),
        request_id=str(raw[4]),
        payload=cast(dict[str, Any], payload) if isinstance(payload, dict) else {},
        digest=str(raw[6]),
        state=cast(AdmissionState, str(raw[7])),
        outcome=None if raw[8] is None else cast(Outcome, str(raw[8])),
        owner_epoch=int(raw[9]),
        generation=None if raw[10] is None else int(raw[10]),
        replaces=None if raw[11] is None else str(raw[11]),
        final_text=None if raw[12] is None else str(raw[12]),
        detail=None if raw[13] is None else str(raw[13]),
        created_at=float(raw[14]),
        updated_at=float(raw[15]),
    )


def _retired_row(raw: Sequence[Any]) -> AdmissionRow:
    """A marker read back as a terminal, retired admission (payload is gone)."""
    return AdmissionRow(
        seq=0,
        principal_id=str(raw[0]),
        session_id=str(raw[1]),
        request_id=str(raw[2]),
        admission_id=str(raw[3]),
        digest=str(raw[4]),
        outcome=cast(Outcome, str(raw[5])),
        generation=None if raw[6] is None else int(raw[6]),
        final_text=None if raw[7] is None else str(raw[7]),
        payload={},
        state="terminal",
        owner_epoch=0,
        replaces=None,
        detail=None,
        created_at=float(raw[8]),
        updated_at=float(raw[8]),
        retired=True,
    )


_MARKER_COLUMNS: Final = (
    "principal_id, session_id, request_id, admission_id, digest, outcome, generation, "
    "final_text, retired_at"
)


def _by_id_in(conn: sqlite3.Connection, admission_id: str) -> AdmissionRow | None:
    found = conn.execute(
        f"SELECT {_COLUMNS} FROM runtime_admissions WHERE admission_id = ?", (admission_id,)
    ).fetchone()
    if found is not None:
        return _row(tuple(found))
    marker = conn.execute(
        f"SELECT {_MARKER_COLUMNS} FROM runtime_retired WHERE admission_id = ?", (admission_id,)
    ).fetchone()
    return None if marker is None else _retired_row(tuple(marker))


def _require(conn: sqlite3.Connection, admission_id: str) -> AdmissionRow:
    row = _by_id_in(conn, _text(admission_id, "admission_id"))
    if row is None:
        raise LedgerRefusal("not_found", field="admission_id")
    return row


def _by_key_in(
    conn: sqlite3.Connection, principal_id: str, session_id: str, request_id: str
) -> AdmissionRow | None:
    found = conn.execute(
        f"SELECT {_COLUMNS} FROM runtime_admissions "
        "WHERE principal_id = ? AND session_id = ? AND request_id = ?",
        (principal_id, session_id, request_id),
    ).fetchone()
    return None if found is None else _row(tuple(found))


def _marker_by_key_in(
    conn: sqlite3.Connection, principal_id: str, session_id: str, request_id: str
) -> AdmissionRow | None:
    found = conn.execute(
        f"SELECT {_MARKER_COLUMNS} FROM runtime_retired "
        "WHERE principal_id = ? AND session_id = ? AND request_id = ?",
        (principal_id, session_id, request_id),
    ).fetchone()
    return None if found is None else _retired_row(tuple(found))


def _check_epoch(conn: sqlite3.Connection, epoch: object) -> None:
    found = conn.execute("SELECT epoch FROM runtime_owner WHERE singleton = 1").fetchone()
    if isinstance(epoch, bool) or not isinstance(epoch, int) or found is None:
        raise LedgerRefusal("stale_epoch")
    if int(found[0]) != epoch:
        raise LedgerRefusal("stale_epoch")


def _generation_arg(generation: object) -> int:
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise LedgerRefusal("stale_generation", field="generation")
    return generation


def _set_state(
    conn: sqlite3.Connection, admission_id: str, now: float, sql: str, params: tuple[Any, ...]
) -> int:
    cur = conn.execute(
        f"UPDATE runtime_admissions SET {sql}, updated_at = ? WHERE admission_id = ?",
        (*params, now, admission_id),
    )
    return max(cur.rowcount, 0)


# -- epoch and generations ------------------------------------------------------------------------


def begin_owner_epoch(store: StoreHandle, instance_id: str) -> OwnerStart:
    """Start a new owner epoch and park every turn the previous owner left started.

    The caller must hold the profile ownership lock (one daemon per profile)."""
    _text(instance_id, "instance_id")
    now = _now(store)

    def apply(conn: sqlite3.Connection) -> OwnerStart:
        conn.execute(
            "INSERT INTO runtime_owner(singleton, epoch, instance_id, started_at) "
            "VALUES (1, 1, ?, ?) ON CONFLICT(singleton) DO UPDATE SET "
            "epoch = runtime_owner.epoch + 1, instance_id = excluded.instance_id, "
            "started_at = excluded.started_at",
            (instance_id, now),
        )
        found = conn.execute("SELECT epoch FROM runtime_owner WHERE singleton = 1").fetchone()
        epoch = int(found[0])
        lost = [
            str(r[0])
            for r in conn.execute(
                "SELECT admission_id FROM runtime_admissions "
                "WHERE state = 'started' AND owner_epoch != ? ORDER BY seq",
                (epoch,),
            )
        ]
        conn.execute(
            "UPDATE runtime_admissions SET state = 'unknown', updated_at = ? "
            "WHERE state = 'started' AND owner_epoch != ?",
            (now, epoch),
        )
        steers = [
            str(r[0])
            for r in conn.execute(
                "SELECT admission_id FROM runtime_admissions WHERE state = 'terminal' "
                "AND outcome = ? AND owner_epoch != ? ORDER BY seq",
                (_STEERING, epoch),
            )
        ]
        conn.execute(
            "UPDATE runtime_admissions SET outcome = 'interrupted', updated_at = ? "
            "WHERE state = 'terminal' AND outcome = ? AND owner_epoch != ?",
            (now, _STEERING, epoch),
        )
        return OwnerStart(epoch, tuple(lost), tuple(steers))

    return store.write(apply, budget=ROUTINE)


def current_epoch(store: StoreHandle) -> int | None:
    with store.read() as conn:
        found = conn.execute("SELECT epoch FROM runtime_owner WHERE singleton = 1").fetchone()
    return None if found is None else int(found[0])


def next_generation_in(conn: sqlite3.Connection, session_id: str, now: float) -> int:
    """Bump and return the session's generation (inside the caller's write transaction)."""
    conn.execute(
        "INSERT INTO runtime_generations(session_id, generation, updated_at) VALUES (?, 1, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET generation = runtime_generations.generation + 1, "
        "updated_at = excluded.updated_at",
        (session_id, now),
    )
    found = conn.execute(
        "SELECT generation FROM runtime_generations WHERE session_id = ?", (session_id,)
    ).fetchone()
    return int(found[0])


def generation_in(conn: sqlite3.Connection, session_id: str) -> int:
    found = conn.execute(
        "SELECT generation FROM runtime_generations WHERE session_id = ?", (session_id,)
    ).fetchone()
    return 0 if found is None else int(found[0])


def generation_of(store: StoreHandle, session_id: str) -> int:
    """The session's latest generation (0 before its first claim or lease)."""
    with store.read() as conn:
        return generation_in(conn, session_id)


# -- reads ----------------------------------------------------------------------------------------


def get(store: StoreHandle, admission_id: str) -> AdmissionRow | None:
    """The admission (or its retirement marker) by id."""
    with store.read() as conn:
        return _by_id_in(conn, admission_id)


def find(
    store: StoreHandle, principal_id: str, session_id: str, request_id: str
) -> AdmissionRow | None:
    """The admission at its key, or its retirement marker."""
    with store.read() as conn:
        row = _by_key_in(conn, principal_id, session_id, request_id)
        if row is None:
            row = _marker_by_key_in(conn, principal_id, session_id, request_id)
        return row


def pending(store: StoreHandle, session_id: str) -> list[AdmissionRow]:
    """The session's queued, started and unknown rows in FIFO order."""
    with store.read() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM runtime_admissions WHERE session_id = ? "
            "AND state IN ('queued', 'started', 'unknown') ORDER BY seq",
            (session_id,),
        ).fetchall()
    return [_row(tuple(r)) for r in rows]


def queue_position(rows: Sequence[AdmissionRow], admission_id: str) -> int | None:
    """1-based place of a queued row among ``rows`` (from ``pending``); None when not queued."""
    place = 0
    for row in rows:
        if row.state != "queued":
            continue
        place += 1
        if row.admission_id == admission_id:
            return place
    return None


def sessions_with_queued(store: StoreHandle) -> list[str]:
    """Sessions that hold queued input, oldest first (a starting owner restarts their workers)."""
    with store.read() as conn:
        rows = conn.execute(
            "SELECT session_id, MIN(seq) AS first FROM runtime_admissions "
            "WHERE state = 'queued' GROUP BY session_id ORDER BY first"
        ).fetchall()
    return [str(r[0]) for r in rows]


# -- admission ------------------------------------------------------------------------------------


def admit(
    store: StoreHandle,
    *,
    epoch: int,
    principal_id: str,
    session_id: str,
    request_id: str,
    payload: Mapping[str, Any],
    replaces: str | None = None,
    authorize: Callable[[sqlite3.Connection], None] | None = None,
) -> Admission:
    """Admit ``payload`` (see the module notes); ``replaces`` names a queued row to swap out.

    ``authorize(conn)`` is the owner's gate for a *new* row (a drain that began, a transcript
    rewrite bound to this input): it runs in the same transaction just before the insert, must not
    commit, may run again on a retry, and raises to refuse — rolling everything back. Exact
    retries never reach it: they cannot create or change accepted work."""
    _text(principal_id, "principal_id")
    _text(session_id, "session_id")
    _text(request_id, "request_id")
    if replaces is not None:
        _text(replaces, "replaces")
    encoded = _encode(payload)  # snapshot the caller's data before waiting for the writer
    digest = payload_digest(session_id, json.loads(encoded))
    now = _now(store)

    def apply(conn: sqlite3.Connection) -> Admission:
        _check_epoch(conn, epoch)
        old = _by_key_in(conn, principal_id, session_id, request_id)
        if old is not None:
            if old.digest != digest or old.replaces != replaces:
                raise LedgerRefusal("admission_conflict", field="request_id")
            return Admission(old, created=False)
        marker = _marker_by_key_in(conn, principal_id, session_id, request_id)
        if marker is not None:
            if marker.digest != digest:
                raise LedgerRefusal("admission_conflict", field="request_id")
            return Admission(marker, created=False)
        if is_tombstoned_in(conn, session_id):
            raise LedgerRefusal("retired", field="session_id")
        if replaces is not None:
            target = _by_id_in(conn, replaces)
            if target is None or target.retired:
                raise LedgerRefusal("not_found", field="replaces")
            if target.session_id != session_id or target.state != "queued":
                raise LedgerRefusal("admission_started", field="replaces")
            _set_state(conn, replaces, now, "state = 'terminal', outcome = 'replaced'", ())
        queued = int(
            conn.execute(
                "SELECT COUNT(*) FROM runtime_admissions WHERE session_id = ? AND state = 'queued'",
                (session_id,),
            ).fetchone()[0]
        )
        if queued >= QUEUE_LIMIT:
            raise LedgerRefusal("queue_full", field="session_id")
        if authorize is not None:
            authorize(conn)
        admission_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO runtime_admissions(admission_id, principal_id, session_id, request_id, "
            "payload_json, digest, state, owner_epoch, replaces, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
            (
                admission_id,
                principal_id,
                session_id,
                request_id,
                encoded,
                digest,
                epoch,
                replaces,
                now,
                now,
            ),
        )
        row = _by_id_in(conn, admission_id)
        assert row is not None
        return Admission(row, created=True)

    return store.write(apply, budget=ROUTINE)


# -- the owner's side -----------------------------------------------------------------------------


class _AnyHead:
    def __repr__(self) -> str:
        return "ANY_HEAD"


ANY_HEAD: Final = _AnyHead()


def claim_head(
    store: StoreHandle,
    *,
    epoch: int,
    session_id: str,
    expect_head: str | None | _AnyHead = ANY_HEAD,
    guard: Callable[[sqlite3.Connection], None] | None = None,
) -> Claim:
    """Start the session's oldest queued row under a fresh generation (see ``Claim``).

    ``expect_head`` is the admission id the caller checked before (None: it saw none queued); a
    different head by the time this write runs claims nothing (``moved``). ``guard(conn)`` (the
    owner's drain gate) runs first inside the transaction and raises to refuse the claim."""
    _text(session_id, "session_id")
    now = _now(store)

    def apply(conn: sqlite3.Connection) -> Claim:
        _check_epoch(conn, epoch)
        if guard is not None:
            guard(conn)
        live = conn.execute(
            f"SELECT {_COLUMNS} FROM runtime_admissions WHERE session_id = ? "
            "AND state IN ('started', 'unknown') ORDER BY seq",
            (session_id,),
        ).fetchall()
        rows = [_row(tuple(r)) for r in live]
        for row in rows:
            if row.state == "unknown":
                return Claim("blocked", row)
        if rows:
            return Claim("running", rows[0])
        head_raw = conn.execute(
            f"SELECT {_COLUMNS} FROM runtime_admissions WHERE session_id = ? AND state = 'queued' "
            "ORDER BY seq LIMIT 1",
            (session_id,),
        ).fetchone()
        head = None if head_raw is None else _row(tuple(head_raw))
        head_id = head.admission_id if head is not None else None
        if not isinstance(expect_head, _AnyHead) and head_id != expect_head:
            return Claim("moved", head)
        if head is None:
            return Claim("idle")
        generation = next_generation_in(conn, session_id, now)
        _set_state(
            conn,
            head.admission_id,
            now,
            "state = 'started', owner_epoch = ?, generation = ?",
            (epoch, generation),
        )
        claimed = _by_id_in(conn, head.admission_id)
        assert claimed is not None
        return Claim("claimed", claimed)

    return store.write(apply, budget=ROUTINE)


def _settle_transfers_in(
    conn: sqlite3.Connection,
    *,
    epoch: int,
    generation: int,
    transfers: Mapping[str, bool],
    outcome: str,
    now: float,
) -> None:
    for admission_id, delivered in transfers.items():
        if not delivered:
            _requeue_in(conn, admission_id, epoch=epoch, generation=generation, now=now)
            continue
        conn.execute(
            "UPDATE runtime_admissions SET outcome = ?, updated_at = ? WHERE admission_id = ? "
            "AND state = 'terminal' AND outcome = ? AND owner_epoch = ? AND generation = ?",
            (
                "steered" if outcome == "completed" else "interrupted",
                now,
                admission_id,
                _STEERING,
                epoch,
                generation,
            ),
        )


def settle(
    store: StoreHandle,
    *,
    epoch: int,
    admission_id: str,
    generation: int,
    outcome: SettleOutcome,
    final_text: str | None = None,
    detail: str | None = None,
    transfers: Mapping[str, bool] | None = None,
) -> AdmissionRow:
    """End a started claim with ``outcome``; fenced by epoch and generation.

    ``transfers`` maps each queued row steered into this claim to whether its text reached the
    turn. Settling the same generation again returns the settled row."""
    if outcome not in _SETTLE_OUTCOMES:
        raise LedgerRefusal("invalid_params", field="outcome")
    _generation_arg(generation)
    text = None if final_text is None else final_text[:MAX_FINAL_TEXT]
    note = clean_text(detail, _DETAIL_CHARS)
    moved = dict(transfers or {})
    now = _now(store)

    def apply(conn: sqlite3.Connection) -> AdmissionRow:
        _check_epoch(conn, epoch)
        row = _require(conn, admission_id)
        if row.terminal and row.generation == generation and row.outcome in _SETTLE_OUTCOMES:
            return row  # an exact repeat of this settle
        if row.state != "started" or row.owner_epoch != epoch or row.generation != generation:
            raise LedgerRefusal("stale_generation", field="generation")
        _set_state(
            conn,
            admission_id,
            now,
            "state = 'terminal', outcome = ?, final_text = ?, detail = ?",
            (outcome, text, note),
        )
        _settle_transfers_in(
            conn, epoch=epoch, generation=generation, transfers=moved, outcome=outcome, now=now
        )
        settled = _by_id_in(conn, admission_id)
        assert settled is not None
        return settled

    return store.write(apply, budget=ROUTINE)


def cancel(store: StoreHandle, *, epoch: int, admission_id: str) -> AdmissionRow:
    """Cancel a queued row. A started or steering row belongs to its running turn
    (``admission_started``); an unknown one needs ``resolve_unknown``."""
    now = _now(store)

    def apply(conn: sqlite3.Connection) -> AdmissionRow:
        _check_epoch(conn, epoch)
        row = _require(conn, admission_id)
        if row.state == "unknown":
            raise LedgerRefusal("unknown_execution", field="admission_id")
        if row.state == "started" or (row.terminal and row.outcome == _STEERING):
            raise LedgerRefusal("admission_started", field="admission_id")
        if row.state == "queued":
            _set_state(conn, admission_id, now, "state = 'terminal', outcome = 'cancelled'", ())
            row = _require(conn, admission_id)
        return row

    return store.write(apply, budget=ROUTINE)


def steer_into(
    store: StoreHandle, *, epoch: int, admission_id: str, generation: int
) -> tuple[AdmissionRow, bool]:
    """Hand a queued row to the running claim ``generation`` of its session.

    ``(row, True)`` when this call made the transfer; ``(row, False)`` for a transfer already made
    (an exact retry; the text must not be delivered twice)."""
    _generation_arg(generation)
    now = _now(store)

    def apply(conn: sqlite3.Connection) -> tuple[AdmissionRow, bool]:
        _check_epoch(conn, epoch)
        row = _require(conn, admission_id)
        if row.terminal and row.outcome in (_STEERING, "steered"):
            return row, False
        if row.state != "queued":
            raise LedgerRefusal("admission_started", field="admission_id")
        running = conn.execute(
            "SELECT 1 FROM runtime_admissions WHERE session_id = ? AND state = 'started' "
            "AND owner_epoch = ? AND generation = ?",
            (row.session_id, epoch, generation),
        ).fetchone()
        if running is None:
            raise LedgerRefusal("session_idle", field="generation")
        _set_state(
            conn,
            admission_id,
            now,
            "state = 'terminal', outcome = ?, owner_epoch = ?, generation = ?",
            (_STEERING, epoch, generation),
        )
        return _require(conn, admission_id), True

    return store.write(apply, budget=ROUTINE)


def _requeue_in(
    conn: sqlite3.Connection, admission_id: str, *, epoch: int, generation: int, now: float
) -> bool:
    cur = conn.execute(
        "UPDATE runtime_admissions SET state = 'queued', outcome = NULL, generation = NULL, "
        "updated_at = ? WHERE admission_id = ? AND state = 'terminal' AND outcome = ? "
        "AND owner_epoch = ? AND generation = ?",
        (now, admission_id, _STEERING, epoch, generation),
    )
    return cur.rowcount == 1


def revert_steer(store: StoreHandle, *, epoch: int, admission_id: str, generation: int) -> bool:
    """Put a transfer whose text never reached the turn back in its FIFO place (its ``seq``).

    False when the claim's settle already decided it."""
    _generation_arg(generation)
    now = _now(store)

    def apply(conn: sqlite3.Connection) -> bool:
        _check_epoch(conn, epoch)
        return _requeue_in(conn, admission_id, epoch=epoch, generation=generation, now=now)

    return store.write(apply, budget=ROUTINE)


def resolve_unknown(
    store: StoreHandle,
    *,
    epoch: int,
    admission_id: str,
    generation: int,
    terminal_write: Callable[[sqlite3.Connection, AdmissionRow], None] | None = None,
) -> AdmissionRow:
    """An operator's discard of a turn lost in a restart; never requeues it.

    ``generation`` must be the unknown row's; a repeat of a done discard returns the row.
    ``terminal_write(conn, row)`` is the owner's effect in the same transaction (closing the lost
    turn in its transcript); raising rolls the discard back, so the FIFO never moves past a lost
    turn that was not closed."""
    gen = _generation_arg(generation)
    now = _now(store)

    def apply(conn: sqlite3.Connection) -> AdmissionRow:
        _check_epoch(conn, epoch)
        row = _require(conn, admission_id)
        if row.terminal and row.outcome == "discarded" and row.generation == gen:
            return row
        if row.state != "unknown" or row.generation != gen:
            raise LedgerRefusal("stale_generation", field="generation")
        if terminal_write is not None:
            terminal_write(conn, row)
        _set_state(conn, admission_id, now, "state = 'terminal', outcome = 'discarded'", ())
        return _require(conn, admission_id)

    return store.write(apply, budget=ROUTINE)


# -- retirement -----------------------------------------------------------------------------------


def _retire_rows_in(conn: sqlite3.Connection, rows: Iterable[AdmissionRow], now: float) -> int:
    count = 0
    for row in rows:
        outcome: str = row.outcome or "cancelled"
        if row.state == "unknown":
            outcome = "discarded"
        elif row.state == "started" or outcome == _STEERING:
            outcome = "interrupted"
        conn.execute(
            f"INSERT OR REPLACE INTO runtime_retired({_MARKER_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.principal_id,
                row.session_id,
                row.request_id,
                row.admission_id,
                row.digest,
                outcome,
                row.generation,
                row.final_text,
                now,
            ),
        )
        conn.execute("DELETE FROM runtime_admissions WHERE admission_id = ?", (row.admission_id,))
        count += 1
    return count


def retire_sessions_in(conn: sqlite3.Connection, session_ids: Iterable[str], now: float) -> int:
    """Retire every admission of ``session_ids`` inside their deletion's transaction."""
    total = 0
    for sid in dict.fromkeys(session_ids):
        rows = [
            _row(tuple(r))
            for r in conn.execute(
                f"SELECT {_COLUMNS} FROM runtime_admissions WHERE session_id = ? ORDER BY seq",
                (sid,),
            )
        ]
        total += _retire_rows_in(conn, rows, now)
        conn.execute("DELETE FROM runtime_generations WHERE session_id = ?", (sid,))
    return total


def retire(store: StoreHandle, admission_ids: Iterable[str]) -> int:
    """Retire terminal admissions by id (non-terminal ones are left alone); returns how many."""
    wanted = list(dict.fromkeys(admission_ids))
    now = _now(store)

    def apply(conn: sqlite3.Connection) -> int:
        rows: list[AdmissionRow] = []
        for admission_id in wanted:
            found = conn.execute(
                f"SELECT {_COLUMNS} FROM runtime_admissions WHERE admission_id = ? "
                "AND state = 'terminal' AND (outcome IS NULL OR outcome != ?)",
                (admission_id, _STEERING),
            ).fetchone()
            if found is not None:
                rows.append(_row(tuple(found)))
        return _retire_rows_in(conn, rows, now)

    return store.write(apply, budget=ROUTINE)


def prune(store: StoreHandle, *, older_than: float = RETENTION_SECONDS) -> int:
    """Retire terminal admissions last changed more than ``older_than`` seconds ago."""
    now = _now(store)

    def apply(conn: sqlite3.Connection) -> int:
        rows = [
            _row(tuple(r))
            for r in conn.execute(
                f"SELECT {_COLUMNS} FROM runtime_admissions WHERE state = 'terminal' "
                "AND (outcome IS NULL OR outcome != ?) AND updated_at < ? ORDER BY seq",
                (_STEERING, now - older_than),
            )
        ]
        return _retire_rows_in(conn, rows, now)

    return store.write(apply, budget=ROUTINE)


def payload_text(row: AdmissionRow) -> str:
    """The input text an admission carries (``payload["text"]``), or ''."""
    value = row.payload.get("text")
    return value if isinstance(value, str) else ""
