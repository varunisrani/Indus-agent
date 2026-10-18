"""``indus auth`` subcommands (P07-08) through the real click app, with an in-memory SecretStore."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from indus.cli import app as app_mod
from indus.cli import prompts
from indus.cli.args import CliState
from indus.cli.commands import auth as auth_cmd
from indus.foundation import config as config_mod
from indus.foundation.config import loader
from indus.foundation.scope import Scope
from indus.providers import bundled
from indus.providers.auth import pool_model
from indus.providers.auth.device_code import DeviceFlowSpec, ReloginRequired, TokenSet
from indus.providers.auth.flows import SignInFlows
from indus.providers.auth.pkce import PkceOutcome
from indus.providers.auth.pool_model import PoolEntry, edit_pool, secret_name
from indus.providers.auth.store import AUTH_FILE, AuthStore
from tests.fakes import oauth_server as fake
from tests.fakes.prompts import scripted
from tests.fakes.secret_store import MemorySecretStore


@pytest.fixture(autouse=True)
def fresh_state() -> Iterator[None]:
    loader.reset()
    pool_model.reset_pending_requests()
    yield
    loader.reset()


@pytest.fixture
def secrets() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture
def clean_scope(scope: Scope, secrets: MemorySecretStore) -> Scope:
    cleared: dict[str, str | None] = {n: None for n in bundled.env_var_names()}
    cleared["INDUS_INFERENCE_PROVIDER"] = None
    return scope.with_env(scope.env.with_overlay(cleared)).with_secrets(secrets)


def run(scope: Scope, *args: str, tty: bool = False, answers: list[Any] | None = None) -> Result:
    state = CliState(scope=scope, stdin_isatty=lambda: tty, stdout_isatty=lambda: tty)
    if answers is None:
        return CliRunner().invoke(app_mod.cli, list(args), obj=state)
    lines: list[str] = []
    io = scripted(*answers, lines=lines)
    saved = prompts.io_factory
    prompts.io_factory = lambda _state, _guidance: io
    try:
        return CliRunner().invoke(app_mod.cli, list(args), obj=state)
    finally:
        prompts.io_factory = saved


def stored(scope: Scope) -> dict[str, Any]:
    return json.loads((scope.home / AUTH_FILE).read_text())


def with_env(scope: Scope, **values: str) -> Scope:
    return scope.with_env(scope.env.with_overlay(dict(values)))


def add_key(scope: Scope, provider: str = "openrouter", n: int = 1, *extra: str) -> str:
    key = fake.fake_token("key", n)
    result = run(scope, "auth", "add", provider, "--api-key", key, *extra)
    assert result.exit_code == 0, result.output
    return key


def test_add_api_key_stores_the_secret_in_the_store_only(
    clean_scope: Scope, secrets: MemorySecretStore
) -> None:
    key = fake.fake_token("key")
    result = run(clean_scope, "auth", "add", "or", "--api-key", key, "--label", "work")
    assert result.exit_code == 0, result.output
    assert 'Added openrouter credential #1 ("work"' in result.stdout
    assert key not in result.output
    rows = stored(clean_scope)["pool"]["openrouter"]
    assert [(r["label"], r["auth_type"], r["source"]) for r in rows] == [
        ("work", "api_key", "manual")
    ]
    assert secrets.get(secret_name("openrouter", rows[0]["id"])) == key
    assert key not in (clean_scope.home / AUTH_FILE).read_text()


def test_non_tty_add_without_key_exits_1_with_guidance(clean_scope: Scope) -> None:
    result = run(clean_scope, "auth", "add", "openrouter")
    assert result.exit_code == 1
    assert "--api-key KEY" in result.stderr
    assert not (clean_scope.home / AUTH_FILE).exists()


def test_tty_add_asks_hidden_for_the_key_and_a_label(
    clean_scope: Scope, secrets: MemorySecretStore
) -> None:
    key = fake.fake_token("typed")
    result = run(clean_scope, "auth", "add", "deepseek", tty=True, answers=[key, "mine"])
    assert result.exit_code == 0, result.output
    assert key not in result.output
    row = stored(clean_scope)["pool"]["deepseek"][0]
    assert row["label"] == "mine" and secrets.get(secret_name("deepseek", row["id"])) == key


def test_placeholder_key_refused(clean_scope: Scope) -> None:
    result = run(clean_scope, "auth", "add", "openrouter", "--api-key", "sk-or-")
    assert result.exit_code == 1 and "placeholder" in result.stderr


def test_unknown_provider_exits_1_with_close_names(clean_scope: Scope) -> None:
    result = run(clean_scope, "auth", "add", "openroutr", "--api-key", fake.fake_token("k"))
    assert result.exit_code == 1
    assert "'openroutr' is not a provider" in result.stderr
    assert "Did you mean: openrouter" in result.stderr


def test_failed_secret_write_leaves_no_row(clean_scope: Scope, secrets: MemorySecretStore) -> None:
    secrets.fail_next_set = True
    result = run(clean_scope, "auth", "add", "openrouter", "--api-key", fake.fake_token("k"))
    assert result.exit_code != 0
    assert not (clean_scope.home / AUTH_FILE).exists()


def test_failed_row_write_removes_the_secret(
    clean_scope: Scope, secrets: MemorySecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(auth_cmd, "edit_pool", boom)
    result = run(clean_scope, "auth", "add", "openrouter", "--api-key", fake.fake_token("k"))
    assert result.exit_code != 0 and secrets.names() == []


def test_list_shows_index_label_type_source_and_status(clean_scope: Scope) -> None:
    scope = with_env(clean_scope, OPENROUTER_API_KEY=fake.fake_token("env"))
    add_key(scope, "openrouter", 1, "--label", "first")
    result = run(scope, "auth", "list")
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "openrouter (2 credentials, fill_first):"
    # The env key was there first, so it keeps the front of the pool.
    assert lines[1].split()[:4] == ["#1", "OPENROUTER_API_KEY", "api_key", "env"]
    assert lines[1].endswith("ok  <- next")
    assert lines[2].split()[:4] == ["#2", "first", "api_key", "manual"]
    assert lines[2].split()[4].startswith("id=") and lines[2].endswith(" ok")


def test_list_with_nothing(clean_scope: Scope) -> None:
    result = run(clean_scope, "auth", "list")
    assert result.exit_code == 0 and "No credentials yet" in result.stdout
    result = run(clean_scope, "auth", "list", "deepseek")
    assert "No credentials for deepseek yet" in result.stdout


def test_benched_entry_shows_time_left_and_reset_clears_it(clean_scope: Scope) -> None:
    add_key(clean_scope, "openrouter", 1, "--label", "a")
    add_key(clean_scope, "openrouter", 2, "--label", "b")
    until = clean_scope.clock.now() + timedelta(minutes=5, seconds=3)
    edit_pool(
        AuthStore(clean_scope.home),
        "openrouter",
        lambda pool, _d: pool.mark_exhausted("a", "rate_limit", until),
    )
    listing = run(clean_scope, "auth", "list", "openrouter").stdout
    assert "benched (rate_limit, 5m 3s left)" in listing
    assert listing.splitlines()[2].endswith("<- next")
    result = run(clean_scope, "auth", "reset", "openrouter", "a")
    assert result.exit_code == 0 and 'credential "a"' in result.stdout
    assert "benched" not in run(clean_scope, "auth", "list", "openrouter").stdout
    result = run(clean_scope, "auth", "reset", "openrouter")
    assert "Cleared the status of 2 openrouter credential(s)." in result.stdout


def test_remove_by_label_deletes_the_secret(clean_scope: Scope, secrets: MemorySecretStore) -> None:
    add_key(clean_scope, "openrouter", 1, "--label", "a")
    add_key(clean_scope, "openrouter", 2, "--label", "b")
    result = run(clean_scope, "auth", "remove", "openrouter", "a")
    assert result.exit_code == 0 and 'Removed openrouter credential #1 ("a")' in result.stdout
    rows = stored(clean_scope)["pool"]["openrouter"]
    assert [r["label"] for r in rows] == ["b"] and rows[0]["priority"] == 0
    assert secrets.names() == [secret_name("openrouter", rows[0]["id"])]
    missing = run(clean_scope, "auth", "remove", "openrouter", "zz")
    assert missing.exit_code == 1 and "'zz'" in missing.stderr


def test_removing_an_env_entry_hides_it_until_the_next_add(clean_scope: Scope) -> None:
    scope = with_env(clean_scope, OPENROUTER_API_KEY=fake.fake_token("env"))
    result = run(scope, "auth", "remove", "openrouter", "OPENROUTER_API_KEY")
    assert result.exit_code == 0, result.output
    assert "indus config unset OPENROUTER_API_KEY" in result.stdout
    assert "No credentials for openrouter" in run(scope, "auth", "list", "openrouter").stdout
    add_key(scope)
    assert "OPENROUTER_API_KEY" in run(scope, "auth", "list", "openrouter").stdout


def test_priority_moves_and_renumbers(clean_scope: Scope) -> None:
    for n, label in enumerate(("a", "b", "c"), start=1):
        add_key(clean_scope, "openrouter", n, "--label", label)
    result = run(clean_scope, "auth", "priority", "openrouter", "c", "0")
    assert result.exit_code == 0 and 'credential "c" to priority 0' in result.stdout
    rows = stored(clean_scope)["pool"]["openrouter"]
    assert [(r["label"], r["priority"]) for r in rows] == [("c", 0), ("a", 1), ("b", 2)]
    result = run(clean_scope, "auth", "priority", "openrouter", "1", "9")
    assert "no position beyond 2" in result.stdout


def test_add_with_priority_zero_goes_first(clean_scope: Scope) -> None:
    add_key(clean_scope, "openrouter", 1, "--label", "a")
    add_key(clean_scope, "openrouter", 2, "--label", "b", "--priority", "0")
    assert [r["label"] for r in stored(clean_scope)["pool"]["openrouter"]] == ["b", "a"]


def test_refresh_rules(
    clean_scope: Scope, secrets: MemorySecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_key(clean_scope, "openrouter", 1, "--label", "key")
    result = run(clean_scope, "auth", "refresh", "openrouter")
    assert result.exit_code == 1 and "is an API key" in result.stderr

    now = clean_scope.clock.now()
    old = TokenSet(
        access_token=fake.fake_token("a", 1),
        obtained_at=now,
        refresh_token=fake.fake_token("r", 1),
        expires_at=now,
    )
    store = AuthStore(clean_scope.home)
    entry = PoolEntry(id="oa11", label="signed", auth_type="oauth", source="device_code")
    secrets.set(secret_name("openrouter", entry.id), old.to_secret())
    edit_pool(store, "openrouter", lambda pool, _d: pool.add(entry))
    result = run(clean_scope, "auth", "refresh", "openrouter")
    assert result.exit_code == 1 and "say which one" in result.stderr

    result = run(clean_scope, "auth", "refresh", "openrouter", "signed")
    assert result.exit_code == 1 and "sign in again" in result.stderr  # key mint: no token endpoint

    device = DeviceFlowSpec("openrouter", fake.DEVICE_URL, fake.TOKEN_URL, fake.CLIENT_ID)
    monkeypatch.setattr(auth_cmd, "flows_for", lambda p: SignInFlows(p, device=device))
    new = TokenSet(
        access_token=fake.fake_token("a", 2),
        obtained_at=now,
        refresh_token=fake.fake_token("r", 2),
        expires_at=now + timedelta(hours=1),
    )
    monkeypatch.setattr(auth_cmd, "run_refresh", lambda env, spec, tokens: new)
    result = run(clean_scope, "auth", "refresh", "openrouter", "signed")
    assert result.exit_code == 0, result.output
    assert TokenSet.from_secret(secrets.get(secret_name("openrouter", "oa11")) or "") == new
    row = stored(clean_scope)["pool"]["openrouter"][1]
    assert row["expires_at"] == new.expires_at.isoformat()  # type: ignore[union-attr]
    assert "token 1h left" in run(clean_scope, "auth", "list").stdout

    def dead(env: object, spec: object, tokens: object) -> TokenSet:
        raise ReloginRequired("the openrouter sign-in is no longer valid (invalid_grant)")

    monkeypatch.setattr(auth_cmd, "run_refresh", dead)
    result = run(clean_scope, "auth", "refresh", "openrouter", "signed")
    assert result.exit_code == 1 and "indus auth add openrouter --type oauth" in result.stderr


def test_oauth_add_runs_the_flow_and_stores_a_minted_key(
    clean_scope: Scope, secrets: MemorySecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    minted = fake.fake_token("minted")
    seen: list[SignInFlows] = []

    def sign_in(env: object, flows: SignInFlows, options: object) -> PkceOutcome:
        seen.append(flows)
        return PkceOutcome(api_key=minted)

    monkeypatch.setattr(auth_cmd, "run_sign_in", sign_in)
    result = run(clean_scope, "auth", "add", "openrouter", "--type", "oauth", "--no-browser")
    assert result.exit_code == 0, result.output
    assert seen[0].pkce is not None and minted not in result.output
    doc = stored(clean_scope)
    row = doc["pool"]["openrouter"][0]
    assert (row["auth_type"], row["source"]) == ("api_key", "pkce")
    assert secrets.get(secret_name("openrouter", row["id"])) == minted
    assert doc["active_provider"] == "openrouter" and "openrouter" in doc["providers"]
    assert minted not in (clean_scope.home / AUTH_FILE).read_text()


def test_oauth_add_for_a_provider_without_a_documented_flow(clean_scope: Scope) -> None:
    result = run(clean_scope, "auth", "add", "anthropic", "--type", "oauth")
    assert result.exit_code == 1
    assert "publishes no sign-in flow" in result.stderr
    assert "indus auth add anthropic --api-key KEY" in result.stderr


def test_keys_are_the_default_type_even_with_a_flow(clean_scope: Scope) -> None:
    result = run(clean_scope, "auth", "add", "openrouter")
    assert result.exit_code == 1 and "--api-key" in result.stderr  # asked for a key, no browser


def test_status_summarises(clean_scope: Scope) -> None:
    add_key(clean_scope, "openrouter", 1, "--label", "a")
    result = run(clean_scope, "auth", "status", "openrouter")
    assert result.exit_code == 0
    assert "credentials: 1, strategy fill_first" in result.stdout
    assert 'next to use: "a"' in result.stdout
    assert "sign-in flows: browser (PKCE)" in result.stdout
    assert "sign-in flows: none" in run(clean_scope, "auth", "status", "anthropic").stdout


def test_logout_clears_state_and_resets_model_provider(
    clean_scope: Scope, secrets: MemorySecretStore
) -> None:
    config_mod.save(clean_scope, {"model": {"provider": "openrouter", "default": "some/model"}})
    scope = with_env(clean_scope, OPENROUTER_API_KEY=fake.fake_token("env"))
    add_key(scope, "openrouter", 1)
    add_key(scope, "deepseek", 2)
    with AuthStore(scope.home).transaction() as doc:
        doc["active_provider"] = "openrouter"
        doc["providers"] = {"openrouter": {"signed_in_at": "x"}}
    result = run(scope, "auth", "logout", "or")
    assert result.exit_code == 0, result.output
    assert "removed 1 stored credential(s)" in result.stdout
    assert "model.provider was openrouter; it is now unset" in result.stdout
    assert "OPENROUTER_API_KEY still holds a key" in result.stdout
    doc = stored(scope)
    assert "active_provider" not in doc and doc["providers"] == {}
    assert [r["source"] for r in doc["pool"]["openrouter"]] == ["env"]
    assert all(not n.startswith("AUTH_OPENROUTER_") for n in secrets.names())
    assert any(n.startswith("AUTH_DEEPSEEK_") for n in secrets.names())
    view = config_mod.load(scope)
    assert view.get("model.provider") == "" and view.get("model.default") == "some/model"


def test_logout_keeps_another_provider_setting(clean_scope: Scope) -> None:
    config_mod.save(clean_scope, {"model": {"provider": "deepseek"}})
    result = run(clean_scope, "auth", "logout", "openrouter")
    assert result.exit_code == 0 and "Nothing was stored" in result.stdout
    assert config_mod.load(clean_scope).get("model.provider") == "deepseek"


def test_bare_auth_without_a_terminal_lists_and_exits_0(clean_scope: Scope) -> None:
    add_key(clean_scope)
    result = run(clean_scope, "auth")
    assert result.exit_code == 0
    assert "openrouter (1 credential" in result.stdout and "indus auth --help" in result.stdout


def test_bare_auth_menu_sets_a_strategy(clean_scope: Scope) -> None:
    result = run(clean_scope, "auth", tty=True, answers=[3, "openrouter", 1])
    assert result.exit_code == 0, result.output
    assert "openrouter now uses round_robin." in result.stdout
    assert config_mod.load(clean_scope).get("credential_pool_strategies") == {
        "openrouter": "round_robin"
    }


def test_inherited_pool_is_read_only_in_a_named_profile(clean_scope: Scope) -> None:
    root = clean_scope.root
    (root / AUTH_FILE).write_text(
        json.dumps(
            {
                "version": 1,
                "providers": {},
                "pool": {"openrouter": [{"id": "r1", "label": "root-key"}]},
            }
        )
    )
    listing = run(clean_scope, "auth", "list", "openrouter")
    assert "from the default profile" in listing.stdout and "root-key" in listing.stdout
    result = run(clean_scope, "auth", "remove", "openrouter", "1")
    assert result.exit_code == 1 and "read-only here" in result.stderr


def test_login_is_not_a_command_and_points_at_auth_add(clean_scope: Scope) -> None:
    result = run(clean_scope, "login")
    assert result.exit_code == 2
    assert "indus auth add PROVIDER" in result.stderr


def test_unknown_auth_subcommand_three_lines(clean_scope: Scope) -> None:
    result = run(clean_scope, "auth", "upgrade")
    assert result.exit_code == 2
    assert result.stderr.splitlines()[0] == "'upgrade' is not an `indus auth` command."


def test_help_lists_every_subcommand(clean_scope: Scope) -> None:
    result = run(clean_scope, "auth", "--help")
    assert result.exit_code == 0
    for name in ("add", "list", "remove", "reset", "priority", "refresh", "status", "logout"):
        assert f"  {name} " in result.stdout
    assert "upgrade" not in result.stdout and "login" not in result.stdout


def test_registered_in_the_top_level_list() -> None:
    assert ("auth", "indus.cli.commands.auth:auth_group") in [
        (n, t) for n, t, _ in app_mod.REGISTRY
    ]


def test_real_secret_store_never_writes_into_auth_json(scope: Scope, tmp_path: Path) -> None:
    """With the P01 file-backed store, the key lands in secrets.json and never in auth.json."""
    cleared: dict[str, str | None] = {n: None for n in bundled.env_var_names()}
    real = scope.with_env(scope.env.with_overlay(cleared))
    key = fake.fake_token("file")
    assert run(real, "auth", "add", "openrouter", "--api-key", key).exit_code == 0
    assert key not in (real.home / AUTH_FILE).read_text()
    assert key in (real.home / "secrets.json").read_text()
