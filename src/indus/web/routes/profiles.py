"""``/api/profiles``: the dashboard's Profiles page and profile builder (P18-16, R04.25).

Machine-level: the profile a route acts on is named in the path, never by ``?profile=``. Every
name is checked against the live profiles under the backend's root first, so a deleted
(tombstoned) or unknown profile is 404 for reads and writes alike, and no route ever creates the
folder of a profile that does not exist.

- ``GET /api/profiles``: each live profile with its model, description, whether it has a persona,
  how many skills it has turned on (``skill_count``: what ``/api/skills`` lists as enabled; null
  when its settings or skills cannot be read; W55-F2-2) and its bot's forever-chat
  (``canonical_session: {session_id} | null``, P24-31: the same lookup as ``bots.open``, read
  only), plus the sticky profile (``active``) and the one this backend
  serves (``current``).
- ``POST /api/profiles {name, clone_from?, clone_all?, description?, no_skills?, alias?}``,
  ``PATCH /api/profiles/{name} {new_name}`` and ``DELETE /api/profiles/{name}`` run the real
  ``indus profile create|rename|delete`` in a child process (``web`` may not import ``cli``; the
  command owns the staging folder, the secret moves, the tombstone and the alias). A refusal is
  400 with the command's own message, masked.
- ``GET``/``POST /api/profiles/active``: the sticky profile new CLI runs pick up.
- ``GET``/``PUT /api/profiles/{name}/persona`` (``PERSONA.md``), ``PUT …/description``
  (``profile.yaml``), ``PUT …/model {provider, model}`` (that profile's ``config.yaml`` through the
  config writer).
- ``POST /api/profiles/{name}/export`` (a background action writing a zip of the profile's
  settings, no history, runtime state or secrets, into its ``backups/`` folder) and
  ``POST /api/profiles/import {archive, name?}`` (a new profile from such a zip; members are
  checked for absolute paths, ``..``, links and size before anything is written).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Final, NoReturn, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from indus.foundation import config as config_module
from indus.foundation import fsio, yamlio
from indus.foundation.config import loader
from indus.foundation.errors import ConfigError, IndusError, InvalidProfileId, ProfileNotFound
from indus.foundation.profiles import (
    DEFAULT_PROFILE,
    IDENTITY_MARKERS,
    clear_tombstone,
    is_live,
    list_profiles,
    normalize_id,
    profile_home,
    profiles_dir,
    read_active,
    write_active,
)
from indus.foundation.redact import redact_text
from indus.foundation.scope import Scope
from indus.learning.skills.resolver import load_catalog
from indus.service import bots_rpc
from indus.web.actions import ActionBusy, Say, actions_of, child_env
from indus.web.scope import ProfileScopes, ProfileUnknown, scopes_of
from indus.web.state import dashboard_state

__all__ = ["EXPORT_SKIP", "PERSONA_FILE", "ROUTE_LIST", "router"]

router = APIRouter()

ROUTE_LIST: Final[tuple[tuple[str, str, bool], ...]] = (
    ("GET", "/api/profiles", False),
    ("POST", "/api/profiles", False),
    ("GET", "/api/profiles/active", False),
    ("POST", "/api/profiles/active", False),
    ("POST", "/api/profiles/import", False),
    ("PATCH", "/api/profiles/{name}", False),
    ("DELETE", "/api/profiles/{name}", False),
    ("GET", "/api/profiles/{name}/persona", False),
    ("PUT", "/api/profiles/{name}/persona", False),
    ("PUT", "/api/profiles/{name}/description", False),
    ("PUT", "/api/profiles/{name}/model", False),
    ("POST", "/api/profiles/{name}/export", False),
)
"""``(method, path, scoped)``; machine-level (the profile is in the path)."""

PERSONA_FILE: Final = "PERSONA.md"
META_FILE: Final = "profile.yaml"
PERSONA_MAX_CHARS: Final = 200_000
COMMAND_TIMEOUT_S: Final = 120.0
EXPORT_MANIFEST: Final = "indus-profile.json"
EXPORT_MAX_BYTES: Final = 512 * 1024 * 1024
EXPORT_SKIP: Final = frozenset(
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
        "auth.json.lock",
        "pairing",
        "platforms",
        "gateway",
    }
)
"""Top-level names an export leaves out: history, runtime state, secrets and channel state."""
_SKIP_SUFFIXES: Final = (".lock", ".pid", ".sock", ".path")

type Runner = Callable[[Sequence[str], Mapping[str, str]], tuple[int, str]]


def _fail(status: int, code: str, message: str) -> NoReturn:
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def run_cli(args: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
    """``python -m indus <args>`` once; ``(exit code, combined output)``."""
    try:
        done = subprocess.run(  # noqa: S603 - our own CLI with a fixed argv
            [sys.executable, "-m", "indus", *args],
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "the profile command did not finish in time"
    except OSError as exc:
        return 127, f"the profile command could not start: {exc}"
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _scopes(request: Request) -> ProfileScopes:
    return scopes_of(request)


Scopes = Annotated[ProfileScopes, Depends(_scopes)]


def _runner(request: Request) -> Runner:
    found = dashboard_state(request.app).extras.get("profiles.run")
    return cast(Runner, found) if callable(found) else run_cli


def _live(scopes: ProfileScopes, name: str) -> tuple[str, Path]:
    try:
        pid = normalize_id(name)
    except InvalidProfileId:
        _fail(400, "bad_name", "That is not a valid profile name.")
    root = scopes.base.root
    try:
        live = is_live(root, pid)
    except (InvalidProfileId, OSError):
        live = False
    if not live:
        _fail(404, "profile_not_found", "No such profile.")
    return pid, profile_home(root, pid)


def _scope_for(scopes: ProfileScopes, name: str) -> Scope:
    try:
        return scopes.resolve(name)
    except ProfileUnknown:
        _fail(404, "profile_not_found", "No such profile.")


def _settings_of(home: Path) -> dict[str, Any]:
    try:
        return loader.canonicalise(loader.read_user_tree((home / loader.CONFIG_FILE).read_bytes()))
    except (OSError, yamlio.YamlError):
        return {}


def _model_of(home: Path) -> tuple[str, str]:
    model = _settings_of(home).get("model")
    if not isinstance(model, dict):
        return "", ""
    section = cast(dict[str, Any], model)
    return str(section.get("default") or ""), str(section.get("provider") or "")


def _read_meta(home: Path) -> dict[str, Any]:
    try:
        loaded = yamlio.load_safe((home / META_FILE).read_bytes())
    except (OSError, yamlio.YamlError):
        return {}
    return cast(dict[str, Any], loaded) if isinstance(loaded, dict) else {}


def _write_meta(home: Path, changes: Mapping[str, Any]) -> None:
    data = {**_read_meta(home), **changes}
    text = yamlio.dump_plain(data) if data else ""
    fsio.atomic_write_text(home / META_FILE, text, mode=0o600, preserve_mode=False)


def _skill_count(scopes: ProfileScopes, pid: str) -> int | None:
    """How many skills ``pid`` has on (plugin skills aside, as ``/api/skills``); None if unread."""
    try:
        scope = scopes.resolve(pid)
        view = config_module.load(scope)
        if view.error is not None:
            return None
        records = load_catalog(scope.with_config(view)).loadable()
    except (ProfileUnknown, IndusError, OSError):
        return None
    return sum(1 for r in records if r.plugin is None and not r.disabled)


def _row(scopes: ProfileScopes, pid: str, sticky: str) -> dict[str, Any]:
    home = profile_home(scopes.base.root, pid)
    model, provider = _model_of(home)
    return {
        "name": pid,
        "is_default": pid == DEFAULT_PROFILE,
        "is_sticky": pid == sticky,
        "is_current": pid == scopes.base.profile_id,
        "model": model or None,
        "provider": provider or None,
        "description": str(_read_meta(home).get("description") or "") or None,
        "has_persona": (home / PERSONA_FILE).is_file(),
        "skill_count": _skill_count(scopes, pid),
    }


def _masked(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return redact_text("\n".join(lines[-6:]))[:2000]


def _run(request: Request, scopes: ProfileScopes, args: Sequence[str]) -> str:
    code, output = _runner(request)(args, child_env(scopes.base))
    if code != 0:
        _fail(400, "profile_command_failed", _masked(output) or "The profile was not changed.")
    return _masked(output)


# -- list and sticky ------------------------------------------------------------------------------


@router.get("/api/profiles")
def list_all(scopes: Scopes) -> dict[str, Any]:
    root = scopes.base.root
    sticky = read_active(root)
    rows = [_row(scopes, pid, sticky) for pid in list_profiles(root)]
    rows = bots_rpc.for_root(scopes.base).profile_rows(rows)  # P24-31 canonical_session
    return {"profiles": rows, "active": sticky, "current": scopes.base.profile_id}


@router.get("/api/profiles/active")
def get_active(scopes: Scopes) -> dict[str, str]:
    return {"active": read_active(scopes.base.root), "current": scopes.base.profile_id}


class ActiveBody(BaseModel):
    name: str


@router.post("/api/profiles/active")
def set_active(body: ActiveBody, scopes: Scopes) -> dict[str, Any]:
    pid, _home = _live(scopes, body.name)
    try:
        write_active(scopes.base.root, pid)
    except ProfileNotFound:
        _fail(404, "profile_not_found", "No such profile.")
    return {"ok": True, "active": pid}


# -- create / rename / delete ---------------------------------------------------------------------


class CreateBody(BaseModel):
    name: str
    clone_from: str | None = None
    clone_all: bool = False
    description: str | None = Field(default=None, max_length=2000)
    no_skills: bool = False
    alias: bool = False


@router.post("/api/profiles", status_code=201)
def create(body: CreateBody, request: Request, scopes: Scopes) -> dict[str, Any]:
    try:
        pid = normalize_id(body.name)
    except InvalidProfileId:
        _fail(400, "bad_name", "Use 1-64 lowercase letters, digits, '-' or '_'.")
    if pid == DEFAULT_PROFILE:
        _fail(400, "bad_name", "'default' is the built-in profile; pick another name.")
    if is_live(scopes.base.root, pid):
        _fail(409, "profile_exists", "A profile with that name already exists.")
    args = ["profile", "create", pid]
    if body.clone_from:
        source, _home = _live(scopes, body.clone_from)
        args += ["--clone-from", source]
        if body.clone_all:
            args.append("--clone-all")
    elif body.clone_all:
        args.append("--clone-all")
    if body.description:
        args += ["--description", body.description]
    if body.no_skills:
        args.append("--no-skills")
    if not body.alias:
        args.append("--no-alias")
    said = _run(request, scopes, args)
    if not is_live(scopes.base.root, pid):
        _fail(500, "profile_command_failed", said or "The profile was not created.")
    return {"ok": True, "name": pid, "message": said}


class RenameBody(BaseModel):
    new_name: str


@router.patch("/api/profiles/{name}")
def rename(name: str, body: RenameBody, request: Request, scopes: Scopes) -> dict[str, Any]:
    old, _home = _live(scopes, name)
    if old == DEFAULT_PROFILE:
        _fail(400, "default_profile", "The default profile cannot be renamed.")
    try:
        new = normalize_id(body.new_name)
    except InvalidProfileId:
        _fail(400, "bad_name", "Use 1-64 lowercase letters, digits, '-' or '_'.")
    if new == old:
        return {"ok": True, "name": new, "message": ""}
    if old == scopes.base.profile_id:
        _fail(409, "profile_in_use", "This dashboard serves that profile; rename it elsewhere.")
    said = _run(request, scopes, ["profile", "rename", old, new])
    return {"ok": True, "name": new, "message": said}


@router.delete("/api/profiles/{name}")
def delete(name: str, request: Request, scopes: Scopes) -> dict[str, Any]:
    pid, _home = _live(scopes, name)
    if pid == DEFAULT_PROFILE:
        _fail(400, "default_profile", "The default profile cannot be deleted.")
    if pid == scopes.base.profile_id:
        _fail(409, "profile_in_use", "This dashboard serves that profile; it cannot delete it.")
    said = _run(request, scopes, ["profile", "delete", pid, "--yes"])
    return {"ok": True, "name": pid, "message": said}


# -- persona, description, model ------------------------------------------------------------------


@router.get("/api/profiles/{name}/persona")
def get_persona(name: str, scopes: Scopes) -> dict[str, Any]:
    _pid, home = _live(scopes, name)
    path = home / PERSONA_FILE
    try:
        return {"content": path.read_text(encoding="utf-8-sig"), "exists": True}
    except FileNotFoundError:
        return {"content": "", "exists": False}
    except (OSError, UnicodeDecodeError):
        _fail(500, "persona_unreadable", f"{PERSONA_FILE} could not be read.")


class PersonaBody(BaseModel):
    content: str = Field(max_length=PERSONA_MAX_CHARS)


@router.put("/api/profiles/{name}/persona")
def put_persona(name: str, body: PersonaBody, scopes: Scopes) -> dict[str, Any]:
    _pid, home = _live(scopes, name)
    try:
        fsio.atomic_write_text(home / PERSONA_FILE, body.content, mode=0o644)
    except OSError:
        _fail(500, "persona_unwritable", f"{PERSONA_FILE} could not be written.")
    return {"ok": True}


class DescriptionBody(BaseModel):
    description: str = Field(default="", max_length=2000)


@router.put("/api/profiles/{name}/description")
def put_description(name: str, body: DescriptionBody, scopes: Scopes) -> dict[str, Any]:
    _pid, home = _live(scopes, name)
    text = body.description.strip()
    try:
        _write_meta(home, {"description": text})
    except OSError:
        _fail(500, "meta_unwritable", f"{META_FILE} could not be written.")
    return {"ok": True, "description": text}


class ModelBody(BaseModel):
    provider: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=400)


@router.put("/api/profiles/{name}/model")
def put_model(name: str, body: ModelBody, scopes: Scopes) -> dict[str, Any]:
    pid, _home = _live(scopes, name)
    scope = _scope_for(scopes, pid)
    provider, model = body.provider.strip(), body.model.strip()
    try:
        config_module.save(scope, {"model": {"provider": provider, "default": model}})
    except ConfigError as exc:
        _fail(500, "config_unwritable", f"{exc.message}. Nothing was changed.")
    return {"ok": True, "provider": provider, "model": model}


# -- export / import ------------------------------------------------------------------------------


def _skipped(rel: PurePosixPath) -> bool:
    if not rel.parts:
        return True
    if rel.parts[0] in EXPORT_SKIP or rel.name.endswith(_SKIP_SUFFIXES):
        return True
    name = rel.name.lower()
    return name == ".env" or name.startswith(".env.")


def export_profile(home: Path, pid: str, archive: Path, say: Say) -> int:
    """Zip ``home``'s settings into ``archive`` (see the module notes); 0 when written."""
    partial = archive.with_name(f".{archive.name}.partial")
    count = 0
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as out:
        out.writestr(EXPORT_MANIFEST, json.dumps({"format": 1, "profile": pid}))
        for path in sorted(home.rglob("*")):
            rel = PurePosixPath(path.relative_to(home).as_posix())
            if _skipped(rel) or path.is_symlink() or not path.is_file():
                continue
            out.write(path, rel.as_posix())
            count += 1
    os.replace(partial, archive)
    os.chmod(archive, 0o600)
    say(f"exported {count} file(s) of profile {pid} to {archive}")
    return 0


@router.post("/api/profiles/{name}/export", status_code=202)
def export(name: str, request: Request, scopes: Scopes) -> dict[str, Any]:
    pid, home = _live(scopes, name)
    folder = home / "backups"
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    archive = folder / f"profile-{pid}-{stamp}.zip"

    def job(say: Say) -> int:
        folder.mkdir(mode=0o700, exist_ok=True)
        return export_profile(home, pid, archive, say)

    actions = actions_of(dashboard_state(request.app).extras)
    try:
        actions.start_job(_scope_for(scopes, pid), "profile-export", job)
    except ActionBusy:
        _fail(409, "action_running", "An export of this profile is already running.")
    return {"ok": True, "action": "profile-export", "profile": pid, "archive": str(archive)}


class ImportBody(BaseModel):
    archive: str = Field(min_length=1)
    name: str | None = None


def _members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    total = 0
    members: list[zipfile.ZipInfo] = []
    for info in archive.infolist():
        rel = PurePosixPath(info.filename)
        if info.filename.startswith(("/", "\\")) or rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"unsafe member {info.filename!r}")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise ValueError(f"link member {info.filename!r}")
        total += info.file_size
        if total > EXPORT_MAX_BYTES:
            raise ValueError("the archive is too large")
        if not info.is_dir():
            members.append(info)
    return members


@router.post("/api/profiles/import", status_code=201)
def import_profile(body: ImportBody, scopes: Scopes) -> dict[str, Any]:
    source = Path(body.archive)
    if not source.is_absolute() or not source.is_file():
        _fail(404, "archive_not_found", "There is no archive at that path.")
    root = scopes.base.root
    try:
        with zipfile.ZipFile(source) as archive:
            members = _members(archive)
            try:
                manifest = json.loads(archive.read(EXPORT_MANIFEST))
            except (KeyError, ValueError):
                manifest = {}
            wanted = body.name or str(cast(dict[str, Any], manifest).get("profile") or "")
            try:
                pid = normalize_id(wanted)
            except InvalidProfileId:
                _fail(400, "bad_name", "Name the new profile (1-64 lowercase letters, digits).")
            if pid == DEFAULT_PROFILE or is_live(root, pid):
                _fail(409, "profile_exists", "A profile with that name already exists.")
            target = profiles_dir(root) / pid
            if target.exists() or target.is_symlink():
                _fail(409, "folder_exists", "A folder with that name is in the way.")
            staging = profiles_dir(root) / f".{pid}.import-{os.getpid()}"
            shutil.rmtree(staging, ignore_errors=True)
            try:
                staging.mkdir(parents=True, mode=0o700)
                for info in members:
                    rel = PurePosixPath(info.filename)
                    if rel.as_posix() == EXPORT_MANIFEST or _skipped(rel):
                        continue
                    dest = staging.joinpath(*rel.parts)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as src, dest.open("wb") as out:
                        shutil.copyfileobj(src, out)
                if not any((staging / marker).is_file() for marker in IDENTITY_MARKERS):
                    (staging / META_FILE).touch(mode=0o600)
                clear_tombstone(root, pid)
                os.rename(staging, target)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
    except (zipfile.BadZipFile, ValueError) as exc:
        _fail(400, "bad_archive", f"That archive cannot be imported: {exc}")
    except IndusError as exc:
        _fail(400, "import_failed", exc.message)
    except OSError:
        _fail(500, "import_failed", "The profile could not be written.")
    return {"ok": True, "name": pid, "path": str(target)}
