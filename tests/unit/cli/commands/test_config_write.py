"""P07-12: `indus config set/unset` routing and coercion (R04.7, R04.8, R04.9, R04.12)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from indus.foundation import yamlio
from indus.foundation.config import loader
from indus.foundation.envfile import read_env_file
from indus.foundation.scope import Scope
from indus.foundation.secrets import KeychainBackend, SecretStore, open_secret_store
from tests.unit.cli.commands.cmdrun import run_cli, write_settings

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "config"
FAKE_KEY = "test" + "-key-" + "0000abcd1234wxyz"


class FakeKeyring:
    """An in-memory keyring backend (never the OS keychain)."""

    priority = 1

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str, /) -> str | None:
        return self.data.get((service, username))

    def set_password(self, service: str, username: str, password: str, /) -> None:
        self.data[(service, username)] = password

    def delete_password(self, service: str, username: str, /) -> None:
        del self.data[(service, username)]


def _keychain_scope(scope: Scope) -> tuple[Scope, FakeKeyring]:
    keyring = FakeKeyring()
    store = SecretStore(
        KeychainBackend(keyring, scope.profile_id), scope.home / "secrets.index.json"
    )
    return scope.with_secrets(store), keyring


def _raw(scope: Scope) -> dict[str, Any]:
    path = scope.home / "config.yaml"
    if not path.exists():
        return {}
    loaded = yamlio.load_safe(path.read_bytes())
    return loaded if isinstance(loaded, dict) else {}


def _set(capsys: pytest.CaptureFixture[str], scope: Scope, *args: str) -> Any:
    return run_cli(capsys, scope, "config", "set", *args)


# -- coercion (R04.7) -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "typed", "stored"),
    [
        ("approvals.mode", "off", "off"),  # string slot keeps the literal
        ("display.language", "no", "no"),
        ("agent.max_turns", "42", 42),
        ("display.cli_refresh_interval", "2.5", 2.5),
        ("display.show_reasoning", "true", True),
        ("display.bell_on_prompt", "off", False),  # bool slot reads the word
        ("custom_nothing", "null", None),
        ("approvals.deny", "[rm -rf build, mkfs]", ["rm -rf build", "mkfs"]),
        ("timezone", "'Asia/Kolkata'", "'Asia/Kolkata'"),
        ("custom_thing", "'quoted'", "quoted"),
        ("custom_count", "12", 12),
    ],
)
def test_coercion_table(
    capsys: pytest.CaptureFixture[str], scope: Scope, key: str, typed: str, stored: Any
) -> None:
    ran = _set(capsys, scope, key, typed, "--force")
    assert ran.code == 0, ran.err
    node: Any = _raw(scope)
    for part in key.split("."):
        node = node[part]
    assert node == stored
    assert type(node) is type(stored)


def test_malformed_structured_value_refused(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    ran = _set(capsys, scope, "approvals.deny", "[rm -rf build")
    assert ran.code == 1
    assert "Nothing was written." in ran.err
    assert not (scope.home / "config.yaml").exists()


def test_section_and_container_guards(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    write_settings(scope, "agent:\n  max_turns: 5\n")
    ran = _set(capsys, scope, "agent", "7")
    assert ran.code == 1
    assert "is a section" in ran.err
    assert _raw(scope)["agent"] == {"max_turns": 5}
    ran = _set(capsys, scope, "approvals.deny", "rm")
    assert ran.code == 1
    assert "must be a list" in ran.err
    ran = _set(capsys, scope, "agent", "7", "--force")
    assert ran.code == 0
    assert _raw(scope)["agent"] == 7


def test_model_shorthand_kept_with_provider(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    write_settings(scope, "model: test-model\n")
    ran = _set(capsys, scope, "model.provider", "openrouter")
    assert ran.code == 0, ran.err
    assert _raw(scope)["model"] == {"default": "test-model", "provider": "openrouter"}
    ran = _set(capsys, scope, "model", "other-model")  # redirected to model.default
    assert ran.code == 0, ran.err
    assert _raw(scope)["model"]["default"] == "other-model"


# -- dotted keys (R04.8) --------------------------------------------------------------------------


def test_dotted_keys(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    write_settings(
        scope,
        "providers:\n  p:\n    base_url: http://127.0.0.1:8000/v1\n    models:\n      grok-4.6:\n"
        "        context_length: 1000\n",
    )
    ran = _set(capsys, scope, "providers.p.models.grok-4.6.context_length", "2000")
    assert ran.code == 0, ran.err
    assert _raw(scope)["providers"]["p"]["models"] == {"grok-4.6": {"context_length": 2000}}
    ran = _set(capsys, scope, "providers.p.models.grok-4.x", "1")
    assert ran.code == 1
    assert "beside the existing key" in ran.err
    ran = _set(capsys, scope, "providers.p.models.grok-5\\.1.context_length", "3000")
    assert ran.code == 0, ran.err
    assert _raw(scope)["providers"]["p"]["models"]["grok-5.1"] == {"context_length": 3000}


def test_bad_keys_refused(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    for key in ("a..b", "a.", ".a", " a", ""):
        ran = _set(capsys, scope, key, "1")
        assert ran.code == 1, key
        assert "Invalid key" in ran.err
    assert not (scope.home / "config.yaml").exists()


# -- routing --------------------------------------------------------------------------------------


def test_credential_goes_to_keychain(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    scope, keyring = _keychain_scope(scope)
    env_file = scope.home / ".env"
    env_file.write_text("OPENAI_API_KEY=older-copy\nOTHER=1\n", encoding="utf-8")
    ran = _set(capsys, scope, "openai_api_key", FAKE_KEY)
    assert ran.code == 0, ran.err
    assert "Set OPENAI_API_KEY in the OS keychain" in ran.out
    assert keyring.data[("indus", f"{scope.profile_id}:OPENAI_API_KEY")] == FAKE_KEY
    assert read_env_file(env_file) == {"OTHER": "1"}
    assert FAKE_KEY not in ran.out
    assert not (scope.home / "config.yaml").exists()


def test_credential_goes_to_env_file_when_file_storage_chosen(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    scope, keyring = _keychain_scope(scope)
    write_settings(scope, "security:\n  secret_store: file\n")
    ran = _set(capsys, scope, "MY_SERVICE_TOKEN", FAKE_KEY)
    assert ran.code == 0, ran.err
    assert read_env_file(scope.home / ".env") == {"MY_SERVICE_TOKEN": FAKE_KEY}
    assert keyring.data == {}


def test_upper_snake_goes_to_env_file(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    for extra in ((), ("--force",)):
        ran = _set(capsys, scope, "OPENAI_BASE_URL", "http://127.0.0.1:1234/v1", *extra)
        assert ran.code == 0, ran.err
    assert read_env_file(scope.home / ".env") == {"OPENAI_BASE_URL": "http://127.0.0.1:1234/v1"}
    assert "OPENAI_BASE_URL" not in _raw(scope)


@pytest.mark.parametrize("name", ["LD_PRELOAD", "PATH", "INDUS_YOLO_MODE", "GIT_ASKPASS"])
def test_denied_env_names_refused(
    capsys: pytest.CaptureFixture[str], scope: Scope, name: str
) -> None:
    ran = _set(capsys, scope, name, "x", "--force")
    assert ran.code == 1
    assert "Nothing was written." in ran.err
    assert not (scope.home / ".env").exists()


def test_denied_credential_shaped_name_refused(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    ran = _set(capsys, scope, "LD_SOME_TOKEN", "x")
    assert ran.code == 1
    assert open_secret_store(scope.home, scope.profile_id, mode="file").names() == []


def test_managed_install_refused(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    (scope.home / ".managed").touch()
    for args in (("agent.max_turns", "3"), ("SOME_SETTING", "1"), ("A_API_KEY", "x")):
        ran = _set(capsys, scope, *args)
        assert ran.code == 1
        assert "managed" in ran.err
    assert run_cli(capsys, scope, "config", "unset", "agent.max_turns").code == 1
    assert not (scope.home / "config.yaml").exists()


def test_wrong_prefix_refused_unless_forced(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    ran = _set(capsys, scope, "gateway.agent.max_turns", "3")
    assert ran.code == 1
    assert "agent.max_turns" in ran.err
    assert not (scope.home / "config.yaml").exists()
    ran = _set(capsys, scope, "gateway.agent.max_turns", "3", "--force")
    assert ran.code == 0
    assert _raw(scope)["gateway"] == {"agent": {"max_turns": 3}}


def test_unknown_key_written_with_notice(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = _set(capsys, scope, "agent.max_turn", "3")
    assert ran.code == 0
    assert "Did you mean: agent.max_turns?" in ran.out
    assert _raw(scope)["agent"] == {"max_turn": 3}
    ran = _set(capsys, scope, "my_own_setting", "x")
    assert ran.code == 0
    assert "not a setting this version knows" in ran.out


def test_api_base_alias(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = _set(capsys, scope, "model.api_base", "http://127.0.0.1:5000/v1")
    assert ran.code == 0, ran.err
    assert "model.base_url" in ran.out
    assert _raw(scope)["model"] == {"base_url": "http://127.0.0.1:5000/v1"}


# -- provider switch (R04.9) ----------------------------------------------------------------------


def test_provider_switch_drops_stale_url(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    write_settings(
        scope,
        "model:\n  provider: openai\n  default: m\n  base_url: https://api.openai.com/v1\n"
        "  api_mode: chat_completions\n",
    )
    ran = _set(capsys, scope, "model.provider", "openrouter")
    assert ran.code == 0, ran.err
    assert "Cleared model.base_url (https://api.openai.com/v1)" in ran.out
    assert _raw(scope)["model"] == {"provider": "openrouter", "default": "m"}


@pytest.mark.parametrize(
    ("url", "provider"),
    [
        ("http://127.0.0.1:11434/v1", "openrouter"),  # local server: kept
        ("https://openrouter.ai/api/v1", "openrouter"),  # the new provider's own host
        ("https://example.test/v1", "custom"),  # a custom endpoint keeps its URL
    ],
)
def test_provider_switch_keeps_matching_url(
    capsys: pytest.CaptureFixture[str], scope: Scope, url: str, provider: str
) -> None:
    write_settings(scope, f"model:\n  provider: openai\n  base_url: {url}\n")
    ran = _set(capsys, scope, "model.provider", provider)
    assert ran.code == 0, ran.err
    assert _raw(scope)["model"]["base_url"] == url
    assert "Cleared" not in ran.out


def test_set_keeps_comments_round_trip(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    path = scope.home / "config.yaml"
    shutil.copy(FIXTURES / "commented.yaml", path)
    loader.invalidate(path)
    before = path.read_text(encoding="utf-8")
    ran = _set(capsys, scope, "logging.level", "ERROR")
    assert ran.code == 0, ran.err
    assert path.read_text(encoding="utf-8") == before.replace("level: WARNING", "level: ERROR")


def test_unreadable_file_refused(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    path = write_settings(scope, "agent: [broken\n")
    before = path.read_bytes()
    ran = _set(capsys, scope, "agent.max_turns", "3")
    assert ran.code == 1
    assert path.read_bytes() == before
    loader.reset()


# -- unset ----------------------------------------------------------------------------------------


def test_unset_routes_and_missing(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    scope, keyring = _keychain_scope(scope)
    assert _set(capsys, scope, "OPENAI_API_KEY", FAKE_KEY).code == 0
    (scope.home / ".env").write_text("OPENAI_API_KEY=old\nPLAIN_SETTING=1\n", encoding="utf-8")
    write_settings(scope, "agent:\n  max_turns: 5\n")

    ran = run_cli(capsys, scope, "config", "unset", "OPENAI_API_KEY")
    assert ran.code == 0, ran.err
    assert keyring.data == {}
    assert "OPENAI_API_KEY" not in read_env_file(scope.home / ".env")

    assert run_cli(capsys, scope, "config", "unset", "PLAIN_SETTING").code == 0
    assert read_env_file(scope.home / ".env") == {}

    assert run_cli(capsys, scope, "config", "unset", "agent.max_turns").code == 0
    assert "agent" not in _raw(scope)

    for key in ("agent.max_turns", "PLAIN_SETTING", "OPENAI_API_KEY"):
        ran = run_cli(capsys, scope, "config", "unset", key)
        assert ran.code == 1
        assert f"Config key not set: {key}" in ran.err


def test_unset_prunes_the_keys_stored_pool_rows(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    import json

    from indus.providers.auth.store import AUTH_FILE

    scope, _keyring = _keychain_scope(scope)
    assert _set(capsys, scope, "OPENROUTER_API_KEY", FAKE_KEY).code == 0
    env_row = {"id": "env-openrouter_api_key", "source": "env", "env_var": "OPENROUTER_API_KEY"}
    manual = {"id": "ab12cd34", "source": "manual", "label": "mine"}
    doc = {"version": 1, "pool": {"openrouter": [env_row, manual], "x": [env_row]}}
    (scope.home / AUTH_FILE).write_text(json.dumps(doc), encoding="utf-8")
    assert run_cli(capsys, scope, "config", "unset", "OPENROUTER_API_KEY").code == 0
    pools = json.loads((scope.home / AUTH_FILE).read_text(encoding="utf-8"))["pool"]
    assert pools == {"openrouter": [manual]}
