"""P07-02 (M2): from a clean home, one scripted setup run reaches a first fake-model answer.

The wizard runs with scripted answers: a custom endpoint pointing at the fake model server, its
key stored in a fake keychain, the model detected from the endpoint's ``/models`` list. Then
``indus chat -q ... --oneshot`` resolves its route from the saved settings (no injected route,
so the first-run guard runs and must let it through) and prints the fake model's answer.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator

import pytest

from indus.cli import app as app_mod
from indus.cli.args import CliState, HostOverrides
from indus.cli.model_picker import PickerDeps
from indus.cli.setup.wizard import SetupDeps, SetupOptions, run_setup
from indus.foundation import config
from indus.foundation.config import loader
from indus.foundation.scope import EnvView, Scope
from indus.foundation.secrets import INDEX_FILE, KeychainBackend, SecretStore
from tests.fakes.agent import any_window
from tests.fakes.model import FakeModelServer, script_of, text_turn
from tests.fakes.model.server import CHAT_BASE_URL
from tests.fakes.prompts import ScriptedDriver, scripted

FAKE_KEY = "test" + "-key-" + "0123456789abcdef"
BUDGET_SECONDS = 180.0  # M2: a first answer within three minutes


class DictKeyring:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str, /) -> str | None:
        return self.data.get((service, username))

    def set_password(self, service: str, username: str, password: str, /) -> None:
        self.data[(service, username)] = password

    def delete_password(self, service: str, username: str, /) -> None:
        del self.data[(service, username)]


@pytest.fixture(autouse=True)
def keep_cwd() -> Iterator[None]:
    here = os.getcwd()
    yield
    os.chdir(here)


@pytest.mark.slow
def test_clean_home_to_first_answer(scope: Scope, capsys: pytest.CaptureFixture[str]) -> None:
    started = time.monotonic()
    loader.reset()
    keyring = DictKeyring()
    store = SecretStore(KeychainBackend(keyring, scope.profile_id), scope.home / INDEX_FILE)
    home = scope.with_env(EnvView({"INDUS_HOME": str(scope.home)})).with_secrets(store)
    assert not config.config_path(home).exists()
    server = FakeModelServer(
        script_of(text_turn("Hello from the fake model.")), models=[{"id": "fake-model-1"}]
    )

    io = scripted(
        "Full setup",
        "Custom endpoint",
        CHAT_BASE_URL,
        FAKE_KEY,
        "Keep",  # reasoning effort (the endpoint offers one model, taken without a question)
        "local",
        ["terminal —", "file —", "web —"],
        "",
        "manual",
    )
    setup_state = CliState(scope=home, stdin_isatty=lambda: True, stdout_isatty=lambda: True)
    deps = SetupDeps(io=io, picker=PickerDeps(client_factory=server.client))
    assert run_setup(setup_state, SetupOptions(), deps=deps) == 0
    assert isinstance(io.driver, ScriptedDriver) and io.driver.answers == []
    saved = config.load(home).user_raw()
    assert saved["model"] == {
        "default": "fake-model-1",
        "provider": "custom",
        "base_url": CHAT_BASE_URL,
    }
    assert keyring.data[("indus", f"{home.profile_id}:OPENAI_API_KEY")] == FAKE_KEY
    capsys.readouterr()

    chat_state = CliState(
        scope=home,
        overrides=HostOverrides(
            client_factory=lambda _route: server.client(), list_models=any_window
        ),
        stdin_isatty=lambda: False,
        stdout_isatty=lambda: False,
    )
    code = app_mod.dispatch(["chat", "-q", "say hello", "--oneshot"], chat_state)
    out = capsys.readouterr()
    assert code == 0, out.err
    assert out.out.strip() == "Hello from the fake model."
    posted = server.model_requests[-1]
    assert posted.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert posted.body["model"] == "fake-model-1"
    elapsed = time.monotonic() - started
    print(f"clean home to first answer: {elapsed:.2f} s")
    assert elapsed < BUDGET_SECONDS
