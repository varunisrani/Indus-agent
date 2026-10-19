"""P21-13: the setup terminal step for the cloud backends (fake keychain, scripted answers)."""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from indus.backends import remote_probes
from indus.cli.args import CliState
from indus.cli.prompts import Interactive
from indus.cli.setup import remote_backends as rb
from indus.cli.setup.wizard import SetupDeps, SetupOptions, run_setup
from indus.foundation import config
from indus.foundation.config import loader
from indus.foundation.scope import EnvView, Scope
from indus.foundation.secrets import INDEX_FILE, KeychainBackend, SecretStore
from tests.fakes.prompts import ScriptedDriver, scripted
from tests.unit.cli.commands._helpers_d import run, store_globals  # noqa: F401

DAYTONA_KEY = "dk" + "-test-" + "0123456789"
VERCEL_TOKEN = "vt" + "-test-" + "abcdef0123"
MODAL_ID = "ak" + "-test-" + "id01"
MODAL_SECRET = "as" + "-test-" + "secret01"


class DictKeyring:
    """A keychain that lives in a dict (never the OS keychain)."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str, /) -> str | None:
        return self.data.get((service, username))

    def set_password(self, service: str, username: str, password: str, /) -> None:
        self.data[(service, username)] = password

    def delete_password(self, service: str, username: str, /) -> None:
        del self.data[(service, username)]


@pytest.fixture(autouse=True)
def fresh_guards() -> Iterator[None]:
    remote_probes.reset_guards()
    yield
    remote_probes.reset_guards()


@pytest.fixture
def keyring() -> DictKeyring:
    return DictKeyring()


def _make(scope: Scope, keyring: DictKeyring, env: dict[str, str] | None = None) -> Scope:
    loader.reset()
    store = SecretStore(KeychainBackend(keyring, scope.profile_id), scope.home / INDEX_FILE)
    view = EnvView({"INDUS_HOME": str(scope.home), **(env or {})})
    return scope.with_env(view).with_secrets(store)


@pytest.fixture
def clean(scope: Scope, keyring: DictKeyring) -> Scope:
    return _make(scope, keyring)


@pytest.fixture
def sdks(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    real = importlib.metadata.distribution

    def choose(present: bool) -> None:
        def lookup(name: str) -> object:
            if name in {"modal", "daytona", "vercel-sandbox"}:
                if present:
                    return object()
                raise importlib.metadata.PackageNotFoundError(name)
            return real(name)

        monkeypatch.setattr(importlib.metadata, "distribution", lookup)

    choose(True)
    return choose


def setup(
    scope: Scope, *answers: object, lines: list[str] | None = None, **options: Any
) -> tuple[int, ScriptedDriver]:
    io = scripted(*answers, lines=lines)
    state = CliState(scope=scope, stdin_isatty=lambda: True, stdout_isatty=lambda: True)
    code = run_setup(state, SetupOptions(**options), deps=SetupDeps(io=io))
    assert isinstance(io.driver, ScriptedDriver)
    return code, io.driver


def _key(scope: Scope, name: str) -> tuple[str, str]:
    return ("indus", f"{scope.profile_id}:{name}")


# -- the picker -----------------------------------------------------------------------------------


def test_the_picker_describes_every_backend(clean: Scope, sdks: Callable[[bool], None]) -> None:
    lines: list[str] = []
    code, driver = setup(clean, "local", section="terminal", lines=lines)
    assert code == 0
    assert driver.answers == []
    for name in ("singularity", "modal", "daytona", "vercel_sandbox"):
        assert rb.label(name) == f"{name} — {rb.DESCRIPTIONS[name]}"
    assert set(rb.DESCRIPTIONS) == {
        "local",
        "docker",
        "ssh",
        "singularity",
        "modal",
        "daytona",
        "vercel_sandbox",
    }
    assert "terminal" not in config.load(clean).user_raw()


# -- interactive ----------------------------------------------------------------------------------


def test_daytona_key_goes_to_the_keychain_not_env(
    clean: Scope, keyring: DictKeyring, sdks: Callable[[bool], None]
) -> None:
    lines: list[str] = []
    code, driver = setup(clean, "daytona", DAYTONA_KEY, section="terminal", lines=lines)
    assert code == 0, lines
    assert driver.answers == []
    assert keyring.data == {_key(clean, "DAYTONA_API_KEY"): DAYTONA_KEY}
    assert config.load(clean).user_raw()["terminal"] == {"backend": "daytona"}
    assert not (clean.home / ".env").exists()
    assert DAYTONA_KEY not in "\n".join(lines)
    assert DAYTONA_KEY not in config.config_path(clean).read_text(encoding="utf-8")
    assert "DAYTONA_API_KEY: saved." in lines
    assert "Daytona API key: " in driver.asked


def test_a_stored_key_is_kept_unless_replaced(
    clean: Scope, keyring: DictKeyring, sdks: Callable[[bool], None]
) -> None:
    store = clean.secrets
    assert isinstance(store, SecretStore)
    store.set("DAYTONA_API_KEY", DAYTONA_KEY)
    code, driver = setup(clean, "daytona", "n", section="terminal")
    assert code == 0
    assert keyring.data[_key(clean, "DAYTONA_API_KEY")] == DAYTONA_KEY
    assert not any(q.startswith("Daytona API key") for q in driver.asked)
    fresh = DAYTONA_KEY + "x"
    code, _driver = setup(clean, "daytona", "y", fresh, section="terminal")
    assert code == 0
    assert keyring.data[_key(clean, "DAYTONA_API_KEY")] == fresh


def test_vercel_asks_the_token_and_both_ids(
    clean: Scope, keyring: DictKeyring, sdks: Callable[[bool], None]
) -> None:
    lines: list[str] = []
    code, driver = setup(
        clean, "vercel_sandbox", VERCEL_TOKEN, "prj_1", "team_1", section="terminal", lines=lines
    )
    assert code == 0, lines
    assert keyring.data == {
        _key(clean, "VERCEL_TOKEN"): VERCEL_TOKEN,
        _key(clean, "VERCEL_PROJECT_ID"): "prj_1",
        _key(clean, "VERCEL_TEAM_ID"): "team_1",
    }
    assert VERCEL_TOKEN not in "\n".join(lines)
    assert not any("OIDC" in q for q in driver.asked)


def test_a_blank_answer_skips_and_a_missing_extra_is_named(
    clean: Scope, keyring: DictKeyring, sdks: Callable[[bool], None]
) -> None:
    sdks(False)
    lines: list[str] = []
    code, _driver = setup(clean, "modal", "", "", section="terminal", lines=lines)
    assert code == 0
    assert keyring.data == {}
    assert any("indus-agent[modal]" in line for line in lines)
    assert "MODAL_TOKEN_ID: skipped." in lines


def test_singularity_asks_nothing(clean: Scope, sdks: Callable[[bool], None]) -> None:
    lines: list[str] = []
    code, driver = setup(clean, "singularity", section="terminal", lines=lines)
    assert code == 0 and driver.answers == []
    assert len(driver.asked) == 1  # only the picker
    assert any("Apptainer or Singularity not found" in line for line in lines)


# -- non-interactive ------------------------------------------------------------------------------


def test_non_interactive_with_env_credentials(
    scope: Scope,
    keyring: DictKeyring,
    sdks: Callable[[bool], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = {"MODAL_TOKEN_ID": MODAL_ID, "MODAL_TOKEN_SECRET": MODAL_SECRET}
    ready = _make(scope, keyring, env)
    io = Interactive(tty=False, driver=ScriptedDriver([]))
    options = SetupOptions(non_interactive=True, terminal_backend="modal")
    assert run_setup(CliState(scope=ready), options, deps=SetupDeps(io=io)) == 0
    assert keyring.data == {
        _key(ready, "MODAL_TOKEN_ID"): MODAL_ID,
        _key(ready, "MODAL_TOKEN_SECRET"): MODAL_SECRET,
    }
    assert config.load(ready).user_raw()["terminal"] == {"backend": "modal"}
    out = capsys.readouterr()
    assert "Stored MODAL_TOKEN_ID from the environment in the OS keychain." in out.out
    assert MODAL_SECRET not in out.out + out.err and MODAL_ID not in out.out + out.err
    assert not (ready.home / ".env").exists()


def test_non_interactive_through_the_command(
    scope: Scope, keyring: DictKeyring, sdks: Callable[[bool], None]
) -> None:
    ready = _make(scope, keyring, {"DAYTONA_API_KEY": DAYTONA_KEY})
    result = run(ready, "setup", "--non-interactive", "--terminal-backend", "daytona")
    assert result.code == 0, result.err
    assert keyring.data == {_key(ready, "DAYTONA_API_KEY"): DAYTONA_KEY}
    assert "Commands run on: daytona." in result.out
    assert DAYTONA_KEY not in result.out + result.err


def test_non_interactive_missing_credentials_changes_nothing(
    clean: Scope,
    keyring: DictKeyring,
    sdks: Callable[[bool], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    options = SetupOptions(non_interactive=True, terminal_backend="vercel_sandbox")
    code = run_setup(CliState(scope=clean), options, deps=SetupDeps(io=Interactive(tty=False)))
    assert code == 1
    err = capsys.readouterr().err
    assert "VERCEL_TOKEN" in err and "unchanged" in err
    assert "terminal" not in config.load(clean).user_raw()
    assert keyring.data == {}


def test_non_interactive_unknown_backend(clean: Scope, capsys: pytest.CaptureFixture[str]) -> None:
    options = SetupOptions(non_interactive=True, terminal_backend="nimbus")
    code = run_setup(CliState(scope=clean), options, deps=SetupDeps(io=Interactive(tty=False)))
    assert code == 2
    assert "no terminal backend called 'nimbus'" in capsys.readouterr().err


def test_non_interactive_backend_without_credentials_to_ask(clean: Scope) -> None:
    options = SetupOptions(non_interactive=True, terminal_backend="docker")
    code = run_setup(CliState(scope=clean), options, deps=SetupDeps(io=Interactive(tty=False)))
    assert code == 0
    assert config.load(clean).user_raw()["terminal"] == {"backend": "docker"}
