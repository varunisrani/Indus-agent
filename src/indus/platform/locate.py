"""Find commands in three tiers, each allowed to do strictly more than the one before.

- ``locate``: file metadata only (stat, access). Explicit paths, then the lookup PATH, then the
  known-directory tables. Never opens, spawns or connects.
- ``inspect``: may read a bounded prefix (64 KiB) of the winning file. Never spawns.
- ``probe``: always fresh, never cached; the only tier that may open a socket (``Effort.NETWORK``)
  or spawn (``Effort.DIAGNOSTIC``), within a deadline (default 3.0 s).

The process environment is read (PATH, for the ambient search) but never modified.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from indus.platform import known_dirs
from indus.platform.facts import os_family
from indus.platform.shapes import (
    Candidate,
    CheckState,
    Effort,
    Inspection,
    LookupContext,
    Observation,
    PathSource,
    Probe,
    Resolution,
    failed,
    missing,
    seen,
    unchecked,
)

INSPECT_LIMIT = 64 * 1024
DEFAULT_DEADLINE = 3.0
DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"

Connector = Callable[[str, int, float], bool]
Runner = Callable[[Sequence[str], float], bool]

# What a ``--version`` style probe may see of the process environment (SEC-8): enough to start a
# program and pick its locale, never a key exported in the launching shell. This tier sits below
# foundation, so there is no scope to build a child env from.
PROBE_ENV_NAMES: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "SYSTEMROOT",
    "PATHEXT",
    "TEMP",
    "TMP",
)


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _has_separator(name: str) -> bool:
    return "/" in name or "\\" in name or os.sep in name


def _search_dirs(ctx: LookupContext, env: Mapping[str, str]) -> list[str]:
    raw = env.get("PATH", os.defpath) if isinstance(ctx.path, PathSource) else ctx.path
    return [d for d in raw.split(os.pathsep) if d]


def _suffixes(ctx: LookupContext, family: str) -> list[str]:
    if family != "win32":
        return [""]
    exts = (ctx.pathext if ctx.pathext is not None else DEFAULT_PATHEXT).split(";")
    return ["", *[e for e in exts if e]]


def _first_executable(base: Path, suffixes: list[str]) -> Path | None:
    for suffix in suffixes:
        candidate = base.with_name(base.name + suffix) if suffix else base
        if _is_executable_file(candidate):
            return candidate
    return None


def which(name: str, path: str | None = None) -> str | None:
    """Plain PATH lookup for callers outside this package (the lookup ratchet keeps it here)."""
    return shutil.which(name, path=path)


def locate(
    name: str,
    ctx: LookupContext | None = None,
    *,
    known: Sequence[str] | None = None,
    extra_dirs: Sequence[Path] = (),
    env: Mapping[str, str] | None = None,
    user_home: Callable[[], Path] | None = None,
    family: str | None = None,
) -> Resolution:
    """Find ``name``; every candidate is kept in probe order, the first present one wins.

    ``known`` defaults to ``known_dirs.command_dirs()``; ``extra_dirs`` (for example the vendored
    ``<home>/bin``) are tried after them.
    """
    context = ctx or LookupContext()
    values = os.environ if env is None else env
    home_fn = user_home if user_home is not None else Path.home
    fam = family if family is not None else os_family()
    text = name.strip()

    if _has_separator(text):
        path = Path(str(home_fn()) + text[1:]) if text.startswith("~/") else Path(text)
        # A relative explicit path would resolve against whatever the cwd is: never trusted.
        present = path.is_absolute() and _is_executable_file(path)
        kind = "explicit_path" if present else "missing"
        return Resolution(kind, (Candidate(str(path), "explicit", present),))

    suffixes = _suffixes(context, fam)
    candidates: list[Candidate] = []
    hit: Path | None = None
    for directory in _search_dirs(context, values):
        hit = _first_executable(Path(directory) / text, suffixes)
        if hit is not None:
            break
    candidates.append(Candidate(str(hit) if hit else text, "PATH", hit is not None))

    entries = list(known) if known is not None else list(known_dirs.command_dirs(fam))
    for entry in entries:
        expanded = known_dirs.expand(entry, values, home_fn)
        if expanded is None:
            continue
        found = _first_executable(expanded / text, suffixes)
        candidates.append(
            Candidate(str(found or expanded / text), f"known_dir:{entry}", found is not None)
        )
    for directory in extra_dirs:
        found = _first_executable(directory / text, suffixes)
        candidates.append(
            Candidate(str(found or directory / text), f"dir:{directory}", found is not None)
        )

    first = next((c for c in candidates if c.present), None)
    if first is None:
        return Resolution("missing", tuple(candidates))
    kind = "path_executable" if first.source == "PATH" else "known_path"
    return Resolution(kind, tuple(candidates))


def read_bounded(path: Path, limit: int = INSPECT_LIMIT) -> bytes:
    with path.open("rb") as handle:
        return handle.read(min(limit, INSPECT_LIMIT))


def inspect(res: Resolution) -> Inspection:
    """Size and ``#!`` interpreter of the winning file, from at most 64 KiB."""
    target = res.path
    if target is None:
        return Inspection(missing("not found"), missing())
    path = Path(target)
    try:
        size = path.stat().st_size
        head = read_bounded(path, 512)
    except OSError as exc:
        return Inspection(failed(str(exc)), failed(str(exc)))
    interpreter: Observation[str]
    if head.startswith(b"#!"):
        line = head[2:].split(b"\n", 1)[0].decode(errors="replace").strip()
        interpreter = seen(line)
    else:
        interpreter = missing("no #! line")
    return Inspection(interpreter, seen(size))


def _tcp_connect(host: str, port: int, deadline: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=deadline):
            return True
    except OSError:
        return False


def probe_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a probe runs with: only :data:`PROBE_ENV_NAMES` from ``source``."""
    values = os.environ if source is None else source
    return {name: values[name] for name in PROBE_ENV_NAMES if name in values}


def _run_quietly(argv: Sequence[str], deadline: float) -> bool:
    try:
        done = subprocess.run(
            list(argv),
            env=probe_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=deadline,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def probe(
    res: Resolution,
    *,
    effort: Effort,
    endpoint: tuple[str, int] | None = None,
    check_args: Sequence[str] = ("--version",),
    deadline: float = DEFAULT_DEADLINE,
    connector: Connector | None = None,
    runner: Runner | None = None,
) -> Probe:
    """Live check, never cached. ``LOCAL`` touches neither ``connector`` nor ``runner``."""
    endpoint_obs: Observation[str] = (
        seen(f"{endpoint[0]}:{endpoint[1]}") if endpoint else missing("no endpoint")
    )
    running: Observation[bool] = unchecked()
    answering: Observation[bool] = unchecked()
    if effort is Effort.LOCAL:
        return Probe(running, answering, endpoint_obs)

    if endpoint is not None:
        connect = connector if connector is not None else _tcp_connect
        ok = connect(endpoint[0], endpoint[1], deadline)
        answering = Observation(CheckState.PRESENT if ok else CheckState.ABSENT, ok)

    if effort is Effort.DIAGNOSTIC and res.path is not None:
        run = runner if runner is not None else _run_quietly
        ok = run([res.path, *check_args], deadline)
        running = Observation(CheckState.PRESENT if ok else CheckState.ABSENT, ok)
    return Probe(running, answering, endpoint_obs)
