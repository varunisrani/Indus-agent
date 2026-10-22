"""The System page's routes (P18-16): host stats, updates, memory provider, credential pool,
operations, action status and shell hooks. Everything is per profile (``read_scope`` /
``write_scope``).

- ``GET /api/system/stats``: CPU count and load, memory and disk of the profile's home (``psutil``
  when installed, else ``/proc`` and ``os``); coarse numbers only.
- ``GET /api/system/update?force=``: install method, version and, for a git checkout, how many
  commits it is behind its upstream; the answer is cached 24 h in ``<home>/cache/update_check.json``
  and ``force=1`` checks again. ``POST /api/system/update`` pulls (``git pull --ff-only``) as a
  background action for git installs only; any other install answers 409 with what to run instead.
- ``GET /api/memory`` / ``PUT /api/memory/provider {provider}``: the external memory provider
  (``""`` = built-in only), written through the config writer; an uninstalled name is refused.
- ``/api/credentials/pool``: list every provider's credentials with a masked preview, never a value;
  add ``{provider, api_key, label?}`` (kept in the secret store, metadata in ``auth.json``);
  ``DELETE …/{provider}/{index}`` (1-based, as listed). An env-held key is hidden from the pool
  rather than deleted, as ``indus auth remove`` does.
- Operations, each a background action (``web.actions``): doctor, backup (+ download of an
  archive it wrote, confined to ``<home>/backups``: with the session header, or — for a link the
  browser opens itself — ``POST /api/ops/backup/download-ticket {archive}`` then
  ``GET /api/backups/download?archive=&ticket=``, a ticket route bound to that one archive in the
  same single-use book as file downloads), import of an archive path or an uploaded zip,
  checkpoint prune (+ a size listing). ``GET /api/actions/{name}/status?lines=`` tails one.
- ``GET /api/ops/prompt-size?session_id=&platform=``: how large the profile's (or one stored
  session's) system prompt and tool schemas are, per P04 tier and per part, in characters and
  estimated tokens (``service.prompt_size``); built locally, no model call, so answered at once.
- ``/api/ops/hooks``: configured shell hooks with their approval; create needs ``consent: true``
  (it also records the approval, so the hook runs) and remove drops the entry and its approval.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final, NoReturn, cast

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

import indus
from indus.foundation import config as config_module
from indus.foundation import fsio
from indus.foundation.errors import ConfigError, IndusError
from indus.foundation.redact import mask
from indus.foundation.scope import Scope
from indus.foundation.secrets import open_secret_store
from indus.plugins import shell_hooks as sh
from indus.providers.auth.pool_model import (
    Pool,
    PoolEntry,
    PoolTargetError,
    SecretWriter,
    edit_pool,
    env_entries,
    load_pool,
    new_entry_id,
    secret_name,
    suppress_env,
    unsuppress,
)
from indus.providers.auth.store import AuthStore
from indus.providers.profile import ProviderProfile
from indus.providers.registry import Registry
from indus.providers.route import is_placeholder
from indus.safety.child_env import child_env
from indus.service import prompt_size
from indus.state import sessions as session_rows
from indus.web.actions import ActionBusy, Actions, Say, actions_of
from indus.web.auth.download_ticket import DownloadRefused
from indus.web.routes.files import download_tickets
from indus.web.routes.sessions_store import reading
from indus.web.scope import read_scope, write_scope
from indus.web.state import dashboard_state

__all__ = ["ACTION_NAMES", "ROUTE_LIST", "add_pool_secret", "provider_profile", "router"]

log = logging.getLogger(__name__)

router = APIRouter()

ROUTE_LIST: Final[tuple[tuple[str, str, bool], ...]] = (
    ("GET", "/api/system/stats", True),
    ("GET", "/api/system/update", True),
    ("POST", "/api/system/update", True),
    ("GET", "/api/memory", True),
    ("PUT", "/api/memory/provider", True),
    ("GET", "/api/credentials/pool", True),
    ("POST", "/api/credentials/pool", True),
    ("DELETE", "/api/credentials/pool/{provider}/{index}", True),
    ("POST", "/api/ops/doctor", True),
    ("POST", "/api/ops/backup", True),
    ("GET", "/api/ops/backup/download", True),
    ("POST", "/api/ops/backup/download-ticket", True),
    ("GET", "/api/backups/download", False),
    ("POST", "/api/ops/import", True),
    ("POST", "/api/ops/import-upload", True),
    ("GET", "/api/ops/checkpoints", True),
    ("POST", "/api/ops/checkpoints/prune", True),
    ("GET", "/api/ops/prompt-size", True),
    ("GET", "/api/ops/hooks", True),
    ("POST", "/api/ops/hooks", True),
    ("DELETE", "/api/ops/hooks", True),
    ("GET", "/api/actions/{name}/status", True),
)
"""``(method, path, scoped)`` of every route here."""

ACTION_NAMES: Final = frozenset(
    {
        "doctor",
        "backup",
        "import",
        "checkpoints-prune",
        "update",
        "daemon-start",
        "daemon-stop",
        "daemon-restart",
        "profile-export",
    }
)
"""Every action a status may be asked for (others are 404)."""

UPDATE_CACHE: Final = Path("cache") / "update_check.json"
UPDATE_TTL_S: Final = 24 * 3600.0
GIT_TIMEOUT_S: Final = 30.0
MAX_IMPORT_BYTES: Final = 4 * 1024 * 1024 * 1024
_CHUNK: Final = 1024 * 1024
_ARCHIVE_NAME: Final = re.compile(r"[^A-Za-z0-9._-]+")

ReadScope = Annotated[Scope, Depends(read_scope)]
WriteScope = Annotated[Scope, Depends(write_scope)]


def _fail(status: int, code: str, message: str, **extra: Any) -> NoReturn:
    raise HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


def _actions(request: Request) -> Actions:
    return actions_of(dashboard_state(request.app).extras)


def _extra(request: Request, key: str) -> Any:
    return dashboard_state(request.app).extras.get(key)


def _started(name: str, scope: Scope) -> dict[str, Any]:
    return {"ok": True, "action": name, "profile": scope.profile_id}


def _start_command(request: Request, scope: Scope, name: str, args: list[str]) -> dict[str, Any]:
    try:
        _actions(request).start_command(scope, name, args)
    except ActionBusy:
        _fail(409, "action_running", f"{name} is already running for this profile.")
    except IndusError as exc:
        _fail(404, "profile_not_found", exc.message)
    return _started(name, scope)


def _start_job(
    request: Request, scope: Scope, name: str, job: Callable[[Say], int]
) -> dict[str, Any]:
    try:
        _actions(request).start_job(scope, name, job)
    except ActionBusy:
        _fail(409, "action_running", f"{name} is already running for this profile.")
    except IndusError as exc:
        _fail(404, "profile_not_found", exc.message)
    return _started(name, scope)


# -- host stats -----------------------------------------------------------------------------------


def _meminfo() -> dict[str, int]:
    found: dict[str, int] = {}
    with contextlib.suppress(OSError, ValueError):
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                found[key] = int(rest.split()[0]) * 1024
    return found


def host_stats(home: Path) -> dict[str, Any]:
    """CPU, memory and disk numbers (``None`` where this host does not say)."""
    stats: dict[str, Any] = {"cpu_count": os.cpu_count(), "load": None, "memory": None}
    with contextlib.suppress(OSError, AttributeError):
        stats["load"] = [round(v, 2) for v in os.getloadavg()]
    if importlib.util.find_spec("psutil") is not None:
        with contextlib.suppress(Exception):
            psutil: Any = importlib.import_module("psutil")
            vm = psutil.virtual_memory()
            stats["memory"] = {"total": int(vm.total), "available": int(vm.available)}
            stats["cpu_percent"] = float(psutil.cpu_percent(interval=None))
    if stats["memory"] is None:
        info = _meminfo()
        if "MemTotal" in info:
            stats["memory"] = {"total": info["MemTotal"], "available": info.get("MemAvailable")}
    try:
        usage = shutil.disk_usage(home)
        stats["disk"] = {"total": usage.total, "free": usage.free, "used": usage.used}
    except OSError:
        stats["disk"] = None
    return stats


@router.get("/api/system/stats")
def get_stats(scope: ReadScope) -> dict[str, Any]:
    return host_stats(scope.home)


# -- updates --------------------------------------------------------------------------------------

_UPDATE_HINTS: Final = {
    "docker": "Pull the newer image and recreate the container.",
    "nix": "Update the Indus Agent package through Nix.",
    "package": "Upgrade the installed package with the tool that installed it.",
    "dev": "This is a source tree without git; update it by hand.",
}


def install_info() -> tuple[str, Path]:
    """``(method, install dir)`` from the stdlib-only version module (``cli`` is a sibling
    layer, so it is loaded by name)."""
    module: Any = importlib.import_module("indus.cli.version")
    return str(module.install_method()), Path(module.install_dir())


def git_env(scope: Scope) -> dict[str, str]:
    """The environment for the dashboard's git calls (SEC-8): the scope's child env.

    ``child_env`` keeps what git needs to reach the remote (``PATH``, ``HOME`` for
    ``~/.gitconfig`` and ``~/.ssh``, ``SSH_AUTH_SOCK``, the proxy variables, ``GIT_*``) and
    drops provider keys and every ``INDUS_`` setting of the dashboard process.
    """
    return child_env(scope)


def git_behind(folder: Path, *, env: Mapping[str, str]) -> int | None:
    """Commits the checkout at ``folder`` is behind its upstream (None when unknown)."""
    try:
        fetched = subprocess.run(  # noqa: S603 - fixed git argv
            ["git", "-C", str(folder), "fetch", "--quiet"],
            capture_output=True,
            env=dict(env),
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
        if fetched.returncode != 0:
            return None
        counted = subprocess.run(  # noqa: S603 - fixed git argv
            ["git", "-C", str(folder), "rev-list", "--count", "HEAD..@{u}"],
            capture_output=True,
            env=dict(env),
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if counted.returncode != 0:
        return None
    try:
        return int(counted.stdout.strip())
    except ValueError:
        return None


def _probe(request: Request, scope: Scope) -> Callable[[Path], int | None]:
    found = _extra(request, "system.git_behind")
    if callable(found):
        return cast(Callable[[Path], int | None], found)
    env = git_env(scope)
    return lambda folder: git_behind(folder, env=env)


def _install(request: Request) -> tuple[str, Path]:
    found = _extra(request, "system.install_info")
    return cast(tuple[str, Path], found()) if callable(found) else install_info()


def _read_cache(path: Path, now: float) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    checked = cast(dict[str, Any], data).get("checked_at")
    if not isinstance(checked, int | float) or not 0 <= now - checked < UPDATE_TTL_S:
        return None
    return cast(dict[str, Any], data)


@router.get("/api/system/update")
def check_update(request: Request, scope: ReadScope, force: bool = False) -> dict[str, Any]:
    method, folder = _install(request)
    payload: dict[str, Any] = {
        "install_method": method,
        "current_version": indus.__version__,
        "can_apply": method == "git",
        "behind": None,
        "update_available": False,
        "message": _UPDATE_HINTS.get(method),
        "checked_at": None,
        "cached": False,
    }
    if method != "git":
        return payload
    cache = scope.home / UPDATE_CACHE
    now = time.time()
    cached = None if force else _read_cache(cache, now)
    if cached is not None:
        behind = cached.get("behind")
        payload.update(behind=behind, checked_at=cached.get("checked_at"), cached=True)
    else:
        behind = _probe(request, scope)(folder)
        payload.update(behind=behind, checked_at=now)
        with contextlib.suppress(OSError):
            cache.parent.mkdir(parents=True, exist_ok=True)
            fsio.atomic_write_json(cache, {"behind": behind, "checked_at": now}, mode=0o600)
    if isinstance(behind, int) and behind > 0:
        payload["update_available"] = True
        payload["message"] = f"{behind} commit(s) behind."
    elif behind == 0:
        payload["message"] = "Up to date."
    else:
        payload["message"] = "The update source could not be reached; try again later."
    return payload


@router.post("/api/system/update", status_code=202)
def apply_update(request: Request, scope: WriteScope) -> dict[str, Any]:
    method, folder = _install(request)
    if method != "git":
        _fail(
            409,
            "update_not_in_place",
            "This install is not a git checkout; the dashboard cannot update it.",
            install_method=method,
            hint=_UPDATE_HINTS.get(method, ""),
        )

    env = git_env(scope)

    def job(say: Say) -> int:
        say(f"git pull --ff-only in {folder}")
        try:
            done = subprocess.run(  # noqa: S603 - fixed git argv
                ["git", "-C", str(folder), "pull", "--ff-only"],
                capture_output=True,
                env=env,
                text=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            say(f"git could not run: {exc}")
            return 1
        for line in (done.stdout + done.stderr).splitlines():
            say(line)
        (scope.home / UPDATE_CACHE).unlink(missing_ok=True)
        say("Restart the dashboard and the daemon to run the new version.")
        return done.returncode

    return _start_job(request, scope, "update", job)


# -- memory provider ------------------------------------------------------------------------------


def _memory_providers() -> list[str]:
    from indus.learning.memory import providers

    return sorted(providers.installed_providers())


def _memory_files(home: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    folder = home / "memories"
    for name in ("MEMORY.md", "USER.md"):
        try:
            out[name] = (folder / name).stat().st_size
        except OSError:
            out[name] = 0
    return out


@router.get("/api/memory")
def get_memory(scope: ReadScope) -> dict[str, Any]:
    view = config_module.load(scope)
    active = str(view.get("memory.provider") or "")
    return {
        "active": active,
        "providers": _memory_providers(),
        "builtin_files": _memory_files(scope.home),
    }


class MemoryBody(BaseModel):
    provider: str = Field(default="", max_length=100)


@router.put("/api/memory/provider")
def set_memory(body: MemoryBody, scope: WriteScope) -> dict[str, Any]:
    from indus.learning.memory import providers

    name = body.provider.strip().lower()
    if name and not providers.is_core_provider(name) and name not in _memory_providers():
        _fail(400, "unknown_memory_provider", "That memory provider is not installed.")
    stored = "" if providers.is_core_provider(name) else name
    try:
        config_module.save(scope, {"memory": {"provider": stored}})
    except ConfigError as exc:
        _fail(500, "config_unwritable", f"{exc.message}. Nothing was changed.")
    return {"ok": True, "active": stored}


# -- credential pool ------------------------------------------------------------------------------


def provider_profile(scope: Scope, name: str) -> ProviderProfile:
    found = Registry(scope).lookup(name.strip().lower())
    if found is None:
        _fail(404, "unknown_provider", "No such provider.")
    return found


def _writer(scope: Scope) -> SecretWriter:
    current = scope.secrets
    if callable(getattr(current, "set", None)) and callable(getattr(current, "delete", None)):
        return cast(SecretWriter, current)
    mode = str(config_module.load(scope).get("security.secret_store") or "auto")
    return open_secret_store(scope.home, scope.profile_id, mode=mode)


def _env_rows(scope: Scope, profile: ProviderProfile) -> list[PoolEntry]:
    try:
        return env_entries(profile, scope.secrets, scope.env)
    except IndusError:
        return env_entries(profile, None, scope.env)


def _preview(scope: Scope, provider: str, entry: PoolEntry) -> str:
    try:
        if entry.source == "env" and entry.env_var:
            value = (scope.secrets.get(entry.env_var) if scope.secrets else None) or scope.env.get(
                entry.env_var
            )
        else:
            value = scope.secrets.get(secret_name(provider, entry.id)) if scope.secrets else None
    except Exception:
        value = None
    if not value:
        return ""
    if entry.auth_type == "oauth":
        return "(signed in)"
    return mask(value)


_SHOWN_FIELDS: Final = (
    "id",
    "label",
    "status",
    "auth_type",
    "source",
    "env_var",
    "expires_at",
    "priority",
    "request_count",
)
"""The pool entry fields a listing shows (metadata only; the value is never one of them)."""


def _entry_row(scope: Scope, provider: str, index: int, entry: PoolEntry) -> dict[str, Any]:
    shown = {name: getattr(entry, name) for name in _SHOWN_FIELDS}
    return {"index": index, **shown, "preview": _preview(scope, provider, entry)}


@router.get("/api/credentials/pool")
def list_pool(scope: ReadScope) -> dict[str, Any]:
    store = AuthStore(scope.home, env=scope.env)
    rows: list[dict[str, Any]] = []
    for profile in Registry(scope).list_providers():
        try:
            pool, inherited = load_pool(store, profile.name, _env_rows(scope, profile))
        except IndusError:
            continue
        if not pool.entries:
            continue
        rows.append(
            {
                "provider": profile.name,
                "label": profile.display_name or profile.name,
                "inherited": inherited,
                "entries": [
                    _entry_row(scope, profile.name, i, e)
                    for i, e in enumerate(pool.entries, start=1)
                ],
            }
        )
    return {"providers": rows}


def add_pool_secret(
    scope: Scope, profile: ProviderProfile, secret: str, entry: PoolEntry
) -> tuple[int, PoolEntry]:
    """Keep ``secret`` in the store, then add ``entry`` to the pool (the secret is dropped again
    when the pool write fails)."""
    writer = _writer(scope)
    name = secret_name(profile.name, entry.id)
    writer.set(name, secret)
    store = AuthStore(scope.home, env=scope.env)

    def change(pool: Pool, doc: dict[str, Any]) -> tuple[int, PoolEntry]:
        unsuppress(doc, profile.name)
        pool.add(entry)
        return pool.resolve(entry.id)

    try:
        return edit_pool(store, profile.name, change, _env_rows(scope, profile))
    except BaseException:
        writer.delete(name)
        raise


class PoolAddBody(BaseModel):
    provider: str = Field(min_length=1, max_length=100)
    api_key: str = Field(min_length=1, max_length=8192)
    label: str | None = Field(default=None, max_length=100)


@router.post("/api/credentials/pool", status_code=201)
def add_pool(body: PoolAddBody, scope: WriteScope) -> dict[str, Any]:
    profile = provider_profile(scope, body.provider)
    value = body.api_key.strip()
    if is_placeholder(value):
        _fail(400, "placeholder_key", "That does not look like a real key; nothing was saved.")
    store = AuthStore(scope.home, env=scope.env)
    pool, inherited = load_pool(store, profile.name, _env_rows(scope, profile))
    if inherited and pool.entries:
        _fail(409, "inherited_pool", "These credentials belong to the default profile.")
    entry = PoolEntry(
        id=new_entry_id(pool.ids()),
        label=(body.label or "").strip() or f"api-key-{len(pool) + 1}",
        auth_type="api_key",
        source="manual",
    )
    try:
        index, saved = add_pool_secret(scope, profile, value, entry)
    except IndusError as exc:
        _fail(500, "pool_unwritable", exc.message)
    return {"ok": True, "provider": profile.name, "index": index, "id": saved.id}


@router.delete("/api/credentials/pool/{provider}/{index}")
def remove_pool(provider: str, index: int, scope: WriteScope) -> dict[str, Any]:
    profile = provider_profile(scope, provider)
    store = AuthStore(scope.home, env=scope.env)
    pool, inherited = load_pool(store, profile.name, _env_rows(scope, profile))
    if inherited and pool.entries:
        _fail(409, "inherited_pool", "These credentials belong to the default profile.")
    if not 1 <= index <= len(pool.entries):
        _fail(404, "no_such_entry", "There is no credential at that index.")

    def change(current: Pool, doc: dict[str, Any]) -> tuple[int, PoolEntry]:
        at, entry = current.remove(index)
        if entry.source == "env" and entry.env_var:
            suppress_env(doc, profile.name, entry.env_var)
        return at, entry

    try:
        _at, removed = edit_pool(store, profile.name, change, _env_rows(scope, profile))
    except PoolTargetError:
        _fail(404, "no_such_entry", "There is no credential at that index.")
    except IndusError as exc:
        _fail(500, "pool_unwritable", exc.message)
    if removed.stores_secret:
        with contextlib.suppress(IndusError):
            _writer(scope).delete(secret_name(profile.name, removed.id))
    return {
        "ok": True,
        "provider": profile.name,
        "removed": removed.label,
        "env_var_kept": removed.env_var if removed.source == "env" else None,
    }


# -- operations -----------------------------------------------------------------------------------


@router.post("/api/ops/doctor", status_code=202)
def run_doctor(request: Request, scope: WriteScope) -> dict[str, Any]:
    return _start_command(request, scope, "doctor", ["doctor"])


def _backups(scope: Scope) -> Path:
    return scope.home / "backups"


@router.post("/api/ops/backup", status_code=202)
def run_backup(request: Request, scope: WriteScope) -> dict[str, Any]:
    folder = _backups(scope)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    archive = folder / f"indus-backup-{stamp}.zip"
    try:
        folder.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        _fail(500, "backup_folder", "The backups folder could not be created.")
    answer = _start_command(request, scope, "backup", ["backup", "-o", str(archive)])
    return {**answer, "archive": str(archive)}


BACKUP_TICKET_PREFIX: Final = "backup:"
"""What a backup ticket is bound to: this prefix and the archive's resolved path, so it can never
be redeemed for a file (``/api/files/download`` compares a bare path), nor a file ticket here."""


def _backup_archive(scope: Scope, archive: str) -> Path:
    folder = _backups(scope).resolve(strict=False)
    try:
        target = Path(archive).resolve(strict=True)
    except (OSError, RuntimeError):
        _fail(404, "not_found", "There is no such backup.")
    if folder not in target.parents or target.suffix != ".zip" or not target.is_file():
        _fail(403, "outside_backups", "Only archives in this profile's backups folder.")
    return target


def _archive_response(target: Path) -> FileResponse:
    return FileResponse(
        path=str(target),
        media_type="application/zip",
        filename=target.name,
        content_disposition_type="attachment",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/ops/backup/download")
def download_backup(archive: str, scope: ReadScope) -> FileResponse:
    return _archive_response(_backup_archive(scope, archive))


class ArchiveBody(BaseModel):
    archive: str = Field(min_length=1)


@router.post("/api/ops/backup/download-ticket")
def mint_backup_ticket(request: Request, body: ArchiveBody, scope: WriteScope) -> JSONResponse:
    target = _backup_archive(scope, body.archive)
    try:
        minted = download_tickets(request).mint(BACKUP_TICKET_PREFIX + str(target))
    except DownloadRefused:
        _fail(429, "too_many_downloads", "Too many downloads are waiting; try again.")
    return JSONResponse(
        {"ticket": minted.ticket, "expires_in": minted.expires_in},
        headers={"Cache-Control": "no-store"},
    )


def _ticket_refused() -> NoReturn:
    _fail(401, "download_ticket_invalid", "That download link is not valid; open it again.")


@router.get("/api/backups/download")
def download_backup_by_ticket(request: Request, archive: str = "", ticket: str = "") -> Any:
    # A ticket route (the gate lets it through without the session token): the ticket is spent
    # and checked before the archive is looked at, so a caller without one learns nothing.
    try:
        bound = download_tickets(request).redeem(ticket)
    except DownloadRefused as refused:
        log.info("backup download ticket refused (%s)", refused.reason)
        _ticket_refused()
    if not bound.startswith(BACKUP_TICKET_PREFIX):
        log.info("backup download ticket refused (not a backup ticket)")
        _ticket_refused()
    try:
        target = Path(archive).resolve(strict=True)
    except (OSError, RuntimeError):
        _ticket_refused()
    # The path the ticket was minted for (inside that profile's backups folder, checked then);
    # resolving again catches a link swapped in since.
    if str(target) != bound.removeprefix(BACKUP_TICKET_PREFIX) or not target.is_file():
        log.info("backup download ticket refused (another archive)")
        _ticket_refused()
    return _archive_response(target)


class ImportBody(BaseModel):
    archive: str = Field(min_length=1)
    force: bool = False


def _import(request: Request, scope: Scope, archive: Path, force: bool) -> dict[str, Any]:
    args = ["import", str(archive), *(["--force"] if force else [])]
    return {**_start_command(request, scope, "import", args), "archive": str(archive)}


@router.post("/api/ops/import", status_code=202)
def run_import(body: ImportBody, request: Request, scope: WriteScope) -> dict[str, Any]:
    archive = Path(body.archive)
    if not archive.is_absolute() or not archive.is_file():
        _fail(404, "archive_not_found", "There is no archive at that path.")
    if not zipfile.is_zipfile(archive):
        _fail(400, "not_a_zip", "That file is not a zip archive.")
    return _import(request, scope, archive, body.force)


@router.post("/api/ops/import-upload", status_code=202)
async def run_import_upload(
    request: Request,
    scope: WriteScope,
    file: Annotated[UploadFile, File()],
    force: Annotated[bool, Form()] = False,
) -> dict[str, Any]:
    folder = _backups(scope)
    try:
        folder.mkdir(mode=0o700, exist_ok=True)
        stem = _ARCHIVE_NAME.sub("-", Path(file.filename or "upload").stem).strip(".-")[:60]
        fd, name = tempfile.mkstemp(prefix=f"import-{stem or 'upload'}-", suffix=".zip", dir=folder)
    except OSError:
        _fail(500, "backup_folder", "The upload could not be stored.")
    target = Path(name)
    kept = False
    try:
        total = 0
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(_CHUNK):
                total += len(chunk)
                if total > MAX_IMPORT_BYTES:
                    _fail(413, "too_large", "That archive is too large.")
                out.write(chunk)
        if not zipfile.is_zipfile(target):
            _fail(400, "not_a_zip", "That file is not a zip archive.")
        kept = True
    finally:
        if not kept:
            target.unlink(missing_ok=True)
        await file.close()
    return _import(request, scope, target, force)


@router.get("/api/ops/checkpoints")
def list_checkpoints(scope: ReadScope) -> dict[str, Any]:
    from indus.backends import checkpoint_api

    try:
        store = checkpoint_api.store_for(scope)
        if store is None:
            return {"projects": [], "total_bytes": 0}
        projects = store.list_projects()
        total = store.size()
    except Exception:
        _fail(500, "checkpoints_unreadable", "The checkpoint store could not be read.")
    return {
        "projects": [
            {
                "key": p.key,
                "workdir": p.workdir,
                "commits": p.commits,
                "bytes": p.size_bytes,
                "last_touch": p.last_touch,
                "exists": p.exists,
            }
            for p in projects
        ],
        "total_bytes": total,
    }


class PruneBody(BaseModel):
    retention_days: int = Field(default=7, ge=0, le=3650)
    max_size_mb: int = Field(default=500, ge=0, le=1_000_000)


@router.post("/api/ops/checkpoints/prune", status_code=202)
def prune_checkpoints(request: Request, scope: WriteScope, body: PruneBody | None = None) -> Any:
    rules = body or PruneBody()
    args = [
        "checkpoints",
        "prune",
        "--retention-days",
        str(rules.retention_days),
        "--max-size-mb",
        str(rules.max_size_mb),
        "--force",
    ]
    return _start_command(request, scope, "checkpoints-prune", args)


_PLATFORM_NAME: Final = re.compile(r"[a-z][a-z0-9_-]{0,31}")


@router.get("/api/ops/prompt-size")
def get_prompt_size(
    scope: ReadScope, session_id: str | None = None, platform: str | None = None
) -> dict[str, Any]:
    wanted = (platform or "").strip().lower() or None
    if wanted is not None and not _PLATFORM_NAME.fullmatch(wanted):
        _fail(400, "bad_platform", "That is not a platform name.")
    facts: prompt_size.SessionFacts | None = None
    if session_id:
        with reading(scope) as view:
            row = session_rows.get(view.as_handle(), session_id) if view is not None else None
        if row is None:
            _fail(404, "session_not_found", "No such session.")
        facts = prompt_size.session_facts(row)
    try:
        measured = asyncio.run(prompt_size.measure(scope, platform=wanted, session=facts))
    except Exception as exc:
        log.warning("prompt-size breakdown failed: %s", type(exc).__name__)
        _fail(500, "prompt_size_failed", "The prompt could not be built for measuring.")
    return measured.to_json()


@router.get("/api/actions/{name}/status")
def action_status(name: str, request: Request, scope: ReadScope, lines: int = 200) -> Any:
    if name not in ACTION_NAMES:
        _fail(404, "unknown_action", "No such action.")
    return _actions(request).status(scope, name, lines)


# -- shell hooks ----------------------------------------------------------------------------------


def _hooks_tree(scope: Scope) -> dict[str, Any]:
    raw = config_module.load(scope).user_raw().get("hooks")
    if not isinstance(raw, dict):
        return {}
    return {str(k): list(cast(list[Any], v)) for k, v in raw.items() if isinstance(v, list)}


@router.get("/api/ops/hooks")
def list_hooks(scope: ReadScope) -> dict[str, Any]:
    specs, warnings = sh.configured_hooks(scope.with_config(config_module.load(scope)))
    consent = sh.ConsentStore(scope.home)
    rows: list[dict[str, Any]] = []
    for spec in specs:
        grant = consent.granted(spec)
        path = sh.script_path(spec.command)
        rows.append(
            {
                "event": spec.event,
                "command": spec.command,
                "matcher": spec.matcher,
                "timeout": spec.timeout_s,
                "approved": grant is not None,
                "grant_id": spec.grant_id,
                "executable": path is not None and os.access(path, os.X_OK),
            }
        )
    return {"hooks": rows, "events": list(sh.EVENTS), "warnings": warnings}


class HookBody(BaseModel):
    event: str = Field(min_length=1, max_length=64)
    command: str = Field(min_length=1, max_length=4096)
    matcher: str | None = Field(default=None, max_length=512)
    timeout: float | None = Field(default=None, gt=0, le=sh.MAX_TIMEOUT_S)
    consent: bool = False


def _hook_fields(event: str, command: str) -> tuple[str, str]:
    event, command = event.strip(), command.strip()
    if event not in sh.EVENTS:
        _fail(400, "unknown_event", f"Unknown event; one of {', '.join(sh.EVENTS)}.")
    if not command:
        _fail(400, "command_required", "Name the command to run.")
    return event, command


@router.post("/api/ops/hooks", status_code=201)
def create_hook(body: HookBody, scope: WriteScope) -> dict[str, Any]:
    if not body.consent:
        _fail(
            400,
            "consent_required",
            "A shell hook runs your command on agent events; confirm with consent: true.",
        )
    event, command = _hook_fields(body.event, body.command)
    hooks = _hooks_tree(scope)
    entry: dict[str, Any] = {"command": command}
    if body.matcher:
        entry["matcher"] = body.matcher
    if body.timeout is not None:
        entry["timeout"] = body.timeout
    current = hooks.get(event, [])
    if any(isinstance(e, dict) and e.get("command") == command for e in current):
        _fail(409, "hook_exists", "That command is already a hook for this event.")
    hooks[event] = [*current, entry]
    try:
        config_module.save(scope, {"hooks": hooks})
    except ConfigError as exc:
        _fail(500, "config_unwritable", f"{exc.message}. Nothing was changed.")
    grant = sh.ConsentStore(scope.home).grant(sh.HookSpec(event=event, command=command))
    return {"ok": True, "event": event, "command": command, "approved": True, "grant_id": grant.id}


class HookRef(BaseModel):
    event: str = Field(min_length=1, max_length=64)
    command: str = Field(min_length=1, max_length=4096)


@router.delete("/api/ops/hooks")
def delete_hook(body: HookRef, scope: WriteScope) -> dict[str, Any]:
    event, command = _hook_fields(body.event, body.command)
    hooks = _hooks_tree(scope)
    current = hooks.get(event, [])
    kept = [e for e in current if not (isinstance(e, dict) and e.get("command") == command)]
    if len(kept) == len(current):
        _fail(404, "no_such_hook", "No such hook.")
    hooks[event] = kept
    try:
        config_module.save(scope, {"hooks": hooks}, replace=False)
    except ConfigError as exc:
        _fail(500, "config_unwritable", f"{exc.message}. Nothing was changed.")
    with contextlib.suppress(IndusError, OSError):
        sh.ConsentStore(scope.home).revoke(sh.HookSpec(event=event, command=command).grant_id)
    return {"ok": True}
