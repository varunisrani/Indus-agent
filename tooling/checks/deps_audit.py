"""Dependency audit (P25-04): known vulnerabilities in every locked package, allow-list expiring.

Every ``name@version`` in ``uv.lock`` and ``pnpm-lock.yaml`` is looked up in OSV
(``api.osv.dev``); ``pnpm audit`` adds the npm registry's own advisories. A finding at or above
``fail_at`` (in the allow file; ``high`` by default, and ``unknown`` severity counts as failing)
fails unless ``deps_audit_allow.json`` lists one of its ids for that package with a reason and an
expiry date that has not passed. Expired, reason-less or malformed entries fail; entries that match
nothing are warned about so they get removed.

The network lookup runs only on request (``--refresh``, the CI audit job). Its answer is stored in
``deps_audit_snapshot.json``; the offline guard (``python -m tooling.checks deps_audit``) evaluates
that snapshot against today's locks and allow file, and warns when the locks changed since. The same
pass checks provenance: every locked package comes from its registry with a hash (git, path and bare
URL sources are named). ``--report`` prints a markdown summary with pin ages; ``--resolve-pins``
checks that the pinned downloads in ``scripts/install/pins.json`` and ``docker/pins.json`` still
answer and that the Docker base digest still resolves.

    python -m tooling.checks.deps_audit [--refresh] [--pnpm-audit] [--resolve-pins] [--report]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

from tooling.checks.common import CHECKS_DIR, DEFAULT_ROOT, Context, Result

NAME = "deps_audit"
ALLOW_FILE = CHECKS_DIR / "deps_audit_allow.json"
SNAPSHOT_FILE = CHECKS_DIR / "deps_audit_snapshot.json"
LOCKS = ("uv.lock", "pnpm-lock.yaml")
OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/"
BATCH_SIZE = 500
TIMEOUT = 60
LEVELS = {"low": 1, "moderate": 2, "high": 3, "critical": 4, "unknown": 3}
"""Severity order; ``unknown`` ranks with ``high`` so an unscored advisory fails closed."""
PNPM_SEVERITY = {
    "info": "low",
    "low": "low",
    "moderate": "moderate",
    "high": "high",
    "critical": "critical",
}

type Fetch = Callable[[str, bytes | None], Any]
"""``fetch(url, body)``: the parsed JSON answer of a GET (body None) or a JSON POST."""


@dataclass(frozen=True, slots=True)
class Package:
    ecosystem: str
    name: str
    version: str
    source: str = "registry"
    hashed: bool = True
    uploaded: str | None = None


@dataclass(frozen=True, slots=True)
class Advisory:
    id: str
    ecosystem: str
    package: str
    version: str
    severity: str
    aliases: tuple[str, ...] = ()
    summary: str = ""
    fixed: tuple[str, ...] = ()
    origin: str = "osv"

    def ids(self) -> set[str]:
        return {self.id, *self.aliases}


@dataclass(slots=True)
class AllowEntry:
    id: str
    package: str
    ecosystem: str
    expires: date
    reason: str


@dataclass(slots=True)
class Policy:
    fail_at: str = "high"
    entries: list[AllowEntry] = field(default_factory=list[AllowEntry])
    problems: list[str] = field(default_factory=list[str])


class AuditUnavailable(RuntimeError):
    """The advisory source could not be asked (network, HTTP error, bad answer)."""


# -- lock files ------------------------------------------------------------------------------------


def read_uv_lock(path: Path) -> list[Package]:
    """Every package of a uv lock except the project itself (editable / virtual roots)."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    found: list[Package] = []
    for raw in cast(list[dict[str, Any]], data.get("package", [])):
        source = cast(dict[str, Any], raw.get("source", {}))
        if "editable" in source or "virtual" in source:
            continue
        kind = "registry" if "registry" in source else next(iter(source), "unknown")
        files: list[dict[str, Any]] = []
        sdist = raw.get("sdist")
        if isinstance(sdist, dict):
            files.append(cast(dict[str, Any], sdist))
        files.extend(cast(list[dict[str, Any]], raw.get("wheels", [])))
        hashed = bool(files) and all(
            str(item.get("hash", "")).startswith("sha256:") for item in files
        )
        times = sorted(str(item["upload-time"]) for item in files if item.get("upload-time"))
        found.append(
            Package(
                "PyPI",
                str(raw["name"]),
                str(raw.get("version", "")),
                kind,
                hashed,
                times[-1] if times else None,
            )
        )
    return found


_PNPM_KEY = re.compile(r"^  '?(?P<name>(?:@[^@/\s']+/)?[^@/\s']+)@(?P<version>[^'\s(]+)'?:\s*$")
_PNPM_SECTION = re.compile(r"^[A-Za-z]")


def read_pnpm_lock(path: Path) -> list[Package]:
    """Every entry of the ``packages:`` sections (all YAML documents of the file)."""
    found: dict[tuple[str, str], Package] = {}
    current: tuple[str, str] | None = None
    in_packages = False
    resolution = ""
    lines = path.read_text(encoding="utf-8").splitlines()

    def flush() -> None:
        if current is None:
            return
        name, version = current
        if "tarball:" in resolution:
            source = "tarball"
        elif "repo:" in resolution or "commit:" in resolution:
            source = "git"
        elif "directory:" in resolution:
            source = "path"
        else:
            source = "registry"
        hashed = "integrity: sha512-" in resolution
        found[(name, version)] = Package("npm", name, version, source, hashed)

    for line in lines:
        if line == "---" or _PNPM_SECTION.match(line):
            flush()
            current = None
            resolution = ""
            in_packages = line.startswith("packages:")
            continue
        if not in_packages:
            continue
        match = _PNPM_KEY.match(line)
        if match:
            flush()
            current = (match["name"], match["version"])
            resolution = ""
        elif current is not None and line.strip().startswith("resolution:"):
            resolution = line.strip()
    flush()
    return sorted(found.values(), key=lambda p: (p.name, p.version))


def read_locks(root: Path) -> list[Package]:
    packages: list[Package] = []
    if (root / "uv.lock").is_file():
        packages.extend(read_uv_lock(root / "uv.lock"))
    if (root / "pnpm-lock.yaml").is_file():
        packages.extend(read_pnpm_lock(root / "pnpm-lock.yaml"))
    return packages


def lock_digests(root: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in LOCKS
        if (root / name).is_file()
    }


# -- severities ------------------------------------------------------------------------------------

_CVSS3 = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "CIA": {"H": 0.56, "L": 0.22, "N": 0.0},
}


def _round_up(value: float) -> float:
    whole = round(value * 100_000)
    if whole % 10_000 == 0:
        return whole / 100_000.0
    return (math.floor(whole / 10_000) + 1) / 10.0


def cvss3_score(vector: str) -> float | None:
    """The CVSS 3.x base score of ``vector``, or None when it is not a complete 3.x vector."""
    if not vector.startswith(("CVSS:3.0/", "CVSS:3.1/")):
        return None
    metrics = dict(part.split(":", 1) for part in vector.split("/")[1:] if ":" in part)
    try:
        changed = metrics["S"] == "C"
        privileges = {"N": 0.85, "L": 0.68 if changed else 0.62, "H": 0.5 if changed else 0.27}
        exploit = (
            8.22
            * _CVSS3["AV"][metrics["AV"]]
            * _CVSS3["AC"][metrics["AC"]]
            * privileges[metrics["PR"]]
            * _CVSS3["UI"][metrics["UI"]]
        )
        c, i, a = (_CVSS3["CIA"][metrics[key]] for key in ("C", "I", "A"))
    except KeyError:
        return None
    base = 1 - (1 - c) * (1 - i) * (1 - a)
    impact = 7.52 * (base - 0.029) - 3.25 * (base - 0.02) ** 15 if changed else 6.42 * base
    if impact <= 0:
        return 0.0
    total = 1.08 * (impact + exploit) if changed else impact + exploit
    return _round_up(min(total, 10.0))


def band(score: float) -> str:
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "moderate"
    return "low"


def osv_severity(record: Mapping[str, Any]) -> str:
    """A record's severity: its database label (GHSA style), else its CVSS 3 score, else unknown."""
    specific = record.get("database_specific")
    if isinstance(specific, dict):
        label = str(cast(dict[str, Any], specific).get("severity", "")).lower()
        if label == "medium":
            label = "moderate"
        if label in ("low", "moderate", "high", "critical"):
            return label
    scores = [
        cvss3_score(str(item.get("score", "")))
        for item in cast(list[dict[str, Any]], record.get("severity") or [])
        if str(item.get("type", "")) == "CVSS_V3"
    ]
    known = [score for score in scores if score is not None]
    return band(max(known)) if known else "unknown"


def _fixed_versions(record: Mapping[str, Any], name: str) -> tuple[str, ...]:
    fixed: list[str] = []
    for affected in cast(list[dict[str, Any]], record.get("affected") or []):
        package = cast(dict[str, Any], affected.get("package") or {})
        if str(package.get("name", "")).lower() != name.lower():
            continue
        for span in cast(list[dict[str, Any]], affected.get("ranges") or []):
            for event in cast(list[dict[str, Any]], span.get("events") or []):
                if "fixed" in event:
                    fixed.append(str(event["fixed"]))
    return tuple(dict.fromkeys(fixed))


# -- sources ---------------------------------------------------------------------------------------


def http_fetch(url: str, body: bytes | None) -> Any:
    """GET (or JSON POST) through the environment's proxy settings, with a timeout."""
    request = urllib.request.Request(
        url,
        data=body,
        method="POST" if body is not None else "GET",
        headers={"Content-Type": "application/json", "User-Agent": "indus-deps-audit"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:  # noqa: S310 (fixed https URLs)
            return json.loads(answer.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        raise AuditUnavailable(f"{url}: {exc}") from exc


def query_osv(packages: Sequence[Package], fetch: Fetch = http_fetch) -> list[Advisory]:
    """OSV advisories for every package (batched), one ``Advisory`` per (record, package)."""
    hits: dict[int, list[str]] = {}
    for start in range(0, len(packages), BATCH_SIZE):
        chunk = packages[start : start + BATCH_SIZE]
        queries: list[dict[str, Any]] = [
            {"package": {"name": p.name, "ecosystem": p.ecosystem}, "version": p.version}
            for p in chunk
        ]
        pending = list(range(len(chunk)))
        tokens: dict[int, str] = {}
        while pending:
            body = [
                {**queries[i], **({"page_token": tokens[i]} if i in tokens else {})}
                for i in pending
            ]
            answer = fetch(OSV_BATCH, json.dumps({"queries": body}).encode())
            results = cast(list[dict[str, Any]], (answer or {}).get("results"))
            if not isinstance(results, list) or len(results) != len(pending):
                raise AuditUnavailable("OSV answered a batch with the wrong number of results")
            again: list[int] = []
            for index, result in zip(pending, results, strict=True):
                for vuln in cast(list[dict[str, Any]], result.get("vulns") or []):
                    hits.setdefault(start + index, []).append(str(vuln["id"]))
                token = result.get("next_page_token")
                if token:
                    tokens[index] = str(token)
                    again.append(index)
            pending = again
    records: dict[str, dict[str, Any]] = {}
    for vuln_id in sorted({vid for ids in hits.values() for vid in ids}):
        records[vuln_id] = cast(dict[str, Any], fetch(OSV_VULN + vuln_id, None))
    found: list[Advisory] = []
    for index, ids in sorted(hits.items()):
        package = packages[index]
        for vuln_id in dict.fromkeys(ids):
            record = records[vuln_id]
            found.append(
                Advisory(
                    id=vuln_id,
                    ecosystem=package.ecosystem,
                    package=package.name,
                    version=package.version,
                    severity=osv_severity(record),
                    aliases=tuple(str(a) for a in record.get("aliases") or ()),
                    summary=str(record.get("summary") or "")[:200],
                    fixed=_fixed_versions(record, package.name),
                )
            )
    return merge_aliases(found)


def merge_aliases(found: Iterable[Advisory]) -> list[Advisory]:
    """One advisory per (package, version, alias group), at the group's highest known severity."""
    groups: list[list[Advisory]] = []
    for advisory in found:
        home = next(
            (
                group
                for group in groups
                if group[0].ecosystem == advisory.ecosystem
                and group[0].package == advisory.package
                and group[0].version == advisory.version
                and any(advisory.ids() & other.ids() for other in group)
            ),
            None,
        )
        if home is None:
            groups.append([advisory])
        else:
            home.append(advisory)
    merged: list[Advisory] = []
    for group in groups:
        known = [a.severity for a in group if a.severity != "unknown"]
        severity = max(known, key=LEVELS.__getitem__) if known else "unknown"
        ids = sorted(
            {i for a in group for i in a.ids()}, key=lambda i: (not i.startswith("GHSA"), i)
        )
        lead = next((a for a in group if a.id.startswith("GHSA")), group[0])
        merged.append(
            Advisory(
                id=lead.id,
                ecosystem=lead.ecosystem,
                package=lead.package,
                version=lead.version,
                severity=severity,
                aliases=tuple(i for i in ids if i != lead.id),
                summary=next((a.summary for a in group if a.summary), ""),
                fixed=tuple(dict.fromkeys(f for a in group for f in a.fixed)),
                origin=lead.origin,
            )
        )
    return sorted(merged, key=lambda a: (a.ecosystem, a.package, a.version, a.id))


def parse_pnpm_audit(answer: Mapping[str, Any]) -> list[Advisory]:
    """``pnpm audit --json`` advisories as ``Advisory`` rows (one per affected version)."""
    found: list[Advisory] = []
    advisories = cast(dict[str, dict[str, Any]], answer.get("advisories") or {})
    for item in advisories.values():
        ghsa = str(item.get("github_advisory_id") or "")
        own = f"NPM-{item.get('id')}"
        patched = str(item.get("patched_versions") or "")
        for version in sorted({str(f.get("version")) for f in item.get("findings") or ()}):
            found.append(
                Advisory(
                    id=ghsa or own,
                    ecosystem="npm",
                    package=str(item.get("module_name", "")),
                    version=version,
                    severity=PNPM_SEVERITY.get(str(item.get("severity", "")).lower(), "unknown"),
                    aliases=(own,) if ghsa else (),
                    summary=str(item.get("title") or "")[:200],
                    fixed=(patched,) if patched and patched != "<0.0.0" else (),
                    origin="pnpm-audit",
                )
            )
    return found


def run_pnpm_audit(root: Path) -> tuple[list[Advisory], str]:
    """``pnpm audit --prod --json`` in ``root`` (advisories, note)."""
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        return [], "pnpm audit skipped: pnpm is not on PATH"
    try:
        out = subprocess.run(
            [pnpm, "audit", "--prod", "--json"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        answer = cast(dict[str, Any], json.loads(out.stdout or "{}"))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise AuditUnavailable(f"pnpm audit: {exc}") from exc
    if "advisories" not in answer:
        raise AuditUnavailable(f"pnpm audit gave no advisories table: {out.stderr.strip()[:300]}")
    rows = parse_pnpm_audit(answer)
    return rows, f"pnpm audit --prod: {len(rows)} advisory rows"


# -- allow file ------------------------------------------------------------------------------------


def read_policy(path: Path = ALLOW_FILE) -> Policy:
    policy = Policy()
    try:
        data = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return policy
    except ValueError as exc:
        policy.problems.append(f"{path.name}: not JSON ({exc})")
        return policy
    fail_at = str(data.get("fail_at", "high"))
    if fail_at not in ("low", "moderate", "high", "critical"):
        policy.problems.append(f"{path.name}: fail_at {fail_at!r} is not a severity")
    else:
        policy.fail_at = fail_at
    for number, raw in enumerate(cast(list[Any], data.get("allow", [])), start=1):
        where = f"{path.name} entry {number}"
        if not isinstance(raw, dict):
            policy.problems.append(f"{where}: not an object")
            continue
        item = cast(dict[str, Any], raw)
        missing = [
            k for k in ("id", "package", "ecosystem", "expires", "reason") if not item.get(k)
        ]
        if missing:
            policy.problems.append(f"{where}: missing {', '.join(missing)}")
            continue
        try:
            expires = date.fromisoformat(str(item["expires"]))
        except ValueError:
            policy.problems.append(f"{where}: expires {item['expires']!r} is not a YYYY-MM-DD date")
            continue
        if len(str(item["reason"]).split()) < 4:
            policy.problems.append(f"{where}: the reason must say why (a sentence, not a word)")
            continue
        policy.entries.append(
            AllowEntry(
                str(item["id"]),
                str(item["package"]),
                str(item["ecosystem"]),
                expires,
                str(item["reason"]),
            )
        )
    return policy


# -- evaluation ------------------------------------------------------------------------------------


@dataclass(slots=True)
class Verdict:
    failures: list[str] = field(default_factory=list[str])
    warnings: list[str] = field(default_factory=list[str])
    notes: list[str] = field(default_factory=list[str])


def _line(advisory: Advisory) -> str:
    fixed = f"; fixed in {', '.join(advisory.fixed)}" if advisory.fixed else ""
    aka = f" ({', '.join(advisory.aliases[:3])})" if advisory.aliases else ""
    return (
        f"{advisory.id}{aka} {advisory.ecosystem} {advisory.package}@{advisory.version} "
        f"[{advisory.severity}] {advisory.summary}{fixed}"
    )


def evaluate(
    packages: Sequence[Package],
    advisories: Sequence[Advisory],
    policy: Policy,
    today: date,
) -> Verdict:
    verdict = Verdict()
    verdict.failures.extend(policy.problems)
    present = {(p.ecosystem, p.name, p.version) for p in packages}
    threshold = LEVELS[policy.fail_at]
    used: set[int] = set()
    for advisory in merge_aliases(advisories):
        if (advisory.ecosystem, advisory.package, advisory.version) not in present:
            continue
        match = next(
            (
                (index, entry)
                for index, entry in enumerate(policy.entries)
                if entry.ecosystem == advisory.ecosystem
                and entry.package == advisory.package
                and entry.id in advisory.ids()
            ),
            None,
        )
        if match is not None:
            index, entry = match
            used.add(index)
            if entry.expires < today:
                verdict.failures.append(
                    f"allow-list entry for {entry.id} ({entry.package}) expired on "
                    f"{entry.expires.isoformat()}: {_line(advisory)}"
                )
            else:
                verdict.notes.append(
                    f"allowed until {entry.expires.isoformat()}: {_line(advisory)} — {entry.reason}"
                )
            continue
        if LEVELS[advisory.severity] >= threshold:
            verdict.failures.append(_line(advisory))
        else:
            verdict.warnings.append(_line(advisory))
    for index, entry in enumerate(policy.entries):
        if index in used:
            continue
        if entry.expires < today:
            verdict.failures.append(
                f"allow-list entry for {entry.id} ({entry.package}) expired on "
                f"{entry.expires.isoformat()} (and matches nothing: remove it)"
            )
        else:
            verdict.warnings.append(
                f"allow-list entry for {entry.id} ({entry.package}) matches no finding: remove it"
            )
    for package in packages:
        if package.source != "registry":
            verdict.failures.append(
                f"{package.ecosystem} {package.name}@{package.version} comes from a "
                f"{package.source} source, not its registry"
            )
        elif not package.hashed:
            verdict.failures.append(
                f"{package.ecosystem} {package.name}@{package.version} is locked without a hash"
            )
    return verdict


# -- snapshot --------------------------------------------------------------------------------------


def write_snapshot(
    path: Path, root: Path, packages: Sequence[Package], advisories: Sequence[Advisory]
) -> None:
    data = {
        "schema": 1,
        "note": "Written by `python -m tooling.checks.deps_audit --refresh` (OSV, pnpm audit). "
        "Do not edit by hand.",
        "fetched_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "locks": lock_digests(root),
        "packages": len(packages),
        "advisories": [
            {**asdict(a), "aliases": list(a.aliases), "fixed": list(a.fixed)}
            for a in merge_aliases(advisories)
        ],
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def read_snapshot(path: Path) -> tuple[dict[str, Any], list[Advisory]] | None:
    try:
        data = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    rows: list[Advisory] = []
    for raw in cast(list[dict[str, Any]], data.get("advisories", [])):
        rows.append(
            Advisory(
                id=str(raw["id"]),
                ecosystem=str(raw["ecosystem"]),
                package=str(raw["package"]),
                version=str(raw["version"]),
                severity=str(raw.get("severity", "unknown")),
                aliases=tuple(str(a) for a in raw.get("aliases", ())),
                summary=str(raw.get("summary", "")),
                fixed=tuple(str(f) for f in raw.get("fixed", ())),
                origin=str(raw.get("origin", "osv")),
            )
        )
    return data, rows


# -- pinned downloads ------------------------------------------------------------------------------

type Probe = Callable[[str, Mapping[str, str]], int]
"""``probe(url, headers)``: the HTTP status of a HEAD request (redirects followed)."""


def http_probe(url: str, headers: Mapping[str, str]) -> int:
    request = urllib.request.Request(url, method="HEAD", headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:  # noqa: S310 (pinned URLs)
            return int(answer.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0


def pinned_urls(root: Path) -> list[str]:
    """Primary download URLs of the installer pins and the Docker bundles (mirrors excluded)."""
    urls: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in cast(dict[str, object], node).items():
                if key == "url" and isinstance(value, str) and value.startswith("https://"):
                    urls.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in cast(list[object], node):
                walk(item)

    install = root / "scripts" / "install" / "pins.json"
    if install.is_file():
        walk(json.loads(install.read_text(encoding="utf-8")))
    docker = root / "docker" / "pins.json"
    if docker.is_file():
        data = cast(dict[str, Any], json.loads(docker.read_text(encoding="utf-8")))
        for bundle in cast(dict[str, dict[str, Any]], data.get("bundles", {})).values():
            template = str(bundle.get("url", ""))
            for item in cast(dict[str, dict[str, Any]], bundle.get("files", {})).values():
                url = template.replace("{file}", str(item.get("file", "")))
                urls.append(url.replace("{version}", str(bundle.get("version", ""))))
    return [u for u in dict.fromkeys(urls) if u.startswith("https://")]


def docker_base(root: Path) -> tuple[str, str] | None:
    """(repository, digest) of the Docker base image (Docker Hub library images only)."""
    path = root / "docker" / "pins.json"
    if not path.is_file():
        return None
    base = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")).get("base", {}))
    image, digest = str(base.get("image", "")), str(base.get("digest", ""))
    if not image or not digest.startswith("sha256:"):
        return None
    return (image if "/" in image else f"library/{image}"), digest


UNVERIFIED = frozenset({0, 429, 500, 502, 503, 504})
"""Statuses that say nothing about the pin (no answer, rate limit, server trouble)."""


def resolve_pins(
    root: Path, probe: Probe = http_probe, fetch: Fetch = http_fetch
) -> tuple[list[str], list[str]]:
    """(problems, unverified) for the pinned downloads and the base digest; a pin that answers
    404 or another refusal is a problem, one the network could not check is only reported."""
    problems: list[str] = []
    unverified: list[str] = []

    def judge(status: int, what: str) -> None:
        if status == 200:
            return
        line = f"{what} ({status or 'no answer'})"
        (unverified if status in UNVERIFIED else problems).append(line)

    for url in pinned_urls(root):
        judge(probe(url, {}), f"pinned download {url}")
    found = docker_base(root)
    if found is not None:
        repository, digest = found
        token_url = (
            "https://auth.docker.io/token?service=registry.docker.io"
            f"&scope=repository:{repository}:pull"
        )
        try:
            token = str(cast(dict[str, Any], fetch(token_url, None)).get("token", ""))
        except AuditUnavailable as exc:
            unverified.append(f"docker base: no registry token ({exc})")
            return problems, unverified
        accept = ", ".join(
            [
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            ]
        )
        status = probe(
            f"https://registry-1.docker.io/v2/{repository}/manifests/{digest}",
            {"Authorization": f"Bearer {token}", "Accept": accept},
        )
        judge(status, f"docker base {repository}@{digest}")
    return problems, unverified


# -- report ----------------------------------------------------------------------------------------


def report(packages: Sequence[Package], advisories: Sequence[Advisory], verdict: Verdict) -> str:
    lines = ["# Dependency audit", ""]
    for ecosystem in ("PyPI", "npm"):
        rows = [p for p in packages if p.ecosystem == ecosystem]
        lines.append(
            f"- {ecosystem}: {len(rows)} locked packages, "
            f"{sum(p.source == 'registry' for p in rows)} from the registry, "
            f"{sum(p.hashed for p in rows)} with a hash"
        )
    uploaded = sorted((p.uploaded, p.name, p.version) for p in packages if p.uploaded)
    if uploaded:
        newest, oldest = uploaded[-1], uploaded[0]
        middle = uploaded[len(uploaded) // 2]
        cutoff = f"{datetime.now(UTC).year - 3}"
        stale = [u for u in uploaded if (u[0] or "") < cutoff]
        lines += [
            "",
            "## Pin age (PyPI upload times in uv.lock)",
            "",
            f"- newest: {newest[1]} {newest[2]} ({newest[0]})",
            f"- median: {middle[1]} {middle[2]} ({middle[0]})",
            f"- oldest: {oldest[1]} {oldest[2]} ({oldest[0]})",
            f"- uploaded before {cutoff}: {len(stale)} "
            f"({', '.join(f'{n} {v}' for _, n, v in stale[:12])}{' …' if len(stale) > 12 else ''})",
        ]
    lines += ["", "## Findings", ""]
    merged = merge_aliases(advisories)
    if not merged:
        lines.append("None.")
    for advisory in merged:
        lines.append(f"- {_line(advisory)}")
    lines += ["", "## Verdict", ""]
    lines += [f"- FAIL: {line}" for line in verdict.failures]
    lines += [f"- warn: {line}" for line in verdict.warnings]
    lines += [f"- {line}" for line in verdict.notes]
    if not (verdict.failures or verdict.warnings or verdict.notes):
        lines.append("- clean")
    return "\n".join(lines) + "\n"


# -- entry points ----------------------------------------------------------------------------------


def run(ctx: Context) -> Result:
    """The offline guard: today's locks against the committed snapshot and allow file."""
    result = Result(NAME)
    packages = read_locks(ctx.root)
    if not packages:
        result.notes.append("no lock files")
        return result
    found = read_snapshot(ctx.root / "tooling" / "checks" / SNAPSHOT_FILE.name)
    if found is None:
        result.setup(
            f"{SNAPSHOT_FILE.name} missing or unreadable: run "
            "`python -m tooling.checks.deps_audit --refresh`"
        )
        return result
    data, advisories = found
    if cast(dict[str, str], data.get("locks", {})) != lock_digests(ctx.root):
        result.warnings.append(
            f"the locks changed after the advisory snapshot of {data.get('fetched_at')}: the CI "
            "audit job (or `python -m tooling.checks.deps_audit --refresh`) checks the new versions"
        )
    policy = read_policy(ctx.root / "tooling" / "checks" / ALLOW_FILE.name)
    verdict = evaluate(packages, advisories, policy, date.today())
    for line in verdict.failures:
        result.fail(line)
    result.warnings.extend(verdict.warnings)
    result.notes.extend(verdict.notes)
    result.notes.append(
        f"{len(packages)} locked packages; snapshot {data.get('fetched_at')} "
        f"({len(advisories)} advisories, fail at {policy.fail_at})"
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tooling.checks.deps_audit", description=__doc__
    )
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--refresh", action="store_true", help="ask OSV now; write the snapshot")
    parser.add_argument("--pnpm-audit", action="store_true", help="also run pnpm audit --prod")
    parser.add_argument("--resolve-pins", action="store_true", help="check pinned downloads")
    parser.add_argument("--report", action="store_true", help="print a markdown report")
    parser.add_argument("--no-write", action="store_true", help="with --refresh: keep the snapshot")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    packages = read_locks(root)
    snapshot = root / "tooling" / "checks" / SNAPSHOT_FILE.name
    notes: list[str] = []
    problems: list[str] = []
    try:
        if args.refresh:
            advisories = query_osv(packages)
            if args.pnpm_audit:
                rows, note = run_pnpm_audit(root)
                advisories = [*advisories, *rows]
                notes.append(note)
            if not args.no_write:
                write_snapshot(snapshot, root, packages, advisories)
        else:
            found = read_snapshot(snapshot)
            if found is None:
                print(f"no snapshot at {snapshot}; run with --refresh", file=sys.stderr)
                return 2
            advisories = found[1]
        if args.resolve_pins:
            problems, unverified = resolve_pins(root)
            notes.extend(f"not verified (network): {line}" for line in unverified)
            problems = [f"does not resolve: {line}" for line in problems]
    except AuditUnavailable as exc:
        print(f"advisory source unavailable: {exc}", file=sys.stderr)
        return 2
    verdict = evaluate(
        packages,
        advisories,
        read_policy(root / "tooling" / "checks" / ALLOW_FILE.name),
        date.today(),
    )
    verdict.failures.extend(problems)
    verdict.notes.extend(notes)
    if args.report:
        print(report(packages, advisories, verdict), end="")
    else:
        for line in verdict.failures:
            print(f"FAIL: {line}")
        for line in verdict.warnings:
            print(f"warning: {line}")
        for line in verdict.notes:
            print(f"note: {line}")
        print(f"{len(packages)} packages; {len(verdict.failures)} failing")
    return 1 if verdict.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
