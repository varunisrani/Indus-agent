"""Shared types and helpers for the guard checks."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OK = 0
VIOLATION = 1
SETUP = 2

CHECKS_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT = CHECKS_DIR.parent.parent
REFERENCE_FILE = CHECKS_DIR / "reference.json"


def _default_ref() -> Path | None:
    try:
        data = json.loads(REFERENCE_FILE.read_text(encoding="utf-8"))
        return Path(data["default_path"]).expanduser()
    except (OSError, ValueError, KeyError, TypeError):
        return None


DEFAULT_REF = _default_ref()

# Folders never walked on either side.
SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "dist",
        "build",
        "vendor",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".turbo",
        ".next",
    }
)


@dataclass
class Context:
    root: Path = DEFAULT_ROOT
    ref: Path | None = None
    require_ref: bool = False
    as_json: bool = False
    closest: int = 0
    strict: bool = False
    body_file: str | None = None
    write_skeleton: bool = False
    report: bool = False
    artifacts: tuple[str, ...] = ()


@dataclass
class Result:
    name: str
    code: int = OK
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0

    def fail(self, message: str) -> None:
        self.violations.append(message)
        self.code = max(self.code, VIOLATION)

    def setup(self, message: str) -> None:
        self.violations.append(message)
        self.code = SETUP

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "code": self.code,
            "violations": self.violations,
            "warnings": self.warnings,
            "notes": self.notes,
            "data": self.data,
            "seconds": round(self.seconds, 3),
        }


def resolve_ref(explicit: str | Path | None) -> Path | None:
    """Pick the reference checkout: explicit flag, then INDUS_REF, then the default path."""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("INDUS_REF")
    if env:
        return Path(env).expanduser()
    if DEFAULT_REF is not None and DEFAULT_REF.is_dir():
        return DEFAULT_REF
    return None


def missing_ref(ctx: Context, result: Result) -> bool:
    """Handle an absent reference. Returns True when the caller should stop."""
    if ctx.ref is not None and ctx.ref.is_dir():
        return False
    where = str(ctx.ref) if ctx.ref is not None else "(none: set --ref or INDUS_REF)"
    if ctx.require_ref:
        result.setup(f"reference checkout not found: {where}")
    else:
        result.warnings.append(f"reference not found ({where}); check skipped")
    return True


def ref_commit(ref: Path) -> str:
    """The reference's git commit, or a fingerprint of its file listing when it is not a repo."""
    try:
        out = subprocess.run(
            ["git", "-C", str(ref), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()[:12]
    except (OSError, subprocess.SubprocessError):
        pass
    import hashlib

    h = hashlib.blake2b(digest_size=6)
    for path in sorted(walk_files(ref)):
        st = path.stat()
        h.update(f"{path.relative_to(ref)}:{st.st_size}:{int(st.st_mtime)}\n".encode())
    return "tree-" + h.hexdigest()


def walk_files(base: Path, skip_dirs: frozenset[str] = SKIP_DIRS) -> Iterator[Path]:
    """All regular files under base, skipping vendored and generated folders."""
    if not base.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
        for name in sorted(filenames):
            path = Path(dirpath, name)
            if path.is_file() and not path.is_symlink():
                yield path


def is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return b"\0" in fh.read(8192)
    except OSError:
        return True


def read_text(path: Path) -> str | None:
    """Read a text file; None for binary or unreadable files."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def rel(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()
