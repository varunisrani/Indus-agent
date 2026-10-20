"""Masking secrets in text and nested objects. The one implementation; later layers wrap it.

Two layers find the secret parts of a text:

1. shape patterns — credentials that look like credentials (vendor key prefixes, bearer headers,
   JWTs, PEM private keys, URL passwords and sensitive query parameters, ``NAME=value`` and
   ``"name": "value"`` where the name is credential-shaped);
2. exact values the profile holds — every ``SecretStore`` value and every secret-looking ``.env``
   value of at least ``MIN_EXACT_LENGTH`` characters, longest first.

Every finder reports the span of the secret part only. Overlapping spans are merged and each merged
span is masked once. The on/off switch is read from the scope on every call; ``force=True`` (spill
files) ignores it.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol, cast

from indus.foundation.envfile import read_env_file
from indus.foundation.envload import redaction_flag

log = logging.getLogger(__name__)

MIN_EXACT_LENGTH = 8
MASK_KEEP = 4
MASK_MIN_PARTIAL = 12
MASK_SHORT = "[redacted]"
MASK_GAP = "…"
DEPTH_MARKER = "[depth limit]"
CYCLE_MARKER = "[cycle]"
MAX_DEPTH = 64


class _Store(Protocol):
    @property
    def generation(self) -> int: ...

    def known_values(self) -> list[str]: ...


class RedactScope(Protocol):
    """What the redactor reads from a ``Scope``."""

    @property
    def home(self) -> Path: ...

    @property
    def env(self) -> Mapping[str, str]: ...

    @property
    def secrets(self) -> _Store | None: ...


def mask(value: str) -> str:
    """``abcd…wxyz`` for values of 12 characters or more, else ``[redacted]``."""
    if len(value) >= MASK_MIN_PARTIAL:
        return f"{value[:MASK_KEEP]}{MASK_GAP}{value[-MASK_KEEP:]}"
    return MASK_SHORT


# -- shape patterns ----------------------------------------------------------------------------
# Each pattern has a group named "s" covering the part to mask.

_EDGE = r"(?<![A-Za-z0-9_\-])"
_VENDOR_KEYS = re.compile(
    _EDGE
    + r"(?P<s>"
    + "|".join(
        [
            r"sk-ant-[A-Za-z0-9_\-]{16,400}",
            r"sk-or-[A-Za-z0-9_\-]{16,400}",
            r"sk-[A-Za-z0-9_\-]{16,400}",
            r"gh[opsur]_[A-Za-z0-9]{20,255}",
            r"github_pat_[A-Za-z0-9_]{20,255}",
            r"xox[abpr]-[A-Za-z0-9\-]{10,255}",
            r"AKIA[0-9A-Z]{16}",
            r"AIza[0-9A-Za-z_\-]{30,60}",
            r"hf_[A-Za-z0-9]{20,255}",
            r"glpat-[A-Za-z0-9_\-]{20,255}",
        ]
    )
    + r")"
)

_AUTH_HEADER = re.compile(
    r"(?i)\b(?:proxy-)?authorization\s*[:=]\s*[\"']?(?:bearer|basic|token|bot)\s+(?P<s>[^\s\"',;]{4,4096})"
)
_BEARER = re.compile(r"(?i)\bbearer\s+(?P<s>[A-Za-z0-9._~+/=\-]{12,4096})")
_JWT = re.compile(
    r"(?<![A-Za-z0-9_\-])(?P<s>eyJ[A-Za-z0-9_\-]{6,4096}\.[A-Za-z0-9_\-]{6,8192}\.[A-Za-z0-9_\-]{6,4096})"
)
_PEM_BEGIN = re.compile(r"-----BEGIN (?:[A-Z0-9]+ ){0,4}PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END (?:[A-Z0-9]+ ){0,4}PRIVATE KEY-----")
_URL_USERINFO = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.\-]{0,31}://[^\s/:@?#]{1,256}:(?P<s>[^\s/@?#]{1,256})@"
)
_QUERY_NAMES = (
    "token|key|api_key|apikey|access_token|refresh_token|id_token|secret|client_secret|"
    "password|passwd|sig|signature|auth|ticket"
)
_QUERY_PARAM = re.compile(r"(?i)[?&](?:" + _QUERY_NAMES + r")=(?P<s>[^&#\s\"'<>]{1,4096})")
# WebSocket tickets (P17-10): offered as a subprotocol, or (refused, but possible in a log) as a
# ``ticket`` query value. A ticket is masked whole: no leading or trailing characters are kept.
_WS_TICKET = re.compile(r"(?i)\bindus-gateway-ticket\.(?P<s>[A-Za-z0-9_\-]{1,512})")
_QUERY_TICKET = re.compile(r"(?i)[?&]ticket=(?P<s>[^&#\s\"'<>]{1,4096})")
_FULL_MASK_PATTERNS = (_WS_TICKET, _QUERY_TICKET)

# A credential-shaped name: some identifier whose underscore/dash/dot-separated parts include one of
# the words, so MONKEY, AUTHOR or KEYBOARD do not count.
_WORDS = "KEY|APIKEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|CREDENTIALS|AUTH"
_SECRET_NAME = (
    r"(?:[A-Za-z0-9]{1,64}[_.\-]){0,8}(?:" + _WORDS + r")(?:[_.\-][A-Za-z0-9]{1,64}){0,8}"
)
_ASSIGN = re.compile(
    r"(?i)(?<![A-Za-z0-9_.\-])" + _SECRET_NAME + r"\s{0,4}=\s{0,4}"
    r"(?:\"(?P<dq>[^\"\n]{1,4096})\"|'(?P<sq>[^'\n]{1,4096})'|(?P<s>[^\s\"'&,;]{1,4096}))"
)
_JSON_PAIR = re.compile(
    r"(?i)[\"'](?:[A-Za-z0-9]{1,64}[_.\-]){0,8}(?:" + _WORDS + r")(?:[_.\-][A-Za-z0-9]{1,64}){0,8}"
    r"[\"']\s{0,4}:\s{0,4}[\"'](?P<s>[^\"'\n]{1,4096})[\"']"
)
_SECRET_KEY_NAME = re.compile(r"(?i)^" + _SECRET_NAME + r"$")

_KEPT_LITERALS = frozenset({"none", "null", "nil", "true", "false", "undefined", ""})


def _is_reference(value: str) -> bool:
    """Values that name a secret instead of holding one: env references, lookups, paths."""
    v = value.strip()
    if v.lower() in _KEPT_LITERALS:
        return True
    if v.startswith(("$", "%", "<", "{{")):
        return True
    if v.startswith(("os.environ", "os.getenv", "process.env", "getenv(", "env(")):
        return True
    return v.startswith(("/", "~/", "./", "../"))


def _pem_spans(text: str) -> list[tuple[int, int]]:
    """Whole private-key blocks; an unterminated BEGIN stops the scan (linear time)."""
    spans: list[tuple[int, int]] = []
    pos = 0
    while (begin := _PEM_BEGIN.search(text, pos)) is not None:
        end = _PEM_END.search(text, begin.end())
        if end is None:
            break
        spans.append((begin.start(), end.end()))
        pos = end.end()
    return spans


def _spans_for(text: str, assignments: bool = True) -> list[tuple[int, int]]:
    spans = _pem_spans(text)
    for pattern in (
        _VENDOR_KEYS,
        _AUTH_HEADER,
        _BEARER,
        _JWT,
        _URL_USERINFO,
        _QUERY_PARAM,
        *_FULL_MASK_PATTERNS,
    ):
        for match in pattern.finditer(text):
            spans.append(match.span("s"))
    if not assignments:
        return spans
    for match in _ASSIGN.finditer(text):
        for group in ("dq", "sq", "s"):
            if match.group(group) is not None:
                if not _is_reference(match.group(group)):
                    spans.append(match.span(group))
                break
    for match in _JSON_PAIR.finditer(text):
        if not _is_reference(match.group("s")):
            spans.append(match.span("s"))
    return spans


def _full_spans(text: str) -> list[tuple[int, int]]:
    """Spans masked whole (``MASK_SHORT``), never as ``abcd…wxyz``."""
    return [m.span("s") for p in _FULL_MASK_PATTERNS for m in p.finditer(text)]


def _merge(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(s for s in spans if s[1] > s[0]):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _exact_pattern(values: Iterable[str]) -> re.Pattern[str] | None:
    unique = sorted({v for v in values if len(v) >= MIN_EXACT_LENGTH}, key=lambda v: (-len(v), v))
    if not unique:
        return None
    return re.compile("|".join(re.escape(v) for v in unique))


def redact_text(text: str, secrets: Iterable[str] = (), *, assignments: bool = True) -> str:
    """Mask credential shapes and the given exact values in ``text`` (no scope needed).

    ``assignments=False`` skips the ``NAME=value`` / ``"name": "value"`` pass (source code)."""
    values = [v for v in secrets if len(v) >= MIN_EXACT_LENGTH]
    return _redact(text, _exact_pattern(values), values, assignments)


def _redact(
    text: str, exact: re.Pattern[str] | None, values: list[str], assignments: bool = True
) -> str:
    spans = _spans_for(text, assignments)
    if exact is not None:
        spans.extend(m.span() for m in exact.finditer(text))
    if not spans:
        return text
    full = _full_spans(text)
    out: list[str] = []
    last = 0
    for start, end in _merge(spans):
        out.append(text[last:start])
        whole = any(s < end and start < e for s, e in full)
        out.append(MASK_SHORT if whole else mask(text[start:end]))
        last = end
    out.append(text[last:])
    result = "".join(out)
    # A mask keeps up to 4 characters at each end; never let those re-form a whole stored value.
    for _ in range(3):
        leftover = [v for v in values if v in result]
        if not leftover:
            break
        for value in sorted(leftover, key=len, reverse=True):
            result = result.replace(value, MASK_SHORT)
    return result


# -- .env values -------------------------------------------------------------------------------

_NUMBER = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_PLAIN_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://[^\s@/]+(?:[/?#][^\s]*)?$")


def _looks_public(value: str) -> bool:
    """``.env`` values that are clearly settings, not secrets: numbers, switches, paths, URLs."""
    v = value.strip()
    if _NUMBER.match(v) or v.lower() in {"true", "false", "yes", "no", "on", "off"}:
        return True
    if v.startswith(("/", "~/", "./", "../")):
        return True
    return bool(_PLAIN_URL.match(v)) and not _QUERY_PARAM.search(v)


class Redactor:
    """Redaction for one scope. Cheap to build; caches its exact-value pattern."""

    def __init__(self, scope: RedactScope) -> None:
        self._scope = scope
        self._lock = threading.Lock()
        self._signature: tuple[object, ...] | None = None
        self._values: list[str] = []
        self._exact: re.Pattern[str] | None = None

    @property
    def enabled(self) -> bool:
        """Read from the scope's env view at call time; on unless switched off."""
        return redaction_flag(self._scope.env)

    def _env_signature(self) -> object:
        try:
            stat = (self._scope.home / ".env").stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size, stat.st_ino)

    def _env_values(self) -> list[str]:
        try:
            values = read_env_file(self._scope.home / ".env").values()
        except Exception as exc:  # redaction must never fail because a file is odd
            log.debug("redaction could not read .env: %s", type(exc).__name__)
            return []
        return [v for v in values if not _looks_public(v)]

    def _current(self) -> tuple[re.Pattern[str] | None, list[str]]:
        store = self._scope.secrets
        key = (self._env_signature(), id(store), getattr(store, "generation", None))
        with self._lock:
            if key == self._signature:
                return self._exact, self._values
        store_values: list[str] = []
        if store is not None:
            try:
                store_values = list(store.known_values())
            except Exception as exc:
                log.debug("redaction could not read the secret store: %s", type(exc).__name__)
        values = sorted(
            {v for v in [*self._env_values(), *store_values] if len(v) >= MIN_EXACT_LENGTH},
            key=len,
            reverse=True,
        )
        exact = _exact_pattern(values)
        with self._lock:
            self._signature, self._values, self._exact = key, values, exact
        return exact, values

    def redact(self, text: str, *, force: bool = False, assignments: bool = True) -> str:
        if not text or not (force or self.enabled):
            return text
        exact, values = self._current()
        return _redact(text, exact, values, assignments)

    def redact_obj(self, obj: Any, *, force: bool = False) -> Any:
        """Return a copy of ``obj`` with every string redacted; values under credential-shaped
        keys are masked whole. Containers deeper than ``MAX_DEPTH`` become a marker, and a
        container met again inside itself becomes ``"[cycle]"``."""
        if not (force or self.enabled):
            return obj
        exact, values = self._current()
        return self._walk(obj, exact, values, 0, set())

    def _walk(
        self,
        obj: Any,
        exact: re.Pattern[str] | None,
        values: list[str],
        depth: int,
        seen: set[int],
    ) -> Any:
        if isinstance(obj, str):
            return _redact(obj, exact, values)
        if not isinstance(obj, dict | list | tuple | set | frozenset):
            return obj
        if depth >= MAX_DEPTH:
            return DEPTH_MARKER
        marker = id(cast(object, obj))
        if marker in seen:
            return CYCLE_MARKER
        seen.add(marker)
        try:
            if isinstance(obj, dict):
                out: dict[Any, Any] = {}
                for key, value in cast(dict[Any, Any], obj).items():
                    if (
                        isinstance(key, str)
                        and isinstance(value, str)
                        and value
                        and _SECRET_KEY_NAME.match(key)
                        and not _is_reference(value)
                    ):
                        out[key] = mask(value)
                    else:
                        out[key] = self._walk(value, exact, values, depth + 1, seen)
                return out
            items = [
                self._walk(v, exact, values, depth + 1, seen) for v in cast(Iterable[Any], obj)
            ]
            if isinstance(obj, tuple):
                return tuple(items)
            if isinstance(obj, frozenset):
                return frozenset(items)
            if isinstance(obj, set):
                return set(items)
            return items
        finally:
            seen.discard(marker)
