"""The keychain probe never prompts and never hangs a start-up (R08.10)."""

from __future__ import annotations

import logging
import stat
import sys
import threading
import types
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from indus.foundation import secrets as secrets_mod
from indus.foundation.secrets import (
    PROBE_DEADLINE_SECONDS,
    SECRETS_FILE,
    SecretStore,
    SecretStoreUnavailable,
    open_secret_store,
    probe_keychain,
    reset_probe_cache,
)
from tests.contract.foundation.test_secret_store_contract import (
    BlockingKeyring,
    FailingKeyring,
    MemoryKeyring,
)
from tests.fakes.clock import FakeClock

LOGGER = "indus.foundation.secrets"
# The real lookup, kept before the autouse fixture in tests/conftest.py swaps it for "fail".
REAL_SYSTEM_KEYRING = secrets_mod._system_keyring  # pyright: ignore[reportPrivateUsage]


@pytest.fixture(autouse=True)
def fresh_probe() -> Iterator[None]:
    reset_probe_cache()
    yield
    reset_probe_cache()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "default"
    path.mkdir(mode=0o700)
    return path


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


def _open_in_thread(home: Path, keyring: object, clock: FakeClock) -> tuple[threading.Thread, dict]:
    result: dict[str, object] = {}

    def run() -> None:
        try:
            result["store"] = open_secret_store(
                home,
                "default",
                mode="auto",
                clock=clock,
                keyring_backend=keyring,  # type: ignore[arg-type]
            )
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion below
            result["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


def test_blocking_keychain_falls_back_at_the_deadline(
    home: Path, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    keyring = BlockingKeyring()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        thread, result = _open_in_thread(home, keyring, clock)
        assert keyring.entered.wait(5)
        clock.advance(PROBE_DEADLINE_SECONDS - 0.5)
        thread.join(0.3)
        assert thread.is_alive(), "gave up before the deadline"
        clock.advance(0.5)
        thread.join(5)
        assert not thread.is_alive(), "did not give up at the deadline"
    store = result["store"]
    assert isinstance(store, SecretStore)
    assert store.backend_name == "file"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    keyring.release.set()  # let the abandoned probe thread finish


def test_unusable_result_is_cached_and_warned_once(
    home: Path, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    keyring = FailingKeyring()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        first = open_secret_store(home, "default", clock=clock, keyring_backend=keyring)
        second = open_secret_store(home, "default", clock=clock, keyring_backend=keyring)
    assert first.backend_name == second.backend_name == "file"
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_probe_asked_once_per_run(clock: FakeClock) -> None:
    keyring = MemoryKeyring()
    assert probe_keychain(keyring, clock=clock) is keyring
    assert probe_keychain(keyring, clock=clock) is keyring
    assert keyring.calls == ["get"]


def test_raising_keychain_falls_back(home: Path, clock: FakeClock) -> None:
    store = open_secret_store(home, "default", clock=clock, keyring_backend=FailingKeyring())
    assert store.backend_name == "file"


def test_fail_backend_falls_back(home: Path, clock: FakeClock) -> None:
    from keyring.backends import fail

    store = open_secret_store(home, "default", clock=clock, keyring_backend=fail.Keyring())
    assert store.backend_name == "file"


def test_null_backend_falls_back(home: Path, clock: FakeClock) -> None:
    from keyring.backends import null

    store = open_secret_store(home, "default", clock=clock, keyring_backend=null.Keyring())
    assert store.backend_name == "file"


def test_usable_keychain_is_used(home: Path, clock: FakeClock) -> None:
    store = open_secret_store(home, "default", clock=clock, keyring_backend=MemoryKeyring())
    assert store.backend_name == "keychain"
    store.set("OPENAI_API_KEY", "test-key-0000")
    assert not (home / SECRETS_FILE).exists()


def test_keychain_mode_raises_when_unusable(home: Path, clock: FakeClock) -> None:
    with pytest.raises(SecretStoreUnavailable) as info:
        open_secret_store(
            home, "default", mode="keychain", clock=clock, keyring_backend=FailingKeyring()
        )
    assert info.value.code == "secrets.unavailable"


def test_file_mode_never_touches_keyring(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> object:
        raise AssertionError("file mode must not look at the keychain")

    monkeypatch.setattr(secrets_mod, "_system_keyring", boom)
    keyring = MemoryKeyring()
    store = open_secret_store(home, "default", mode="file", keyring_backend=keyring)
    store.set("OPENAI_API_KEY", "test-key-0000")
    assert keyring.calls == []
    assert stat.S_IMODE((home / SECRETS_FILE).stat().st_mode) == 0o600


def test_system_keyring_lookup_runs_inside_the_probe(
    home: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def fake_system() -> object:
        seen.append(threading.current_thread().name)
        return FailingKeyring()

    monkeypatch.setattr(secrets_mod, "_system_keyring", fake_system)
    store = open_secret_store(home, "default", clock=clock)
    assert store.backend_name == "file"
    assert seen == ["indus-keychain-probe"]


def test_unknown_mode_refused(home: Path) -> None:
    from indus.foundation.errors import SecretStoreError

    with pytest.raises(SecretStoreError):
        open_secret_store(home, "default", mode="vault")


def _fake_keyring_module(gate: threading.Event, calls: list[str]) -> types.ModuleType:
    module = types.ModuleType("keyring")
    backend = MemoryKeyring()

    def get_keyring() -> object:
        calls.append(threading.current_thread().name)
        if threading.current_thread().name == "indus-keyring-discovery":
            assert gate.wait(10), "the test never opened the gate"
        return backend

    module.get_keyring = get_keyring  # type: ignore[attr-defined]
    return module


def test_backend_discovery_starts_once_and_the_probe_waits_for_it(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W48-e-3: the CLI starts discovery at process start; the probe joins it, then asks."""
    gate = threading.Event()
    calls: list[str] = []
    monkeypatch.setitem(sys.modules, "keyring", _fake_keyring_module(gate, calls))
    monkeypatch.setattr(secrets_mod, "_system_keyring", REAL_SYSTEM_KEYRING)
    secrets_mod.start_backend_discovery()
    secrets_mod.start_backend_discovery()  # a second start is a no-op
    found: dict[str, object] = {}
    opener = threading.Thread(
        target=lambda: found.update(store=open_secret_store(home, "default")), daemon=True
    )
    opener.start()
    probing = threading.Event()
    for _ in range(1000):
        if any(t.name == "indus-keychain-probe" for t in threading.enumerate()):
            probing.set()
            break
        threading.Event().wait(0.01)
    assert probing.is_set()
    assert calls == ["indus-keyring-discovery"]  # the probe is waiting on discovery
    gate.set()
    opener.join(10)
    assert calls == ["indus-keyring-discovery", "indus-keychain-probe"]
    store = found["store"]
    assert isinstance(store, SecretStore)
    assert store.backend_name == "keychain"


def test_a_failed_discovery_leaves_the_answer_to_the_probe(
    home: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = types.ModuleType("keyring")

    def get_keyring() -> object:
        raise RuntimeError("no backend module")

    module.get_keyring = get_keyring  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", module)
    monkeypatch.setattr(secrets_mod, "_system_keyring", REAL_SYSTEM_KEYRING)
    secrets_mod.start_backend_discovery()
    store = open_secret_store(home, "default", clock=clock)
    assert store.backend_name == "file"
