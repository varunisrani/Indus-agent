# pyright: strict
"""Release versions, tags and channel words: the one grammar every caller imports (P23-21).

Shapes (R10.26, R10.27):

- the source tree says ``0.0.0`` (``SOURCE_VERSION``); releases stamp the real version into the
  build tree only;
- a stable release is plain SemVer ``X.Y.Z`` with no leading zeros and a major of at most three
  digits (a four-digit major would be a calendar label, never ours);
- a canary is ``X.Y.Z-canary.<YYYYMMDDTHHMMSSZ>`` where ``X.Y.Z`` is the patch after the newest
  stable, so SemVer precedence alone orders it above the last stable and below its own stable;
- tags are ``v`` + version; attempt refs ``rc.<N>-v<version>`` (the attempt number first, so an
  attempt never reads as a prerelease of its version); abandon markers ``abandoned-`` + attempt.

The channel words are exactly ``stable``, ``canary`` and ``main``; no other spelling is used
anywhere (scripts, workflows, docs). ``RELEASE_REPOSITORY`` names where releases are published;
it is the one place to change when the owner settles D-16.690: the installers' ``pins.json``,
``docker/pins.json``, the Nix module's image default, ``install.cmd`` and the desktop's update feed
are copies that tests hold equal to it (``tests/unit/packaging/test_release_base.py``).

Standard library only: ``indus --version`` and the release scripts both import it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

__all__ = [
    "CANARY",
    "CHANNELS",
    "MAIN",
    "RELEASE_CHANNELS",
    "RELEASE_IMAGE",
    "RELEASE_REPOSITORY",
    "SANDBOX_IMAGE",
    "SOURCE_VERSION",
    "STABLE",
    "AttemptRef",
    "Version",
    "attempt_ref",
    "canary_stamp",
    "marker_ref",
    "parse_attempt_ref",
    "parse_marker_ref",
    "parse_tag",
    "parse_version",
    "tag_record_text",
]

STABLE = "stable"
CANARY = "canary"
MAIN = "main"
CHANNELS: tuple[str, ...] = (STABLE, CANARY, MAIN)
"""Every channel a source checkout can follow."""
RELEASE_CHANNELS: tuple[str, ...] = (STABLE, CANARY)
"""The channels that have published releases (and a channel record)."""

SOURCE_VERSION = "0.0.0"

RELEASE_REPOSITORY = "indusagi/indus-agent"
"""``owner/name`` on GitHub where releases and the ``channels`` record live (D-16.690: owner
decision pending; change it here only)."""
RELEASE_IMAGE = f"ghcr.io/{RELEASE_REPOSITORY}"
"""The product image on GHCR; channel aliases (``stable``, ``latest``, ``canary``) move on it."""
SANDBOX_IMAGE = f"{RELEASE_IMAGE}-sandbox"
"""The sandbox image (D-16.64); its tags always name the variant (``stable-slim``,
``stable-browser``, ``main-slim``)."""

_NUMBER = r"(?:0|[1-9][0-9]*)"
_MAJOR = r"(?:0|[1-9][0-9]{0,2})"
_STAMP = r"[0-9]{8}T[0-9]{6}Z"
_VERSION = (
    rf"(?P<major>{_MAJOR})\.(?P<minor>{_NUMBER})\.(?P<patch>{_NUMBER})"
    rf"(?:-{CANARY}\.(?P<stamp>{_STAMP}))?"
)
_VERSION_RE = re.compile(rf"{_VERSION}")
_TAG_RE = re.compile(rf"v{_VERSION}")
_ATTEMPT_RE = re.compile(rf"rc\.(?P<attempt>[1-9][0-9]*)-v(?P<version>{_VERSION})")
_MARKER_PREFIX = "abandoned-"
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def _real_stamp(stamp: str) -> bool:
    try:
        datetime.strptime(stamp, _STAMP_FORMAT)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, order=False)
class Version:
    """A release version. Compare with ``key`` (or the rich comparisons, which use it)."""

    major: int
    minor: int
    patch: int
    stamp: str | None = None

    @property
    def channel(self) -> str:
        return STABLE if self.stamp is None else CANARY

    @property
    def is_canary(self) -> bool:
        return self.stamp is not None

    @property
    def core(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    @property
    def key(self) -> tuple[int, int, int, int, str]:
        """SemVer precedence for our two shapes: a prerelease sorts below its plain version, and
        canary stamps are fixed-width UTC times, so their text order is their time order."""
        return (*self.core, 0 if self.stamp is not None else 1, self.stamp or "")

    @property
    def tag(self) -> str:
        return f"v{self}"

    def stable(self) -> Version:
        """The plain version this one leads to (itself for a stable)."""
        return Version(self.major, self.minor, self.patch)

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        return text if self.stamp is None else f"{text}-{CANARY}.{self.stamp}"

    def __lt__(self, other: Version) -> bool:
        return self.key < other.key

    def __le__(self, other: Version) -> bool:
        return self.key <= other.key

    def __gt__(self, other: Version) -> bool:
        return self.key > other.key

    def __ge__(self, other: Version) -> bool:
        return self.key >= other.key


def _from_match(match: re.Match[str] | None) -> Version | None:
    if match is None:
        return None
    stamp = match.group("stamp")
    if stamp is not None and not _real_stamp(stamp):
        return None
    return Version(
        int(match.group("major")), int(match.group("minor")), int(match.group("patch")), stamp
    )


def parse_version(text: object) -> Version | None:
    """``"1.2.3"`` / ``"1.2.4-canary.20261021T064100Z"`` → ``Version``; anything else → None."""
    if not isinstance(text, str):
        return None
    return _from_match(_VERSION_RE.fullmatch(text))


def parse_tag(text: object) -> Version | None:
    """A release tag (``v`` + version) → ``Version``; anything else (attempt refs, receipts of
    other kinds, malformed tags) → None."""
    if not isinstance(text, str):
        return None
    return _from_match(_TAG_RE.fullmatch(text))


def canary_stamp(when: datetime) -> str:
    """The canary stamp for an instant (converted to UTC)."""
    return when.astimezone(UTC).strftime(_STAMP_FORMAT)


@dataclass(frozen=True)
class AttemptRef:
    version: Version
    attempt: int

    @property
    def name(self) -> str:
        return f"rc.{self.attempt}-v{self.version}"

    @property
    def marker(self) -> str:
        return _MARKER_PREFIX + self.name


def parse_attempt_ref(text: object) -> AttemptRef | None:
    """``rc.<N>-v<version>`` → ``AttemptRef``; anything else → None."""
    if not isinstance(text, str):
        return None
    match = _ATTEMPT_RE.fullmatch(text)
    if match is None:
        return None
    version = parse_version(match.group("version"))
    return None if version is None else AttemptRef(version, int(match.group("attempt")))


def parse_marker_ref(text: object) -> AttemptRef | None:
    """``abandoned-rc.<N>-v<version>`` → the attempt it clears; anything else → None."""
    if not isinstance(text, str) or not text.startswith(_MARKER_PREFIX):
        return None
    return parse_attempt_ref(text[len(_MARKER_PREFIX) :])


def attempt_ref(version: Version, attempt: int) -> str:
    if attempt < 1:
        raise ValueError(f"attempt numbers start at 1, not {attempt}")
    return AttemptRef(version, attempt).name


def marker_ref(version: Version, attempt: int) -> str:
    return _MARKER_PREFIX + attempt_ref(version, attempt)


def tag_record_text(message: str) -> str:
    """The JSON part of an annotated tag's message: a signed tag keeps its armoured signature in
    the message after the record, so readers cut it off here before parsing."""
    head, _sep, _rest = message.partition("\n-----BEGIN ")
    return head.strip()
