"""``<home>/auth.json``: which providers a profile signed in to, and its credential pools.

The file holds metadata only (D-16.78). Tokens and keys live in the profile's ``SecretStore``
under names from ``pool_model.secret_name``; ``save`` refuses a document that carries a value in a
secret-shaped field, so a slip in a caller cannot leak one into the JSON.

Reading (R04.16):

- no file → an empty store;
- the OS refuses the read (permissions, too many open files, a stalled mount) →
  ``AuthStoreUnreadable`` and the file is left exactly as it is: callers read-modify-write, so an
  "empty" answer here would erase every credential on the next save;
- the bytes are not a JSON object → the original is copied to ``auth.json.corrupt``, a warning is
  logged and the store starts empty.

Writing: under a cross-process lock (``auth.json.lock``, 15 s; a timeout names the holder's PID),
atomically, file 0600 and folder 0700. The folder may not be ``/``, a top-level directory or the
Indus install tree.

A named profile may read the root (default) profile's store as a read-only fallback for a
provider its own store does not mention. A test run never reaches the real user's store: the
constructor refuses it before anything touches the disk (R03.24).
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import shutil
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import indus
from indus.foundation import fsio
from indus.foundation.errors import (
    AuthIsolationRefused,
    AuthStoreLocked,
    AuthStoreUnreadable,
    AuthUnsafePath,
)
from indus.foundation.home import PROFILES_DIR, root_of, secure_dir
from indus.state.connection import GUARD_BYPASS_ENV, looks_like_test_run, real_default_root

log = logging.getLogger(__name__)

AUTH_FILE = "auth.json"
CORRUPT_SUFFIX = ".corrupt"
LOCK_SUFFIX = ".lock"
STORE_VERSION = 1
LOCK_TIMEOUT_SECONDS = 15.0

# Field names that would hold secret material. A non-empty string under one of these anywhere in
# the document is refused at save time.
SECRET_FIELDS = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "key",
        "token",
        "secret",
        "client_secret",
        "password",
        "device_code",
        "code_verifier",
    }
)

_SECTIONS: tuple[tuple[str, type], ...] = (
    ("providers", dict),
    ("pool", dict),
    ("suppressed_sources", dict),
)
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})


def empty_document() -> dict[str, Any]:
    return {"version": STORE_VERSION, "providers": {}, "pool": {}}


def _install_root() -> Path:
    package = Path(indus.__file__).resolve().parent
    return package.parent.parent if package.parent.name == "src" else package


def _is_real_store(path: Path, env: Mapping[str, str] | None) -> bool:
    """Is ``path`` the auth store of the real user's root or one of its named profiles?

    ``env`` None means the process environment (read by the P02 guard helpers)."""
    if (env or {}).get(GUARD_BYPASS_ENV, "").strip().lower() in _TRUE_WORDS:
        return False
    root = real_default_root(env)
    if root is None:
        return False
    resolved = path.resolve(strict=False)
    real = root.resolve(strict=False)
    if resolved.parent == real:
        return True
    try:
        parts = resolved.relative_to(real).parts
    except ValueError:
        return False
    return len(parts) == 3 and parts[0] == PROFILES_DIR


def _secret_fields_in(node: object, where: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in cast(dict[object, object], node).items():
            path = f"{where}.{key}" if where else str(key)
            if str(key).lower() in SECRET_FIELDS and isinstance(value, str) and value:
                found.append(path)
            found.extend(_secret_fields_in(value, path))
    elif isinstance(node, list):
        for i, item in enumerate(cast(list[object], node)):
            found.extend(_secret_fields_in(item, f"{where}[{i}]"))
    return found


def _normalised(raw: dict[str, Any]) -> dict[str, Any]:
    doc = dict(raw)
    doc["version"] = STORE_VERSION
    for name, kind in _SECTIONS:
        if name in doc and not isinstance(doc[name], kind):
            log.warning("auth.json: the %s section is not a %s; treating it as empty", name, kind)
            doc[name] = kind()
    doc.setdefault("providers", {})
    doc.setdefault("pool", {})
    active = doc.get("active_provider")
    if active is not None and not isinstance(active, str):
        doc.pop("active_provider")
    return doc


class AuthStore:
    """One profile's ``auth.json``. Cheap to construct; every read goes to the disk."""

    def __init__(
        self,
        home: Path,
        *,
        env: Mapping[str, str] | None = None,
        lock_timeout: float = LOCK_TIMEOUT_SECONDS,
        lock_clock: fsio.MonotonicClock | None = None,
        lock_wait: Callable[[float], object] | None = None,
    ) -> None:
        self.home = Path(home)
        self.path = self.home / AUTH_FILE
        root = root_of(self.home)
        self.fallback_path: Path | None = root / AUTH_FILE if root != self.home else None
        for candidate in (self.path, self.fallback_path):
            if (
                candidate is not None
                and _is_real_store(candidate, env)
                and looks_like_test_run(env)
            ):
                raise AuthIsolationRefused(
                    f"refusing to open the real auth store {candidate} from a test run",
                    hint=f"point INDUS_HOME at a temporary folder; {GUARD_BYPASS_ENV}=1 overrides",
                )
        self._lock_timeout = lock_timeout
        self._lock_clock = lock_clock
        self._lock_wait = lock_wait

    def __repr__(self) -> str:
        return f"AuthStore(path={str(self.path)!r})"

    @property
    def corrupt_path(self) -> Path:
        return self.path.with_name(self.path.name + CORRUPT_SUFFIX)

    # -- reading ------------------------------------------------------------------------------

    def _read_path(self, path: Path, *, quarantine: bool) -> dict[str, Any]:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return empty_document()
        except OSError as exc:
            if not quarantine:
                log.debug("auth fallback %s unreadable: %s", path, exc)
                return empty_document()
            raise AuthStoreUnreadable(
                f"{path} could not be read ({exc.strerror or exc}); it was left as it is",
                detail=str(exc),
                hint="check the file's owner and permissions, then run the command again",
            ) from exc
        try:
            raw = json.loads(data.decode("utf-8-sig"))
            if not isinstance(raw, dict):
                raise ValueError("the top level is not a JSON object")
        except (UnicodeDecodeError, ValueError) as exc:
            if not quarantine:
                log.debug("auth fallback %s is not valid JSON: %s", path, exc)
                return empty_document()
            self._set_aside(path, exc)
            return empty_document()
        return _normalised(cast(dict[str, Any], raw))

    def _set_aside(self, path: Path, why: Exception) -> None:
        target = path.with_name(path.name + CORRUPT_SUFFIX)
        try:
            shutil.copy2(path, target)
        except OSError as exc:
            log.warning(
                "%s is damaged (%s) and a copy could not be kept at %s (%s); starting with an "
                "empty auth store",
                path,
                why,
                target,
                exc,
            )
            return
        log.warning(
            "%s is damaged (%s); the original was copied to %s and the auth store starts empty",
            path,
            why,
            target,
        )

    def read(self) -> dict[str, Any]:
        """This profile's store (no fallback)."""
        return self._read_path(self.path, quarantine=True)

    def read_fallback(self) -> dict[str, Any]:
        """The root profile's store for a named profile (read-only; errors read as empty)."""
        if self.fallback_path is None:
            return empty_document()
        return self._read_path(self.fallback_path, quarantine=False)

    def provider_state(self, provider: str) -> tuple[dict[str, Any] | None, bool]:
        """``(state, inherited)``: this store's ``providers.<provider>``, else the root's."""
        own = self.read().get("providers", {}).get(provider)
        if isinstance(own, dict):
            return copy.deepcopy(cast(dict[str, Any], own)), False
        inherited = self.read_fallback().get("providers", {}).get(provider)
        if isinstance(inherited, dict):
            return copy.deepcopy(cast(dict[str, Any], inherited)), True
        return None, False

    def pool_rows(self, provider: str) -> tuple[list[dict[str, Any]], bool]:
        """``(rows, inherited)``: this store's pool rows for ``provider``, else the root's."""
        own = self.read().get("pool", {}).get(provider)
        if isinstance(own, list):
            return _rows(cast(list[object], own)), False
        inherited = self.read_fallback().get("pool", {}).get(provider)
        if isinstance(inherited, list):
            return _rows(cast(list[object], inherited)), True
        return [], False

    def signed_in(self) -> list[str]:
        """Providers this profile (or, for a named profile, the root) signed in to: a non-empty
        ``providers.<id>`` record or a pool row of ``auth_type`` ``oauth``. Sorted."""
        found: set[str] = set()
        for doc in (self.read(), self.read_fallback()):
            providers = doc.get("providers")
            if isinstance(providers, dict):
                for name, state in cast(dict[str, Any], providers).items():
                    if isinstance(state, dict) and state:
                        found.add(str(name))
            pool = doc.get("pool")
            if isinstance(pool, dict):
                for name, rows in cast(dict[str, Any], pool).items():
                    if isinstance(rows, list) and any(
                        r.get("auth_type") == "oauth" for r in _rows(cast(list[object], rows))
                    ):
                        found.add(str(name))
        return sorted(found)

    # -- writing ------------------------------------------------------------------------------

    def _lock(self) -> fsio.FileLock:
        return fsio.FileLock(
            self.path.with_name(self.path.name + LOCK_SUFFIX),
            timeout=self._lock_timeout,
            clock=self._lock_clock,
            wait=self._lock_wait,
        )

    def _check_folder(self) -> None:
        folder = self.path.parent.resolve(strict=False)
        if len(folder.parts) < 3:
            raise AuthUnsafePath(
                f"refusing to keep auth.json in {folder}: it is a filesystem root or a top-level "
                "directory",
                hint="check INDUS_HOME; it probably points at the wrong place",
            )
        install = _install_root()
        if folder == install or folder.is_relative_to(install):
            raise AuthUnsafePath(
                f"refusing to keep auth.json inside the Indus install tree ({install})",
                hint="point INDUS_HOME outside the installed program",
            )

    def save(self, doc: Mapping[str, Any]) -> Path:
        """Write ``doc`` as the whole store (callers normally use ``transaction``)."""
        leaked = _secret_fields_in(doc)
        if leaked:
            raise ValueError(
                "auth.json holds metadata only; secret material belongs in the SecretStore "
                f"(fields: {', '.join(leaked)})"
            )
        self._check_folder()
        secure_dir(self.path.parent, home=self.home)
        out = dict(doc)
        out["version"] = STORE_VERSION
        with self._locked():
            return fsio.atomic_write_json(
                self.path, out, sort_keys=False, mode=0o600, preserve_mode=False
            )

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self._check_folder()
        secure_dir(self.path.parent, home=self.home)
        lock = self._lock()
        try:
            lock.acquire()
        except fsio.LockTimeout as exc:
            who = f"process {exc.holder_pid}" if exc.holder_pid is not None else "another process"
            raise AuthStoreLocked(
                f"{self.path} is in use by {who}; gave up after {self._lock_timeout:g} s",
                holder_pid=exc.holder_pid,
                hint="wait for the other indus command to finish, then try again",
            ) from exc
        try:
            yield
        finally:
            lock.release()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[dict[str, Any]]:
        """Lock, read, let the caller change the document, write it back if it changed."""
        with self._locked():
            doc = self.read()
            before = copy.deepcopy(doc)
            yield doc
            if doc != before:
                self.save(doc)


def _rows(raw: list[object]) -> list[dict[str, Any]]:
    return [copy.deepcopy(cast(dict[str, Any], r)) for r in raw if isinstance(r, dict)]
