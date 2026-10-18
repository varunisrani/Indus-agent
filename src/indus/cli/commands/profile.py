"""``indus profile``: list, use, show, create (with clones), delete, rename, describe, alias.

Ids, reserved names, identity markers, tombstones and the sticky active file are P01's
(``foundation.profiles``). A new profile is built in a hidden staging dir beside the profile dirs
and published by one rename, so nothing ever sees half a profile. Clones copy file contents (a
symlinked ``config.yaml`` or ``.env`` in the source becomes a private file, so the clone never
writes through to the source) and, unless ``--clone-channels``, leave messaging channel settings
behind: channel names in ``.env`` (P01 env writer), platform sections in ``config.yaml`` (P01
config writer), channel entries of the secret store and channel state dirs (R04.26). Secret store
entries are copied by name into the new profile's namespace, except credential pool secrets
(``AUTH_<PROVIDER>_<ID>``): ``auth.json`` is not cloned, so its sign-ins and pool keys stay with
their profile.

Delete writes the tombstone first, then removes the secret store entries, the alias and the dir,
the dir only when it carries an identity marker: a marker-less dir is never removed (R04.25).
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import stat
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import click

from indus.cli.commands._support import err, state_of
from indus.cli.lazy import LazyGroup
from indus.cli.prompts import Cancelled, ask_text, io_for
from indus.foundation import fsio, yamlio
from indus.foundation.config import loader, writer
from indus.foundation.envfile import read_env_file, set_values
from indus.foundation.errors import IndusError, InvalidProfileId, ProfileNotFound
from indus.foundation.home import dir_mode, is_managed
from indus.foundation.profiles import (
    DEFAULT_PROFILE,
    RESERVED_IDS,
    clear_tombstone,
    has_identity,
    is_live,
    is_tombstoned,
    list_profiles,
    normalize_id,
    profile_home,
    profiles_dir,
    read_active,
    tombstone,
    write_active,
)
from indus.foundation.scope import Scope
from indus.foundation.secrets import SecretStore, open_secret_store
from indus.platform.locate import which
from indus.providers.auth.pool_model import is_pool_secret, pool_secret_names
from indus.providers.auth.store import AuthStore

__all__ = [
    "ALIAS_MARKER",
    "CHANNEL_ENV_NAMES",
    "CHANNEL_PLATFORMS",
    "ProfileProblem",
    "alias_dir",
    "alias_script",
    "create_profile",
    "delete_profile",
    "is_channel_name",
    "profile_command",
    "rename_profile",
]

META_FILE = "profile.yaml"
ALIAS_MARKER = "# indus profile alias"
CHANNEL_PLATFORMS: tuple[str, ...] = (
    "telegram",
    "discord",
    "slack",
    "whatsapp",
    "signal",
    "matrix",
    "mattermost",
    "email",
    "sms",
    "twilio",
    "feishu",
    "dingtalk",
    "wecom",
    "weixin",
    "qqbot",
    "bluebubbles",
    "homeassistant",
)
"""Messaging platforms whose settings a clone leaves behind (until P15's platform registry)."""
CHANNEL_ENV_NAMES = frozenset({"GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS"})
CHANNEL_STATE_DIRS = ("platforms", "pairing", "whatsapp")

CLONE_FILES = ("config.yaml", ".env", "PERSONA.md")
# Never copied by --clone-all: history, runtime state and things handled another way.
CLONE_ALL_SKIP_ROOT = frozenset(
    {
        "profiles",
        "state.db",
        "state.db-wal",
        "state.db-shm",
        "state.db-journal",
        "sessions",
        "backups",
        "state-snapshots",
        "checkpoints",
        "logs",
        "cache",
        "cron",
        "active_profile",
        "secrets.json",
        "secrets.index.json",
        "auth.json",
        META_FILE,
    }
)
_SKIP_ANYWHERE_SUFFIXES = (".lock", ".pid", ".sock")


class ProfileProblem(Exception):
    """A profile operation that cannot go ahead; the message says why."""


# -- small helpers --------------------------------------------------------------------------------


def _canon(name: str) -> str:
    try:
        return normalize_id(name)
    except InvalidProfileId as exc:
        raise ProfileProblem(exc.message + (f" ({exc.hint})" if exc.hint else "")) from exc


def _live_home(root: Path, name: str) -> tuple[str, Path]:
    pid = _canon(name)
    if not is_live(root, pid):
        raise ProfileProblem(
            f"Profile '{pid}' does not exist. Run `indus profile list` to see the profiles."
        )
    return pid, profile_home(root, pid)


def read_meta(home: Path) -> dict[str, Any]:
    try:
        loaded = yamlio.load_safe((home / META_FILE).read_bytes())
    except (OSError, yamlio.YamlError):
        return {}
    return cast(dict[str, Any], loaded) if isinstance(loaded, dict) else {}


def write_meta(home: Path, changes: Mapping[str, Any]) -> None:
    data = {**read_meta(home), **changes}
    text = yamlio.dump_plain(data) if data else ""
    fsio.atomic_write_text(home / META_FILE, text, mode=0o600, preserve_mode=False)


def _settings_of(home: Path) -> dict[str, Any]:
    try:
        return loader.read_user_tree((home / loader.CONFIG_FILE).read_bytes())
    except (OSError, yamlio.YamlError):
        return {}


def _model_of(home: Path) -> tuple[str, str]:
    model = loader.canonicalise(_settings_of(home)).get("model")
    if not isinstance(model, dict):
        return "", ""
    section = cast(dict[str, Any], model)
    return str(section.get("default") or ""), str(section.get("provider") or "")


def _store_mode(home: Path) -> str:
    security = _settings_of(home).get("security")
    if isinstance(security, dict):
        mode = cast(dict[str, Any], security).get("secret_store")
        if isinstance(mode, str) and mode in ("auto", "keychain", "file"):
            return mode
    return "auto"


def _store_for(home: Path, pid: str, scope: Scope) -> SecretStore:
    if home == scope.home and isinstance(scope.secrets, SecretStore):
        return scope.secrets
    return open_secret_store(home, pid, mode=_store_mode(home), clock=scope.clock)


def is_channel_name(name: str) -> bool:
    upper = name.upper()
    return upper in CHANNEL_ENV_NAMES or any(
        upper.startswith(f"{platform.upper()}_") for platform in CHANNEL_PLATFORMS
    )


# -- alias scripts --------------------------------------------------------------------------------


def alias_dir(env: Mapping[str, str]) -> Path:
    """Where alias scripts go: ``.local/bin`` under ``HOME`` (``indus/bin`` under ``LOCALAPPDATA``
    on Windows), read from ``env`` so tests never touch the real home."""
    if sys.platform == "win32":
        base = env.get("LOCALAPPDATA") or str(Path(env.get("USERPROFILE", "~")) / "AppData/Local")
        return Path(base) / "indus" / "bin"
    return Path(env.get("HOME") or str(Path.home())) / ".local" / "bin"


def _alias_file(env: Mapping[str, str], alias: str) -> Path:
    return alias_dir(env) / (f"{alias}.bat" if sys.platform == "win32" else alias)


def indus_command(env: Mapping[str, str]) -> list[str]:
    """How an alias script starts ``indus``: its absolute path, else this interpreter."""
    found = which("indus", path=env.get("PATH"))
    if found:
        return [str(Path(found).resolve())]
    return [sys.executable, "-m", "indus"]


def alias_script(profile: str, command: list[str], *, windows: bool = False) -> str:
    if windows:
        words = " ".join(f'"{w}"' if " " in w else w for w in command)
        return f"@echo off\r\nrem {ALIAS_MARKER[2:]}\r\n{words} -p {profile} %*\r\n"
    words = " ".join(shlex.quote(w) for w in command)
    return f'#!/bin/sh\n{ALIAS_MARKER}\nexec {words} -p {profile} "$@"\n'


def _is_ours(path: Path) -> bool:
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return False
    return ALIAS_MARKER[2:] in head


def _alias_target(path: Path) -> str | None:
    if not _is_ours(path):
        return None
    words = path.read_text(encoding="utf-8", errors="replace").split()
    for index, word in enumerate(words[:-1]):
        if word == "-p":
            return words[index + 1]
    return None


def aliases_for(env: Mapping[str, str], pid: str) -> list[Path]:
    folder = alias_dir(env)
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return []
    return [p for p in entries if p.is_file() and _alias_target(p) == pid]


def _command_names() -> set[str]:
    from indus.cli.app import REGISTRY

    return {name for name, _, _ in REGISTRY}


def alias_problem(env: Mapping[str, str], alias: str) -> str | None:
    """Why ``alias`` cannot be used (None when it can)."""
    try:
        name = normalize_id(alias)
    except InvalidProfileId:
        return f"'{alias}' cannot be an alias: use lowercase letters, digits, '-' or '_'."
    if name == DEFAULT_PROFILE or name in RESERVED_IDS or name == "indus":
        return f"'{name}' is a reserved name."
    if name in _command_names():
        return f"'{name}' is an `indus` command, so it cannot be an alias."
    target = _alias_file(env, name)
    if (target.exists() or target.is_symlink()) and not _is_ours(target):
        return f"{target} already exists and was not made by `indus profile alias`."
    elsewhere = which(name, path=env.get("PATH"))
    if elsewhere and Path(elsewhere) != target:
        return f"'{name}' would shadow or be shadowed by {elsewhere}."
    return None


def write_alias(env: Mapping[str, str], alias: str, pid: str) -> Path:
    target = _alias_file(env, alias)
    target.parent.mkdir(parents=True, exist_ok=True)
    windows = sys.platform == "win32"
    fsio.atomic_write_text(target, alias_script(pid, indus_command(env), windows=windows))
    if not windows:
        mode = target.stat().st_mode
        target.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def _on_path(env: Mapping[str, str], folder: Path) -> bool:
    return str(folder) in env.get("PATH", "").split(os.pathsep)


# -- create ---------------------------------------------------------------------------------------


@dataclass
class CloneReport:
    home: Path
    stripped: list[str] = field(default_factory=list[str])
    secrets_copied: int = 0
    warnings: list[str] = field(default_factory=list[str])


@dataclass(frozen=True)
class _StagingScope:
    """What the P01 config writer needs for a home that is not a live profile yet."""

    home: Path
    env: Mapping[str, str]
    clock: Any


def _copy_file(source: Path, target: Path) -> None:
    if not source.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)  # follows symlinks: the clone gets its own file
    if target.name == ".env":
        with contextlib.suppress(OSError):
            target.chmod(0o600)


def _clone_files(source: Path, staging: Path) -> None:
    for name in CLONE_FILES:
        _copy_file(source / name, staging / name)
    memories = source / "memories"
    if memories.is_dir():
        for entry in sorted(memories.iterdir()):
            if entry.suffix == ".md":
                _copy_file(entry, staging / "memories" / entry.name)
    skills = source / "skills"
    if skills.is_dir():
        shutil.copytree(
            skills,
            staging / "skills",
            symlinks=False,
            ignore_dangling_symlinks=True,
            dirs_exist_ok=True,
        )


def _clone_all(source: Path, staging: Path, keep_channels: bool) -> None:
    skip_root = set(CLONE_ALL_SKIP_ROOT)
    if not keep_channels:
        skip_root |= set(CHANNEL_STATE_DIRS)

    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped = {n for n in names if n == "__pycache__" or n.endswith(_SKIP_ANYWHERE_SUFFIXES)}
        if Path(directory) == source:
            skipped |= {n for n in names if n in skip_root or n.startswith(".")}
            skipped -= {".env"}
        return skipped

    shutil.copytree(source, staging, symlinks=True, ignore=ignore)
    for name in CLONE_FILES:
        path = staging / name
        if path.is_symlink():
            real = (source / name).resolve()
            path.unlink()
            if real.is_file():
                shutil.copyfile(real, path)


def _strip_env(staging: Path) -> list[str]:
    path = staging / ".env"
    names = [n for n in read_env_file(path) if is_channel_name(n)]
    if names:
        set_values(path, dict.fromkeys(names))
    return names


def _strip_settings(staging: Path, scope: Scope) -> list[str]:
    raw = _settings_of(staging)
    removed: list[str] = []
    for key in [*CHANNEL_PLATFORMS, "platforms"]:
        if key in raw:
            del raw[key]
            removed.append(key)
    gateway = raw.get("gateway")
    if isinstance(gateway, dict):
        section = cast(dict[str, Any], gateway)
        for key in [*CHANNEL_PLATFORMS, "platforms"]:
            if key in section:
                del section[key]
                removed.append(f"gateway.{key}")
        if not section:
            del raw["gateway"]
    if not removed:
        return []
    backups = staging / "backups"
    had_backups = backups.exists()
    writer.save(_StagingScope(staging, scope.env, scope.clock), raw, replace=True)
    if not had_backups:
        shutil.rmtree(backups, ignore_errors=True)  # the writer's copy still held the channels
    return removed


def _copy_secrets(
    source: Path, source_id: str, target: Path, pid: str, scope: Scope, keep_channels: bool
) -> tuple[int, list[str]]:
    try:
        src = _store_for(source, source_id, scope)
        names = src.names()
    except IndusError as exc:
        return 0, [f"stored keys were not copied: {exc.message}"]
    if not names:
        return 0, []
    copied = 0
    stripped: list[str] = []
    pool_names = pool_secret_names(AuthStore(source))
    try:
        dst = open_secret_store(target, pid, mode=_store_mode(target), clock=scope.clock)
        for name in names:
            if is_pool_secret(name, pool_names):
                continue  # sign-ins and pool keys stay with their profile (auth.json is not cloned)
            if not keep_channels and is_channel_name(name):
                stripped.append(f"stored key {name}")
                continue
            value = src.get(name)
            if value:
                dst.set(name, value)
                copied += 1
    except IndusError as exc:
        return copied, [f"some stored keys were not copied: {exc.message}"]
    return copied, stripped


def _tidy(staging: Path, scope: Scope) -> None:
    """Drop the lock files the writers left in the staging dir and give it the home's mode."""
    for name in (".env.lock", "config.yaml.lock"):
        (staging / name).unlink(missing_ok=True)
    if not is_managed(staging, scope.env):
        with contextlib.suppress(OSError):
            staging.chmod(dir_mode(scope.env))


def create_profile(
    scope: Scope,
    name: str,
    *,
    clone: bool = False,
    clone_all: bool = False,
    clone_from: str | None = None,
    clone_channels: bool = False,
    description: str | None = None,
) -> CloneReport:
    root = scope.root
    pid = _canon(name)
    if pid == DEFAULT_PROFILE:
        raise ProfileProblem("'default' is the built-in profile; pick another name.")
    target = profiles_dir(root) / pid
    if is_live(root, pid):
        raise ProfileProblem(f"Profile '{pid}' already exists.")
    if target.exists() or target.is_symlink():
        raise ProfileProblem(
            f"{target} exists but is not a profile (it has no profile files). Move it away first; "
            "it was left untouched."
        )
    cloning = clone or clone_all or clone_from is not None
    if clone_channels and not cloning:
        raise ProfileProblem(
            "--clone-channels only applies with --clone, --clone-all or --clone-from."
        )
    source: Path | None = None
    source_id = scope.profile_id
    if cloning:
        if clone_from is not None:
            source_id, source = _live_home(root, clone_from)
        else:
            source = scope.home
        if not source.is_dir():
            raise ProfileProblem(f"The profile to clone from has no home at {source}.")
    staging = profiles_dir(root) / f".{pid}.staging-{os.getpid()}"
    if staging.exists() or staging.is_symlink():
        shutil.rmtree(staging, ignore_errors=True)
        staging.unlink(missing_ok=True)
    report = CloneReport(target)
    try:
        if source is not None and clone_all:
            _clone_all(source, staging, clone_channels)
        else:
            staging.mkdir(parents=True)
            if source is not None:
                _clone_files(source, staging)
        if source is not None and not clone_channels:
            report.stripped += [f".env {n}" for n in _strip_env(staging)]
            report.stripped += [f"setting {k}" for k in _strip_settings(staging, scope)]
        write_meta(staging, {"description": description.strip()} if description else {})
        _tidy(staging, scope)
        clear_tombstone(root, pid)
        os.rename(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if source is not None:
        copied, notes = _copy_secrets(source, source_id, target, pid, scope, clone_channels)
        report.secrets_copied = copied
        for note in notes:
            (report.stripped if note.startswith("stored key ") else report.warnings).append(note)
    return report


# -- delete / rename ------------------------------------------------------------------------------


def _drop_secrets(home: Path, pid: str, scope: Scope) -> list[str]:
    try:
        store = _store_for(home, pid, scope)
        for name in store.names():
            store.delete(name)
    except IndusError as exc:
        return [f"stored keys could not all be removed: {exc.message}"]
    return []


def delete_profile(scope: Scope, name: str) -> list[str]:
    """Delete a named profile (see the module notes); returns warning lines."""
    root = scope.root
    pid, home = _live_home(root, name)
    if pid == DEFAULT_PROFILE:
        raise ProfileProblem("The default profile cannot be deleted.")
    tombstone(root, pid, clock=scope.clock)
    warnings = _drop_secrets(home, pid, scope)
    for path in aliases_for(scope.env, pid):
        path.unlink(missing_ok=True)
    if has_identity(home):
        try:
            shutil.rmtree(home)
        except OSError as exc:
            warnings.append(f"{home} could not be removed completely: {exc}")
    else:
        warnings.append(f"{home} has no profile files, so it was left in place.")
    if read_active(root) == pid:
        write_active(root, DEFAULT_PROFILE)
        warnings.append("The sticky profile was this one; it is now default.")
    return warnings


def rename_profile(scope: Scope, old: str, new: str) -> list[str]:
    root = scope.root
    old_id, old_home = _live_home(root, old)
    if old_id == DEFAULT_PROFILE:
        raise ProfileProblem("The default profile cannot be renamed.")
    new_id = _canon(new)
    if new_id == DEFAULT_PROFILE:
        raise ProfileProblem("'default' is the built-in profile; pick another name.")
    new_home = profiles_dir(root) / new_id
    if is_live(root, new_id) or new_home.exists() or new_home.is_symlink():
        raise ProfileProblem(f"{new_home} already exists.")
    notes: list[str] = []
    if is_tombstoned(root, new_id):
        clear_tombstone(root, new_id)
    os.rename(old_home, new_home)
    try:
        old_store = open_secret_store(
            new_home, old_id, mode=_store_mode(new_home), clock=scope.clock
        )
        if old_store.backend_name == "keychain":
            new_store = open_secret_store(
                new_home, new_id, mode=_store_mode(new_home), clock=scope.clock
            )
            for key in old_store.names():
                value = old_store.get(key)
                if value is not None:
                    new_store.set(key, value)
                old_store.delete(key)
    except IndusError as exc:
        notes.append(f"stored keys could not all be moved: {exc.message}")
    for path in aliases_for(scope.env, old_id):
        alias = path.stem if path.suffix == ".bat" else path.name
        write_alias(scope.env, alias, new_id)
        notes.append(f"Alias {path} now starts {new_id}.")
    if read_active(root) == old_id:
        write_active(root, new_id)
        notes.append(f"The sticky profile is now {new_id}.")
    return notes


# -- commands -------------------------------------------------------------------------------------


def _root_scope(ctx: click.Context) -> Scope:
    return state_of(ctx).scope


def _run(action: Callable[[], int]) -> int:
    try:
        return action()
    except ProfileProblem as exc:
        err(str(exc))
        return 1
    except IndusError as exc:
        err(exc.message + (f" ({exc.hint})" if exc.hint else ""))
        return 1
    except OSError as exc:
        err(f"The profile could not be changed: {exc}")
        return 1


def _list(scope: Scope) -> int:
    root = scope.root
    sticky = read_active(root)
    rows: list[tuple[str, str, str, str]] = []
    for pid in list_profiles(root):
        home = profile_home(root, pid)
        model, provider = _model_of(home)
        shown = f"{model} ({provider})" if model and provider else model or provider or "—"
        alias = ", ".join(p.name for p in aliases_for(scope.env, pid)) or "—"
        marks = ("*" if pid == sticky else " ") + (">" if pid == scope.profile_id else " ")
        rows.append((marks, pid, shown, alias))
    width = max(len(r[1]) for r in rows)
    model_width = max(len(r[2]) for r in rows)
    for marks, pid, shown, alias in rows:
        click.echo(f"{marks} {pid:<{width}}  {shown:<{model_width}}  {alias}")
    click.echo("(* sticky profile, > this run's profile)")
    return 0


def _profile_names(scope: Scope) -> Iterable[str]:
    return list_profiles(scope.root)


@click.group(
    "profile",
    cls=LazyGroup,
    invoke_without_command=True,
    help="Create, switch and manage profiles. With no subcommand, the same as `profile list`.",
)
@click.pass_context
def profile_command(ctx: click.Context) -> int:
    if ctx.invoked_subcommand is not None:
        return 0
    return _list(_root_scope(ctx))


@profile_command.command("list", help="List the profiles.")
@click.pass_context
def list_command(ctx: click.Context) -> int:
    return _list(_root_scope(ctx))


@profile_command.command("use", help="Make NAME the profile used when -p is not given.")
@click.argument("name")
@click.pass_context
def use_command(ctx: click.Context, name: str) -> int:
    scope = _root_scope(ctx)

    def go() -> int:
        pid = _canon(name)
        try:
            write_active(scope.root, pid)
        except ProfileNotFound:
            raise ProfileProblem(f"Profile '{pid}' does not exist.") from None
        if pid == DEFAULT_PROFILE:
            click.echo("The default profile is used when -p is not given.")
        else:
            click.echo(f"Profile '{pid}' is used when -p is not given.")
        return 0

    return _run(go)


@profile_command.command("show", help="Show one profile's details.")
@click.argument("name")
@click.pass_context
def show_command(ctx: click.Context, name: str) -> int:
    scope = _root_scope(ctx)

    def go() -> int:
        pid, home = _live_home(scope.root, name)
        model, provider = _model_of(home)
        skills = home / "skills"
        count = (
            len([p for p in skills.iterdir() if not p.name.startswith(".")])
            if skills.is_dir()
            else 0
        )
        click.echo(f"Profile: {pid}")
        click.echo(f"Path:    {home}")
        click.echo(f"Model:   {model or '—'}" + (f" ({provider})" if provider else ""))
        click.echo(f".env:    {'present' if (home / '.env').is_file() else 'none'}")
        click.echo(f"Persona: {'present' if (home / 'PERSONA.md').is_file() else 'none'}")
        click.echo(f"Skills:  {count}")
        for path in aliases_for(scope.env, pid):
            click.echo(f"Alias:   {path.name} ({path})")
        description = str(read_meta(home).get("description") or "")
        if description:
            click.echo(f"About:   {description}")
        return 0

    return _run(go)


@profile_command.command("create", help="Create a profile, empty or cloned from another one.")
@click.argument("name")
@click.option("--clone", is_flag=True, help="Copy settings, .env, persona, memories and skills.")
@click.option("--clone-all", is_flag=True, help="Copy everything except history and runtime state.")
@click.option(
    "--clone-from",
    metavar="SRC",
    default=None,
    help="Clone this profile (default: the current one).",
)
@click.option(
    "--clone-channels", is_flag=True, help="Also copy messaging channel settings and tokens."
)
@click.option("--no-alias", is_flag=True, help="Do not write an alias script.")
@click.option("--description", default=None, help="What this profile is for.")
@click.pass_context
def create_command(
    ctx: click.Context,
    name: str,
    clone: bool,
    clone_all: bool,
    clone_from: str | None,
    clone_channels: bool,
    no_alias: bool,
    description: str | None,
) -> int:
    scope = _root_scope(ctx)

    def go() -> int:
        report = create_profile(
            scope,
            name,
            clone=clone,
            clone_all=clone_all,
            clone_from=clone_from,
            clone_channels=clone_channels,
            description=description,
        )
        pid = report.home.name
        click.echo(f"Created profile '{pid}' at {report.home}")
        if report.secrets_copied:
            click.echo(f"Copied {report.secrets_copied} stored key(s).")
        if report.stripped:
            click.echo("Left behind (messaging channels; use --clone-channels to copy them):")
            for item in report.stripped:
                click.echo(f"  {item}")
        for warning in report.warnings:
            err(f"Warning: {warning}")
        if not no_alias:
            problem = alias_problem(scope.env, pid)
            if problem:
                err(f"No alias written: {problem}")
            else:
                path = write_alias(scope.env, pid, pid)
                click.echo(f"Alias: {path}")
                if not _on_path(scope.env, path.parent):
                    err(f"Warning: {path.parent} is not on your PATH.")
        click.echo(f"Use it with `indus -p {pid}` or `indus profile use {pid}`.")
        return 0

    return _run(go)


@profile_command.command("delete", help="Delete a profile (it cannot be brought back).")
@click.argument("name")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")
@click.pass_context
def delete_command(ctx: click.Context, name: str, yes: bool) -> int:
    state = state_of(ctx)
    scope = state.scope

    def go() -> int:
        pid, home = _live_home(scope.root, name)
        if pid == DEFAULT_PROFILE:
            raise ProfileProblem("The default profile cannot be deleted.")
        if not yes:
            io = io_for(state)
            if not io.tty:
                raise ProfileProblem(
                    f"Deleting '{pid}' needs confirmation; run `indus profile delete {pid} -y`."
                )
            click.echo(f"This deletes {home}, its sessions and its stored keys.")
            try:
                typed = ask_text(io, f"Type '{pid}' to confirm")
            except Cancelled:
                typed = ""
            if typed.strip() != pid:
                click.echo("Cancelled; nothing was deleted.")
                return 1
        for line in delete_profile(scope, pid):
            err(line)
        click.echo(f"Profile '{pid}' deleted.")
        return 0

    return _run(go)


@profile_command.command("rename", help="Rename a profile.")
@click.argument("old")
@click.argument("new")
@click.pass_context
def rename_command(ctx: click.Context, old: str, new: str) -> int:
    scope = _root_scope(ctx)

    def go() -> int:
        notes = rename_profile(scope, old, new)
        click.echo(f"Renamed '{_canon(old)}' to '{_canon(new)}'.")
        for line in notes:
            click.echo(line)
        return 0

    return _run(go)


@profile_command.command("describe", help="Show or set what a profile is for.")
@click.argument("name", required=False)
@click.option("--text", default=None, help="The new description.")
@click.pass_context
def describe_command(ctx: click.Context, name: str | None, text: str | None) -> int:
    scope = _root_scope(ctx)

    def go() -> int:
        pid, home = _live_home(scope.root, name or scope.profile_id)
        if text is None:
            current = str(read_meta(home).get("description") or "")
            click.echo(current or f"(no description for '{pid}')")
            return 0
        write_meta(home, {"description": text.strip()})
        click.echo(f"Description saved for '{pid}'.")
        return 0

    return _run(go)


@profile_command.command(
    "alias", help="Write (or remove) a command that starts indus with a profile."
)
@click.argument("name")
@click.option("--remove", is_flag=True, help="Remove the alias instead.")
@click.option(
    "--name", "alias_name", default=None, help="The alias's name (default: the profile's)."
)
@click.pass_context
def alias_command(ctx: click.Context, name: str, remove: bool, alias_name: str | None) -> int:
    scope = _root_scope(ctx)

    def go() -> int:
        typed = alias_name or name
        try:
            alias = normalize_id(typed)
        except InvalidProfileId:
            raise ProfileProblem(
                f"'{typed}' cannot be an alias: use lowercase letters, digits, '-' or '_'."
            ) from None
        target = _alias_file(scope.env, alias)
        if remove:
            if not _is_ours(target):
                raise ProfileProblem(f"There is no alias '{alias}' made by `indus profile alias`.")
            target.unlink()
            click.echo(f"Removed {target}")
            return 0
        pid, _home = _live_home(scope.root, name)
        problem = alias_problem(scope.env, alias)
        if problem:
            raise ProfileProblem(problem)
        path = write_alias(scope.env, alias, pid)
        click.echo(f"Alias written: {path} (starts `indus -p {pid}`)")
        if not _on_path(scope.env, path.parent):
            err(f"Warning: {path.parent} is not on your PATH.")
        return 0

    return _run(go)
