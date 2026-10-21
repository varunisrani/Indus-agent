"""Where an update goes: channels, targets, the never-backwards rule and the plan (P23-17).

Channels (words from ``indus.foundation.versions``):

- ``stable``: the newest ``vX.Y.Z`` tag, checked out at its exact commit;
- ``canary``: the newest ``vX.Y.Z-canary.<stamp>`` tag, or a newer stable when one exists (a
  stable that a canary leads to sorts above it), so canary users are never stranded;
- ``main``: the tip of ``origin/main``.

The channel in force is, first match wins: ``--channel`` (one run), the install's saved choice
``updates.installs.<id>.channel`` (``--set-channel``), then the default — ``stable`` for a clone
of the official repository (``RELEASE_REPOSITORY``), ``main`` for a fork or mirror. Only the
default is "no explicit choice": then a checkout whose HEAD is not an ancestor of the target is
left where it is (``ahead``). An explicit choice may move back (``rollback``).

Tags are read from the checkout's own ``origin`` (fetched first), never from our release host, so
forks and mirrors work the same way. A target whose store schema (``SCHEMA_VERSION`` in its
``src/indus/state/schema.py``) is older than any live store under the root is refused before
anything moves: an older build cannot read a newer store.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from indus.foundation.fsio import atomic_write_json
from indus.foundation.versions import (
    CANARY,
    CHANNELS,
    MAIN,
    RELEASE_REPOSITORY,
    STABLE,
    Version,
    parse_tag,
)

__all__ = [
    "INSTALLS_KEY",
    "NETWORK_TIMEOUT_S",
    "ChannelChoice",
    "Decision",
    "Git",
    "GitError",
    "Target",
    "build_plan",
    "choose_channel",
    "current_version",
    "decide",
    "fetch",
    "is_official",
    "live_store_schema",
    "release_tags",
    "resolve_target",
    "saved_channel",
    "saved_extras",
    "store_refusal",
    "target_store_schema",
    "write_check_cache",
]

INSTALLS_KEY = "updates.installs"
"""Per-install settings: ``updates.installs.<id>.channel`` and ``.extras``."""
NETWORK_TIMEOUT_S = 300.0
_LOCAL_TIMEOUT_S = 120.0
_SCHEMA_FILE = "src/indus/state/schema.py"
_SCHEMA_LINE = re.compile(r"^SCHEMA_VERSION\s*=\s*(\d+)\s*$", re.MULTILINE)
_STORE_NAME = "state.db"

type Action = Literal["update", "rollback", "up_to_date", "ahead", "none"]


class GitError(RuntimeError):
    """A git command failed; ``detail`` is git's own last line."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


type Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass
class Git:
    """git in one checkout. No prompts (an auth failure fails fast), C locale, bounded waits."""

    root: Path
    env: Mapping[str, str]
    runner: Runner = subprocess.run
    log: Callable[[str], None] | None = None

    def _env(self) -> dict[str, str]:
        env = dict(self.env)
        env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_ASKPASS": ""})
        return env

    def run(
        self, *args: str, check: bool = True, network: bool = False
    ) -> subprocess.CompletedProcess[str]:
        argv = ["git", *args]
        try:
            done = self.runner(
                argv,
                cwd=str(self.root),
                env=self._env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=NETWORK_TIMEOUT_S if network else _LOCAL_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} timed out", str(exc)) from exc
        except OSError as exc:
            raise GitError(f"could not run git: {exc}") from exc
        if self.log is not None:
            self.log(f"$ {' '.join(argv)}\n{done.stdout}{done.stderr}")
        if check and done.returncode != 0:
            lines = (done.stderr or done.stdout or "").strip().splitlines()
            raise GitError(f"git {args[0]} failed", lines[-1] if lines else "")
        return done

    def out(self, *args: str) -> str:
        return self.run(*args).stdout.strip()

    def ok(self, *args: str) -> bool:
        return self.run(*args, check=False).returncode == 0

    def head(self) -> str:
        return self.out("rev-parse", "HEAD")

    def resolve(self, ref: str) -> str | None:
        done = self.run("rev-parse", "-q", "--verify", f"{ref}^{{commit}}", check=False)
        sha = done.stdout.strip()
        return sha if done.returncode == 0 and sha else None

    def is_ancestor(self, older: str, newer: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", older, newer)

    def dirty(self) -> bool:
        return bool(self.out("status", "--porcelain", "--untracked-files=normal"))

    def branch(self) -> str | None:
        name = self.run("symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
        return name or None

    def remote_url(self, name: str = "origin") -> str:
        """The configured URL as written (``insteadOf`` rewrites not applied)."""
        return self.run("config", "--get", f"remote.{name}.url", check=False).stdout.strip()

    def show(self, commit: str, path: str) -> str | None:
        done = self.run("show", f"{commit}:{path}", check=False)
        return done.stdout if done.returncode == 0 else None


# -- channels --------------------------------------------------------------------------------------


_OFFICIAL = re.compile(
    r"github\.com[:/]+" + re.escape(RELEASE_REPOSITORY) + r"(?:\.git)?/?$", re.IGNORECASE
)


def is_official(remote_url: str) -> bool:
    """Is ``origin`` the official repository (https or ssh form, ``.git`` optional)?"""
    return bool(_OFFICIAL.search(remote_url.strip()))


@dataclass(frozen=True)
class ChannelChoice:
    channel: str
    source: Literal["flag", "saved", "default"]

    @property
    def explicit(self) -> bool:
        return self.source != "default"


def choose_channel(flag: str | None, saved: object, *, official: bool) -> ChannelChoice:
    """``--channel`` beats the saved choice, which beats the default (see the module notes)."""
    if flag:
        if flag not in CHANNELS:
            raise ValueError(f"unknown channel {flag!r} (use {', '.join(CHANNELS)})")
        return ChannelChoice(flag, "flag")
    if isinstance(saved, str) and saved in CHANNELS:
        return ChannelChoice(saved, "saved")
    return ChannelChoice(STABLE if official else MAIN, "default")


def _install_row(installs: object, iid: str) -> Mapping[str, object]:
    """``updates.installs.<iid>`` as a mapping (empty when missing or malformed)."""
    if not isinstance(installs, Mapping):
        return {}
    row = cast(Mapping[str, object], installs).get(iid)
    return cast(Mapping[str, object], row) if isinstance(row, Mapping) else {}


def saved_channel(installs: object, iid: str) -> str | None:
    """``updates.installs.<iid>.channel`` from the ``updates.installs`` mapping."""
    value = _install_row(installs, iid).get("channel")
    return value if isinstance(value, str) and value in CHANNELS else None


def saved_extras(installs: object, iid: str) -> list[str]:
    """``updates.installs.<iid>.extras``: the optional extras this install was set up with."""
    extras = _install_row(installs, iid).get("extras")
    if not isinstance(extras, list):
        return []
    names = [str(x) for x in cast(list[object], extras) if isinstance(x, str)]
    return [x for x in names if re.fullmatch(r"[a-z0-9-]+", x)]


def fetch(git: Git) -> None:
    """Fetch release tags and the main branch from ``origin`` (writes no file in the tree)."""
    git.run(
        "fetch",
        "--quiet",
        "--tags",
        "origin",
        f"+refs/heads/{MAIN}:refs/remotes/origin/{MAIN}",
        network=True,
    )


def release_tags(git: Git) -> list[tuple[Version, str]]:
    """Every well-formed release tag, oldest first (malformed tags are ignored)."""
    found: list[tuple[Version, str]] = []
    for name in git.out("tag", "--list", "v*").splitlines():
        version = parse_tag(name.strip())
        if version is not None:
            found.append((version, name.strip()))
    return sorted(found, key=lambda row: row[0].key)


@dataclass(frozen=True)
class Target:
    channel: str
    commit: str
    version: Version | None = None
    tag: str | None = None

    @property
    def label(self) -> str:
        if self.version is not None:
            return str(self.version)
        return f"{MAIN}@{self.commit[:10]}"


def resolve_target(git: Git, channel: str) -> Target | None:
    """The commit ``channel`` points at now (after ``fetch``), or None when it has none yet."""
    if channel == MAIN:
        sha = git.resolve(f"refs/remotes/origin/{MAIN}")
        return None if sha is None else Target(MAIN, sha)
    tags = release_tags(git)
    stables = [row for row in tags if not row[0].is_canary]
    pool = stables if channel == STABLE else tags if channel == CANARY else []
    if not pool:
        return None
    version, tag = pool[-1]
    sha = git.resolve(f"refs/tags/{tag}")
    if sha is None:
        return None
    return Target(channel, sha, version, tag)


def current_version(git: Git, head: str) -> Version | None:
    """The newest release tag on ``head`` itself, if any."""
    on_head = [
        version for version, tag in release_tags(git) if git.resolve(f"refs/tags/{tag}") == head
    ]
    return max(on_head, default=None)


@dataclass(frozen=True)
class Decision:
    action: Action
    head: str
    target: Target | None
    reason: str

    @property
    def moves(self) -> bool:
        return self.action in ("update", "rollback")


def decide(git: Git, choice: ChannelChoice, target: Target | None) -> Decision:
    """What an update would do (see the never-backwards rule in the module notes)."""
    head = git.head()
    if target is None:
        if choice.channel == MAIN:
            reason = f"origin has no {MAIN} branch"
        else:
            reason = f"no {choice.channel} release has been published yet"
        return Decision("none", head, None, reason)
    if head == target.commit:
        return Decision("up_to_date", head, target, f"already on {target.label}")
    if git.is_ancestor(head, target.commit):
        return Decision("update", head, target, f"{target.label} is newer")
    if not choice.explicit:
        return Decision(
            "ahead",
            head,
            target,
            f"this checkout is not behind {target.label}; it is left where it is",
        )
    if git.is_ancestor(target.commit, head):
        return Decision("rollback", head, target, f"moving back to {target.label}")
    return Decision("update", head, target, f"switching to {target.label} (history differs)")


# -- store schema ----------------------------------------------------------------------------------


def target_store_schema(git: Git, commit: str) -> int:
    """``SCHEMA_VERSION`` of the store code at ``commit`` (1 when the file says nothing)."""
    text = git.show(commit, _SCHEMA_FILE)
    found = _SCHEMA_LINE.search(text or "")
    return int(found.group(1)) if found else 1


def _store_version(path: Path) -> int | None:
    if not path.is_file():
        return None
    with contextlib.suppress(sqlite3.Error, ValueError):
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        finally:
            conn.close()
        return None if row is None else int(row[0])
    return None


def live_store_schema(root: Path) -> int | None:
    """The newest schema any store under ``root`` (default and named profiles) is at."""
    stores = [root / _STORE_NAME, *sorted((root / "profiles").glob(f"*/{_STORE_NAME}"))]
    found = [v for path in stores if (v := _store_version(path)) is not None]
    return max(found, default=None)


def store_refusal(git: Git, root: Path, target: Target) -> str | None:
    """Why ``target`` must not be installed over the live stores, or None."""
    live = live_store_schema(root)
    if live is None:
        return None
    wanted = target_store_schema(git, target.commit)
    if wanted >= live:
        return None
    return (
        f"{target.label} reads store schema {wanted}, but your sessions are already stored at "
        f"schema {live}; an older build cannot read them. Nothing was changed. To go back anyway, "
        "restore a backup taken before the newer release (indus import)."
    )


# -- plan and check --------------------------------------------------------------------------------


def build_plan(
    *,
    kind: str,
    checkout: Path,
    iid: str,
    choice: ChannelChoice | None,
    decision: Decision | None,
    current: Version | None,
    services: Sequence[Mapping[str, Any]],
    refusal: str | None = None,
) -> dict[str, Any]:
    """The ``--plan`` JSON (schema 1)."""
    target = decision.target if decision is not None else None
    return {
        "schema": 1,
        "install_kind": kind,
        "install_id": iid,
        "install_dir": str(checkout),
        "channel": None if choice is None else choice.channel,
        "channel_source": None if choice is None else choice.source,
        "action": "refused" if refusal else (decision.action if decision else "none"),
        "reason": refusal or (decision.reason if decision else ""),
        "from": {
            "commit": decision.head if decision else None,
            "version": None if current is None else str(current),
        },
        "to": None
        if target is None
        else {
            "commit": target.commit,
            "version": None if target.version is None else str(target.version),
            "tag": target.tag,
        },
        "services": [dict(s) for s in services],
    }


def write_check_cache(
    path: Path,
    *,
    decision: Decision,
    channel: str,
    notices: bool,
    now: datetime,
) -> None:
    """What ``indus --version`` shows later (``cli.version`` reads it, stdlib only)."""
    target = decision.target
    data = {
        "schema": 1,
        "checked_at": now.isoformat(),
        "channel": channel,
        "available": decision.action == "update",
        "target": None if target is None else target.label,
        "notices": notices,
    }
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, data)
