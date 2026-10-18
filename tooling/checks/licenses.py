"""Licences: every runtime dependency carries a licence from ``licenses_allow.json``.

Python: ``[project].dependencies`` of pyproject.toml, followed transitively through installed
metadata (``Requires-Dist``; entries behind ``extra ==`` markers only for extras we request).
The licence comes from ``License-Expression``, then a ``License`` field that is an SPDX
expression, then ``License ::`` classifiers, then a best-effort reading of the ``License`` text.
Node (best effort): production ``dependencies`` of ``apps/*/package.json`` read from installed
``node_modules``; skipped when nothing is installed.
"""

from __future__ import annotations

import importlib.metadata as md
import json
import re
import sys
import tomllib
from collections import deque
from pathlib import Path

from tooling.checks.common import CHECKS_DIR, Context, Result

NAME = "licenses"
ALLOW_FILE = CHECKS_DIR / "licenses_allow.json"

CLASSIFIERS = {
    "MIT License": "MIT",
    "BSD License": "BSD-3-Clause",
    "Apache Software License": "Apache-2.0",
    "ISC License (ISCL)": "ISC",
    "Python Software Foundation License": "PSF-2.0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "The Unlicense (Unlicense)": "Unlicense",
    "CC0 1.0 Universal (CC0 1.0) Public Domain Dedication": "CC0-1.0",
    "Zero-Clause BSD (0BSD)": "0BSD",
}
SHORT_NAMES = {
    "mit": "MIT",
    "mit license": "MIT",
    "the mit license": "MIT",
    "expat": "MIT",
    "bsd": "BSD-3-Clause",
    "bsd license": "BSD-3-Clause",
    "new bsd": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "3-clause bsd": "BSD-3-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "modified bsd": "BSD-3-Clause",
    "simplified bsd": "BSD-2-Clause",
    "bsd 2-clause": "BSD-2-Clause",
    "2-clause bsd": "BSD-2-Clause",
    "apache 2.0": "Apache-2.0",
    "apache 2": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache license version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "apache software license 2.0": "Apache-2.0",
    "isc": "ISC",
    "isc license": "ISC",
    "psf": "PSF-2.0",
    "psfl": "PSF-2.0",
    "python software foundation license": "PSF-2.0",
    "mpl 2.0": "MPL-2.0",
    "mozilla public license 2.0": "MPL-2.0",
    "unlicense": "Unlicense",
    "public domain": "Unlicense",
    "cc0": "CC0-1.0",
}
SPDX_TOKEN = re.compile(r"\(|\)|[A-Za-z0-9.+\-:]+")
REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?")
EXTRA_MARKER = re.compile(r"extra\s*==\s*['\"]([^'\"]+)['\"]")


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# --------------------------------------------------------------------------- SPDX


def spdx_allowed(expr: str, allow: set[str]) -> bool | None:
    """True/False for a parseable SPDX expression, None when it does not parse."""
    tokens = SPDX_TOKEN.findall(expr)
    if not tokens or "".join(tokens).replace(" ", "") != re.sub(r"\s+", "", expr):
        return None
    pos = 0

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def take() -> str:
        nonlocal pos
        pos += 1
        return tokens[pos - 1]

    def factor() -> bool:
        tok = take()
        if tok == "(":
            value = expr_or()
            if take() != ")":
                raise ValueError("unbalanced")
            return value
        if tok.upper() in ("AND", "OR", "WITH", ")"):
            raise ValueError("operator where an id was expected")
        value = tok.rstrip("+") in allow
        if (peek() or "").upper() == "WITH":
            take()
            take()  # an exception only widens a licence
        return value

    def term() -> bool:
        value = factor()
        while (peek() or "").upper() == "AND":
            take()
            value = factor() and value
        return value

    def expr_or() -> bool:
        value = term()
        while (peek() or "").upper() == "OR":
            take()
            value = term() or value
        return value

    try:
        value = expr_or()
    except (ValueError, IndexError):
        return None
    return value if pos == len(tokens) else None


def _from_text(text: str) -> str | None:
    low = " ".join(text.lower().split())
    if low in SHORT_NAMES:
        return SHORT_NAMES[low]
    if "permission is hereby granted, free of charge" in low:
        return "MIT"
    if "apache license" in low and "version 2.0" in low:
        return "Apache-2.0"
    if "redistribution and use in source and binary forms" in low:
        return "BSD-3-Clause" if "neither the name" in low else "BSD-2-Clause"
    if "permission to use, copy, modify, and/or distribute" in low:
        return "ISC"
    if "mozilla public license" in low and "2.0" in low:
        return "MPL-2.0"
    return None


def license_of(meta: md.PackageMetadata, allow: set[str]) -> tuple[str, bool]:
    """(licence as found, allowed?)."""
    expr = meta.get("License-Expression")
    if expr:
        return expr, bool(spdx_allowed(expr, allow))
    field = (meta.get("License") or "").strip()
    if field and len(field) < 120 and "\n" not in field:
        ok = spdx_allowed(field, allow)
        if ok:
            return field, True
    found = []
    for c in meta.get_all("Classifier") or []:
        if c.startswith("License ::"):
            leaf = c.split("::")[-1].strip()
            found.append(CLASSIFIERS.get(leaf, leaf))
    found = [f for f in found if f != "OSI Approved"]
    if found:
        return " OR ".join(found), any(f in allow for f in found)
    if field:
        guess = _from_text(field)
        if guess:
            return guess, guess in allow
        return field.splitlines()[0][:60], False
    return "UNKNOWN", False


# --------------------------------------------------------------------------- python side


def _split_req(req: str) -> tuple[str, set[str], str]:
    head, _, marker = req.partition(";")
    m = REQ_NAME.match(head)
    if not m:
        return "", set(), marker
    extras = {norm(e) for e in (m.group(2) or "").split(",") if e.strip()}
    return norm(m.group(1)), extras, marker


def python_deps(root: Path, result: Result) -> list[tuple[str, md.PackageMetadata]]:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        result.notes.append("no pyproject.toml: no Python dependencies")
        return []
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project", {})
    own = norm(project.get("name", ""))
    direct = project.get("dependencies", []) or []
    if not direct:
        result.notes.append("no runtime Python dependencies in pyproject.toml")
        return []
    queue: deque[tuple[str, set[str], bool]] = deque()
    for req in direct:
        name, extras, marker = _split_req(req)
        if name:
            queue.append((name, extras, not marker.strip()))
    seen: dict[str, set[str]] = {}
    out: list[tuple[str, md.PackageMetadata]] = []
    while queue:
        name, extras, required = queue.popleft()
        if name == own:
            continue
        if name in seen and extras <= seen[name]:
            continue
        first = name not in seen
        seen[name] = seen.get(name, set()) | extras
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            if required:
                result.setup(f"{name} is a runtime dependency but is not installed (run uv sync)")
            else:
                result.notes.append(f"{name}: conditional dependency not installed; skipped")
            continue
        if first:
            out.append((name, dist.metadata))
        for req in dist.requires or []:
            dep, dep_extras, marker = _split_req(req)
            if not dep:
                continue
            wanted = set(EXTRA_MARKER.findall(marker))
            if wanted and not ({norm(w) for w in wanted} & seen[name]):
                continue
            queue.append((dep, dep_extras, not marker.strip()))
    return out


# --------------------------------------------------------------------------- node side


def _node_license(pkg: dict) -> str:
    lic = pkg.get("license")
    if isinstance(lic, dict):
        lic = lic.get("type")
    if not lic and isinstance(pkg.get("licenses"), list):
        types = [x.get("type") for x in pkg["licenses"] if isinstance(x, dict) and x.get("type")]
        lic = " OR ".join(types)
    return str(lic or "UNKNOWN")


def _resolve(name: str, start: Path, root: Path) -> Path | None:
    here = start
    while True:
        cand = here / "node_modules" / name / "package.json"
        if cand.is_file():
            return cand
        if here == root or here.parent == here:
            return None
        here = here.parent


def node_deps(root: Path, result: Result) -> list[tuple[str, str]]:
    manifests = sorted((root / "apps").glob("*/package.json"))
    if not manifests:
        return []
    out: list[tuple[str, str]] = []
    seen: set[Path] = set()
    any_installed = False
    for manifest in manifests:
        pkg = json.loads(manifest.read_text(encoding="utf-8"))
        queue = deque((d, manifest.parent) for d in (pkg.get("dependencies") or {}))
        while queue:
            dep, start = queue.popleft()
            found = _resolve(dep, start, root)
            if found is None or found in seen:
                continue
            any_installed = True
            seen.add(found)
            data = json.loads(found.read_text(encoding="utf-8"))
            out.append((dep, _node_license(data)))
            queue.extend((d, found.parent) for d in (data.get("dependencies") or {}))
    if not any_installed:
        result.notes.append("apps/*/package.json found but node_modules is not installed; skipped")
    return out


# --------------------------------------------------------------------------- check entry


def load_allow(path: Path = ALLOW_FILE) -> tuple[set[str], dict[str, dict], list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return set(), {}, [f"{path.name}: cannot read: {exc}"]
    allow = set(data.get("allow", []))
    exceptions = {norm(k): v for k, v in (data.get("exceptions") or {}).items()}
    problems = [
        f"{path.name}: exception for {k} needs a reason"
        for k, v in exceptions.items()
        if not isinstance(v, dict) or not str(v.get("reason", "")).strip()
    ]
    return allow, exceptions, problems


def run(ctx: Context, allow_path: Path = ALLOW_FILE) -> Result:
    result = Result(NAME)
    allow, exceptions, problems = load_allow(allow_path)
    for p in problems:
        result.setup(p)
    if problems:
        return result
    checked: list[tuple[str, str, bool]] = []
    for name, meta in python_deps(ctx.root, result):
        lic, ok = license_of(meta, allow)
        checked.append((name, lic, ok))
    for name, lic in node_deps(ctx.root, result):
        checked.append((f"npm:{name}", lic, bool(spdx_allowed(lic, allow))))
    for name, lic, ok in checked:
        if ok:
            continue
        key = norm(name.removeprefix("npm:"))
        if key in exceptions or norm(name) in exceptions:
            result.notes.append(f"{name}: {lic} allowed by exception")
            continue
        result.fail(f"{name}: licence {lic!r} is not in the allow-list (add an exception + reason)")
    result.data["dependencies"] = [{"name": n, "license": lic, "ok": ok} for n, lic, ok in checked]
    result.notes.append(f"checked {len(checked)} dependencies")
    return result


if __name__ == "__main__":
    from tooling.checks.__main__ import main

    raise SystemExit(main([NAME, *sys.argv[1:]]))
