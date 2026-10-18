"""No raw environ: product code reads the process environment only where it is allowed (R02.22).

Child processes get their environment from ``indus.safety.child_env`` and everything else reads
the scope's env view. This check parses every ``src/**/*.py`` file and reports:

- ``os.environ`` / ``os.environb`` / ``os.getenv`` / ``os.putenv`` / ``os.unsetenv`` through any
  alias of the ``os`` module;
- ``from os import environ`` (and the other names above).

Files that own the process environment (bootstrap, the one ``EnvView`` snapshot, pre-scope
platform probes) are listed in ``no_raw_environ_allow.json`` with a reason. An entry without a
reason is a violation; an entry whose file no longer exists or no longer reads the environment is
a warning so the list stays honest.
"""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from tooling.checks.common import CHECKS_DIR, Context, Result, read_text, rel, walk_files

NAME = "no_raw_environ"
ALLOW_FILE = CHECKS_DIR / "no_raw_environ_allow.json"
SCAN_DIR = "src"
ENV_NAMES = frozenset({"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"})


@dataclass(frozen=True)
class Hit:
    line: int
    what: str


def _os_aliases(tree: ast.AST) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                if name.name == "os":
                    aliases.add(name.asname or "os")
    return aliases


def find_hits(source: str) -> list[Hit]:
    """Raw environment reads in ``source`` (raises SyntaxError for unparsable text)."""
    tree = ast.parse(source)
    aliases = _os_aliases(tree) or {"os"}
    hits: list[Hit] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os" and node.level == 0:
            for name in node.names:
                if name.name in ENV_NAMES:
                    hits.append(Hit(node.lineno, f"from os import {name.name}"))
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in ENV_NAMES
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
        ):
            hits.append(Hit(node.lineno, f"{node.value.id}.{node.attr}"))
    return sorted(hits, key=lambda h: (h.line, h.what))


def load_allow(path: Path = ALLOW_FILE) -> tuple[dict[str, str], list[str]]:
    """Return ({relative path: reason}, problems)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"{path.name}: cannot read: {exc}"]
    allow = data.get("allow") if isinstance(data, dict) else None
    if not isinstance(allow, dict):
        return {}, [f"{path.name}: needs an 'allow' object"]
    out: dict[str, str] = {}
    problems: list[str] = []
    for key, reason in allow.items():
        if not isinstance(reason, str) or not reason.strip():
            problems.append(f"{path.name}: {key} needs a non-empty reason")
            continue
        out[str(key)] = reason
    return out, problems


def run(ctx: Context, allow_file: Path = ALLOW_FILE) -> Result:
    result = Result(NAME)
    base = ctx.root / SCAN_DIR
    if not base.is_dir():
        result.notes.append(f"no {SCAN_DIR}/ under the root; nothing to check")
        return result
    allow, problems = load_allow(allow_file)
    for problem in problems:
        result.fail(problem)
    used: set[str] = set()
    for path in walk_files(base):
        if path.suffix != ".py":
            continue
        name = rel(path, ctx.root)
        text = read_text(path)
        if text is None:
            continue
        try:
            hits = find_hits(text)
        except SyntaxError as exc:
            result.fail(f"{name}: cannot parse ({exc.msg}, line {exc.lineno})")
            continue
        if not hits:
            continue
        if name in allow:
            used.add(name)
            continue
        for hit in hits:
            result.fail(
                f"{name}:{hit.line}: {hit.what} — build child environments with "
                "indus.safety.child_env and read the scope's env view elsewhere"
            )
    for name in sorted(set(allow) - used):
        if (ctx.root / name).is_file():
            result.warnings.append(f"{ALLOW_FILE.name}: {name} no longer reads the environment")
        else:
            result.warnings.append(f"{ALLOW_FILE.name}: {name} does not exist")
    result.data["allowed"] = sorted(used)
    return result


if __name__ == "__main__":
    from tooling.checks.__main__ import main

    raise SystemExit(main([NAME, *sys.argv[1:]]))
