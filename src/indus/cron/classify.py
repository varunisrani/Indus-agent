"""What a finished cron run means: failed, silent or a report (P14-12, R07.14, R06.14).

Failure: the run raised, timed out or never reached the model, or the reply's first line is
exactly ``FAILURE_TOKEN`` (the lines after it are the evidence; the token anywhere else is plain
content). Silence: the whole reply, or its first line alone, or its last line alone, is one of
``SILENCE_TOKENS`` (case and surrounding spaces ignored); a token inside a sentence is content.
Silence is a success whose delivery is suppressed. A failed run is always delivered, whatever its
text looks like.

``RunOutcome`` is what the runner hands to bookkeeping and delivery (C3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

__all__ = [
    "FAILURE_TOKEN",
    "SILENCE_TOKENS",
    "RunOutcome",
    "RunStatus",
    "Verdict",
    "classify_reply",
    "failure_evidence",
    "is_silent",
]

FAILURE_TOKEN = "[JOB_FAILED]"
SILENCE_TOKENS: tuple[str, ...] = ("[SILENT]", "SILENT", "NO_REPLY", "NO REPLY")
_SILENCE = frozenset(token.upper() for token in SILENCE_TOKENS)
DEFAULT_FAILURE_EVIDENCE = "The job reported a failure without details."

RunStatus = Literal["ok", "silent", "failed", "blocked_config", "skipped", "no_change"]


def failure_evidence(text: str) -> str | None:
    """The evidence when the reply's first line is the failure token alone, else None."""
    lines = (text or "").splitlines()
    if not lines or lines[0].strip() != FAILURE_TOKEN:
        return None
    rest = "\n".join(lines[1:]).strip()
    return rest or DEFAULT_FAILURE_EVIDENCE


def _is_token(line: str) -> bool:
    return " ".join(line.split()).upper() in _SILENCE


def is_silent(text: str) -> bool:
    """True when the reply asks for no delivery (see the module notes)."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    lines = [line for line in stripped.splitlines() if line.strip()]
    return _is_token(stripped) or _is_token(lines[0]) or _is_token(lines[-1])


@dataclass(frozen=True)
class Verdict:
    status: Literal["ok", "silent", "failed"]
    evidence: str | None = None

    @property
    def delivers(self) -> bool:
        return self.status != "silent"


def classify_reply(text: str) -> Verdict:
    """The verdict on a completed agent reply. The failure token wins over silence."""
    evidence = failure_evidence(text)
    if evidence is not None:
        return Verdict("failed", evidence)
    if is_silent(text):
        return Verdict("silent")
    return Verdict("ok")


@dataclass(frozen=True)
class RunOutcome:
    """One run's result, for delivery, bookkeeping and incidents (C3).

    ``message`` is what a delivery would send (the reply, or the failure / block text);
    ``deliver`` false means suppressed (silent, a repeated preflight block, a skip).
    ``unreachable`` marks a failure before any model call for a network reason (C3's re-run
    ladder); ``retry_after`` is the provider's reopening instant on a closed usage window;
    ``api_calls`` counts the model calls that were answered (a quota hold needs none).
    """

    status: RunStatus
    message: str = ""
    deliver: bool = False
    error: str | None = None
    document: Path | None = None
    kind: Literal["scheduled", "manual", "retry"] = "scheduled"
    scheduled_for: datetime | None = None
    unreachable: bool = False
    retry_after: datetime | None = None
    failure_reason: str | None = None
    job_removed: bool = False
    session_id: str | None = None
    api_calls: int = 0

    @property
    def failed(self) -> bool:
        return self.status in ("failed", "blocked_config")
