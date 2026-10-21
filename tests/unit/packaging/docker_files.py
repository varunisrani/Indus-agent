"""Shared helpers for the container-image tests (P23-08..12): the repository's Docker files, the
pins script loaded by path (``scripts/`` is not a package), and the banned-identifier scan."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from tooling.checks.banned import load_patterns, scan_text

ROOT = Path(__file__).resolve().parents[3]
PINS_SCRIPT = ROOT / "scripts" / "docker" / "pins.py"
DOCKERFILE = ROOT / "docker" / "Dockerfile"
SANDBOX_DOCKERFILE = ROOT / "docker" / "sandbox" / "Dockerfile"
DOCKER_DIR = ROOT / "docker"
WORKFLOW = ROOT / ".github" / "workflows" / "docker.yml"

# Every file this slice ships outside the scanned product dirs (src, scripts, tests, ...).
DOCKER_FILES = (
    *sorted(p for p in DOCKER_DIR.rglob("*") if p.is_file()),
    ROOT / ".dockerignore",
    ROOT / ".hadolint.yaml",
    WORKFLOW,
)


def load_pins() -> ModuleType:
    """``scripts/docker/pins.py`` as a module (cached under a private name)."""
    name = "_indus_docker_pins"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, PINS_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def banned_hits(texts: dict[str, str]) -> list[str]:
    """``name:line:col: match`` for every banned reference identifier in ``texts``."""
    patterns, problems = load_patterns()
    assert not problems, problems
    out: list[str] = []
    for name, text in texts.items():
        for line, col, match, _hint in scan_text(text, patterns):
            out.append(f"{name}:{line}:{col}: {match}")
    return out


def instructions(dockerfile: str) -> list[tuple[str, str]]:
    """``(INSTRUCTION, arguments)`` per Dockerfile instruction, continuation lines joined and
    comments dropped."""
    out: list[tuple[str, str]] = []
    pending = ""
    for raw in dockerfile.splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if pending and line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        full = (pending + line).strip()
        pending = ""
        if not full:
            continue
        word, _, rest = full.partition(" ")
        out.append((word.upper(), rest.strip()))
    return out


def stages(dockerfile: str) -> dict[str, list[tuple[str, str]]]:
    """Instructions per named stage (``FROM x AS name``); unnamed stages are keyed by index."""
    out: dict[str, list[tuple[str, str]]] = {}
    current: list[tuple[str, str]] | None = None
    for word, rest in instructions(dockerfile):
        if word == "FROM":
            parts = rest.split()
            name = parts[-1] if len(parts) >= 3 and parts[-2].upper() == "AS" else str(len(out))
            current = out.setdefault(name, [])
        if current is not None:
            current.append((word, rest))
    return out
