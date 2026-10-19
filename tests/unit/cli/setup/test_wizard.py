"""P07-02: the setup wizard (scripted answers, a fake keychain, respx for model lists)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import respx

from indus.cli.args import CliState
from indus.cli.model_picker import PickerDeps
from indus.cli.prompts import Interactive
from indus.cli.setup.wizard import CANCELLED, SetupDeps, SetupOptions, run_setup
from indus.foundation import config
from indus.foundation.config import backups, loader
from indus.foundation.scope import EnvView, Scope
from indus.foundation.secrets import INDEX_FILE, KeychainBackend, SecretStore
from tests.fakes.prompts import BACK, CANCEL, ScriptedDriver, scripted

FAKE_KEY = "test" + "-key-" + "0123456789abcdef"
MODELS = "https://openrouter.ai/api/v1/models"
# the cli bundle's rows (video_gen is offered but not in the bundle)
BUNDLE_TOOLS = [
    "browser —",
    "clarify —",
    "code_execution —",
    "computer_use —",  # P10-20
    "delegation —",
    "file —",
    "image_gen —",  # P10-16
    "memory —",
    "search —",
    "session_search —",
    "skills —",
    "terminal —",
    "todo —",
    "tts —",  # P10-17
    "vision —",  # P10-15
    "web —",
]


def secrets_of(scope: Scope) -> SecretStore:
    store = scope.secrets
    assert isinstance(store, SecretStore)
    return store


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


@pytest.fixture
def keyring() -> DictKeyring:
    return DictKeyring()


@pytest.fixture
def clean(scope: Scope, keyring: DictKeyring) -> Scope:
    loader.reset()
    store = SecretStore(KeychainBackend(keyring, scope.profile_id), scope.home / INDEX_FILE)
    return scope.with_env(EnvView({"INDUS_HOME": str(scope.home)})).with_secrets(store)


@pytest.fixture(autouse=True)
def models() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(MODELS).respond(200, json={"data": [{"id": "a/one"}, {"id": "b/two"}]})
        yield router


def setup(
    scope: Scope, *answers: object, lines: list[str] | None = None, **options: Any
) -> tuple[int, ScriptedDriver]:
    io = scripted(*answers, lines=lines)
    state = CliState(scope=scope, stdin_isatty=lambda: True, stdout_isatty=lambda: True)
    deps = SetupDeps(io=io, picker=PickerDeps(revalidate=lambda job: job()))
    code = run_setup(state, SetupOptions(**options), deps=deps)
    assert isinstance(io.driver, ScriptedDriver)
    return code, io.driver


def raw(scope: Scope) -> dict[str, Any]:
    return config.load(scope).user_raw()


MODEL_ANSWERS = ("OpenRouter", "Paste an API key", FAKE_KEY, "b/two", "Keep")


def test_full_first_install(clean: Scope, keyring: DictKeyring) -> None:
    config.save(clean, {"terminal": {"timeout": 99}})
    lines: list[str] = []
    code, driver = setup(
        clean,
        "Full setup",
        *MODEL_ANSWERS,
        "local",
        ["terminal —", "file —", "web —"],
        "",
        "manual",
        lines=lines,
    )
    assert code == 0, lines
    saved = raw(clean)
    assert saved["model"] == {"default": "b/two", "provider": "openrouter"}
    assert saved["platform_toolsets"] == {"cli": ["file", "terminal", "web"]}
    assert saved["onboarding"] == {"setup_completed": True}
    assert saved["terminal"] == {"timeout": 99}
    assert "agent" not in saved and "approvals" not in saved
    assert keyring.data == {("indus", f"{clean.profile_id}:OPENROUTER_API_KEY"): FAKE_KEY}
    assert FAKE_KEY not in config.config_path(clean).read_text(encoding="utf-8")
    assert "Setup complete." in lines
    assert "  Keys:       the OS keychain" + f" (also read: {clean.home / '.env'})" in lines
    assert any(line.startswith("To undo this setup:  cp ") for line in lines)
    titles = [line for line in lines if line.startswith("== ")]
    assert titles == [
        "== Model and provider ==",
        "== Terminal backend ==",
        "== Tools ==",
        "== Agent defaults ==",
        "== Messaging gateway ==",
    ]
    assert driver.answers == []


def test_backup_before_reset(clean: Scope, capsys: pytest.CaptureFixture[str]) -> None:
    config.save(clean, {"model": {"provider": "deepseek", "default": "deepseek-chat"}})
    before = config.config_path(clean).read_bytes()
    io = Interactive(tty=False)
    state = CliState(scope=clean)
    assert run_setup(state, SetupOptions(reset=True), deps=SetupDeps(io=io)) == 0
    copies = backups.list_backups(config.config_path(clean), "pre-setup")
    assert len(copies) == 1 and copies[0].read_bytes() == before
    text = config.config_path(clean).read_text(encoding="utf-8")
    assert "deepseek" not in text
    assert all(line.startswith("#") or not line.strip() for line in text.splitlines()[1:])
    out = capsys.readouterr().out
    assert "reset to the built-in values" in out and str(copies[0]) in out


def test_non_tty_prints_guidance(clean: Scope, capsys: pytest.CaptureFixture[str]) -> None:
    driver = ScriptedDriver([])
    for io, options in (
        (Interactive(tty=False, driver=driver), SetupOptions()),
        (Interactive(tty=True, driver=driver), SetupOptions(non_interactive=True)),
    ):
        state = CliState(scope=clean)
        assert run_setup(state, options, deps=SetupDeps(io=io)) == 0
        out = capsys.readouterr().out
        assert "indus config set model.provider custom" in out and "indus auth add" in out
    assert driver.asked == []
    assert not config.config_path(clean).exists()


def test_cancel_mid_way_leaves_later_sections(clean: Scope) -> None:
    lines: list[str] = []
    code, _driver = setup(clean, "Full setup", *MODEL_ANSWERS, "local", CANCEL, lines=lines)
    assert code == 1
    assert CANCELLED in lines
    saved = raw(clean)
    assert saved["model"] == {"default": "b/two", "provider": "openrouter"}
    assert "platform_toolsets" not in saved and "onboarding" not in saved


def test_single_section(clean: Scope) -> None:
    lines: list[str] = []
    code, driver = setup(clean, "40", "smart", section="agent", lines=lines)
    assert code == 0
    assert raw(clean)["agent"] == {"max_turns": 40}
    assert raw(clean)["approvals"] == {"mode": "smart"}
    assert driver.asked[0].endswith("[150]: ")
    assert "Agent defaults: done." in lines


def test_unknown_section(clean: Scope, capsys: pytest.CaptureFixture[str]) -> None:
    code, _driver = setup(clean, section="tts")
    assert code == 2
    assert "Sections: model, terminal, tools, agent, gateway" in capsys.readouterr().err


def test_managed_install_refused(clean: Scope, capsys: pytest.CaptureFixture[str]) -> None:
    managed = clean.with_env(clean.env.with_overlay({"INDUS_MANAGED": "1"}))
    code, driver = setup(managed, reset=True)
    assert code == 1 and driver.asked == []
    assert "managed" in capsys.readouterr().err
    assert not config.config_path(clean).exists()


def test_reconfigure_keeps_current_values(clean: Scope) -> None:
    secrets_of(clean).set("DEEPSEEK_API_KEY", FAKE_KEY)
    config.save(
        clean,
        {
            "model": {"provider": "deepseek", "default": "deepseek-chat"},
            "agent": {"max_turns": 77},
            "platform_toolsets": {"cli": ["terminal", "file"]},
        },
    )
    before = raw(clean)
    lines: list[str] = []
    code, driver = setup(
        clean,
        "Leave unchanged",
        "local",
        ["terminal —", "file —"],
        "",
        "manual",
        lines=lines,
    )
    assert code == 0
    after = raw(clean)
    assert after.pop("onboarding") == {"setup_completed": True}
    assert after == before
    assert "Indus Agent is already set up. Each question shows the current value;" in lines
    assert "Model calls per turn before the agent stops and sums up (0 = no cap) [77]: " in (
        driver.asked
    )


def test_quick_runs_only_missing(clean: Scope) -> None:
    config.save(clean, {"onboarding": {"setup_completed": True}})
    lines: list[str] = []
    code, driver = setup(clean, *MODEL_ANSWERS, quick=True, lines=lines)
    assert code == 0 and driver.answers == []
    assert raw(clean)["model"] == {"default": "b/two", "provider": "openrouter"}
    assert "== Terminal backend ==" not in lines
    code, driver = setup(clean, quick=True, lines=lines)
    assert code == 0 and driver.asked == []
    assert any(line.startswith("Nothing is missing.") for line in lines)


def test_quick_on_a_first_install_runs_everything(clean: Scope) -> None:
    lines: list[str] = []
    code, _driver = setup(
        clean, "Blank slate", *MODEL_ANSWERS, ["terminal —", "file —"], quick=True, lines=lines
    )
    assert code == 0
    assert "Nothing is set up yet, so the full first-time setup runs." in lines


def test_blank_slate(clean: Scope) -> None:
    lines: list[str] = []
    code, driver = setup(
        clean, "Blank slate", *MODEL_ANSWERS, ["terminal —", "file —"], lines=lines
    )
    assert code == 0
    saved = raw(clean)
    assert saved["platform_toolsets"] == {"cli": ["file", "terminal"]}
    assert "terminal" not in saved and "agent" not in saved
    assert "Commands run on this machine (local)." in lines
    assert not any("Where should commands run" in q for q in driver.asked)


def test_unavailable_backend_keeps_local(clean: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    from indus.cli.setup import sections

    monkeypatch.setattr(sections, "_backends", lambda: ["local"])  # docker not built here
    lines: list[str] = []
    code, _driver = setup(clean, "docker", section="terminal", lines=lines)
    assert code == 0
    assert "terminal" not in raw(clean)
    assert any("not available in this version" in line for line in lines)


def test_back_from_terminal_returns_to_model(clean: Scope) -> None:
    lines: list[str] = []
    code, driver = setup(
        clean,
        "Full setup",
        *MODEL_ANSWERS,
        BACK,  # at the terminal question: back to the model section's last question
        "Keep it",  # the key was saved on the first pass, so the flow now asks this instead
        "a/one",
        "high",
        "local",
        BUNDLE_TOOLS,
        "",
        "manual",
        lines=lines,
    )
    assert code == 0, lines
    saved = raw(clean)
    assert saved["model"] == {"default": "a/one", "provider": "openrouter"}
    assert saved["agent"] == {"reasoning_effort": "high"}
    assert "platform_toolsets" not in saved
    assert driver.asked.count("Provider:") == 1
    assert "Back to Model and provider." in lines
