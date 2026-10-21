"""Generate ``THIRD_PARTY_NOTICES.md`` from what Indus Agent ships (P25-17).

What counts as shipped:

- Python: every package ``uv.lock`` reaches from the project's dependencies and from each of its
  extras (all platforms; markers are not evaluated, since some install ships each package);
- npm: every package ``pnpm-lock.yaml`` reaches from the production dependencies of a workspace
  app or package (workspace links are our own code and are skipped);
- binaries: what the installers download (``scripts/install/pins.json``) and what the Docker image
  adds (``docker/pins.json``: the base image and the bundles not marked "never shipped").

Locks carry no licences, so licences come from ``notices_data.json`` (rows keyed by package and
version, written by ``refresh`` from installed metadata or by hand). The allow-list, per-package
exceptions and copyleft obligation notes are the licence guard's (``tooling/checks/
licenses_allow.json``). The full text of each SPDX id that occurs comes from ``licence_texts/``.

Usage: ``python scripts/release/notices.py`` writes the file; ``--check`` exits 1 when the
committed file is stale or a licence problem exists; ``refresh`` updates the licence rows from the
installed Python distributions and ``node_modules`` (run it with every extra installed).
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import re
import sys
import tomllib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tooling.checks import licenses  # noqa: E402 - needs ROOT on sys.path when run as a script

HERE = Path(__file__).resolve().parent
UV_LOCK = ROOT / "uv.lock"
PNPM_LOCK = ROOT / "pnpm-lock.yaml"
INSTALL_PINS = ROOT / "scripts" / "install" / "pins.json"
DOCKER_PINS = ROOT / "docker" / "pins.json"
DATA_FILE = HERE / "notices_data.json"
TEXTS_DIR = HERE / "licence_texts"
CREDIT_FILE = ROOT / "docs" / "internals" / "notices-credit.md"
OUTPUT = ROOT / "THIRD_PARTY_NOTICES.md"
COMMAND = "python scripts/release/notices.py"

PROJECT = "indus-agent"
BASE = "base"
COPYLEFT = re.compile(r"^(?:A?GPL|LGPL|MPL|EPL|CDDL|EUPL|OSL|CPL)-", re.IGNORECASE)
NOT_SHIPPED = "never shipped"
SPDX_WORDS = frozenset({"AND", "OR", "WITH"})

KIND_ORDER = ("Python", "npm", "binary", "image")


@dataclass(frozen=True)
class Component:
    kind: str
    name: str
    version: str
    used_by: tuple[str, ...]
    licence: str = ""
    note: str = ""

    @property
    def key(self) -> tuple[int, str, str]:
        return (KIND_ORDER.index(self.kind), self.name.lower(), self.version)


# -- the Python lock ----------------------------------------------------------------------------


def norm(name: str) -> str:
    return licenses.norm(name)


def python_components(lock_text: str, project: str = PROJECT) -> list[Component]:
    """Packages reachable from ``project``'s dependencies and extras, with who brings them."""
    data = tomllib.loads(lock_text)
    by_name: dict[str, list[dict[str, Any]]] = {}
    for pkg in cast(list[dict[str, Any]], data.get("package", [])):
        by_name.setdefault(norm(pkg["name"]), []).append(pkg)
    roots = by_name.get(norm(project))
    if not roots:
        raise NoticeError(f"uv.lock has no package {project!r}")
    root = roots[0]
    starts: list[tuple[str, list[dict[str, Any]]]] = [(BASE, root.get("dependencies", []))]
    for extra, deps in sorted(
        cast(dict[str, list[dict[str, Any]]], root.get("optional-dependencies", {})).items()
    ):
        starts.append((f"extra {extra}", deps))

    reached: dict[tuple[str, str], set[str]] = {}
    for label, deps in starts:
        queue = list(deps)
        seen: set[tuple[str, str, tuple[str, ...]]] = set()
        while queue:
            dep = queue.pop()
            name = norm(dep["name"])
            if name == norm(project):
                continue
            candidates = by_name.get(name, [])
            if "version" in dep:
                candidates = [p for p in candidates if p.get("version") == dep["version"]]
            extras = tuple(sorted(cast(list[str], dep.get("extra", []))))
            for pkg in candidates:
                token = (name, str(pkg.get("version", "")), extras)
                if token in seen:
                    continue
                seen.add(token)
                reached.setdefault((pkg["name"], str(pkg.get("version", ""))), set()).add(label)
                queue.extend(pkg.get("dependencies", []))
                optional = cast(
                    dict[str, list[dict[str, Any]]], pkg.get("optional-dependencies", {})
                )
                for extra in extras:
                    queue.extend(optional.get(extra, []))
    return [
        Component("Python", name, version, _order_users(users))
        for (name, version), users in reached.items()
    ]


def _order_users(users: Iterable[str]) -> tuple[str, ...]:
    """``base`` first, then the rest sorted; ``base`` alone when it is there (extras repeat it)."""
    found = set(users)
    if BASE in found:
        return (BASE,)
    return tuple(sorted(found))


# -- the pnpm lock ------------------------------------------------------------------------------

_PEERS = re.compile(r"\(.*\)$")


def _pnpm_document(text: str) -> dict[str, Any]:
    from ruamel.yaml import YAML

    docs = [d for d in YAML(typ="safe").load_all(text) if isinstance(d, dict)]
    for doc in reversed(docs):
        importers = cast(dict[str, Any], doc.get("importers") or {})
        if any(path != "." for path in importers) or "snapshots" in doc:
            return cast(dict[str, Any], doc)
    raise NoticeError("pnpm-lock.yaml has no importers")


def _resolve_npm(name: str, version: str) -> tuple[str, str] | None:
    """``(package name, version with peers)`` for a dependency entry; None for workspace links."""
    if version.startswith(("link:", "file:", "workspace:")):
        return None
    if not version[:1].isdigit() and "@" in version.lstrip("@"):
        # an alias: ``npm:other@1.2.3`` is locked as ``other@1.2.3``
        alias, _, real = version.lstrip("@").partition("@")
        return ("@" + alias if version.startswith("@") else alias), real
    return name, version


def npm_components(lock_text: str) -> list[Component]:
    doc = _pnpm_document(lock_text)
    snapshots = cast(dict[str, dict[str, Any]], doc.get("snapshots") or {})
    reached: dict[tuple[str, str], set[str]] = {}
    for importer, spec in sorted(
        cast(dict[str, dict[str, Any]], doc.get("importers") or {}).items()
    ):
        if importer == ".":
            continue
        queue: list[tuple[str, str]] = []
        for section in ("dependencies", "optionalDependencies"):
            for name, entry in cast(dict[str, Any], spec.get(section) or {}).items():
                version = entry["version"] if isinstance(entry, dict) else str(entry)
                resolved = _resolve_npm(name, str(version))
                if resolved:
                    queue.append(resolved)
        seen: set[tuple[str, str]] = set()
        while queue:
            name, version = queue.pop()
            if (name, version) in seen:
                continue
            seen.add((name, version))
            plain = _PEERS.sub("", version)
            reached.setdefault((name, plain), set()).add(importer)
            snap = snapshots.get(f"{name}@{version}") or {}
            for section in ("dependencies", "optionalDependencies"):
                for dep, dep_version in cast(dict[str, Any], snap.get(section) or {}).items():
                    resolved = _resolve_npm(dep, str(dep_version))
                    if resolved:
                        queue.append(resolved)
    return [
        Component("npm", name, version, tuple(sorted(users)))
        for (name, version), users in reached.items()
    ]


# -- pinned binaries ----------------------------------------------------------------------------


def pinned_components(
    install_pins: Mapping[str, Any], docker_pins: Mapping[str, Any], data: Mapping[str, Any]
) -> tuple[list[Component], list[str]]:
    """Binaries the installers and the Docker image ship; problems for pins without a row."""
    rows = cast(Mapping[str, Mapping[str, str]], data.get("binaries", {}))
    found: list[Component] = []
    problems: list[str] = []
    for key, value in install_pins.items():
        if not isinstance(value, Mapping) or key == "release":
            continue
        pin = cast(Mapping[str, Any], value)
        version = str(pin.get("version") or pin.get("provision") or "")
        if not version:
            continue
        row = rows.get(key)
        if row is None:
            problems.append(f"pinned binary {key!r} (scripts/install/pins.json) has no licence row")
            continue
        found.append(
            Component(
                "binary",
                row.get("name", key),
                version,
                ("installers",),
                row["licence"],
                row.get("note", ""),
            )
        )
    base = cast(Mapping[str, Any], docker_pins.get("base") or {})
    if base:
        row = rows.get("docker-base")
        name = str(base.get("image", ""))
        if row is None:
            problems.append(f"Docker base image {name} has no licence row")
        else:
            found.append(
                Component(
                    "image",
                    name,
                    f"{base.get('tag')} ({str(base.get('digest', ''))[:19]})",
                    ("Docker image",),
                    row["licence"],
                    row.get("note", ""),
                )
            )
    for key, value in cast(Mapping[str, Any], docker_pins.get("bundles") or {}).items():
        bundle = cast(Mapping[str, Any], value)
        licence = str(bundle.get("licence", ""))
        if NOT_SHIPPED in licence:
            continue
        row = rows.get(f"docker-{key}", {})
        found.append(
            Component(
                "binary",
                key,
                str(bundle.get("version", "")),
                ("Docker image",),
                row.get("licence", licence),
                row.get("note", ""),
            )
        )
    return found, problems


# -- licence rows -------------------------------------------------------------------------------


class NoticeError(Exception):
    """The inputs cannot be read."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise NoticeError(f"{path.name}: cannot read: {exc}") from exc


def _row_key(component: Component) -> str:
    return norm(component.name) if component.kind == "Python" else component.name


def attach_licences(
    components: Iterable[Component], data: Mapping[str, Any]
) -> tuple[list[Component], list[str]]:
    """Fill each package's licence from its row; a missing row or another version is unknown."""
    tables = cast(Mapping[str, Mapping[str, Mapping[str, Any]]], data.get("licences", {}))
    out: list[Component] = []
    problems: list[str] = []
    for comp in components:
        if comp.licence:
            out.append(comp)
            continue
        versions = tables.get(comp.kind, {}).get(_row_key(comp), {})
        row = versions.get(comp.version)
        if not isinstance(row, Mapping) or not str(row.get("licence", "")).strip():
            problems.append(
                f"{comp.kind} {comp.name} {comp.version}: unknown licence "
                f"(run `{COMMAND} refresh` with it installed, or add the row by hand)"
            )
            continue
        row_map = cast(Mapping[str, Any], row)
        out.append(
            replace(comp, licence=str(row_map["licence"]), note=str(row_map.get("note", "")))
        )
    return out, problems


def licence_ids(expression: str) -> list[str]:
    """The SPDX licence ids an expression names (exceptions after ``WITH`` left out)."""
    tokens = re.findall(r"[A-Za-z0-9.+\-:]+", expression)
    ids: list[str] = []
    skip = False
    for token in tokens:
        if token.upper() == "WITH":
            skip = True
            continue
        if token.upper() in SPDX_WORDS:
            continue
        if skip:
            skip = False
            continue
        ident = token.rstrip("+")
        if ident not in ids:
            ids.append(ident)
    return ids


@dataclass
class Policy:
    allow: set[str]
    exceptions: dict[str, dict[str, Any]]
    obligations: dict[str, str]

    @classmethod
    def load(cls, path: Path = licenses.ALLOW_FILE) -> Policy:
        allow, exceptions, problems = licenses.load_allow(path)
        if problems:
            raise NoticeError("; ".join(problems))
        return cls(allow, exceptions, licenses.load_obligations(path))


def policy_problems(components: Iterable[Component], policy: Policy) -> list[str]:
    problems: list[str] = []
    for comp in components:
        verdict = licenses.spdx_allowed(comp.licence, policy.allow)
        if verdict is None:
            problems.append(
                f"{comp.kind} {comp.name} {comp.version}: {comp.licence!r} is not an SPDX "
                "expression"
            )
            continue
        if not verdict and norm(comp.name) not in policy.exceptions:
            problems.append(
                f"{comp.kind} {comp.name} {comp.version}: licence {comp.licence!r} is not on the "
                "allow-list (tooling/checks/licenses_allow.json) and has no exception"
            )
        ids = licence_ids(comp.licence)
        copyleft = {i for i in ids if COPYLEFT.match(i)}
        if copyleft and licenses.spdx_allowed(comp.licence, policy.allow - copyleft):
            continue  # a permissive alternative is offered (``MPL-2.0 OR Apache-2.0``)
        for ident in ids:
            if ident in copyleft and ident not in policy.obligations:
                problems.append(
                    f"{comp.kind} {comp.name} {comp.version}: copyleft {ident} needs an obligation "
                    "note under `obligations` in tooling/checks/licenses_allow.json"
                )
    return problems


def load_texts(ids: Iterable[str], texts_dir: Path = TEXTS_DIR) -> tuple[dict[str, str], list[str]]:
    texts: dict[str, str] = {}
    problems: list[str] = []
    for ident in sorted(set(ids)):
        path = texts_dir / f"{ident}.txt"
        if not path.is_file():
            shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
            problems.append(f"licence text {ident}: missing {shown}")
            continue
        texts[ident] = path.read_text(encoding="utf-8").strip("\n")
    return texts, problems


# -- rendering ----------------------------------------------------------------------------------

MARKER = (
    "<!-- Generated by scripts/release/notices.py from the locks and pins. Do not edit: run it. -->"
)


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _fence(text: str) -> list[str]:
    fence = "````" if "```" in text else "```"
    return [fence + "text", text, fence]


def credit_section(text: str) -> str:
    """The credit file without its leading HTML comment."""
    body = re.sub(r"\A\s*<!--.*?-->\s*", "", text, flags=re.DOTALL)
    return body.strip("\n")


def render(
    components: Sequence[Component],
    texts: Mapping[str, str],
    policy: Policy,
    data: Mapping[str, Any],
    credit: str,
) -> str:
    lines: list[str] = [MARKER, "", "# Third-party notices", ""]
    lines += [credit_section(credit), ""]

    kinds = {k: sum(1 for c in components if c.kind == k) for k in KIND_ORDER}
    lines += ["## What Indus Agent ships", ""]
    lines += [
        "Every component below is redistributed by at least one way of installing Indus Agent "
        f"(installers, Docker image, desktop app, `uv sync` with extras): {kinds['Python']} Python "
        f"packages, {kinds['npm']} npm packages bundled into the dashboard, terminal UI and "
        f"desktop app, {kinds['binary']} pinned binaries and {kinds['image']} base image. "
        '"Used by" says which part brings each one in: `base` is the core install, '
        "`extra <name>` an optional extra, `apps/<name>` a JavaScript app.",
        "",
        "Components are grouped by licence (SPDX expression). Each licence's full text follows "
        "once at the end; every package's own copyright notice stays in its distribution.",
        "",
    ]
    groups: dict[str, list[Component]] = {}
    for comp in components:
        groups.setdefault(comp.licence, []).append(comp)
    for licence in sorted(groups, key=str.lower):
        members = sorted(groups[licence], key=lambda c: c.key)
        lines += [f"### {licence}", ""]
        lines += ["| Component | Version | Kind | Used by | Note |", "|---|---|---|---|---|"]
        for comp in members:
            exception = policy.exceptions.get(norm(comp.name))
            note = comp.note
            if exception and not licenses.spdx_allowed(comp.licence, policy.allow):
                lead = note.rstrip(". ") + ". " if note else ""
                note = lead + "Allowed by exception: " + str(exception["reason"])
            lines.append(
                f"| {_cell(comp.name)} | {_cell(comp.version)} | {comp.kind} | "
                f"{_cell(', '.join(comp.used_by))} | {_cell(note)} |"
            )
        lines.append("")

    used_ids = sorted({i for c in components for i in licence_ids(c.licence)}, key=str.lower)
    copyleft = [i for i in used_ids if COPYLEFT.match(i) and i in policy.obligations]
    if copyleft:
        lines += ["## Obligations", ""]
        for ident in copyleft:
            lines += [f"- **{ident}**: {policy.obligations[ident]}"]
        lines.append("")

    for section in cast(list[Mapping[str, Any]], data.get("sections", [])):
        lines += [f"## {section['title']}", "", str(section["text"]), ""]
        rows = cast(list[list[str]], section.get("rows", []))
        if rows:
            header = cast(list[str], section["columns"])
            lines += ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
            lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
            lines.append("")

    lines += ["## Licence texts", ""]
    for ident in used_ids:
        lines += [f"### {ident}", ""] + _fence(texts[ident]) + [""]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


# -- the whole run ------------------------------------------------------------------------------


@dataclass
class Inputs:
    uv_lock: str
    pnpm_lock: str
    install_pins: Mapping[str, Any]
    docker_pins: Mapping[str, Any]
    data: Mapping[str, Any]
    policy: Policy
    credit: str
    texts_dir: Path = TEXTS_DIR

    @classmethod
    def from_repo(cls) -> Inputs:
        return cls(
            uv_lock=UV_LOCK.read_text(encoding="utf-8"),
            pnpm_lock=PNPM_LOCK.read_text(encoding="utf-8"),
            install_pins=load_json(INSTALL_PINS),
            docker_pins=load_json(DOCKER_PINS),
            data=load_json(DATA_FILE),
            policy=Policy.load(),
            credit=CREDIT_FILE.read_text(encoding="utf-8"),
        )


@dataclass
class Outcome:
    text: str
    components: list[Component]
    problems: list[str] = field(default_factory=list[str])


def components_of(inputs: Inputs) -> tuple[list[Component], list[str]]:
    found = python_components(inputs.uv_lock) + npm_components(inputs.pnpm_lock)
    pinned, problems = pinned_components(inputs.install_pins, inputs.docker_pins, inputs.data)
    with_licences, unknown = attach_licences(found + pinned, inputs.data)
    return sorted(with_licences, key=lambda c: c.key), problems + unknown


def generate(inputs: Inputs) -> Outcome:
    components, problems = components_of(inputs)
    problems += policy_problems(components, inputs.policy)
    ids = {i for c in components for i in licence_ids(c.licence)}
    texts, missing = load_texts(ids, inputs.texts_dir)
    problems += missing
    if problems:
        return Outcome("", components, problems)
    return Outcome(render(components, texts, inputs.policy, inputs.data, inputs.credit), components)


# -- refresh ------------------------------------------------------------------------------------


def _python_licence(name: str, version: str, allow: set[str]) -> str | None:
    try:
        dist = md.distribution(name)
    except md.PackageNotFoundError:
        return None
    if dist.version != version:
        return None
    found, _ok = licenses.license_of(dist.metadata, allow)
    return (
        found if licenses.spdx_allowed(found, allow | set(licence_ids(found))) is not None else None
    )


def _npm_licence(name: str, version: str, root: Path = ROOT) -> str | None:
    store = root / "node_modules" / ".pnpm"
    escaped = name.replace("/", "+")
    for path in sorted(store.glob(f"{escaped}@{version}*/node_modules/{name}/package.json")):
        try:
            pkg = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
        if pkg.get("version") == version:
            return licenses._node_license(pkg)  # pyright: ignore[reportPrivateUsage]
    return None


def refresh(inputs: Inputs) -> tuple[dict[str, Any], list[str]]:
    """Licence rows for every locked package: installed metadata where it is readable, the
    existing row otherwise (rows for versions no longer locked are dropped)."""
    data = json.loads(json.dumps(inputs.data))
    old = cast(dict[str, dict[str, dict[str, Any]]], data.get("licences", {}))
    fresh: dict[str, dict[str, dict[str, Any]]] = {}
    still_unknown: list[str] = []
    for comp in python_components(inputs.uv_lock) + npm_components(inputs.pnpm_lock):
        key = _row_key(comp)
        previous = old.get(comp.kind, {}).get(key, {}).get(comp.version)
        if comp.kind == "Python":
            found = _python_licence(comp.name, comp.version, inputs.policy.allow)
        else:
            found = _npm_licence(comp.name, comp.version)
        row: dict[str, Any] | None
        if isinstance(previous, dict) and previous.get("source") == "manual":
            row = cast(dict[str, Any], previous)
        elif found and found != "UNKNOWN":
            row = {"licence": found, "source": "metadata"}
            if isinstance(previous, dict) and previous.get("note"):
                row["note"] = previous["note"]
        else:
            row = cast(dict[str, Any], previous) if isinstance(previous, dict) else None
        if row is None:
            still_unknown.append(f"{comp.kind} {comp.name} {comp.version}")
            continue
        fresh.setdefault(comp.kind, {}).setdefault(key, {})[comp.version] = row
    data["licences"] = {
        kind: {name: dict(sorted(versions.items())) for name, versions in sorted(rows.items())}
        for kind, rows in sorted(fresh.items())
    }
    return data, still_unknown


def dump_json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


# -- entry --------------------------------------------------------------------------------------


def _report(problems: Iterable[str]) -> Iterator[str]:
    for line in problems:
        yield f"notices: {line}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate THIRD_PARTY_NOTICES.md (P25-17).")
    parser.add_argument("action", nargs="?", choices=("write", "refresh"), default="write")
    parser.add_argument("--check", action="store_true", help="fail when the file is stale")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    try:
        inputs = Inputs.from_repo()
    except (NoticeError, OSError) as exc:
        print(f"notices: {exc}", file=sys.stderr)
        return 2
    if args.action == "refresh":
        data, unknown = refresh(inputs)
        DATA_FILE.write_text(dump_json(data), encoding="utf-8", newline="\n")
        for line in _report(f"{u}: no licence found; add a manual row" for u in unknown):
            print(line, file=sys.stderr)
        print(f"notices: licence rows refreshed ({DATA_FILE.name})")
        return 1 if unknown else 0
    outcome = generate(inputs)
    if outcome.problems:
        for line in _report(outcome.problems):
            print(line, file=sys.stderr)
        return 1
    current = args.output.read_text(encoding="utf-8") if args.output.is_file() else None
    if args.check:
        if current != outcome.text:
            print(
                f"notices: {args.output.name} is stale: run `{COMMAND}` and commit it",
                file=sys.stderr,
            )
            return 1
        print(f"notices: {args.output.name} is fresh ({len(outcome.components)} components)")
        return 0
    if current != outcome.text:
        args.output.write_text(outcome.text, encoding="utf-8", newline="\n")
    print(f"notices: wrote {args.output.name} ({len(outcome.components)} components)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
