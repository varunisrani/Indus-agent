"""The built-in ``indus doctor`` checks, in their run order (P07-19).

python → packages → home → config → env → keychain → auth store → providers (``--live`` probes)
→ session store → terminal backend → git and rg → tools → desktop control, then the messaging
gateway (``gateway_checks``, P16-36). Each returns findings; with ``ctx.fix`` the home, config and
env checks repair what is safe and say so.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import httpx

from indus.cli.doctor import (
    DoctorContext,
    Finding,
    Severity,
    gateway_checks,  # noqa: F401 - registers the messaging gateway section (P16-36)
    register,
    remote_backends,  # registers the cloud backends check (P21-13)
)
from indus.foundation import config as config_mod
from indus.foundation import fsio
from indus.foundation.config import loader as config_loader
from indus.foundation.envfile import is_denied, parse_line
from indus.foundation.fsio import FileLock
from indus.foundation.home import dir_mode, is_managed, secure_dir
from indus.foundation.layout import SKELETON_DIRS
from indus.foundation.secrets import probe_keychain
from indus.platform.locate import which
from indus.safety.child_env import child_env
from indus.state import browse, fts
from indus.state.errors import Bucket

__all__ = ["DIST_NAME", "LIVE_TIMEOUT_KEY", "MIN_PYTHON", "SECRET_FILES", "TOOL_TIMEOUT_SECONDS"]

MIN_PYTHON = (3, 13)
DIST_NAME = "indus-agent"
SECRET_FILES = (".env", "config.yaml", "auth.json", "secrets.json", "state.db")
LIVE_TIMEOUT_KEY = "doctor.live_probe_timeout_s"
DEFAULT_LIVE_TIMEOUT = 10.0
TOOL_TIMEOUT_SECONDS = 5.0
QUARANTINE_FILE = ".env.quarantine"
_REQ_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def _posix() -> bool:
    return os.name == "posix"


# -- python and packages ------------------------------------------------------------------------


@register("python", "Python", order=10)
def check_python(ctx: DoctorContext) -> list[Finding]:
    found: list[Finding] = []
    version = ".".join(str(p) for p in sys.version_info[:3])
    if sys.version_info[:2] < MIN_PYTHON:
        found.append(
            Finding("python", "error", f"Python {version}: Indus Agent needs 3.13 or newer.")
        )
    else:
        found.append(Finding("python", "ok", f"Python {version}"))
    if fts.fts5_available():
        found.append(Finding("python.fts5", "ok", "SQLite full-text search (FTS5) available"))
    else:
        found.append(
            Finding(
                "python.fts5",
                "warn",
                "this Python's SQLite has no full-text search (FTS5); session search is slower",
                fix="use a Python build with FTS5 (the uv-managed interpreters have it)",
            )
        )
    return found


def _required_names() -> list[str] | None:
    try:
        requires = importlib.metadata.requires(DIST_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None
    names: list[str] = []
    for line in requires or []:
        if "extra ==" in line:
            continue
        found = _REQ_NAME.match(line.strip())
        if found:
            names.append(found.group(0))
    return names


@register("packages", "Required packages", order=20)
def check_packages(ctx: DoctorContext) -> list[Finding]:
    names = _required_names()
    if names is None:
        return [
            Finding("packages", "info", "package metadata not found (running from a source tree)")
        ]
    missing: list[str] = []
    for name in names:
        try:
            importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)
    if missing:
        return [
            Finding(
                "packages",
                "error",
                f"missing package(s): {', '.join(missing)}",
                fix="reinstall Indus Agent (or `uv sync` in a source checkout)",
            )
        ]
    return [Finding("packages", "ok", f"{len(names)} required package(s) installed")]


# -- home ------------------------------------------------------------------------------------------


def _loose(mode: int, allowed: int) -> bool:
    return bool(mode & 0o777 & ~allowed)


@register("home", "Home folder", order=30)
def check_home(ctx: DoctorContext) -> list[Finding]:
    scope = ctx.scope
    home = scope.home
    managed = is_managed(home, scope.env)
    found: list[Finding] = []
    if not home.is_dir():
        if ctx.fix:
            secure_dir(home, home=home, env=scope.env)
            found.append(Finding("home", "ok", f"created {home}", fixed=True))
        else:
            return [Finding("home", "error", f"{home} does not exist", fix="run with --fix")]
    missing = [name for name in SKELETON_DIRS if not (home / name).is_dir()]
    if missing and ctx.fix:
        for name in missing:
            secure_dir(home / name, home=home, env=scope.env)
        found.append(Finding("home.dirs", "ok", f"created {', '.join(missing)}", fixed=True))
    elif missing:
        found.append(
            Finding(
                "home.dirs",
                "warn",
                f"missing folder(s) in {home}: {', '.join(missing)}",
                fix="run with --fix",
            )
        )
    if managed or not _posix():
        found.append(Finding("home", "ok", f"{home} (permissions managed elsewhere)"))
        return found
    want_dir = dir_mode(scope.env)
    if _loose(home.stat().st_mode, want_dir):
        if ctx.fix:
            os.chmod(home, want_dir)
            found.append(Finding("home.mode", "ok", f"set {home} to {want_dir:o}", fixed=True))
        else:
            found.append(
                Finding(
                    "home.mode",
                    "warn",
                    f"{home} can be read by other users "
                    f"(mode {stat.S_IMODE(home.stat().st_mode):o})",
                    fix="run with --fix",
                )
            )
    for name in SECRET_FILES:
        path = home / name
        if not path.is_file() or path.is_symlink():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        if not _loose(mode, 0o600):
            continue
        if ctx.fix:
            os.chmod(path, 0o600)
            found.append(Finding("home.mode", "ok", f"set {name} to 600", fixed=True))
        else:
            found.append(
                Finding(
                    "home.mode",
                    "warn",
                    f"{name} can be read by other users (mode {mode:o})",
                    fix="run with --fix",
                )
            )
    if not any(f.problem for f in found):
        found.append(Finding("home", "ok", f"{home} is in order"))
    return found


# -- config ----------------------------------------------------------------------------------------


_CONFIG_IDS = {"version": "config.version", "unknown": "config.unknown", "env": "config.env"}
_CONFIG_SEVERITY: dict[str, Severity] = {"error": "error", "warning": "warn", "info": "info"}


@register("config", "Configuration", order=40)
def check_config(ctx: DoctorContext) -> list[Finding]:
    """The settings file: created from the commented example with --fix when missing, then
    ``indus config check``'s findings (one rule set for both commands)."""
    from indus.cli.commands.config_check import check_settings
    from indus.cli.commands.config_read import seed_config

    scope = ctx.scope
    path = config_mod.config_path(scope)
    if not path.is_file():
        if ctx.fix:
            secure_dir(path.parent, home=scope.home, env=scope.env)
            seed_config(scope)
            config_loader.invalidate(path)
            return [
                Finding("config", "ok", f"created {path} from the commented example", fixed=True)
            ]
        return [
            Finding(
                "config",
                "warn",
                f"no {path.name} yet; built-in defaults are in use",
                fix="run `indus setup`, or --fix to create a commented starter file",
            )
        ]
    view = config_mod.load(scope)
    if view.failed or view.error is not None:
        return [
            Finding(
                "config",
                "error",
                f"{path} cannot be read ({view.error}); the last good settings are in use",
                fix="fix the file (`indus config edit`) or restore one from backups/config/",
            )
        ]
    found: list[Finding] = []
    for item in check_settings(scope):
        if item.severity == "info":
            continue
        fix = (
            "run `indus config migrate`" if item.topic == "version" else "run `indus config check`"
        )
        if item.topic == "version" and item.severity == "error":
            fix = "update Indus Agent"
        found.append(
            Finding(
                _CONFIG_IDS.get(item.topic, "config.value"),
                "info" if item.topic == "unknown" else _CONFIG_SEVERITY[item.severity],
                f"config.yaml: {item.message}",
                fix=None if item.topic == "unknown" else fix,
            )
        )
    if not any(f.problem for f in found):
        found.append(Finding("config", "ok", f"{path} reads cleanly"))
    return found


# -- .env -----------------------------------------------------------------------------------------


def _env_problems(lines: list[str]) -> list[tuple[int, str]]:
    bad: list[tuple[int, str]] = []
    for number, line in enumerate(lines, 1):
        parsed = parse_line(line)
        if parsed is None:
            continue
        name = parsed[0]
        if is_denied(name):
            bad.append((number, f"line {number} sets {name}, which changes how programs start"))
        elif not line.isascii():
            bad.append(
                (number, f"line {number} ({name}) has non-ASCII characters (a pasted quote?)")
            )
    return bad


def _quarantine(path: Path, lines: list[str], numbers: set[int]) -> None:
    with FileLock(path.with_name(path.name + ".lock")):
        keep = [line for i, line in enumerate(lines, 1) if i not in numbers]
        moved = [line for i, line in enumerate(lines, 1) if i in numbers]
        target = path.with_name(QUARANTINE_FILE)
        old = target.read_text(encoding="utf-8") if target.is_file() else ""
        fsio.atomic_write_text(
            target, old + "".join(m if m.endswith("\n") else m + "\n" for m in moved)
        )
        fsio.atomic_write_text(path, "".join(keep))


@register("env", "Keys file (.env)", order=50)
def check_env(ctx: DoctorContext) -> list[Finding]:
    path = ctx.scope.home / ".env"
    if not path.is_file():
        return [Finding("env", "info", "no .env file (keys can live in the keychain instead)")]
    data = path.read_bytes()
    lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)
    bad = _env_problems(lines)
    if not bad:
        return [Finding("env", "ok", f"{path} looks clean")]
    if ctx.fix:
        _quarantine(path, lines, {n for n, _ in bad})
        return [
            Finding(
                "env",
                "ok",
                f"moved {len(bad)} line(s) from .env to {QUARANTINE_FILE}",
                fixed=True,
            )
        ]
    return [
        Finding("env", "warn", text, fix="run with --fix to move the line aside") for _, text in bad
    ]


# -- keychain and auth store ---------------------------------------------------------------------


@register("keychain", "Keychain", order=60)
def check_keychain(ctx: DoctorContext) -> list[Finding]:
    view = config_mod.load(ctx.scope)
    mode = str(view.get("security.secret_store") or "auto")
    if mode == "file":
        return [
            Finding(
                "keychain",
                "ok",
                "keys are kept in the owner-only file (security.secret_store: file)",
            )
        ]
    if probe_keychain() is not None:
        return [Finding("keychain", "ok", "OS keychain available")]
    if mode == "keychain":
        return [
            Finding(
                "keychain",
                "error",
                "security.secret_store is keychain, but no OS keychain is available",
                fix="unlock or install a keychain, or set security.secret_store to auto",
            )
        ]
    return [Finding("keychain", "info", "no OS keychain here; keys go to an owner-only file")]


@register("auth", "Sign-ins", order=70)
def check_auth(ctx: DoctorContext) -> list[Finding]:
    path = ctx.scope.home / "auth.json"
    if not path.is_file():
        return [Finding("auth", "info", "no stored sign-ins")]
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [
            Finding(
                "auth",
                "error",
                f"auth.json cannot be read ({exc})",
                fix="sign in again with `indus auth add`",
            )
        ]
    if not isinstance(data, dict):
        return [Finding("auth", "error", "auth.json is not a sign-in store", fix="move it aside")]
    return [Finding("auth", "ok", "auth.json reads cleanly")]


# -- providers ------------------------------------------------------------------------------------


def _live_timeout(ctx: DoctorContext) -> float:
    raw = config_mod.load(ctx.scope).get(LIVE_TIMEOUT_KEY)
    if isinstance(raw, int | float) and not isinstance(raw, bool) and raw > 0:
        return float(raw)
    return DEFAULT_LIVE_TIMEOUT


def _probe(ctx: DoctorContext, name: str, timeout: float) -> Finding:
    from indus.providers import model_list
    from indus.providers import route as route_mod

    try:
        found = route_mod.resolve(ctx.scope, provider=name)
    except Exception as exc:
        return Finding(f"providers.live.{name}", "warn", f"{name}: no route ({exc})")
    url = model_list.models_url(found)
    client = ctx.extras.get("http_client")
    try:
        if isinstance(client, httpx.Client):
            response = client.get(url, headers=model_list.request_headers(found), timeout=timeout)
        else:
            with httpx.Client(timeout=timeout) as own:
                response = own.get(url, headers=model_list.request_headers(found))
    except httpx.TimeoutException:
        return Finding(
            f"providers.live.{name}", "warn", f"{name}: no answer within {timeout:g}s ({url})"
        )
    except httpx.HTTPError as exc:
        return Finding(f"providers.live.{name}", "warn", f"{name}: {type(exc).__name__} ({url})")
    if response.status_code >= 400:
        return Finding(
            f"providers.live.{name}",
            "warn",
            f"{name}: HTTP {response.status_code} from {url}",
            fix="check the key with `indus auth list`"
            if response.status_code in (401, 403)
            else None,
        )
    return Finding(f"providers.live.{name}", "ok", f"{name}: reachable ({url})")


@register("providers", "Providers", order=80)
def check_providers(ctx: DoctorContext) -> list[Finding]:
    from indus.cli.commands.status import credentialed_providers, provider_state

    state = provider_state(ctx.scope)
    found: list[Finding] = []
    if state.provider is None:
        found.append(
            Finding(
                "providers",
                "warn",
                f"no provider is usable: {state.problem}",
                fix="run `indus setup`, or `indus auth add PROVIDER`",
            )
        )
    else:
        found.append(
            Finding("providers", "ok", f"{state.model} via {state.provider} ({state.source})")
        )
    if not ctx.live:
        return found
    usable = credentialed_providers(ctx.scope)
    if not usable:
        found.append(Finding("providers.live", "info", "no provider with a key to probe"))
        return found
    timeout = _live_timeout(ctx)
    found.extend(_probe(ctx, p.name, timeout) for p in usable)
    return found


# -- store ----------------------------------------------------------------------------------------


@register("store", "Session store", order=90)
def check_store(ctx: DoctorContext) -> list[Finding]:
    report = browse.health(ctx.scope)
    if report.state == "missing":
        return [Finding("store", "info", "no session store yet (made on the first saved chat)")]
    if report.state == "busy":
        return [
            Finding("store", "warn", f"the session store is busy: {report.line or report.detail}")
        ]
    if report.state in ("corrupt", "unavailable"):
        return [
            Finding(
                "store",
                "error",
                f"the session store is {report.state}: {report.line or report.detail}",
                fix="run `indus sessions repair` (or `indus sessions recover --output FILE`)",
            )
        ]
    if report.bucket is Bucket.FTS_INDEX:
        return [
            Finding(
                "store",
                "warn",
                "the session search index is out of date or damaged",
                fix="run `indus sessions repair`",
            )
        ]
    return [
        Finding(
            "store",
            "ok",
            f"session store healthy ({report.journal_mode} journal, "
            f"schema {report.schema_version})",
        )
    ]


# -- backend, external tools, tools ---------------------------------------------------------------


@register("backend", "Terminal backend", order=100)
def check_backend(ctx: DoctorContext) -> list[Finding]:
    backend = str(config_mod.load(ctx.scope).get("terminal.backend") or "local")
    if backend == "local":
        return [Finding("backend", "ok", "commands run on this machine (terminal.backend: local)")]
    if backend == "docker":
        from indus.backends.docker.probe import doctor_rows

        return [
            Finding(row.id, row.severity, row.message, fix=row.fix)
            for row in doctor_rows(ctx.scope.with_config(config_mod.load(ctx.scope)))
        ]
    if backend == "ssh":
        from indus.backends.ssh.environment import setup_findings

        scoped = ctx.scope.with_config(config_mod.load(ctx.scope))
        return [
            Finding("backend", cast(Severity, severity), message, fix=fix)
            for severity, message, fix in setup_findings(scoped)
        ]
    if backend in remote_backends.REMOTE_BACKENDS:
        return remote_backends.detail_findings(ctx.scope, backend)
    return [
        Finding(
            "backend",
            "warn",
            f"terminal.backend is {backend}, which this build cannot use yet",
            fix="set terminal.backend to local",
        )
    ]


def _version_of(path: str, env: dict[str, str]) -> str:
    try:
        done = subprocess.run(
            [path, "--version"],
            capture_output=True,
            env=env,
            text=True,
            timeout=TOOL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    first = (done.stdout or done.stderr).strip().splitlines()
    return first[0] if first else ""


@register("external", "git and ripgrep", order=110)
def check_external(ctx: DoctorContext) -> list[Finding]:
    found: list[Finding] = []
    env = child_env(ctx.scope)  # SEC-8: no provider key reaches a `--version` probe
    for name, label, why in (
        ("git", "git", "needed for repository work"),
        ("rg", "ripgrep (rg)", "file search falls back to a slower scan"),
    ):
        path = which(name)
        if path is None:
            found.append(Finding(f"external.{name}", "info", f"{label} not found ({why})"))
        else:
            version = _version_of(path, env)
            found.append(Finding(f"external.{name}", "ok", f"{label}: {version or path}"))
    return found


@register("tools", "Tools", order=120)
def check_tools(ctx: DoctorContext) -> list[Finding]:
    from indus.tools.builtin import load_builtins
    from indus.tools.registry import ToolRegistry

    registry = ToolRegistry(clock=ctx.scope.clock)
    load_builtins(registry)
    memo: dict[Any, bool] = {}
    entries = registry.entries(ctx.scope)
    off = [e.name for e in entries if not registry.available(e, ctx.scope, memo)]
    found = [
        Finding("tools", "ok", f"{len(entries) - len(off)} of {len(entries)} tool(s) available")
    ]
    if off:
        found.append(
            Finding(
                "tools.off", "info", f"not available here (missing key or extra): {', '.join(off)}"
            )
        )
    return found


@register("computer_use", "Desktop control", order=125)
def check_computer_use(ctx: DoctorContext) -> list[Finding]:
    from indus.tools.builtin.computer_use.driver import doctor_rows

    scoped = ctx.scope.with_config(config_mod.load(ctx.scope))
    return [Finding(row.id, row.severity, row.message, fix=row.fix) for row in doctor_rows(scoped)]
