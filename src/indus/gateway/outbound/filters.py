"""Final-reply filters: secret masking and silence tokens (P15-17, R06.14, R07.14).

A reply that is *only* a silence token (``[SILENT]``, ``SILENT``, ``NO_REPLY``, ``NO REPLY``;
whitespace, case and stray edge punctuation ignored, at most 64 characters) means the agent chose
not to answer: nothing is sent, the turn stays in the transcript. Prose that mentions a token is
delivered. A failed turn is never silenced, and a person who wrote to us expecting an answer gets
a short notice instead of nothing; only internal wakes and messages the adapter knows need no
reply may stay silent.
"""

from __future__ import annotations

import unicodedata
from typing import Literal

from indus.foundation.scope import Scope
from indus.safety.redaction import redact_output

__all__ = [
    "SILENCE_TOKENS",
    "SILENT_NOTICE",
    "SilenceDecision",
    "could_become_silence",
    "decide_silence",
    "is_silence",
    "redact_final",
]

SILENCE_TOKENS = frozenset({"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"})
SILENT_NOTICE = "Nothing further to add."
_MAX_TOKEN_TEXT = 64

type SilenceDecision = Literal["deliver", "silent", "notice"]


def _edge_punctuation(char: str) -> bool:
    # brackets are part of `[SILENT]`, so `[SILENT` never collapses into `SILENT`
    return char not in "[]" and unicodedata.category(char).startswith("P")


def _forms(text: str) -> list[str]:
    stripped = text.strip()
    if not 0 < len(stripped) <= _MAX_TOKEN_TEXT:
        return []
    start, end = 0, len(stripped)
    while start < end and _edge_punctuation(stripped[start]):
        start += 1
    while end > start and _edge_punctuation(stripped[end - 1]):
        end -= 1
    candidates = {stripped, stripped[start:end].strip()}
    return [" ".join(c.upper().split()) for c in candidates if c]


def is_silence(text: str) -> bool:
    """True when the whole of ``text`` is a silence token."""
    return any(form in SILENCE_TOKENS for form in _forms(text))


def could_become_silence(text: str) -> bool:
    """True while streamed ``text`` is a non-empty prefix of a token (``NO`` on the way to
    ``NO_REPLY``): the stream holds such text back instead of showing and retracting it."""
    return any(
        form and any(token.startswith(form) for token in SILENCE_TOKENS) for form in _forms(text)
    )


def decide_silence(
    text: str,
    *,
    failed: bool,
    reply_expected: bool | None,
    internal: bool,
) -> SilenceDecision:
    """``deliver`` the reply, stay ``silent``, or send the short ``notice`` instead."""
    if failed or not is_silence(text):
        return "deliver"
    if internal or reply_expected is False:
        return "silent"
    return "notice"


def redact_final(text: str, scope: Scope | None) -> str:
    """``text`` with secrets masked (P05 redactor). Forced on: a chat reply is masked even when
    ``security.redact_secrets`` is off."""
    return redact_output(text, force=True, scope=scope)
