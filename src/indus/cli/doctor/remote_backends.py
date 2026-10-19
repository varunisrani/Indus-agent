"""``indus doctor`` rows for the cloud and container backends (P21-13).

One summary line per backend (``singularity``, ``modal``, ``daytona``, ``vercel_sandbox``) says
whether it can run here, through the same guarded probe the terminal uses. For the backend the
profile selected, ``check_backend`` adds the detail rows from ``detail_findings``: the CLI or the
SDK extra, credentials (names only, never values), the backend's own settings refusals, a writable
scratch or cache dir, and for Singularity how full its scratch dir is.

No SDK is imported (package metadata only) and nothing touches the network.
"""

from __future__ import annotations

import os
from pathlib import Path

from indus.cli.doctor import DoctorContext, Finding, register
from indus.foundation import config as config_mod
from indus.foundation.scope import Scope

__all__ = ["REMOTE_BACKENDS", "check_remote_backends", "detail_findings", "writable_problem"]

REMOTE_BACKENDS = ("singularity", "modal", "daytona", "vercel_sandbox")
SETUP_FIX = "run `indus setup terminal` (or store the names with `indus auth`)"
GIB = 1024**3


def _scoped(scope: Scope) -> Scope:
    return scope.with_config(config_mod.load(scope))


def _selected(scope: Scope) -> str:
    return str(config_mod.load(scope).get("terminal.backend") or "local").strip().lower()


def _probe_reason(scope: Scope, backend: str) -> str | None:
    from indus.backends.registry import default_entries

    for entry in default_entries():
        if entry.name == backend:
            return entry.probe(scope) if entry.probe is not None else None
    return f"no {backend} backend is registered"


def writable_problem(path: Path) -> str | None:
    """Why ``path`` (a directory, created on first use) cannot be written, or None."""
    if path.exists():
        if not path.is_dir():
            return f"{path} is not a directory"
        return None if os.access(path, os.W_OK | os.X_OK) else f"{path} is not writable"
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if os.access(parent, os.W_OK | os.X_OK):
        return None
    return f"{path} does not exist and {parent} is not writable, so it cannot be created"


# -- detail rows ----------------------------------------------------------------------------------


def _scratch_row(backend: str, path: Path, what: str) -> Finding:
    problem = writable_problem(path)
    if problem is None:
        return Finding(f"backend.{backend}.scratch", "ok", f"{what}: {path}")
    return Finding(
        f"backend.{backend}.scratch",
        "error",
        problem,
        fix=f"make {path} writable or point terminal.scratch_dir somewhere writable",
    )


def _singularity_rows(scope: Scope) -> list[Finding]:
    from indus.backends.singularity import NOT_FOUND_REASON, dir_usage, find_cli, settings_for

    rows: list[Finding] = []
    cli = find_cli(scope)
    if cli is None:
        rows.append(
            Finding(
                "backend.singularity",
                "error",
                f"terminal.backend is singularity but {NOT_FOUND_REASON} on PATH",
                fix="install Apptainer (1.2 or later) or SingularityCE (4.0 or later)",
            )
        )
    else:
        rows.append(Finding("backend.singularity", "ok", f"container CLI: {cli}"))
    settings = settings_for(scope)
    rows.append(_scratch_row("singularity", settings.scratch_dir, "scratch dir"))
    limit = settings.disk_warning_gb
    used = dir_usage(settings.scratch_dir) if settings.scratch_dir.is_dir() else 0
    if limit > 0 and used > limit * GIB:
        rows.append(
            Finding(
                "backend.singularity.disk",
                "warn",
                f"the scratch dir uses {used / GIB:.1f} GB, above the {limit:g} GB warning level",
                fix="remove old overlays or SIF files there, or raise terminal.disk_warning_gb",
            )
        )
    else:
        rows.append(
            Finding(
                "backend.singularity.disk",
                "ok",
                f"the scratch dir uses {used / GIB:.1f} GB (warning above {limit:g} GB)",
            )
        )
    return rows


def _settings_reason(scope: Scope, backend: str) -> str | None:
    if backend == "modal":
        from indus.backends.modal.lifecycle import requirement_reason as modal_reason

        return modal_reason(scope)
    if backend == "daytona":
        from indus.backends.daytona import requirement_reason as daytona_reason

        return daytona_reason(scope)
    from indus.backends.vercel.lifecycle import requirement_reason as vercel_reason

    return vercel_reason(scope)


def _sdk_rows(scope: Scope, backend: str) -> list[Finding]:
    from indus.backends.sdk_entries import extra_missing
    from indus.backends.sync_back import temp_root
    from indus.backends.vendor_env import vendor_credentials

    rows: list[Finding] = []
    missing_extra = extra_missing(backend)
    if missing_extra is not None:
        rows.append(
            Finding(
                f"backend.{backend}",
                "error",
                missing_extra,
                fix="install the extra named above, then run `indus doctor` again",
            )
        )
    else:
        rows.append(Finding(f"backend.{backend}", "ok", f"the {backend} SDK is installed"))
    found = vendor_credentials(scope, backend)
    reason = found.reason(backend)
    if reason is None:
        names = ", ".join(sorted(found.values))
        rows.append(
            Finding(
                f"backend.{backend}.credentials",
                "ok",
                f"credentials present: {names} (values not shown)",
            )
        )
        settings = _settings_reason(scope, backend)
        if settings is not None:
            rows.append(
                Finding(
                    f"backend.{backend}.settings",
                    "error",
                    settings,
                    fix="fix the terminal settings in config.yaml (`indus config`)",
                )
            )
    else:
        rows.append(Finding(f"backend.{backend}.credentials", "error", reason, fix=SETUP_FIX))
    rows.append(_scratch_row(backend, temp_root(scope.home), "sync-back cache dir"))
    return rows


def detail_findings(scope: Scope, backend: str) -> list[Finding]:
    """The detail rows for the selected ``backend`` (one of ``REMOTE_BACKENDS``)."""
    scoped = _scoped(scope)
    if backend == "singularity":
        return _singularity_rows(scoped)
    return _sdk_rows(scoped, backend)


# -- the summary check ----------------------------------------------------------------------------


@register("remote_backends", "Cloud and container backends", order=105)
def check_remote_backends(ctx: DoctorContext) -> list[Finding]:
    scope = _scoped(ctx.scope)
    selected = _selected(ctx.scope)
    rows: list[Finding] = []
    for backend in REMOTE_BACKENDS:
        reason = _probe_reason(scope, backend)
        mark = " (selected)" if backend == selected else ""
        if reason is None:
            rows.append(Finding(f"backend.{backend}.ready", "ok", f"{backend}{mark}: can run here"))
            continue
        if backend == selected:
            message = f"{backend}{mark}: cannot run here: {reason} (details under Terminal backend)"
        else:
            message = f"{backend}: not available: {reason}"
        rows.append(Finding(f"backend.{backend}.ready", "info", message))
    return rows
