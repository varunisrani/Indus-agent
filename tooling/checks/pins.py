"""Pins: one dependency policy, checked literally (D-16.52).

1. Python requirements in ``[project].dependencies``, ``[project.optional-dependencies]`` and
   ``[dependency-groups]`` are ``name[extras]==X.Y[.Z]``. Markers may use only ``sys_platform``
   and ``platform_machine``. A bounded range ``>=a,<b`` passes only with a matching entry and a
   reason in ``pins_allow.json``.
2. ``[tool.uv] exclude-newer`` is an RFC 3339 timestamp at least 14 days before the lock date in
   ``pins_meta.json``; every file in ``uv.lock`` was uploaded at or before it.
3. ``package.json`` dependencies, devDependencies and optionalDependencies are exact versions;
   ``workspace:*`` only for ``@indus/*``.
4. With any ``package.json`` present, ``pnpm-workspace.yaml`` sets ``minimumReleaseAge >= 20160``.
5. Git sources name a full 40-hex commit.
6. Every Dockerfile base outside ``tests/`` (``Dockerfile`` or ``*.Dockerfile``) is pinned by
   ``@sha256:<64 hex>``, directly or through an ``ARG`` default; ``scratch`` and earlier stages
   need nothing. An all-zero digest is an owner placeholder: with an ``owner-to-fill`` mark in
   the file it is reported as a warning, without one it fails.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from tooling.checks.common import CHECKS_DIR, Context, Result, rel, walk_files

NAME = "pins"
ALLOW_FILE = CHECKS_DIR / "pins_allow.json"
META_FILE = CHECKS_DIR / "pins_meta.json"

MIN_AGE = timedelta(days=14)
MIN_RELEASE_AGE_MINUTES = 20160
ALLOWED_MARKER_VARS = frozenset({"sys_platform", "platform_machine"})
NODE_FIELDS = ("dependencies", "devDependencies", "optionalDependencies")

REQ = re.compile(
    r"""^\s*(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*
        (?:\[(?P<extras>[^\]]*)\])?\s*
        (?P<rest>[^;]*?)\s*
        (?:;\s*(?P<marker>.*))?$""",
    re.VERBOSE,
)
EXACT_PY = re.compile(r"^==\d+\.\d+(?:\.\d+)?$")
BOUNDED_PY = re.compile(r"^>=\d+(?:\.\d+)*,<\d+(?:\.\d+)*$")
# A marker variable: an identifier that is not an operator word and sits outside quotes.
MARKER_NAMES = re.compile(
    r"\b(?!and\b|or\b|in\b|not\b)([a-z][a-z_]*)\b(?=(?:[^'\"]|'[^']*'|\"[^\"]*\")*$)"
)
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$")
SHA = re.compile(r"^[0-9a-f]{40}$")
EXACT_NODE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
NODE_GIT = re.compile(r"^(?:git\+[^#]+|git://[^#]+|github:[^#]+|[\w.-]+/[\w.-]+)#(?P<rev>.*)$")
RELEASE_AGE = re.compile(r"^minimumReleaseAge\s*:\s*(?P<value>[^#]*?)\s*(?:#.*)?$", re.MULTILINE)
UPLOAD_TIME = re.compile(r'upload-time\s*=\s*"([^"]+)"')
DOCKER_ARG = re.compile(r"^\s*ARG\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:=(?P<value>\S*))?", re.I)
DOCKER_FROM = re.compile(
    r"^\s*FROM\s+(?:--platform=\S+\s+)?(?P<image>\S+)(?:\s+AS\s+(?P<stage>\S+))?", re.I
)
DOCKER_REF = re.compile(r"^\$\{?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}?$")
DIGEST_PIN = re.compile(r"@sha256:(?P<hex>[0-9a-f]{64})$")
PLACEHOLDER_HEX = "0" * 64
OWNER_MARK = "owner-to-fill"


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# --------------------------------------------------------------------------- policy files


def load_allow(path: Path) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Allow entries keyed by normalised name, plus setup problems."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"{path.name}: cannot read: {exc}"]
    entries = data.get("allow", {}) if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}, [f"{path.name}: 'allow' must be an object of name -> {{spec, reason}}"]
    out: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    for name, entry in entries.items():
        if not isinstance(entry, dict):
            problems.append(f"{path.name}: {name}: entry must be an object with spec and reason")
            continue
        spec = re.sub(r"\s+", "", str(entry.get("spec", "")))
        reason = str(entry.get("reason", "")).strip()
        if not reason:
            problems.append(f"{path.name}: {name}: entry needs a non-empty reason")
            continue
        if not BOUNDED_PY.match(spec):
            problems.append(f"{path.name}: {name}: spec {spec!r} must be a bounded range '>=a,<b'")
            continue
        out[norm(name)] = {"spec": spec, "reason": reason}
    return out, problems


def load_meta(path: Path) -> tuple[date | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return date.fromisoformat(str(data["lock_date"])), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{path.name}: cannot read: {exc}"
    except (KeyError, TypeError, ValueError):
        return None, f"{path.name}: needs 'lock_date' as YYYY-MM-DD"


def parse_timestamp(text: str) -> datetime | None:
    """A full RFC 3339 date-time (date, time and offset), or None."""
    if not RFC3339.match(text):
        return None
    try:
        return datetime.fromisoformat(text.replace("z", "Z").replace("t", "T"))
    except ValueError:
        return None


# --------------------------------------------------------------------------- python side


def marker_problem(marker: str) -> str | None:
    names = set(MARKER_NAMES.findall(marker))
    bad = sorted(names - ALLOWED_MARKER_VARS)
    if bad:
        return (
            f"marker uses {', '.join(bad)}; only sys_platform/platform_machine are allowed"
            " -> drop the marker (one requires-python line covers interpreters)"
        )
    return None


def check_requirement(
    req: str, where: str, allow: dict[str, dict[str, str]], used: set[str], result: Result
) -> None:
    m = REQ.match(req)
    if not m:
        result.fail(f"{where}: {req!r}: not a requirement -> write name==X.Y.Z")
        return
    name, rest, marker = m.group("name"), m.group("rest").strip(), m.group("marker")
    if marker is not None:
        problem = marker_problem(marker)
        if problem:
            result.fail(f"{where}: {req!r}: {problem}")
    if rest.startswith("@"):
        url = rest[1:].strip()
        if not url.startswith("git+"):
            result.fail(f"{where}: {req!r}: direct URL dependency -> use a pinned index release")
            return
        rev = url.rsplit("@", 1)[1] if url.count("@") >= 1 and "://" in url else ""
        rev = rev.split("#", 1)[0]
        if not SHA.match(rev):
            result.fail(f"{where}: {req!r}: git source without a 40-hex commit -> append @<sha>")
        return
    spec = re.sub(r"\s+", "", rest)
    if EXACT_PY.match(spec):
        return
    entry = allow.get(norm(name))
    if entry and spec == entry["spec"]:
        used.add(norm(name))
        return
    if entry:
        result.fail(
            f"{where}: {req!r}: spec {spec!r} differs from the allowed {entry['spec']!r}"
            " -> match pins_allow.json or pin name==X.Y.Z"
        )
        return
    result.fail(f"{where}: {req!r}: not an exact pin -> write {name}==X.Y.Z")


def iter_requirements(data: dict[str, Any]) -> list[tuple[str, str]]:
    """(where, requirement) for every Python dependency entry the policy covers."""
    out: list[tuple[str, str]] = []
    project = data.get("project", {}) or {}
    for req in project.get("dependencies", []) or []:
        out.append(("[project].dependencies", req))
    for extra, reqs in (project.get("optional-dependencies", {}) or {}).items():
        for req in reqs or []:
            out.append((f"[project.optional-dependencies].{extra}", req))
    for group, reqs in (data.get("dependency-groups", {}) or {}).items():
        for req in reqs or []:
            if isinstance(req, str):  # {include-group = ...} tables are not requirements
                out.append((f"[dependency-groups].{group}", req))
    return out


def check_uv_sources(data: dict[str, Any], result: Result) -> None:
    sources = ((data.get("tool", {}) or {}).get("uv", {}) or {}).get("sources", {}) or {}
    for name, value in sources.items():
        for src in value if isinstance(value, list) else [value]:
            if not isinstance(src, dict) or "git" not in src:
                continue
            where = f"pyproject.toml: [tool.uv.sources].{name}"
            if "branch" in src or "tag" in src:
                result.fail(f"{where}: git source uses branch/tag -> use rev = '<40-hex sha>'")
            elif not SHA.match(str(src.get("rev", ""))):
                result.fail(f"{where}: git source without a 40-hex rev -> set rev = '<sha>'")


def check_window(
    data: dict[str, Any], root: Path, meta_path: Path, result: Result, today: date
) -> None:
    uv = (data.get("tool", {}) or {}).get("uv", {}) or {}
    raw = uv.get("exclude-newer")
    if raw is None:
        result.fail(
            "pyproject.toml: [tool.uv] exclude-newer is missing"
            " -> set an RFC 3339 timestamp 14 days before the lock date"
        )
        return
    cutoff = parse_timestamp(str(raw))
    if cutoff is None:
        result.fail(
            f"pyproject.toml: [tool.uv] exclude-newer {raw!r} is not an RFC 3339 timestamp"
            " -> e.g. 2026-10-04T00:00:00Z"
        )
        return
    lock_date, problem = load_meta(meta_path)
    if problem or lock_date is None:
        result.setup(problem or f"{meta_path.name}: no lock date")
        return
    if lock_date > today + timedelta(days=1):
        result.fail(f"{meta_path.name}: lock_date {lock_date} is in the future -> today or earlier")
    lock_start = datetime(lock_date.year, lock_date.month, lock_date.day, tzinfo=UTC)
    if lock_start - cutoff < MIN_AGE:
        latest = (lock_start - MIN_AGE).strftime("%Y-%m-%dT%H:%M:%SZ")
        result.fail(
            f"pyproject.toml: [tool.uv] exclude-newer {raw} is less than 14 days before the lock"
            f" date {lock_date} -> set it to {latest} or earlier"
        )
    result.data["exclude_newer"] = str(raw)
    result.data["lock_date"] = lock_date.isoformat()
    lock = root / "uv.lock"
    if not lock.is_file():
        result.notes.append("no uv.lock: upload times not checked")
        return
    check_lock_ages(lock, cutoff, result)


def check_lock_ages(lock: Path, cutoff: datetime, result: Result) -> None:
    name = "?"
    young: dict[str, str] = {}
    for line in lock.read_text(encoding="utf-8").splitlines():
        m = re.match(r'^name\s*=\s*"([^"]+)"', line)
        if m:
            name = m.group(1)
        for stamp in UPLOAD_TIME.findall(line):
            when = parse_timestamp(stamp)
            if when is not None and when > cutoff and stamp > young.get(name, ""):
                young[name] = stamp
    for pkg, stamp in sorted(young.items()):
        result.fail(
            f"uv.lock: {pkg}: uploaded {stamp}, after exclude-newer"
            " -> relock with `uv lock` so the cutoff applies"
        )


def check_python(root: Path, allow_path: Path, meta_path: Path, result: Result, today: date) -> int:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        result.notes.append("no pyproject.toml: no Python pins to check")
        return 0
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        result.setup(f"pyproject.toml: cannot parse: {exc}")
        return 0
    allow, problems = load_allow(allow_path)
    for p in problems:
        result.setup(p)
    if problems:
        return 0
    used: set[str] = set()
    reqs = iter_requirements(data)
    for where, req in reqs:
        check_requirement(req, f"pyproject.toml: {where}", allow, used, result)
    for name in sorted(set(allow) - used):
        result.warnings.append(f"{allow_path.name}: {name}: allow entry is not used by any range")
    check_uv_sources(data, result)
    check_window(data, root, meta_path, result, today)
    return len(reqs)


# --------------------------------------------------------------------------- node side


def node_spec_problem(name: str, spec: str) -> str | None:
    if EXACT_NODE.match(spec):
        return None
    if spec.startswith("workspace:"):
        if name.startswith("@indus/") and spec == "workspace:*":
            return None
        return "workspace protocol is only for @indus/* as workspace:*"
    m = NODE_GIT.match(spec)
    if m:
        return None if SHA.match(m.group("rev")) else "git source without a 40-hex commit after #"
    return "not an exact X.Y.Z version"


def check_node(root: Path, result: Result) -> int:
    manifests = [p for p in walk_files(root) if p.name == "package.json"]
    count = 0
    for manifest in manifests:
        where = rel(manifest, root)
        try:
            pkg = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            result.fail(f"{where}: cannot parse: {exc}")
            continue
        for field in NODE_FIELDS:
            deps = pkg.get(field) or {}
            if not isinstance(deps, dict):
                result.fail(f"{where}: {field} must be an object")
                continue
            for name, spec in deps.items():
                count += 1
                problem = node_spec_problem(name, str(spec))
                if problem:
                    result.fail(f"{where}: {field}.{name} {spec!r}: {problem} -> pin X.Y.Z")
    if manifests:
        check_workspace(root, result)
    return count


def check_workspace(root: Path, result: Result) -> None:
    ws = root / "pnpm-workspace.yaml"
    fix = f"-> add 'minimumReleaseAge: {MIN_RELEASE_AGE_MINUTES}' (14 days in minutes)"
    if not ws.is_file():
        result.fail(f"pnpm-workspace.yaml: missing while package.json files exist {fix}")
        return
    m = RELEASE_AGE.search(ws.read_text(encoding="utf-8"))
    if not m:
        result.fail(f"pnpm-workspace.yaml: minimumReleaseAge is not set {fix}")
        return
    value = m.group("value").strip().strip("'\"")
    if not value.isdigit() or int(value) < MIN_RELEASE_AGE_MINUTES:
        result.fail(f"pnpm-workspace.yaml: minimumReleaseAge {value!r} is below 14 days {fix}")


# --------------------------------------------------------------------------- dockerfiles


def is_dockerfile(path: Path, root: Path) -> bool:
    if path.name != "Dockerfile" and not path.name.endswith(".Dockerfile"):
        return False
    return path.relative_to(root).parts[0] != "tests"


def check_dockerfile(path: Path, root: Path, result: Result) -> int:
    """Rule 6 for one Dockerfile; returns how many bases it checked."""
    where = rel(path, root)
    text = path.read_text(encoding="utf-8")
    args: dict[str, str] = {}
    stages: set[str] = set()
    count = 0
    for number, line in enumerate(text.splitlines(), start=1):
        arg = DOCKER_ARG.match(line)
        if arg:
            args[arg.group("name")] = arg.group("value") or ""
            continue
        base = DOCKER_FROM.match(line)
        if not base:
            continue
        image = base.group("image")
        if base.group("stage"):
            stages.add(base.group("stage").lower())
        if image.lower() == "scratch" or image.lower() in stages:
            continue
        count += 1
        ref = DOCKER_REF.match(image)
        if ref:
            image = args.get(ref.group("name"), "")
        pin = DIGEST_PIN.search(image)
        if pin is None:
            result.fail(
                f"{where}:{number}: base {image or base.group('image')!r} is not pinned"
                " -> write <image>:<tag>@sha256:<digest>"
            )
        elif pin.group("hex") == PLACEHOLDER_HEX:
            if OWNER_MARK in text:
                result.warnings.append(
                    f"{where}:{number}: base digest is the owner-to-fill placeholder"
                    " -> resolve it with `docker buildx imagetools inspect` and fill it in"
                )
            else:
                result.fail(f"{where}:{number}: all-zero base digest without an owner-to-fill mark")
    return count


def check_docker(root: Path, result: Result) -> int:
    return sum(
        check_dockerfile(path, root, result)
        for path in walk_files(root)
        if is_dockerfile(path, root)
    )


# --------------------------------------------------------------------------- entry


def run(
    ctx: Context,
    allow_path: Path = ALLOW_FILE,
    meta_path: Path = META_FILE,
    today: date | None = None,
) -> Result:
    result = Result(NAME)
    today = today or datetime.now(UTC).date()
    py = check_python(ctx.root, allow_path, meta_path, result, today)
    node = check_node(ctx.root, result)
    bases = check_docker(ctx.root, result)
    result.notes.append(
        f"checked {py} Python and {node} Node dependency entries and {bases} Dockerfile bases"
    )
    return result


if __name__ == "__main__":
    from tooling.checks.__main__ import main

    raise SystemExit(main([NAME, *sys.argv[1:]]))
