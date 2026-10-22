# pyright: strict
"""Strict parsing of decimal numbers that arrive as outside text (SEC-6).

``str.isdigit()`` is true for far more than ``0``-``9``: superscripts (``"²"``), circled digits
(``"①"``) and other scripts' digits. ``int()`` then raises on some of them, and on a string of
thousands of digits it raises or burns CPU. A header, a chat message, a platform id or a peer's
paging token is outside text, so a check of the form ``text.isdigit()`` followed by ``int(text)``
can crash a handler that should have answered "not a number".

:func:`parse_digits` accepts only ASCII ``0``-``9`` (and one leading ``-`` when asked), at most
``max_len`` digits, and answers ``None`` for anything else. It never raises. Whitespace is not
trimmed: a caller that tolerates it strips first, so what is accepted stays visible at the call.
:func:`is_digits` is the same test without the conversion, for ids kept as text.
"""

from __future__ import annotations

from typing import Final, TypeGuard

# 20 digits hold every unsigned 64-bit value (platform snowflakes, Content-Length, ms stamps).
MAX_DIGITS: Final = 20

# No caller may raise the cap past this: ``int()`` refuses longer strings (4,300 digits by default).
HARD_MAX_DIGITS: Final = 4000

_ASCII_DIGITS: Final = frozenset("0123456789")


def is_digits(text: object, *, max_len: int = MAX_DIGITS, signed: bool = False) -> TypeGuard[str]:
    """True when ``text`` is 1..``max_len`` ASCII digits (after one ``-`` when ``signed``).

    A true answer makes ``int(text)`` safe: it cannot raise and stays cheap.
    """
    if not isinstance(text, str):
        return False
    body = text[1:] if signed and text.startswith("-") else text
    return 0 < len(body) <= min(max_len, HARD_MAX_DIGITS) and all(
        ch in _ASCII_DIGITS for ch in body
    )


def parse_digits(text: object, *, max_len: int = MAX_DIGITS, signed: bool = False) -> int | None:
    """The integer ``text`` spells in ASCII digits, or ``None`` (see the module docstring)."""
    if not is_digits(text, max_len=max_len, signed=signed):
        return None
    return int(text)
