"""Which session a chat opens, and the launch policy frozen into a session when it is created.

Selection (research 04 §3.4.3):

- ``--in DIR``: ``~`` expanded; the directory must exist (else exit 1); the process moves there
  and the recorded-cwd restore is skipped.
- ``-r VALUE`` / ``-c NAME``: an exact id, else a unique id prefix, else a title (the newest
  ``NAME #n`` before the bare ``NAME``); the hit is followed along its compression chain to the
  live tip. A miss is "No session found matching 'NAME'." on stderr and exit 1.
- bare ``-c`` / ``--resume latest``: this terminal's breadcrumb when it names a session that still
  exists (``session.terminal_continue``), else the newest CLI session of this workspace (git root,
  else cwd), else exit 1. ``--resume latest`` is checked before any title is looked up; a
  conversation whose title is literally "latest" is opened with ``-c latest`` or by its id.

Launch policy (R04.1, kept in-process): the working directory, route, reasoning, toolsets, turn
cap, skills and the safety switches are stored in the new session's ``model_config["launch"]``.
A resume may repeat a stored value; a flag given with a different value is refused (exit 2).
Flags not given on resume take the stored values.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from indus.cli.args import BARE_CONTINUE, LATEST, ChatArgs, GlobalArgs, split_csv
from indus.foundation.errors import StoreError
from indus.state import lineage, listing, sessions, titles
from indus.state.registry import StoreHandle

__all__ = [
    "BARE_CONTINUE",
    "CLI_SOURCES",
    "LATEST",
    "LAUNCH_KEY",
    "LaunchPolicy",
    "SelectionError",
    "check_frozen",
    "enter_in_dir",
    "find_named",
    "latest_for_workspace",
    "launch_from_args",
    "replace_cwd",
    "select_session",
    "stored_launch",
    "workspace_key",
]

log = logging.getLogger(__name__)

LAUNCH_KEY = "launch"
CLI_SOURCES: tuple[str, ...] = ("cli", "oneshot")
"""Sources counted as this surface's history for ``-c`` and ``--resume latest``."""

_LIST_HINT = "Run `indus sessions list` to see your sessions, or start a new one with `indus`."


class SelectionError(Exception):
    """A selection problem the user must fix: printed as is, then ``code`` is the exit code."""

    def __init__(self, message: str, *, code: int = 1, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint

    def lines(self) -> list[str]:
        return [self.message, *([self.hint] if self.hint else [])]


# -- launch policy ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class LaunchPolicy:
    """What a session was started with. ``None`` / empty means "the configured default"."""

    cwd: str | None = None
    model: str | None = None
    provider: str | None = None
    reasoning: str | None = None
    toolsets: tuple[str, ...] | None = None
    max_turns: int | None = None
    skills: tuple[str, ...] = ()
    yolo: bool = False
    pass_session_id: bool = False
    ignore_rules: bool = False
    ignore_user_config: bool = False
    safe_mode: bool = False

    def record(self) -> dict[str, Any]:
        out = asdict(self)
        out["toolsets"] = list(self.toolsets) if self.toolsets is not None else None
        out["skills"] = list(self.skills)
        return out

    @classmethod
    def from_record(cls, data: Mapping[str, Any]) -> LaunchPolicy:
        def text(name: str) -> str | None:
            value = data.get(name)
            return value if isinstance(value, str) and value else None

        toolsets = data.get("toolsets")
        skills = data.get("skills")
        turns = data.get("max_turns")
        return cls(
            cwd=text("cwd"),
            model=text("model"),
            provider=text("provider"),
            reasoning=text("reasoning"),
            toolsets=tuple(str(t) for t in toolsets) if isinstance(toolsets, list) else None,
            max_turns=turns if isinstance(turns, int) and not isinstance(turns, bool) else None,
            skills=tuple(str(s) for s in skills) if isinstance(skills, list) else (),
            yolo=data.get("yolo") is True,
            pass_session_id=data.get("pass_session_id") is True,
            ignore_rules=data.get("ignore_rules") is True,
            ignore_user_config=data.get("ignore_user_config") is True,
            safe_mode=data.get("safe_mode") is True,
        )


def launch_from_args(
    args: GlobalArgs, chat: ChatArgs, *, cwd: Path
) -> tuple[LaunchPolicy, Mapping[str, Any]]:
    """The policy for a new session from this run's flags, and the flags given explicitly."""
    toolsets = split_csv(args.toolsets)
    skills = split_csv(list(args.skills)) or []
    policy = LaunchPolicy(
        cwd=str(cwd),
        model=args.model,
        provider=args.provider,
        reasoning=args.reasoning,
        toolsets=tuple(toolsets) if toolsets is not None else None,
        max_turns=chat.max_turns,
        skills=tuple(skills),
        yolo=args.yolo,
        pass_session_id=args.pass_session_id,
        ignore_rules=args.ignore_rules,
        ignore_user_config=args.ignore_user_config,
        safe_mode=args.safe_mode,
    )
    given: dict[str, Any] = {}
    if args.in_dir is not None:
        given["cwd"] = str(cwd)
    for name in ("model", "provider", "reasoning"):
        value = getattr(args, name)
        if value is not None:
            given[name] = value
    if toolsets is not None:
        given["toolsets"] = tuple(toolsets)
    if chat.max_turns is not None:
        given["max_turns"] = chat.max_turns
    if skills:
        given["skills"] = tuple(skills)
    for name in ("yolo", "pass_session_id", "ignore_rules", "ignore_user_config", "safe_mode"):
        if getattr(args, name):
            given[name] = True
    return policy, given


_FLAG_NAMES = {
    "cwd": "--in",
    "model": "--model",
    "provider": "--provider",
    "reasoning": "--reasoning",
    "toolsets": "--toolsets",
    "max_turns": "--max-turns",
    "skills": "--skills",
    "yolo": "--yolo",
    "pass_session_id": "--pass-session-id",
    "ignore_rules": "--ignore-rules",
    "ignore_user_config": "--ignore-user-config",
    "safe_mode": "--safe-mode",
}


def _same(name: str, given: Any, stored: Any) -> bool:
    if name == "cwd":
        if not stored:
            return True
        try:
            return os.path.realpath(str(given)) == os.path.realpath(str(stored))
        except (OSError, ValueError):
            return str(given) == str(stored)
    if name in ("toolsets", "skills"):
        return sorted(given or ()) == sorted(stored or ())
    return given == stored


def check_frozen(stored: LaunchPolicy, given: Mapping[str, Any]) -> list[str]:
    """Flags given on resume whose value differs from the session's stored one."""
    conflicts: list[str] = []
    for name, value in given.items():
        if not _same(name, value, getattr(stored, name)):
            conflicts.append(_FLAG_NAMES.get(name, name))
    return conflicts


def replace_cwd(policy: LaunchPolicy, cwd: Path) -> LaunchPolicy:
    """``policy`` working in ``cwd`` (where the CLI actually is after restore or ``--in``)."""
    return replace(policy, cwd=str(cwd))


def stored_launch(row: Mapping[str, Any] | None) -> LaunchPolicy | None:
    """The launch record of a session row, or None when it has none (another surface made it)."""
    if row is None:
        return None
    config = sessions.parse_model_config(row.get("model_config"))
    data = config.get(LAUNCH_KEY)
    if not isinstance(data, Mapping):
        return None
    policy = LaunchPolicy.from_record(data)  # pyright: ignore[reportUnknownArgumentType]
    if policy.cwd is None and isinstance(row.get("cwd"), str):
        policy = replace(policy, cwd=row["cwd"])
    return policy


# -- workspace and lookups -------------------------------------------------------------------------


def workspace_key(cwd: Path) -> Path:
    """The git work tree holding ``cwd`` (a ``.git`` dir or file), else ``cwd`` itself."""
    here = cwd
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    return here


def enter_in_dir(raw: str) -> Path:
    """``--in DIR``: expand ``~``, check it exists, change into it. Raises ``SelectionError``."""
    target = Path(os.path.expanduser(raw))
    with contextlib.suppress(OSError):
        target = target.resolve()
    if not target.is_dir():
        raise SelectionError(f"--in: no such directory: {raw}")
    try:
        os.chdir(target)
    except OSError as exc:
        raise SelectionError(f"--in: cannot enter {raw}: {exc.strerror or exc}") from exc
    return target


def _tip(store: StoreHandle, session_id: str) -> str:
    try:
        with store.read() as conn:
            return lineage.tip_in(conn, session_id)
    except StoreError as exc:
        log.warning("could not follow session %s to its newest part: %s", session_id, exc)
        return session_id


def find_named(store: StoreHandle, text: str) -> str | None:
    """An exact id, a unique id prefix, then a title; followed to the compression tip."""
    wanted = text.strip()
    if not wanted:
        return None
    found: str | None = None
    try:
        found = sessions.resolve_id(store, wanted)
    except sessions.AmbiguousSessionId:
        found = None
    except sessions.SessionNotFound:
        found = None
    if found is None:
        found = titles.resolve_title(store, wanted)
    return _tip(store, found) if found is not None else None


def latest_for_workspace(
    store: StoreHandle, workspace: Path, sources: Sequence[str] = CLI_SOURCES
) -> str | None:
    """The most recently used session of ``workspace`` from this surface's sources."""
    filters = listing.ListFilters(sources=list(sources), workspace=str(workspace))
    rows = listing.list_sessions(store, filters, limit=50)
    if not rows:
        return None
    best = max(rows, key=lambda r: (r.last_active or r.started_at, r.started_at))
    return _tip(store, best.lineage_ids[-1] if best.lineage_ids else best.id)


def _exists(store: StoreHandle, session_id: str) -> bool:
    try:
        return sessions.get(store, session_id) is not None
    except StoreError:
        return False


def select_session(
    store: StoreHandle,
    args: GlobalArgs,
    *,
    cwd: Path,
    breadcrumb: str | None = None,
) -> str | None:
    """The session id to resume for ``args``, or None for a new session.

    ``breadcrumb`` is this terminal's recorded session (``resume.read_breadcrumb``), consulted
    only for the "latest" forms. Raises ``SelectionError`` on a miss.
    """
    if args.resume is not None:
        wanted = args.resume
        latest = wanted.strip().lower() == LATEST
    elif args.continue_ is not None:
        wanted = args.continue_
        latest = wanted == BARE_CONTINUE
    else:
        return None
    if latest:
        if breadcrumb and _exists(store, breadcrumb):
            return _tip(store, breadcrumb)
        found = latest_for_workspace(store, workspace_key(cwd))
        if found is None:
            raise SelectionError(
                "No earlier session to continue in this workspace.", hint=_LIST_HINT
            )
        return found
    found = find_named(store, wanted)
    if found is None:
        raise SelectionError(f"No session found matching '{wanted}'.", hint=_LIST_HINT)
    return found
