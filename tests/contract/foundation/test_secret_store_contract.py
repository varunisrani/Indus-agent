"""Every ``SecretStore`` backend passes this suite. Never touches the real keychain.

The in-memory keyring backends below are the test doubles for ``keyring``: ``MemoryKeyring`` stores
values in a dict, ``BlockingKeyring`` never answers a lookup, ``FailingKeyring`` raises on every
call. They duck-type the ``keyring`` backend interface and are never registered with ``keyring``.
"""

from __future__ import annotations

import json
import stat
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from indus.foundation.secrets import (
    INDEX_FILE,
    SECRETS_FILE,
    SERVICE,
    FileBackend,
    KeychainBackend,
    SecretStore,
    open_secret_store,
    reset_probe_cache,
)


class MemoryKeyring:
    priority = 5.0

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.calls: list[str] = []

    def get_password(self, service: str, username: str) -> str | None:
        self.calls.append("get")
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.calls.append("set")
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.calls.append("delete")
        del self.items[(service, username)]


class BlockingKeyring(MemoryKeyring):
    """``get`` blocks until released (as a keychain waiting on an unlock dialog would)."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def get_password(self, service: str, username: str) -> str | None:
        self.entered.set()
        self.release.wait(30)
        return None


class FailingKeyring(MemoryKeyring):
    def get_password(self, service: str, username: str) -> str | None:
        raise RuntimeError("keychain is locked")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise RuntimeError("keychain is locked")


@pytest.fixture(autouse=True)
def fresh_probe() -> Iterator[None]:
    reset_probe_cache()
    yield
    reset_probe_cache()


StoreFactory = Callable[[Path, str], SecretStore]


def _file_store(home: Path, profile_id: str) -> SecretStore:
    return open_secret_store(home, profile_id, mode="file")


_SHARED_KEYRING = MemoryKeyring()


def _keychain_store(home: Path, profile_id: str) -> SecretStore:
    store = open_secret_store(home, profile_id, mode="keychain", keyring_backend=_SHARED_KEYRING)
    assert store.backend_name == "keychain"
    return store


@pytest.fixture(params=[_file_store, _keychain_store], ids=["file", "keychain"])
def make_store(request: pytest.FixtureRequest) -> Iterator[StoreFactory]:
    _SHARED_KEYRING.items.clear()
    yield request.param


@pytest.fixture
def homes(tmp_path: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for name in ("default", "work"):
        path = tmp_path / name
        path.mkdir(mode=0o700)
        out[name] = path
    return out


def test_round_trip(make_store: StoreFactory, homes: dict[str, Path]) -> None:
    store = make_store(homes["default"], "default")
    assert store.get("OPENAI_API_KEY") is None
    store.set("OPENAI_API_KEY", "test-key-0000")
    assert store.get("OPENAI_API_KEY") == "test-key-0000"
    assert store.names() == ["OPENAI_API_KEY"]
    store.set("OPENAI_API_KEY", "test-key-0001")
    assert store.get("OPENAI_API_KEY") == "test-key-0001"
    assert store.delete("OPENAI_API_KEY") is True
    assert store.get("OPENAI_API_KEY") is None
    assert store.names() == []
    assert store.delete("OPENAI_API_KEY") is False


def test_values_survive_reopen(make_store: StoreFactory, homes: dict[str, Path]) -> None:
    make_store(homes["default"], "default").set("A_TOKEN", "test-token-1111")
    again = make_store(homes["default"], "default")
    assert again.get("A_TOKEN") == "test-token-1111"
    assert again.names() == ["A_TOKEN"]


@pytest.mark.parametrize(
    "value", ["pässwörd-ключ-密钥", "emoji 🔑 key", "", "with\nnewline", "  spaced  "]
)
def test_unicode_and_odd_values(
    make_store: StoreFactory, homes: dict[str, Path], value: str
) -> None:
    store = make_store(homes["default"], "default")
    store.set("ODD_SECRET", value)
    assert make_store(homes["default"], "default").get("ODD_SECRET") == value


def test_profile_isolation(make_store: StoreFactory, homes: dict[str, Path]) -> None:
    default = make_store(homes["default"], "default")
    work = make_store(homes["work"], "work")
    default.set("OPENAI_API_KEY", "test-key-default")
    assert work.get("OPENAI_API_KEY") is None
    assert work.names() == []
    work.set("OPENAI_API_KEY", "test-key-work")
    assert default.get("OPENAI_API_KEY") == "test-key-default"


@pytest.mark.parametrize("name", ["", "1X", "A-B", "A B", "../x"])
def test_bad_names_refused(make_store: StoreFactory, homes: dict[str, Path], name: str) -> None:
    store = make_store(homes["default"], "default")
    with pytest.raises(ValueError):
        store.set(name, "v")
    with pytest.raises(ValueError):
        store.get(name)


def test_repr_never_shows_values(make_store: StoreFactory, homes: dict[str, Path]) -> None:
    store = make_store(homes["default"], "default")
    store.set("OPENAI_API_KEY", "test-key-secret-value")
    assert "test-key-secret-value" not in repr(store)
    assert "test-key-secret-value" not in repr(store._backend)  # pyright: ignore[reportPrivateUsage]


def test_index_holds_names_only(make_store: StoreFactory, homes: dict[str, Path]) -> None:
    store = make_store(homes["default"], "default")
    store.set("OPENAI_API_KEY", "test-key-indexed")
    index = homes["default"] / INDEX_FILE
    text = index.read_text()
    assert "test-key-indexed" not in text
    assert json.loads(text)["names"] == ["OPENAI_API_KEY"]
    assert stat.S_IMODE(index.stat().st_mode) == 0o600


def test_resolve_prefers_store_over_env(make_store: StoreFactory, homes: dict[str, Path]) -> None:
    store = make_store(homes["default"], "default")
    env = {"OPENAI_API_KEY": "test-key-from-env", "ONLY_ENV": "e"}
    assert store.resolve("OPENAI_API_KEY", env) == "test-key-from-env"
    store.set("OPENAI_API_KEY", "test-key-from-store")
    assert store.resolve("OPENAI_API_KEY", env) == "test-key-from-store"
    assert store.resolve("ONLY_ENV", env) == "e"
    assert store.resolve("NOWHERE", env) is None


def test_migrate_from_env_once(make_store: StoreFactory, homes: dict[str, Path]) -> None:
    store = make_store(homes["default"], "default")
    env = {"OPENAI_API_KEY": "test-key-env", "EMPTY_KEY": ""}
    assert store.migrate_from_env(["OPENAI_API_KEY", "EMPTY_KEY", "MISSING"], env) == [
        "OPENAI_API_KEY"
    ]
    assert store.get("OPENAI_API_KEY") == "test-key-env"
    store.set("OPENAI_API_KEY", "test-key-rotated")
    assert store.migrate_from_env(["OPENAI_API_KEY"], env) == []
    assert store.get("OPENAI_API_KEY") == "test-key-rotated"
    assert store.migrated_names() == ["OPENAI_API_KEY"]


def test_known_values_and_generation(make_store: StoreFactory, homes: dict[str, Path]) -> None:
    store = make_store(homes["default"], "default")
    first = store.generation
    store.set("A_KEY", "test-value-a")
    store.set("B_KEY", "test-value-b")
    assert sorted(store.known_values()) == ["test-value-a", "test-value-b"]
    assert store.generation > first


# -- backend specifics -------------------------------------------------------------------------


def test_file_backend_is_owner_only(homes: dict[str, Path]) -> None:
    store = open_secret_store(homes["default"], "default", mode="file")
    store.set("OPENAI_API_KEY", "test-key-0000")
    path = homes["default"] / SECRETS_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    store.set("OPENAI_API_KEY", "test-key-0001")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_file_backend_corrupt_file_is_left_alone(homes: dict[str, Path]) -> None:
    from indus.foundation.errors import SecretStoreError

    path = homes["default"] / SECRETS_FILE
    path.write_text("{not json")
    store = SecretStore(FileBackend(homes["default"]), homes["default"] / INDEX_FILE)
    with pytest.raises(SecretStoreError):
        store.get("A_KEY")
    with pytest.raises(SecretStoreError):
        store.set("A_KEY", "v")
    assert path.read_text() == "{not json"


def test_file_backend_lists_names_without_index(homes: dict[str, Path]) -> None:
    store = open_secret_store(homes["default"], "default", mode="file")
    store.set("A_KEY", "test-value-a")
    (homes["default"] / INDEX_FILE).unlink()
    assert open_secret_store(homes["default"], "default", mode="file").names() == ["A_KEY"]


def test_keychain_accounts_are_per_profile() -> None:
    keyring = MemoryKeyring()
    KeychainBackend(keyring, "work").set("OPENAI_API_KEY", "test-key-0000")
    assert keyring.items == {(SERVICE, "work:OPENAI_API_KEY"): "test-key-0000"}


def test_keychain_errors_become_store_errors() -> None:
    from indus.foundation.errors import SecretStoreError

    backend = KeychainBackend(FailingKeyring(), "default")
    with pytest.raises(SecretStoreError):
        backend.get("A_KEY")
    with pytest.raises(SecretStoreError):
        backend.set("A_KEY", "v")
