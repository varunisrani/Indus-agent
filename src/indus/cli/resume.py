"""What resuming a session puts back, and the per-terminal breadcrumb.

On resume the CLI moves into the session's recorded directory (unless ``--no-restore-cwd`` or
``--in``; a directory that is gone gives a warning and the CLI stays put) and re-arms the
session's own switches: auto-approve from the row's ``yolo`` flag; toolsets from the launch record
(``session_select``); reasoning from ``model_config["reasoning_effort"]`` (``/reasoning``), else
the launch record; model and provider from ``model_config["model_override"]`` (the last ``/model``
switch, ``{provider, model}``), else the launch record. ``resume_session`` does all of it in one
call for ``indus -c`` and for the REPL's ``/resume`` and ``/branch``.

Breadcrumb: every new or resumed session is written to ``<home>/terminal-sessions/<terminal>``
(JSON: session id, cwd, time), so a bare ``indus -c`` in the same terminal continues the same
conversation while another terminal continues its own. The terminal is ``INDUS_TERMINAL_ID`` when
set, else the TTY device of stdin or stdout, else a terminal multiplexer's pane variable; with
none of them nothing is written or read. ``session.terminal_continue: false`` turns the feature
off. Breadcrumbs older than ``STALE_AFTER_S`` are ignored and pruned. Nothing here raises.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from indus.foundation import fsio
from indus.foundation.home import secure_dir
from indus.foundation.scope import Scope

__all__ = [
    "BREADCRUMB_DIR",
    "STALE_AFTER_S",
    "TERMINAL_ENV",
    "MODEL_OVERRIDE_KEY",
    "REASONING_KEY",
    "CwdOutcome",
    "Resumed",
    "apply_switches",
    "breadcrumbs_enabled",
    "read_breadcrumb",
    "rearm_auto_approve",
    "restore_cwd",
    "resume_session",
    "stored_model_override",
    "stored_reasoning",
    "terminal_id",
    "write_breadcrumb",
]

log = logging.getLogger(__name__)

BREADCRUMB_DIR = "terminal-sessions"
TERMINAL_ENV = "INDUS_TERMINAL_ID"
STALE_AFTER_S = 30 * 24 * 3600.0
_PANE_VARS = (
    "TMUX_PANE",
    "ZELLIJ_PANE_ID",
    "KITTY_WINDOW_ID",
    "WEZTERM_PANE",
    "TERM_SESSION_ID",
    "WT_SESSION",
)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

type TtyName = Callable[[int], str]


def _clean(raw: str) -> str:
    return _UNSAFE.sub("-", raw.strip().strip("/"))[:120].strip("-")


def _tty_of(fd_source: object, ttyname: TtyName) -> str | None:
    fileno = getattr(fd_source, "fileno", None)
    if not callable(fileno):
        return None
    try:
        fd = fileno()
        if not isinstance(fd, int) or not os.isatty(fd):
            return None
        return ttyname(fd)
    except (OSError, ValueError, AttributeError):
        return None


def terminal_id(env: Mapping[str, str], *, ttyname: TtyName | None = None) -> str | None:
    """This terminal's identity (see the module notes), safe as a file name."""
    pinned = env.get(TERMINAL_ENV, "").strip()
    if pinned:
        word = _clean(pinned)
        return f"id-{word}" if word else None
    namer: TtyName = ttyname if ttyname is not None else os.ttyname
    for stream in (sys.stdin, sys.stdout):
        name = _tty_of(stream, namer)
        if name:
            word = _clean(name)
            if word:
                return f"tty-{word}"
    for var in _PANE_VARS:
        value = env.get(var, "").strip()
        if value:
            word = _clean(value)
            if word:
                return f"{var.lower()}-{word}"
    return None


def breadcrumbs_enabled(scope: Scope) -> bool:
    config = scope.config
    if config is None:
        return True
    try:
        value = config.get("session.terminal_continue")
    except Exception:
        return True
    return value is not False


def _dir(scope: Scope) -> Path:
    return scope.home / BREADCRUMB_DIR


def _now(scope: Scope) -> float:
    return scope.clock.now().timestamp()


def write_breadcrumb(
    scope: Scope, session_id: str, cwd: Path | str | None = None, *, ttyname: TtyName | None = None
) -> Path | None:
    """Record ``session_id`` as this terminal's session; returns the file, or None if skipped."""
    try:
        if not session_id or not breadcrumbs_enabled(scope):
            return None
        ident = terminal_id(scope.env, ttyname=ttyname)
        if ident is None:
            return None
        folder = secure_dir(_dir(scope), home=scope.home, env=scope.env)
        now = _now(scope)
        where = str(cwd) if cwd is not None else os.getcwd()
        path = fsio.atomic_write_json(
            folder / ident, {"session_id": session_id, "cwd": where, "at": now}, mode=0o600
        )
        _prune(folder, now)
        return path
    except Exception:
        log.debug("could not write the terminal breadcrumb", exc_info=True)
        return None


def _prune(folder: Path, now: float) -> None:
    try:
        entries = list(folder.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_file() and now - entry.stat().st_mtime > STALE_AFTER_S:
                entry.unlink()
        except OSError:
            continue


def read_breadcrumb(scope: Scope, *, ttyname: TtyName | None = None) -> str | None:
    """This terminal's recorded session id, or None (off, no terminal, missing, broken, stale)."""
    try:
        if not breadcrumbs_enabled(scope):
            return None
        ident = terminal_id(scope.env, ttyname=ttyname)
        if ident is None:
            return None
        data: Any = json.loads((_dir(scope) / ident).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        record: dict[str, Any] = data  # pyright: ignore[reportUnknownVariableType]
        session_id = record.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return None
        at = record.get("at")
        if isinstance(at, int | float) and _now(scope) - float(at) > STALE_AFTER_S:
            return None
        return session_id.strip()
    except (OSError, ValueError):
        return None
    except Exception:
        log.debug("could not read the terminal breadcrumb", exc_info=True)
        return None


@dataclass(frozen=True)
class CwdOutcome:
    """What ``restore_cwd`` did: ``moved`` into ``path``, ``stayed`` (gone or failed, with a
    ``message``), or ``kept`` (nothing to do)."""

    action: str
    path: str | None = None
    message: str | None = None


def restore_cwd(recorded: str | None, *, skip: bool) -> CwdOutcome:
    """Move into a resumed session's recorded directory (see the module notes)."""
    if skip or not recorded:
        return CwdOutcome("kept")
    target = os.path.expanduser(recorded)
    try:
        current = os.getcwd()
    except OSError:
        current = None
    if current is not None and os.path.realpath(target) == os.path.realpath(current):
        return CwdOutcome("kept", target)
    if not os.path.isdir(target):
        return CwdOutcome(
            "stayed",
            target,
            f"The session's folder {target} is gone; staying in {current or '.'}.",
        )
    try:
        os.chdir(target)
    except OSError as exc:
        return CwdOutcome(
            "stayed", target, f"Could not enter the session's folder {target}: {exc.strerror}."
        )
    return CwdOutcome("moved", target, f"Back in the session's folder: {target}")


def rearm_auto_approve(scope: Scope, session_id: str, row: Mapping[str, Any] | None) -> bool:
    """Re-arm the session's stored auto-approve switch; True when this call turned it on."""
    from indus.safety.approvals.decision import APPROVALS, session_key
    from indus.state import sessions

    return APPROVALS.restore_auto_approve(
        session_key(scope, session_id), sessions.yolo_enabled(row)
    )


# -- one call for every resume path ----------------------------------------------------------------

REASONING_KEY = "reasoning_effort"
"""``model_config`` key ``/reasoning`` writes; it beats the launch record's reasoning."""


@dataclass(frozen=True)
class Resumed:
    """What ``resume_session`` restored. ``policy`` is the session's launch record with the later
    in-session switches applied (``reasoning``); ``history`` is the stored transcript when a host
    was given (for the recap); ``notices`` are lines for the user."""

    session_id: str
    row: Mapping[str, Any] | None
    policy: Any
    reasoning: str | None
    toolsets: tuple[str, ...] | None
    auto_approve: bool
    cwd: CwdOutcome
    history: list[Any]
    notices: tuple[str, ...]


def stored_reasoning(row: Mapping[str, Any] | None) -> str | None:
    """The session's reasoning switch from ``model_config`` (``/reasoning``), if any."""
    if row is None:
        return None
    from indus.state import sessions

    value = sessions.parse_model_config(row.get("model_config")).get(REASONING_KEY)
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


MODEL_OVERRIDE_KEY = "model_override"
"""``model_config`` key ``/model`` writes (``{provider, model}``); it beats the launch record."""


def stored_model_override(row: Mapping[str, Any] | None) -> tuple[str | None, str] | None:
    """``(provider, model)`` of the session's last ``/model`` switch, if any."""
    if row is None:
        return None
    from indus.state import sessions

    value = sessions.parse_model_config(row.get("model_config")).get(MODEL_OVERRIDE_KEY)
    if not isinstance(value, Mapping):
        return None
    record = cast(Mapping[str, Any], value)
    model = record.get("model")
    provider = record.get("provider")
    if not isinstance(model, str) or not model.strip():
        return None
    chosen = provider.strip() if isinstance(provider, str) and provider.strip() else None
    return chosen, model.strip()


def apply_switches(policy: Any, row: Mapping[str, Any] | None) -> Any:
    """``policy`` (a launch record) with the session's later ``/reasoning`` and ``/model``
    switches laid over it."""
    if policy is None:
        return None
    from dataclasses import replace

    reasoning = stored_reasoning(row)
    if reasoning is not None and reasoning != policy.reasoning:
        policy = replace(policy, reasoning=reasoning)
    override = stored_model_override(row)
    if override is not None:
        provider, model = override
        policy = replace(policy, model=model, provider=provider or policy.provider)
    return policy


def _handle_of(store: Any, host: Any) -> Any:
    if store is not None:
        return store
    service_store = host.service.store
    handle = getattr(service_store, "handle", None)
    if handle is None:
        raise ValueError("the host's session store has no state store handle")
    return handle


def resume_session(
    scope: Scope,
    session_id: str,
    *,
    store: Any = None,
    host: Any = None,
    restore: bool = True,
    breadcrumb: bool = True,
) -> Resumed:
    """Everything a resume puts back, for ``indus -c`` and the REPL's ``/resume`` alike.

    Give the store handle (``store``) or the in-process host (``host``; its service's state
    store is used, and the transcript is loaded through it for the recap). ``restore`` False
    skips the folder change (``--no-restore-cwd``, ``--in``). Re-arms the stored auto-approve
    switch, reads the launch record and the later ``/reasoning`` switch, writes this terminal's
    breadcrumb. Never raises for a missing record or folder; an unknown session gives
    ``row=None``.
    """
    from indus.cli.session_select import replace_cwd, stored_launch
    from indus.state import sessions

    handle = _handle_of(store, host)
    row = sessions.get(handle, session_id)
    policy = apply_switches(stored_launch(row), row)
    reasoning = stored_reasoning(row) or (policy.reasoning if policy is not None else None)
    recorded = policy.cwd if policy is not None and policy.cwd else (row or {}).get("cwd")
    outcome = restore_cwd(recorded if isinstance(recorded, str) else None, skip=not restore)
    here = Path.cwd()
    if policy is not None:
        policy = replace_cwd(policy, here)
    armed = rearm_auto_approve(scope, session_id, row)
    history: list[Any] = []
    if host is not None:
        try:
            history = list(host.call(host.service.store.load(session_id)))
        except Exception:
            log.warning("could not load the history of %s for the recap", session_id, exc_info=True)
    if breadcrumb:
        write_breadcrumb(scope, session_id, here)
    notices: list[str] = []
    if outcome.message:
        notices.append(outcome.message)
    if armed:
        notices.append("Auto-approve is on for this session again.")
    return Resumed(
        session_id=session_id,
        row=row,
        policy=policy,
        reasoning=reasoning,
        toolsets=policy.toolsets if policy is not None else None,
        auto_approve=sessions.yolo_enabled(row),
        cwd=outcome,
        history=history,
        notices=tuple(notices),
    )
