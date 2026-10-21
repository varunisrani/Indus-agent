"""``/api/files``: the dashboard's Files page (P18-16).

Machine-level, not per profile: the routes browse the host's file system, so they take no
``?profile=``. Where they may reach:

- ``INDUS_DASHBOARD_FILES_ROOT`` set (read from the backend's own scope): a locked root. Every path
  is confined to it; a relative path is taken under it and an empty path, ``.`` or ``/`` is the root
  itself.
- unset: browsing starts at the user's home and every path must be absolute.

Either way a path holding a ``..`` part is refused (400), the path is resolved with symlinks
followed and, under a locked root, must still lie inside it (403: a link pointing out of the root
does not escape). Secret-holding names (``.env*``, ``auth.json``, the secret store files,
``config.yaml``, pairing and token folders, SSH/GnuPG/cloud key folders) are never listed, read,
downloaded, written or deleted.

Routes: list ``GET /api/files?path=``, text preview ``GET /api/files/read?path=`` (512 KiB, else
``text: null``), ``POST /api/files/download-ticket {path}`` → ``{ticket, expires_in}`` then
``GET /api/files/download?path=&ticket=`` (an attachment, opened in a new tab: it carries a
single-use, 30-second ticket bound to that resolved path instead of any credential,
``auth.download_ticket``; a bad, spent, expired or other-path ticket is one 401
``download_ticket_invalid``), ``POST /api/files/mkdir
{path}``, multipart ``POST /api/files/upload`` (``path``, ``file``, optional ``overwrite``;
256 MiB, streamed to a hidden temp file beside the target and renamed only when complete) and
``DELETE /api/files?path=&recursive=``.
"""

from __future__ import annotations

import contextlib
import logging
import mimetypes
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final, NoReturn

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from indus.web.auth.download_ticket import DownloadRefused, DownloadTickets
from indus.web.scope import scopes_of
from indus.web.state import dashboard_state

__all__ = [
    "DOWNLOADS_KEY",
    "FILES_ROOT_ENV",
    "MAX_FILE_BYTES",
    "PREVIEW_BYTES",
    "ROUTE_LIST",
    "FilesPolicy",
    "download_tickets",
    "is_sensitive",
    "router",
]

log = logging.getLogger(__name__)

router = APIRouter()

ROUTE_LIST: Final[tuple[tuple[str, str, bool], ...]] = (
    ("GET", "/api/files", False),
    ("DELETE", "/api/files", False),
    ("GET", "/api/files/read", False),
    ("GET", "/api/files/download", False),
    ("POST", "/api/files/download-ticket", False),
    ("POST", "/api/files/mkdir", False),
    ("POST", "/api/files/upload", False),
)
"""``(method, path, scoped)`` of every route here (machine-level: none takes a profile)."""

FILES_ROOT_ENV: Final = "INDUS_DASHBOARD_FILES_ROOT"
DOWNLOADS_KEY: Final = "files.download_tickets"
"""``DashboardState.extras`` key of this process's ``DownloadTickets`` (made on first use)."""
MAX_FILE_BYTES: Final = 256 * 1024 * 1024
PREVIEW_BYTES: Final = 512 * 1024
_CHUNK: Final = 1024 * 1024

_SECRET_FILES: Final = frozenset(
    {
        ".envrc",
        ".git-credentials",
        ".netrc",
        ".pgpass",
        "auth.json",
        "auth.json.lock",
        "config.yaml",
        "credentials",
        "credentials.json",
        "secrets.json",
        "secrets.index.json",
        "dashboard.json",
        "serve.json",
        "gateway.sock.path",
    }
)
_SECRET_DIRS: Final = frozenset({".ssh", ".gnupg", ".aws", "pairing", "mcp-tokens", "mcp_auth"})


def is_sensitive(path: Path) -> bool:
    """A name (or a folder on the way) that holds secrets (case-insensitive)."""
    name = path.name.lower()
    if name == ".env" or name.startswith(".env."):
        return True
    if name in _SECRET_FILES:
        return True
    return any(part.lower() in _SECRET_DIRS for part in path.parts)


def _fail(status: int, code: str, message: str) -> NoReturn:
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


@dataclass(frozen=True)
class FilesPolicy:
    locked_root: Path | None
    start: Path
    user_home: str = ""

    def under_root(self, path: Path) -> bool:
        root = self.locked_root
        return root is None or path == root or root in path.parents


def policy_of(request: Request) -> FilesPolicy:
    """The root rules for this backend (its own scope's env)."""
    env = scopes_of(request).base.env
    raw = (env.get(FILES_ROOT_ENV) or "").strip()
    home = (env.get("HOME") or "").strip()
    if raw:
        root = _expand(raw, home)
        if not root.is_absolute():
            _fail(500, "files_root_invalid", f"{FILES_ROOT_ENV} must be an absolute path.")
        try:
            resolved = root.resolve(strict=True)
        except OSError:
            _fail(500, "files_root_missing", f"The folder named by {FILES_ROOT_ENV} is missing.")
        if not resolved.is_dir():
            _fail(500, "files_root_invalid", f"{FILES_ROOT_ENV} is not a folder.")
        return FilesPolicy(resolved, resolved, home)
    start = Path(home) if home and Path(home).is_absolute() else Path("/")
    return FilesPolicy(None, start, home)


def _expand(text: str, home: str) -> Path:
    """``~`` / ``~/x`` against the backend's own ``HOME`` (never the process env)."""
    if text == "~" or text.startswith("~/"):
        if not home:
            _fail(400, "path_not_absolute", "Use an absolute path.")
        return Path(home) / text[2:]
    return Path(text)


def _canonical(candidate: Path, *, for_write: bool) -> Path:
    """``candidate`` with symlinks resolved; a write target that does not exist yet keeps its
    own name under its resolved parent."""
    try:
        if for_write and not os.path.lexists(candidate):
            return candidate.parent.resolve(strict=False) / candidate.name
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        _fail(400, "bad_path", "That path cannot be resolved.")


def resolve_path(policy: FilesPolicy, raw: str | None, *, for_write: bool = False) -> Path:
    """The real path ``raw`` names under ``policy`` (400 / 403 when it may not be used)."""
    text = (raw or "").strip()
    if "\x00" in text:
        _fail(400, "bad_path", "A path cannot contain a NUL character.")
    root = policy.locked_root
    if root is not None and text in ("", ".", "/"):
        candidate = root
    elif not text:
        candidate = policy.start
    else:
        given = Path(text) if root is not None else _expand(text, policy.user_home)
        if ".." in given.parts:
            _fail(400, "path_traversal", "A path cannot contain '..'.")
        if not given.is_absolute():
            if root is None:
                _fail(400, "path_not_absolute", "Use an absolute path.")
            given = root / given
        candidate = given
    resolved = _canonical(candidate, for_write=for_write)
    if not policy.under_root(resolved):
        _fail(403, "outside_root", "That path is outside the folder this dashboard may browse.")
    if is_sensitive(resolved) or is_sensitive(candidate):
        _fail(403, "sensitive_path", "Files that hold secrets are not available here.")
    return resolved


def _meta(policy: FilesPolicy) -> dict[str, Any]:
    root = str(policy.locked_root) if policy.locked_root is not None else None
    return {"locked_root": root}


def _entry(policy: FilesPolicy, path: Path) -> dict[str, Any] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    if policy.locked_root is not None and path.is_symlink():
        try:
            target = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if not policy.under_root(target):
            return None
    is_dir = stat.S_ISDIR(info.st_mode)
    return {
        "name": path.name,
        "path": str(path),
        "is_directory": is_dir,
        "size": None if is_dir else info.st_size,
        "mtime": info.st_mtime,
    }


class PathBody(BaseModel):
    path: str


# -- reads ----------------------------------------------------------------------------------------


@router.get("/api/files")
def list_files(request: Request, path: str | None = None) -> dict[str, Any]:
    policy = policy_of(request)
    target = resolve_path(policy, path)
    if not target.exists():
        _fail(404, "not_found", "There is nothing at that path.")
    if not target.is_dir():
        _fail(400, "not_a_directory", "That path is not a folder.")
    entries: list[dict[str, Any]] = []
    try:
        with os.scandir(target) as scan:
            for item in scan:
                child = Path(item.path)
                if is_sensitive(child):
                    continue
                row = _entry(policy, child)
                if row is not None:
                    entries.append(row)
    except PermissionError:
        _fail(403, "not_readable", "That folder cannot be read.")
    except OSError:
        _fail(500, "read_failed", "That folder could not be read.")
    entries.sort(key=lambda row: (not row["is_directory"], str(row["name"]).lower()))
    parent: str | None = None
    if target.parent != target and target != policy.locked_root:
        parent = str(target.parent)
    return {"path": str(target), "parent": parent, "entries": entries, **_meta(policy)}


def _readable_file(request: Request, path: str) -> tuple[FilesPolicy, Path, int]:
    policy = policy_of(request)
    target = resolve_path(policy, path)
    if not target.exists():
        _fail(404, "not_found", "There is no file at that path.")
    if not target.is_file():
        _fail(400, "not_a_file", "That path is not a file.")
    try:
        size = target.stat().st_size
    except OSError:
        _fail(500, "read_failed", "That file could not be read.")
    if size > MAX_FILE_BYTES:
        _fail(413, "too_large", "That file is larger than 256 MiB.")
    return policy, target, size


def _mime(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


@router.get("/api/files/read")
def read_file(request: Request, path: str) -> dict[str, Any]:
    policy, target, size = _readable_file(request, path)
    try:
        with target.open("rb") as handle:
            head = handle.read(PREVIEW_BYTES)
    except PermissionError:
        _fail(403, "not_readable", "That file cannot be read.")
    except OSError:
        _fail(500, "read_failed", "That file could not be read.")
    text: str | None
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        text = None if size <= PREVIEW_BYTES else _cut_text(head)
    if text is not None and "\x00" in text:
        text = None
    return {
        "path": str(target),
        "name": target.name,
        "size": size,
        "mime_type": _mime(target),
        "text": text,
        "truncated": size > PREVIEW_BYTES,
        **_meta(policy),
    }


def _cut_text(head: bytes) -> str | None:
    """A preview cut inside a multi-byte character: drop the partial tail and try again."""
    for drop in range(1, 4):
        with contextlib.suppress(UnicodeDecodeError):
            return head[:-drop].decode("utf-8")
    return None


def download_tickets(request: Request) -> DownloadTickets:
    """This dashboard process's download-ticket book (files and backup archives share it)."""
    extras = dashboard_state(request.app).extras
    book = extras.get(DOWNLOADS_KEY)
    if not isinstance(book, DownloadTickets):
        book = extras.setdefault(DOWNLOADS_KEY, DownloadTickets())
    return book


@router.post("/api/files/download-ticket")
def mint_download_ticket(request: Request, body: PathBody) -> JSONResponse:
    _policy, target, _size = _readable_file(request, body.path)
    try:
        minted = download_tickets(request).mint(str(target))
    except DownloadRefused:
        _fail(429, "too_many_downloads", "Too many downloads are waiting; try again.")
    return JSONResponse(
        {"ticket": minted.ticket, "expires_in": minted.expires_in},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/files/download")
def download_file(request: Request, path: str = "", ticket: str = "") -> FileResponse:
    # The gate lets this route through without the session token (a new tab cannot send one), so
    # the ticket is checked before the path is looked at: nothing about the file system answers a
    # caller without a good ticket.
    try:
        bound = download_tickets(request).redeem(ticket)
    except DownloadRefused as refused:
        log.info("download ticket refused (%s)", refused.reason)
        _fail(401, "download_ticket_invalid", "That download link is not valid; open it again.")
    _policy, target, _size = _readable_file(request, path)
    if str(target) != bound:
        log.info("download ticket refused (another path)")
        _fail(401, "download_ticket_invalid", "That download link is not valid; open it again.")
    return FileResponse(
        path=str(target),
        media_type=_mime(target),
        filename=target.name,
        content_disposition_type="attachment",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
    )


# -- writes ---------------------------------------------------------------------------------------


def _written(policy: FilesPolicy, target: Path) -> dict[str, Any]:
    return {"ok": True, "path": str(target), "entry": _entry(policy, target), **_meta(policy)}


@router.post("/api/files/mkdir")
def make_directory(request: Request, body: PathBody) -> dict[str, Any]:
    policy = policy_of(request)
    target = resolve_path(policy, body.path, for_write=True)
    if target.exists() and not target.is_dir():
        _fail(409, "file_exists", "A file already has that name.")
    try:
        target.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        _fail(403, "not_writable", "That folder cannot be created here.")
    except OSError:
        _fail(500, "write_failed", "That folder could not be created.")
    return _written(policy, target)


@router.post("/api/files/upload")
async def upload_file(
    request: Request,
    file: Annotated[UploadFile, File()],
    path: Annotated[str, Form()],
    overwrite: Annotated[bool, Form()] = False,
) -> dict[str, Any]:
    policy = policy_of(request)
    target = resolve_path(policy, path, for_write=True)
    if target.is_dir():
        _fail(409, "directory_exists", "A folder already has that name.")
    if target.exists() and not overwrite:
        _fail(409, "file_exists", "A file already has that name.")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".part", dir=target.parent
        )
    except PermissionError:
        _fail(403, "not_writable", "Files cannot be written there.")
    except OSError:
        _fail(500, "write_failed", "The upload could not be started.")
    temp = Path(temp_name)
    done = False
    try:
        total = 0
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(_CHUNK):
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    _fail(413, "too_large", "Uploads are limited to 256 MiB.")
                out.write(chunk)
        os.replace(temp, target)
        done = True
    except OSError:
        _fail(500, "write_failed", "The upload could not be written.")
    finally:
        if not done:
            temp.unlink(missing_ok=True)
        await file.close()
    return _written(policy, target)


@router.delete("/api/files")
def delete_path(request: Request, path: str, recursive: bool = False) -> dict[str, Any]:
    policy = policy_of(request)
    target = resolve_path(policy, path, for_write=True)
    if target == policy.locked_root or target.parent == target:
        _fail(400, "root_refused", "The top folder cannot be deleted.")
    if not os.path.lexists(target):
        _fail(404, "not_found", "There is nothing at that path.")
    try:
        if target.is_dir() and not target.is_symlink():
            if recursive:
                shutil.rmtree(target)
            else:
                target.rmdir()
        else:
            target.unlink()
    except OSError:
        if target.is_dir() and not recursive:
            _fail(409, "not_empty", "That folder is not empty; delete it with recursive=true.")
        _fail(500, "delete_failed", "That path could not be deleted.")
    return {"ok": True, "path": str(target), **_meta(policy)}
