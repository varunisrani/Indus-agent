"""Per-profile secret storage: the OS keychain when it works, else an owner-only file.

``open_secret_store`` picks the backend from ``security.secret_store``:

- ``auto`` (default) checks the keychain without ever letting it prompt: a daemon thread looks up a
  sentinel account and the caller waits at most ``PROBE_DEADLINE_SECONDS`` on the injected clock. A
  fail/null backend, an exception or the deadline marks the keychain unusable for the rest of the
  process run (the answer is cached, never retried) and the file backend is used, with one warning.
- ``keychain`` raises ``SecretStoreUnavailable`` when that check fails.
- ``file`` never touches the keychain (a CLI process may already have started the library's
  backend discovery, ``start_backend_discovery``, which reads and writes no entry).

Keychains cannot list their entries, so the names (never values) live in
``<home>/secrets.index.json`` for every backend.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from indus.foundation.envfile import NAME_RE
from indus.foundation.errors import SecretStoreError
from indus.foundation.errors import SecretStoreUnavailable as SecretStoreUnavailable  # re-export
from indus.foundation.fsio import FileLock, atomic_write_bytes

log = logging.getLogger(__name__)

SERVICE = "indus"
PROBE_ACCOUNT = "__indus_probe__"
PROBE_DEADLINE_SECONDS = 2.0
_PROBE_POLL_SECONDS = 0.01
LOCK_TIMEOUT_SECONDS = 15.0
SECRETS_FILE = "secrets.json"
INDEX_FILE = "secrets.index.json"
FORMAT_VERSION = 1

SecretStoreMode = Literal["auto", "keychain", "file"]


class MonotonicClock(Protocol):
    def monotonic(self) -> float: ...


class _SystemMonotonic:
    def monotonic(self) -> float:
        return time.monotonic()


class KeyringLike(Protocol):
    """The part of a ``keyring`` backend we use."""

    def get_password(self, service: str, username: str, /) -> str | None: ...

    def set_password(self, service: str, username: str, password: str, /) -> None: ...

    def delete_password(self, service: str, username: str, /) -> None: ...


class SecretBackend(Protocol):
    name: str

    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> bool: ...

    def listed_names(self) -> set[str]:
        """Names the backend can list itself (empty when it cannot)."""
        ...


def _check_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise ValueError(f"{name!r} is not a valid secret name")


def _store_error(code_detail: str, exc: BaseException) -> SecretStoreError:
    return SecretStoreError(
        "secrets.backend_failed",
        f"the secret store could not {code_detail}",
        detail=f"{type(exc).__name__}: {exc}",
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecretStoreError(
            "secrets.unreadable",
            f"{path.name} cannot be read; it was left as it is",
            detail=f"{path}: {exc}",
        ) from exc
    if not isinstance(data, dict):
        raise SecretStoreError("secrets.unreadable", f"{path.name} is not a JSON object")
    return cast(dict[str, Any], data)


def _name_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return sorted({str(n) for n in cast(list[object], raw)})


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    atomic_write_bytes(path, text.encode("utf-8"), mode=0o600, preserve_mode=False)


def _lock(path: Path) -> FileLock:
    return FileLock(path.with_name(path.name + ".lock"), timeout=LOCK_TIMEOUT_SECONDS)


# -- backends ----------------------------------------------------------------------------------


class FileBackend:
    """``<home>/secrets.json``, owner-only, written atomically under a file lock."""

    name = "file"

    def __init__(self, home: Path) -> None:
        self.path = home / SECRETS_FILE

    def __repr__(self) -> str:
        return f"FileBackend(path={str(self.path)!r})"

    def _secrets(self) -> dict[str, str]:
        data = _read_json(self.path) or {}
        raw = data.get("secrets", {})
        if not isinstance(raw, dict):
            raise SecretStoreError("secrets.unreadable", f"{self.path.name} has no secrets table")
        return {str(k): str(v) for k, v in cast(dict[object, object], raw).items()}

    def _save(self, secrets: Mapping[str, str]) -> None:
        _write_json(self.path, {"version": FORMAT_VERSION, "secrets": dict(secrets)})

    def get(self, name: str) -> str | None:
        return self._secrets().get(name)

    def set(self, name: str, value: str) -> None:
        with _lock(self.path):
            secrets = self._secrets()
            secrets[name] = value
            self._save(secrets)

    def delete(self, name: str) -> bool:
        with _lock(self.path):
            secrets = self._secrets()
            if name not in secrets:
                return False
            del secrets[name]
            self._save(secrets)
            return True

    def listed_names(self) -> set[str]:
        return set(self._secrets())


class KeychainBackend:
    """OS keychain through ``keyring``: service ``indus``, account ``<profile_id>:<NAME>``."""

    name = "keychain"

    def __init__(self, keyring_backend: KeyringLike, profile_id: str) -> None:
        self._keyring = keyring_backend
        self.profile_id = profile_id

    def __repr__(self) -> str:
        return f"KeychainBackend(profile_id={self.profile_id!r})"

    def _account(self, name: str) -> str:
        return f"{self.profile_id}:{name}"

    def get(self, name: str) -> str | None:
        try:
            return self._keyring.get_password(SERVICE, self._account(name))
        except Exception as exc:
            raise _store_error("read from the keychain", exc) from exc

    def set(self, name: str, value: str) -> None:
        try:
            self._keyring.set_password(SERVICE, self._account(name), value)
        except Exception as exc:
            raise _store_error("write to the keychain", exc) from exc

    def delete(self, name: str) -> bool:
        if self.get(name) is None:
            return False
        try:
            self._keyring.delete_password(SERVICE, self._account(name))
        except Exception as exc:
            raise _store_error("delete from the keychain", exc) from exc
        return True

    def listed_names(self) -> set[str]:
        return set()


# -- the store ---------------------------------------------------------------------------------


class SecretStore:
    """One profile's secrets. Values are never logged and never appear in ``repr``."""

    def __init__(self, backend: SecretBackend, index_path: Path) -> None:
        self._backend = backend
        self._index_path = index_path
        self._cache: dict[str, str | None] = {}
        self._lock = threading.RLock()
        self._generation = 0

    def __repr__(self) -> str:
        return f"SecretStore(backend={self._backend.name!r}, index={str(self._index_path)!r})"

    @property
    def backend_name(self) -> str:
        return self._backend.name

    @property
    def index_path(self) -> Path:
        """Where this store keeps its name index (inside the profile home it belongs to)."""
        return self._index_path

    @property
    def generation(self) -> int:
        """Bumped on every change; lets callers cache derived data (redaction patterns)."""
        return self._generation

    # index ------------------------------------------------------------------------------------

    def _index(self) -> dict[str, Any]:
        data = _read_json(self._index_path) or {}
        names = data.get("names", [])
        migrated = data.get("migrated", [])
        return {"names": _name_list(names), "migrated": _name_list(migrated)}

    def _update_index(
        self, *, add: Iterable[str] = (), remove: Iterable[str] = (), migrated: Iterable[str] = ()
    ) -> None:
        with _lock(self._index_path):
            index = self._index()
            names = (set(index["names"]) | set(add)) - set(remove)
            done = set(index["migrated"]) | set(migrated)
            new = {"version": FORMAT_VERSION, "names": sorted(names), "migrated": sorted(done)}
            if new["names"] != index["names"] or new["migrated"] != index["migrated"]:
                _write_json(self._index_path, new)

    # api --------------------------------------------------------------------------------------

    def get(self, name: str) -> str | None:
        _check_name(name)
        with self._lock:
            if name not in self._cache:
                self._cache[name] = self._backend.get(name)
            return self._cache[name]

    def set(self, name: str, value: str) -> None:
        _check_name(name)
        if not isinstance(value, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError("secret values are strings")
        with self._lock:
            self._backend.set(name, value)
            self._cache[name] = value
            self._generation += 1
            self._update_index(add=[name])

    def delete(self, name: str) -> bool:
        _check_name(name)
        with self._lock:
            removed = self._backend.delete(name)
            self._cache[name] = None
            self._generation += 1
            self._update_index(remove=[name])
            return removed

    def names(self) -> list[str]:
        return sorted(set(self._index()["names"]) | self._backend.listed_names())

    def resolve(self, name: str, env: Mapping[str, str]) -> str | None:
        """The store's value, else the scope's env view (which already has ``.env`` applied)."""
        value = self.get(name)
        return value if value is not None else env.get(name)

    def known_values(self) -> list[str]:
        """Every stored value, for exact-value redaction. Never log the result."""
        values: list[str] = []
        for name in self.names():
            value = self.get(name)
            if value:
                values.append(value)
        return values

    def migrated_names(self) -> list[str]:
        return list(self._index()["migrated"])

    def migrate_from_env(self, names: Iterable[str], env: Mapping[str, str]) -> list[str]:
        """Copy values from ``env`` into the store once per name; returns the names copied.

        A name already recorded as migrated is skipped even if it is in ``env`` again, so a later
        edit of the store is not overwritten by a stale ``.env`` line.
        """
        with self._lock:
            done = set(self.migrated_names())
            copied: list[str] = []
            for name in names:
                _check_name(name)
                value = env.get(name)
                if name in done or not value:
                    continue
                self._backend.set(name, value)
                self._cache[name] = value
                copied.append(name)
            if copied:
                self._generation += 1
                self._update_index(add=copied, migrated=copied)
            return copied


# -- keychain probe ----------------------------------------------------------------------------

_probe_lock = threading.Lock()
# id(backend or None) -> (backend kept alive, usable backend or None)
_probe_cache: dict[int, tuple[object, KeyringLike | None]] = {}
_fallback_warned = False
_discovery: threading.Thread | None = None


def reset_probe_cache() -> None:
    """Forget probe results, the fallback warning and a started discovery (tests only)."""
    global _fallback_warned, _discovery
    with _probe_lock:
        _probe_cache.clear()
        _fallback_warned = False
        _discovery = None


def _discover_backend() -> None:
    try:
        import keyring

        keyring.get_keyring()
    except Exception:  # the probe asks again and reports what went wrong
        log.debug("keyring backend discovery failed", exc_info=True)


def start_backend_discovery() -> None:
    """Start the ``keyring`` library finding its backend, in a daemon thread (process start only).

    Finding the backend imports every installed backend module and scans package metadata; on
    Linux the macOS module even runs the C toolchain to look for its frameworks. That costs about
    250 ms here, much of it waiting on child processes, so the CLI starts it before its own
    imports and the probe later waits for it (W48-e-3). Discovery reads and writes no entry; the
    probe's own lookup still happens only when ``open_secret_store`` asks for the keychain.
    """
    global _discovery
    with _probe_lock:
        if _discovery is not None:
            return
        _discovery = threading.Thread(
            target=_discover_backend, name="indus-keyring-discovery", daemon=True
        )
        thread = _discovery
    thread.start()


def _system_keyring() -> KeyringLike:
    discovery = _discovery
    if discovery is not None:
        discovery.join()  # runs on the probe's worker thread, which the caller bounds by deadline
    import keyring

    return keyring.get_keyring()


def _looks_unusable(backend: object) -> bool:
    try:
        priority = float(getattr(backend, "priority", 1))
    except Exception:
        return True
    return priority <= 0


def _run_probe(
    keyring_backend: KeyringLike | None, clock: MonotonicClock, deadline: float
) -> tuple[KeyringLike | None, str]:
    done = threading.Event()
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            backend = keyring_backend if keyring_backend is not None else _system_keyring()
            if _looks_unusable(backend):
                box["why"] = f"{type(backend).__module__}.{type(backend).__name__} has no storage"
                return
            backend.get_password(SERVICE, PROBE_ACCOUNT)
            box["backend"] = backend
        except Exception as exc:
            box["why"] = f"{type(exc).__name__}: {exc}"
        finally:
            done.set()

    thread = threading.Thread(target=work, name="indus-keychain-probe", daemon=True)
    start = clock.monotonic()
    thread.start()
    while not done.wait(_PROBE_POLL_SECONDS):
        if clock.monotonic() - start >= deadline:
            return None, f"no answer within {deadline:g} s"
    found = box.get("backend")
    return found, str(box.get("why", ""))


def probe_keychain(
    keyring_backend: KeyringLike | None = None,
    *,
    clock: MonotonicClock | None = None,
    deadline: float = PROBE_DEADLINE_SECONDS,
) -> KeyringLike | None:
    """Return a usable keyring backend, or ``None``. Asked at most once per backend per process."""
    key = id(keyring_backend)
    with _probe_lock:
        if key in _probe_cache:
            return _probe_cache[key][1]
    found, why = _run_probe(keyring_backend, clock or _SystemMonotonic(), deadline)
    with _probe_lock:
        if key in _probe_cache:  # another thread finished first; keep its answer
            return _probe_cache[key][1]
        _probe_cache[key] = (keyring_backend, found)
    if found is None:
        log.debug("keychain not usable for this run: %s", why)
    return found


def _warn_fallback_once() -> None:
    global _fallback_warned
    with _probe_lock:
        if _fallback_warned:
            return
        _fallback_warned = True
    log.warning(
        "the OS keychain is not available; secrets are kept in an owner-only file in the profile "
        "home for this run"
    )


def open_secret_store(
    home: Path,
    profile_id: str,
    *,
    mode: str = "auto",
    clock: MonotonicClock | None = None,
    keyring_backend: KeyringLike | None = None,
) -> SecretStore:
    """Open the secret store of the profile at ``home`` for ``security.secret_store`` = ``mode``."""
    index = home / INDEX_FILE
    if mode == "file":
        return SecretStore(FileBackend(home), index)
    if mode not in ("auto", "keychain"):
        raise SecretStoreError(
            "secrets.bad_mode",
            f"security.secret_store must be auto, keychain or file, not {mode!r}",
        )
    found = probe_keychain(keyring_backend, clock=clock)
    if found is not None:
        return SecretStore(KeychainBackend(found, profile_id), index)
    if mode == "keychain":
        raise SecretStoreUnavailable(
            "the OS keychain is not available",
            hint="set security.secret_store to auto or file to keep secrets in an owner-only file",
        )
    _warn_fallback_once()
    return SecretStore(FileBackend(home), index)
