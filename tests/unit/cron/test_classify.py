"""Silence and failure tokens (P14-12, R07.14, R06.14)."""

from __future__ import annotations

import pytest

from indus.cron.classify import (
    DEFAULT_FAILURE_EVIDENCE,
    FAILURE_TOKEN,
    classify_reply,
    failure_evidence,
    is_silent,
)


@pytest.mark.parametrize(
    "text",
    [
        "[SILENT]",
        "  [SILENT]  \n",
        "nothing new.\n[SILENT]",
        "[SILENT]\nnothing changed since the last run",
        "silent",
        "Silent",
        "NO_REPLY",
        "no reply",
        "No   Reply",
        "[silent]",
    ],
)
def test_silent_forms(text: str) -> None:
    assert is_silent(text)
    assert classify_reply(text).status == "silent"
    assert not classify_reply(text).delivers


@pytest.mark.parametrize(
    "text",
    [
        "I will stay [SILENT] about the rest",
        "The build is green. Nothing else to say, so: silent mode off.",
        "[SILENT] no changes detected",  # the token is not alone on its line
        "",
        "   ",
        "Silent retry succeeded",
    ],
)
def test_content_is_not_silent(text: str) -> None:
    assert not is_silent(text)


def test_failure_token_on_the_first_line_fails_the_run() -> None:
    reply = f"{FAILURE_TOKEN}\nthe deploy check returned 503 three times"
    assert failure_evidence(reply) == "the deploy check returned 503 three times"
    verdict = classify_reply(reply)
    assert verdict.status == "failed" and verdict.delivers
    assert verdict.evidence == "the deploy check returned 503 three times"


def test_failure_token_alone_has_default_evidence() -> None:
    assert failure_evidence(f"  {FAILURE_TOKEN}  ") == DEFAULT_FAILURE_EVIDENCE


def test_failure_token_elsewhere_is_content() -> None:
    reply = f"Report\n{FAILURE_TOKEN}\nall good"
    assert failure_evidence(reply) is None
    assert classify_reply(reply).status == "ok"
    assert classify_reply(f"I did not use {FAILURE_TOKEN} today").status == "ok"


def test_failed_and_silent_is_delivered() -> None:
    # A failure always reaches the destination, even when the text also looks silent.
    reply = f"{FAILURE_TOKEN}\n[SILENT]"
    verdict = classify_reply(reply)
    assert verdict.status == "failed" and verdict.delivers
