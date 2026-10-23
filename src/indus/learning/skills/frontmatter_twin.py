"""Write or check ``shipped_frontmatter.json``, the shipped skills' parsed front matter (W50-f-2).

Every run reads the front matter of the shipped skills (the copies seeded into ``<home>/skills``),
and the pure-Python YAML loader took about 20 ms for the 15 of them. The package also ships each
shipped ``SKILL.md`` front-matter block's parsed mapping keyed by the block's sha256;
``format.split_frontmatter`` uses an entry only for exactly that text, so an edited copy, a user's
own skill or a stale twin is parsed as before. A block JSON cannot carry exactly (a YAML date,
a non-finite number, a non-text key) is listed under ``skipped`` and always parsed.

Usage: ``uv run python -m indus.learning.skills.frontmatter_twin`` rewrites the twin from the
shipped tree (``skills/`` in a checkout, ``indus/_bundled_skills`` in a wheel); ``--check`` exits 1
when it is stale (a unit test and the ``generated_twins`` guard run the same check).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from indus.foundation import yamlio
from indus.learning.skills.bundled_sync import bundled_source, discover
from indus.learning.skills.format import SHIPPED_TWIN, SKILL_FILE, block_digest, frontmatter_block

NOTE = (
    "generated from the shipped skills by `python -m indus.learning.skills.frontmatter_twin`; "
    "do not edit"
)


def plain_json(value: object) -> bool:
    """True when JSON carries ``value`` back with the same types (str keys, finite numbers)."""
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(cast(float, value))
    if type(value) is list:
        return all(plain_json(item) for item in cast(list[Any], value))
    if type(value) is dict:
        mapping = cast(dict[Any, Any], value)
        return all(type(k) is str and plain_json(v) for k, v in mapping.items())
    return False


def collect(source: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """``({relative path: {sha256, frontmatter}}, skipped relative paths)`` for ``source``."""
    skills: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []
    for package in discover(source):
        rel = package.rel.as_posix()
        text = (package.path / SKILL_FILE).read_text(encoding="utf-8")
        block, _ = frontmatter_block(text)
        if block is None or not block.strip():
            skipped.append(rel)
            continue
        loaded = yamlio.load_safe(block)
        if not isinstance(loaded, dict) or not plain_json(loaded):
            skipped.append(rel)
            continue
        skills[rel] = {"sha256": block_digest(block), "frontmatter": loaded}
    return skills, skipped


def render(source: Path) -> str:
    """The twin's text for the shipped tree at ``source``."""
    skills, skipped = collect(source)
    twin = {"note": NOTE, "skills": skills, "skipped": skipped}
    return json.dumps(twin, indent=1, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m indus.learning.skills.frontmatter_twin",
        description=(__doc__ or "").splitlines()[0],
    )
    parser.add_argument("--check", action="store_true", help="exit 1 if the twin is stale")
    args = parser.parse_args(argv)
    source = bundled_source({})  # the shipped tree itself, never INDUS_BUNDLED_SKILLS
    if not source.is_dir():
        sys.stderr.write(f"no shipped skills at {source}\n")
        return 2
    wanted = render(source)
    current = SHIPPED_TWIN.read_text(encoding="utf-8") if SHIPPED_TWIN.is_file() else None
    if args.check:
        if current == wanted:
            return 0
        sys.stdout.write(
            f"{SHIPPED_TWIN.name} is stale; run "
            "`uv run python -m indus.learning.skills.frontmatter_twin`\n"
        )
        return 1
    if current != wanted:
        SHIPPED_TWIN.write_text(wanted, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
