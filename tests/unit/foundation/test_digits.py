"""Strict ASCII-digit parsing for outside text (SEC-6, wave 46)."""

from __future__ import annotations

import pytest

from indus.foundation.digits import MAX_DIGITS, is_digits, parse_digits


@pytest.mark.parametrize(
    ("text", "value"), [("0", 0), ("7", 7), ("0042", 42), ("9" * 20, 10**20 - 1)]
)
def test_plain_ascii_digits_parse(text: str, value: int) -> None:
    assert is_digits(text)
    assert parse_digits(text) == value


@pytest.mark.parametrize(
    "text",
    [
        "",
        "²",  # superscript two: str.isdigit() is true, int() raises
        "①",  # circled one: the same
        "١٢",  # Arabic-Indic digits: int() takes them, we do not
        "１２",  # fullwidth digits
        " 12",
        "12 ",
        "1_000",
        "+5",
        "-5",
        "1.5",
        "0x10",
        "9" * (MAX_DIGITS + 1),
    ],
)
def test_anything_else_is_not_a_number(text: str) -> None:
    assert not is_digits(text)
    assert parse_digits(text) is None


@pytest.mark.parametrize("value", [None, 12, 1.0, b"12", ["1"], True])
def test_non_text_is_not_a_number(value: object) -> None:
    assert not is_digits(value)
    assert parse_digits(value) is None


def test_signed_takes_one_leading_minus() -> None:
    assert parse_digits("-1001234567890", signed=True) == -1001234567890
    assert parse_digits("17", signed=True) == 17
    for text in ("-", "--5", "-²", "5-", "+5", "- 5"):
        assert parse_digits(text, signed=True) is None, text


def test_length_cap_counts_digits_not_the_sign() -> None:
    assert parse_digits("123", max_len=3) == 123
    assert parse_digits("1234", max_len=3) is None
    assert parse_digits("-123", max_len=3, signed=True) == -123


def test_a_huge_digit_string_is_refused_without_raising() -> None:
    """``int()`` raises past 4,300 digits (and is slow before that); the cap answers first."""
    assert parse_digits("7" * 5000) is None
    assert parse_digits("7" * 5000, max_len=10_000) is None  # no caller can lift it past int()
    assert parse_digits("7" * 4000, max_len=10_000) == int("7" * 4000)
