"""P15-17: silence tokens and final-reply masking (R06.14, R07.14)."""

from __future__ import annotations

import pytest

from indus.gateway.outbound.filters import (
    SILENT_NOTICE,
    could_become_silence,
    decide_silence,
    is_silence,
    redact_final,
)


@pytest.mark.parametrize(
    ("text", "silent"),
    [
        ("[SILENT]", True),
        ("SILENT", True),
        ("silent", True),
        ("  no_reply \n", True),
        ("NO   REPLY", True),
        ("No Reply", True),
        ("**NO_REPLY**", True),
        (".NO_REPLY.", True),
        ("[SILENT", False),
        ("SILENTLY", False),
        ("I will stay SILENT on this.", False),
        ("NO_REPLY needed? I think not.", False),
        ("Reply with NO_REPLY when nothing changed.", False),
        ("", False),
        ("   ", False),
        (" " * 30 + "SILENT" + " " * 40, True),
        ("x" * 60 + " SILENT", False),
    ],
)
def test_silence_table(text: str, silent: bool) -> None:
    assert is_silence(text) is silent


@pytest.mark.parametrize(
    ("text", "held"),
    [
        ("NO", True),
        ("NO_R", True),
        ("[SIL", True),
        ("sil", True),
        ("Hello", False),
        ("NO_REPLY!", True),
    ],
)
def test_streamed_prefixes_of_a_token_are_held(text: str, held: bool) -> None:
    assert could_become_silence(text) is held


@pytest.mark.parametrize(
    ("text", "failed", "reply_expected", "internal", "decision"),
    [
        ("NO_REPLY", False, None, True, "silent"),
        ("NO_REPLY", False, False, False, "silent"),
        ("NO_REPLY", False, True, False, "notice"),
        ("NO_REPLY", False, None, False, "notice"),
        ("NO_REPLY", True, False, True, "deliver"),
        ("NO_REPLY is my answer", False, False, True, "deliver"),
    ],
)
def test_decide_silence(
    text: str, failed: bool, reply_expected: bool | None, internal: bool, decision: str
) -> None:
    got = decide_silence(text, failed=failed, reply_expected=reply_expected, internal=internal)
    assert got == decision


def test_failed_turn_is_never_silenced() -> None:
    assert decide_silence("[SILENT]", failed=True, reply_expected=False, internal=True) == "deliver"


def test_notice_text_is_short() -> None:
    assert 0 < len(SILENT_NOTICE) <= 40


def test_redact_final_masks_tokens_without_a_scope() -> None:
    token = "ghp_" + "a1B2" * 9
    masked = redact_final(f"use {token} here", None)
    assert token not in masked and masked.startswith("use ")
