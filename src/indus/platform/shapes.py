"""Result and context shapes for command lookup.

Callers branch on ``Observation.state``; ``detail`` is for people and logs and is never parsed.
``Probe`` is deliberately not truthy: ``if probe:`` raises, so callers read ``running`` /
``answering`` explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, StrEnum, auto
from typing import Any, Literal


class CheckState(StrEnum):
    """How far a check got. Values are the lowercase member names."""

    PRESENT = auto()
    ABSENT = auto()
    UNAVAILABLE = auto()  # the check cannot run on this host
    ERROR = auto()
    NOT_CHECKED = auto()


@dataclass(frozen=True)
class Observation[T]:
    """One checked fact: branch on ``state``; ``detail`` is free text for people."""

    state: CheckState
    value: T | None = None
    detail: str = ""

    @property
    def is_present(self) -> bool:
        return self.state is CheckState.PRESENT


def unchecked() -> Observation[Any]:
    return Observation(CheckState.NOT_CHECKED)


def seen[T](value: T, detail: str = "") -> Observation[T]:
    return Observation(CheckState.PRESENT, value, detail)


def missing(detail: str = "") -> Observation[Any]:
    return Observation(CheckState.ABSENT, None, detail)


def failed(detail: str) -> Observation[Any]:
    return Observation(CheckState.ERROR, None, detail)


@dataclass(frozen=True)
class Candidate:
    """One place looked at, in probe order. ``source`` is ``explicit``, ``PATH`` or a known dir."""

    value: str
    source: str
    present: bool


ResolutionKind = Literal["explicit_path", "path_executable", "known_path", "missing"]


@dataclass(frozen=True)
class Resolution:
    kind: ResolutionKind
    candidates: tuple[Candidate, ...] = ()

    @property
    def found(self) -> bool:
        return self.kind != "missing"

    @property
    def winner(self) -> Candidate | None:
        return next((c for c in self.candidates if c.present), None)

    @property
    def path(self) -> str | None:
        first = self.winner
        return first.value if first is not None else None

    @property
    def present(self) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if c.present)


class PathSource(Enum):
    UNSET = "unset"  # caller did not say: use the ambient PATH
    AMBIENT = "ambient"  # explicitly the process PATH


@dataclass(frozen=True)
class LookupContext:
    """``path``: ``PathSource`` or an explicit search string (``""`` searches nothing).

    ``pathext`` (Windows) lists suffixes to try; it is never read from the process env.
    """

    path: PathSource | str = PathSource.UNSET
    pathext: str | None = None


class Effort(Enum):
    """What ``probe`` may do: ``LOCAL`` neither connects nor spawns, ``NETWORK`` may open a
    socket (never spawn), ``DIAGNOSTIC`` may also spawn."""

    LOCAL = "local"
    NETWORK = "network"
    DIAGNOSTIC = "diagnostic"


@dataclass(frozen=True)
class Inspection:
    """What a bounded read of the winning file showed."""

    interpreter: Observation[str] = field(default_factory=unchecked)
    size: Observation[int] = field(default_factory=unchecked)


@dataclass(frozen=True)
class Probe:
    running: Observation[bool]
    answering: Observation[bool]
    endpoint: Observation[str]

    def __bool__(self) -> bool:
        raise TypeError("Probe has no truth value; read .running / .answering / .endpoint")
