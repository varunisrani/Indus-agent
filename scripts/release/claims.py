"""Lanes, the claim an attempt ref carries, the draft warning and the image set (P23-22, P23-23).

A release is a set of *lanes*: each is a build or a suite in ``release.yml``. The claim — the
JSON message of the annotated attempt ref ``rc.<N>-v<version>`` — records the set once, so a rerun
can never change it, and the gate judges every job against it: a job whose lane is on must
succeed; it may be skipped only when its lane is off (R10.18). A lane marked ``optional`` is off
unless someone names it (``release.py --lanes``): the Termux APT channel (P24-21) and the MSIX
bundle (P24-22, which packs ``desktop-win``'s own unpacked apps, so it needs that lane on too).

Claim (schema 1): ``kind`` (``stable`` / ``canary``), ``version``, ``attempt``, ``commit`` (40
hex), ``autopublish``, ``lanes`` (sorted names), ``epoch`` (the attempt ref's tagger time, unix
seconds; also the release date every leg stamps).
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # run as a script (the gate step needs no environment)
    _ROOT = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(_ROOT), str(_ROOT / "src")]

from indus.foundation.versions import (  # noqa: E402
    CANARY,
    RELEASE_IMAGE,
    SANDBOX_IMAGE,
    STABLE,
    parse_version,
    tag_record_text,
)

__all__ = [
    "ALIASES",
    "DEFAULT_LANES",
    "IMAGES",
    "JOB_LANES",
    "LANES",
    "LANE_NAMES",
    "LANE_REQUIRES",
    "OPTIONAL_LANES",
    "WARNING_BEGIN",
    "WARNING_END",
    "Claim",
    "ClaimError",
    "ImageSpec",
    "Lane",
    "draft_body",
    "expected_results",
    "gate",
    "jobs_to_run",
    "parse_lanes",
    "strip_warnings",
]

SHA_RE = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class Lane:
    name: str
    doc: str
    optional: bool = False
    """Off unless named: never part of the default set (W49 integration)."""


LANES: tuple[Lane, ...] = (
    Lane("ci", "the full Python suite, lint, types and the clean-room guards"),
    Lane("docker", "product and sandbox images built, tested and pushed by digest"),
    Lane("nix", "nix flake check"),
    Lane("install-e2e", "fresh installs: install.sh on Ubuntu and macOS, install.ps1 on Windows"),
    Lane("desktop-mac", "the signed macOS desktop app (DMG + ZIP)"),
    Lane("desktop-win", "the signed Windows desktop installer"),
    Lane("desktop-linux", "the Linux desktop AppImage and .deb"),
    Lane("upgrade", "the previous release updated to this commit with `indus update`"),
    Lane(
        "desktop-msix",
        "MSIX packages, bundle and App Installer feed from the Windows lane's apps",
        optional=True,
    ),
    Lane("termux", "the signed Termux APT repository (aarch64 .deb), termux.yml", optional=True),
)
LANE_NAMES: tuple[str, ...] = tuple(lane.name for lane in LANES)
OPTIONAL_LANES: frozenset[str] = frozenset(lane.name for lane in LANES if lane.optional)
DEFAULT_LANES: frozenset[str] = frozenset(LANE_NAMES) - OPTIONAL_LANES
"""What a claim selects when ``--lanes`` is not given: every lane but the optional ones."""

LANE_REQUIRES: dict[str, frozenset[str]] = {"desktop-msix": frozenset({"desktop-win"})}
"""Lanes that are meaningless alone: naming one without these is refused, never quietly skipped."""

JOB_LANES: dict[str, frozenset[str]] = {
    "ci": frozenset({"ci"}),
    "docker": frozenset({"docker"}),
    "nix": frozenset({"nix"}),
    "install-e2e": frozenset({"install-e2e"}),
    "desktop-mac": frozenset({"desktop-mac"}),
    "desktop-win": frozenset({"desktop-win"}),
    "desktop-linux": frozenset({"desktop-linux"}),
    "upgrade": frozenset({"upgrade"}),
    "desktop-msix": frozenset({"desktop-msix", "desktop-win"}),
    "termux": frozenset({"termux"}),
    "web-pages": frozenset(),
    "upload": frozenset(),
}
"""``release.yml`` job → the lanes that must all be on for it to run (none: it always runs)."""


class ClaimError(ValueError):
    pass


def parse_lanes(text: str | None) -> frozenset[str]:
    """``"ci,docker"`` → the lane set; None/empty → the default set (no optional lane). Unknown
    names refuse, and so does a lane named without the lanes it needs (``LANE_REQUIRES``)."""
    if text is None or not text.strip():
        return DEFAULT_LANES
    names = {part.strip() for part in text.split(",") if part.strip()}
    unknown = sorted(names - set(LANE_NAMES))
    if unknown:
        raise ClaimError(f"unknown lane(s): {', '.join(unknown)} (lanes: {', '.join(LANE_NAMES)})")
    for name in sorted(names):
        missing = sorted(LANE_REQUIRES.get(name, frozenset()) - names)
        if missing:
            raise ClaimError(f"lane {name} needs {', '.join(missing)} too")
    return frozenset(names)


def jobs_to_run(lanes: Iterable[str]) -> dict[str, bool]:
    chosen = set(lanes)
    return {job: needs <= chosen for job, needs in JOB_LANES.items()}


def expected_results(lanes: Iterable[str]) -> dict[str, str]:
    """Each gated job's required result: ``success`` when it runs, ``skipped`` when its lane is
    off. There is no other acceptable result (R10.18)."""
    return {job: "success" if runs else "skipped" for job, runs in jobs_to_run(lanes).items()}


def gate(needs: Mapping[str, Any], lanes: Sequence[str]) -> list[str]:
    """Problems with a run's job results (empty: the gate passes). ``needs`` is the workflow's
    ``toJSON(needs)``; ``admit`` must have succeeded and every gated job must have exactly its
    expected result, so a selected job that was skipped fails the gate (R10.18)."""
    problems: list[str] = []
    admitted = (needs.get("admit") or {}).get("result")
    if admitted != "success":
        problems.append(f"admit={admitted or 'missing'} (expected success)")
    for job, wanted in sorted(expected_results(lanes).items()):
        got = (needs.get(job) or {}).get("result") or "missing"
        if got != wanted:
            problems.append(f"{job}={got} (expected {wanted})")
    return problems


@dataclass(frozen=True)
class Claim:
    kind: str
    version: str
    attempt: int
    commit: str
    autopublish: bool
    lanes: frozenset[str]
    epoch: int

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "kind": self.kind,
            "version": self.version,
            "attempt": self.attempt,
            "commit": self.commit,
            "autopublish": self.autopublish,
            "lanes": sorted(self.lanes),
            "epoch": self.epoch,
        }

    def message(self) -> str:
        return json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_message(cls, text: str) -> Claim:
        try:
            data = json.loads(tag_record_text(text))
        except ValueError as exc:
            raise ClaimError("the attempt ref's message is not a claim") from exc
        return cls.from_json(data)

    @classmethod
    def from_json(cls, data: object) -> Claim:
        if not isinstance(data, Mapping):
            raise ClaimError("the claim is not an object")
        row: Mapping[str, Any] = data  # type: ignore[assignment]
        keys = {"schema", "kind", "version", "attempt", "commit", "autopublish", "lanes", "epoch"}
        if set(row) != keys or row.get("schema") != 1:
            raise ClaimError("the claim does not have exactly the schema-1 fields")
        version = parse_version(row["version"])
        kind = row["kind"]
        if version is None or kind not in (STABLE, CANARY) or version.channel != kind:
            raise ClaimError("the claim's kind and version do not agree")
        attempt, epoch = row["attempt"], row["epoch"]
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ClaimError("the claim's attempt number is invalid")
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch <= 0:
            raise ClaimError("the claim's epoch is invalid")
        commit = row["commit"]
        if not isinstance(commit, str) or not SHA_RE.fullmatch(commit):
            raise ClaimError("the claim does not name an exact commit")
        if not isinstance(row["autopublish"], bool) or not isinstance(row["lanes"], list):
            raise ClaimError("the claim's autopublish or lanes are invalid")
        names = ",".join(str(x) for x in row["lanes"])
        if not names:
            raise ClaimError("the claim selects no lane")
        lanes = parse_lanes(names)
        return cls(kind, str(version), attempt, commit, row["autopublish"], lanes, epoch)


# -- the draft warning -----------------------------------------------------------------------------

WARNING_BEGIN = "<!-- indus-release-warning -->"
WARNING_END = "<!-- /indus-release-warning -->"
_WARNING = (
    f"{WARNING_BEGIN}\n"
    "> **Do not publish this draft by hand.** The release workflow publishes it after every lane\n"
    "> has passed: it writes the receipt tag, moves the image aliases and the channel record, and\n"
    "> only then makes this release public. Publishing it here skips all of that and cannot be\n"
    "> undone (published releases are immutable).\n"
    f"{WARNING_END}"
)
_WARNING_RE = re.compile(re.escape(WARNING_BEGIN) + r".*?" + re.escape(WARNING_END) + r"\n*", re.S)


def draft_body(changelog: str) -> str:
    """The draft's body: the warning above and below the changelog."""
    return f"{_WARNING}\n\n{changelog.strip()}\n\n{_WARNING}\n"


def strip_warnings(body: str) -> str:
    return _WARNING_RE.sub("", body).strip() + "\n"


# -- images ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ImageSpec:
    """One published image: its repository and the tag suffix of its variant."""

    name: str
    repository: str
    suffix: str = ""

    def tag_for(self, word: str) -> str:
        return f"{word}{self.suffix}"


IMAGES: tuple[ImageSpec, ...] = (
    ImageSpec("product", RELEASE_IMAGE),
    ImageSpec("product-browser", RELEASE_IMAGE, "-browser"),
    ImageSpec("sandbox", SANDBOX_IMAGE, "-slim"),
    ImageSpec("sandbox-browser", SANDBOX_IMAGE, "-browser"),
)
"""What the docker lane pushes by digest, tagged ``<attempt ref><suffix>`` (P23-12,
``docker.yml``: the product's slim image carries no suffix, the sandbox always names its variant).
The aliases follow the same rule: ``stable`` / ``stable-browser`` on the product,
``stable-slim`` / ``stable-browser`` on the sandbox."""

ALIASES: dict[str, tuple[str, ...]] = {STABLE: ("stable", "latest"), CANARY: ("canary",)}
"""The aliases a publication moves, per release kind (only the controller moves them)."""


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """``claims.py gate``: the workflow's strict gate (``NEEDS`` and ``LANES`` as JSON)."""
    args = list(sys.argv[1:] if argv is None else argv)
    values = os.environ if env is None else env
    if args != ["gate"]:
        print("usage: claims.py gate  (reads NEEDS and LANES)", file=sys.stderr)
        return 2
    try:
        needs = json.loads(values.get("NEEDS") or "{}")
        lanes = json.loads(values.get("LANES") or "[]")
    except ValueError as exc:
        print(f"✗ NEEDS or LANES is not JSON: {exc}", file=sys.stderr)
        return 1
    problems = gate(needs, lanes)
    for line in problems:
        print(f"✗ {line}", file=sys.stderr)
    if not problems:
        print("✓ every selected job succeeded; only jobs of lanes that were off were skipped")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
