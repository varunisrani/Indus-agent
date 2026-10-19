"""Skill bundles: ``<home>/skill-bundles/<slug>.yaml`` files that load several skills at once.

File shape::

    name: release-day          # optional; default = file stem; slugified into the command
    description: Ship a release
    skills: [git-workflow, test-first]
    instruction: Work through the release checklist.   # optional, sent first

``/<slug> [text]`` loads every listed skill in one user message, the bundle's instruction first,
then the user's text. Missing skills are skipped and named. A bundle beats a same-slug skill.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from indus.foundation import yamlio
from indus.foundation.fsio import atomic_write_text
from indus.foundation.scope import Scope
from indus.learning.skills.invoke import Expansion, ShellRunner, expand_skills
from indus.learning.skills.resolver import SkillCatalog

__all__ = [
    "BUNDLES_DIR",
    "Bundle",
    "BundleError",
    "bundle_expanders",
    "bundles_dir",
    "delete_bundle",
    "expand_bundle",
    "load_bundle",
    "reload_bundles",
    "save_bundle",
    "scan_bundles",
    "slugify",
]

log = logging.getLogger(__name__)

BUNDLES_DIR = "skill-bundles"
SUFFIXES = (".yaml", ".yml")
FRAME = "[The user ran the /{slug} skill bundle: {names}. They are loaded in full below.]"
BUNDLE_INSTRUCTION = "Bundle instruction: {text}"

_NOT_SLUG = re.compile(r"[^a-z0-9._-]+")


class BundleError(ValueError):
    pass


@dataclass(frozen=True)
class Bundle:
    slug: str
    skills: tuple[str, ...]
    description: str = ""
    instruction: str = ""
    path: Path | None = None


def slugify(text: str) -> str:
    return _NOT_SLUG.sub("-", text.strip().lower()).strip("-._")


def bundles_dir(home: Path) -> Path:
    return home / BUNDLES_DIR


def _text(value: object, what: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BundleError(f"{what} must be text")
    return value.strip()


def parse_bundle(data: object, *, stem: str, path: Path | None = None) -> Bundle:
    if not isinstance(data, Mapping):
        raise BundleError("a bundle file must be a mapping with a 'skills' list")
    body = cast(Mapping[str, Any], data)
    raw_skills = body.get("skills")
    if not isinstance(raw_skills, list) or not raw_skills:
        raise BundleError("'skills' must be a non-empty list of skill names")
    skills: list[str] = []
    for item in cast(list[Any], raw_skills):
        if not isinstance(item, str) or not item.strip():
            raise BundleError("every entry of 'skills' must be a skill name")
        if item.strip() not in skills:
            skills.append(item.strip())
    slug = slugify(_text(body.get("name"), "name") or stem)
    if not slug:
        raise BundleError("the bundle name gives an empty command word")
    return Bundle(
        slug=slug,
        skills=tuple(skills),
        description=_text(body.get("description"), "description"),
        instruction=_text(body.get("instruction"), "instruction"),
        path=path,
    )


def load_bundle(path: Path) -> Bundle:
    try:
        data = yamlio.load_safe(path)
    except (OSError, yamlio.YamlError) as exc:
        raise BundleError(f"cannot read {path.name}: {exc}") from exc
    return parse_bundle(data, stem=path.stem, path=path)


_CACHE: dict[str, tuple[tuple[tuple[str, int, int], ...], dict[str, Bundle]]] = {}
_CACHE_LOCK = threading.Lock()


def _signature(folder: Path) -> tuple[tuple[str, int, int], ...]:
    rows: list[tuple[str, int, int]] = []
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return ()
    for entry in entries:
        if entry.suffix not in SUFFIXES or not entry.is_file():
            continue
        st = entry.stat()
        rows.append((entry.name, st.st_mtime_ns, st.st_size))
    return tuple(rows)


def reload_bundles() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def scan_bundles(home: Path) -> dict[str, Bundle]:
    """Every valid bundle by slug (invalid files are logged and skipped)."""
    folder = bundles_dir(home)
    sig = _signature(folder)
    key = str(folder)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None and hit[0] == sig:
            return dict(hit[1])
    found: dict[str, Bundle] = {}
    for name, _, _ in sig:
        path = folder / name
        try:
            bundle = load_bundle(path)
        except BundleError as exc:
            log.warning("skill bundle %s skipped: %s", path, exc)
            continue
        if bundle.slug in found:
            log.warning("skill bundle %s skipped: /%s is already defined", path, bundle.slug)
            continue
        found[bundle.slug] = bundle
    with _CACHE_LOCK:
        _CACHE[key] = (sig, found)
    return dict(found)


def save_bundle(
    home: Path,
    name: str,
    skills: list[str],
    *,
    description: str = "",
    instruction: str = "",
    overwrite: bool = False,
) -> Path:
    data: dict[str, Any] = {"name": name, "skills": skills}
    if description:
        data["description"] = description
    if instruction:
        data["instruction"] = instruction
    bundle = parse_bundle(data, stem=name)
    folder = bundles_dir(home)
    path = folder / f"{bundle.slug}.yaml"
    existing = scan_bundles(home).get(bundle.slug)
    if (path.exists() or existing is not None) and not overwrite:
        raise BundleError(f"bundle /{bundle.slug} already exists; use --force to replace it")
    folder.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, yamlio.dump_plain(data))
    reload_bundles()
    return path


def delete_bundle(home: Path, name: str) -> Path:
    slug = slugify(name)
    bundle = scan_bundles(home).get(slug)
    if bundle is None or bundle.path is None:
        raise BundleError(f"no bundle named {name!r}")
    bundle.path.unlink()
    reload_bundles()
    return bundle.path


def expand_bundle(
    bundle: Bundle,
    instruction: str,
    *,
    scope: Scope,
    session_id: str | None = None,
    catalog: SkillCatalog | None = None,
    runner: ShellRunner | None = None,
) -> Expansion | None:
    said = "\n".join(
        part
        for part in (
            BUNDLE_INSTRUCTION.format(text=bundle.instruction) if bundle.instruction else "",
            instruction.strip(),
        )
        if part
    )
    frame = FRAME.format(slug=bundle.slug, names=", ".join(bundle.skills))
    return expand_skills(
        list(bundle.skills),
        said,
        scope=scope,
        session_id=session_id,
        catalog=catalog,
        runner=runner,
        frame=frame,
        instruction_first=True,
    )


def bundle_expanders(
    scope: Scope, session_id: str | None = None
) -> dict[str, Callable[[str], Expansion | None]]:
    """``{slug: expander}`` for ``CommandTable.bundles``."""
    out: dict[str, Callable[[str], Expansion | None]] = {}
    for slug, bundle in scan_bundles(scope.home).items():

        def run(text: str, _bundle: Bundle = bundle) -> Expansion | None:
            return expand_bundle(_bundle, text, scope=scope, session_id=session_id)

        out[slug] = run
    return out
