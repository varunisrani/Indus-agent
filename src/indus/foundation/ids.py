"""Minting and recognising product ids. A new id shape is added here or nowhere.

Shapes (00-layout section 2):

- session ``YYYYMMDD_HHMMSS_<hex>``: 6 hex for interactive surfaces, 8 for the gateway, 12 for
  imports (many rows minted in the same second);
- cron run ``cron_<job>_<YYYYmmdd_HHMMSS>``;
- background ``bg_<HHMMSS>_<6 hex>``;
- API session ``api_<epoch seconds>_<8 hex>``;
- message uid: 32 lowercase hex.

Clock and random source are injectable so tests get exact ids.
"""

from __future__ import annotations

import random
import re
import secrets
from datetime import datetime
from enum import StrEnum
from typing import Protocol

SESSION_HEX_LENGTHS = (6, 8, 12)
INTERACTIVE_HEX = 6
GATEWAY_HEX = 8
IMPORT_HEX = 12
MAX_ID_LENGTH = 128


class _HasNow(Protocol):
    def now(self) -> datetime: ...


class IdShape(StrEnum):
    SESSION = "session"
    CRON_RUN = "cron_run"
    BACKGROUND = "background"
    API_SESSION = "api_session"
    MESSAGE_UID = "message_uid"


_JOB = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
_PATTERNS: dict[IdShape, re.Pattern[str]] = {
    IdShape.SESSION: re.compile(r"(\d{8})_(\d{6})_(?:[0-9a-f]{6}|[0-9a-f]{8}|[0-9a-f]{12})"),
    IdShape.CRON_RUN: re.compile(rf"cron_{_JOB}_(\d{{8}})_(\d{{6}})"),
    IdShape.BACKGROUND: re.compile(r"bg_()(\d{6})_[0-9a-f]{6}"),
    IdShape.API_SESSION: re.compile(r"api_[1-9]\d{8,11}_[0-9a-f]{8}"),
    IdShape.MESSAGE_UID: re.compile(r"[0-9a-f]{32}"),
}
_SESSION_SHAPES = frozenset(
    {IdShape.SESSION, IdShape.CRON_RUN, IdShape.BACKGROUND, IdShape.API_SESSION}
)
_JOB_ID = re.compile(_JOB)


def _rng(rng: random.Random | None) -> random.Random:
    return rng if rng is not None else secrets.SystemRandom()


def _hex(rng: random.Random | None, length: int) -> str:
    return format(_rng(rng).getrandbits(length * 4), f"0{length}x")


def new_session_id(
    clock: _HasNow, rng: random.Random | None = None, hex_len: int = INTERACTIVE_HEX
) -> str:
    """A fresh session id stamped with ``clock.now()`` in the clock's own zone."""
    if hex_len not in SESSION_HEX_LENGTHS:
        raise ValueError(f"session id hex length must be one of {SESSION_HEX_LENGTHS}")
    return f"{clock.now():%Y%m%d_%H%M%S}_{_hex(rng, hex_len)}"


def new_message_uid(rng: random.Random | None = None) -> str:
    return _hex(rng, 32)


def new_cron_run_id(job_id: str, clock: _HasNow) -> str:
    if not _JOB_ID.fullmatch(job_id):
        raise ValueError(f"invalid cron job id: {job_id!r}")
    return f"cron_{job_id}_{clock.now():%Y%m%d_%H%M%S}"


def new_background_id(clock: _HasNow, rng: random.Random | None = None) -> str:
    return f"bg_{clock.now():%H%M%S}_{_hex(rng, 6)}"


def new_api_session_id(clock: _HasNow, rng: random.Random | None = None) -> str:
    return f"api_{int(clock.now().timestamp())}_{_hex(rng, 8)}"


def _stamp_ok(date: str, time: str) -> bool:
    try:
        if date:
            datetime.strptime(date + time, "%Y%m%d%H%M%S")
        else:
            datetime.strptime(time, "%H%M%S")
    except ValueError:
        return False
    return True


def recognise(value: object) -> IdShape | None:
    """The shape ``value`` has, or None for anything we never mint."""
    if not isinstance(value, str) or not value or len(value) > MAX_ID_LENGTH:
        return None
    for shape, pattern in _PATTERNS.items():
        match = pattern.fullmatch(value)
        if match is None:
            continue
        if pattern.groups >= 2 and not _stamp_ok(match.group(1), match.group(2)):
            return None
        return shape
    return None


def is_valid_session_id(value: object) -> bool:
    """True for every shape a store may hold as a session id (not message uids)."""
    return recognise(value) in _SESSION_SHAPES
