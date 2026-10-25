"""``indus auth`` with the ChatGPT sign-in (D-16.1282): add (browser / --device), status, logout,
and the doctor rows. The flow itself is replaced by a fake at the ``run_sign_in`` seam, or runs
against the fake sign-in service; nothing reaches OpenAI."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from indus.cli.commands import auth as auth_cmd
from indus.cli.doctor import DoctorContext
from indus.cli.doctor.checks import check_auth
from indus.foundation.config import loader
from indus.foundation.scope import Scope
from indus.providers import bundled
from indus.providers.auth import pool_model
from indus.providers.auth.device_code import TokenSet
from indus.providers.auth.flows import SignInFlows
from indus.providers.auth.pkce import PkceOutcome
from indus.providers.auth.pool_model import PoolEntry, edit_pool, secret_name
from indus.providers.auth.store import AuthStore
from tests.fakes import chatgpt_auth as fake
from tests.fakes.secret_store import MemorySecretStore
from tests.unit.cli.commands.test_auth import run, stored


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


def _signed_in(scope: Scope, *, hours: float = 1.0) -> TokenSet:
    now = scope.clock.now()
    return TokenSet(
        fake.fake_jwt(1, plan="pro"),
        now,
        refresh_token=fake.fake_refresh(1),
        expires_at=now + timedelta(hours=hours),
    )


def _fake_flow(
    seen: list[tuple[SignInFlows, Any]], tokens: TokenSet, *, via_device: bool = False
) -> Any:
    def sign_in(env: object, flows: SignInFlows, options: Any) -> PkceOutcome:
        seen.append((flows, options))
        return PkceOutcome(tokens=tokens, via_device=via_device)

    return sign_in


def test_add_signs_in_with_the_browser_by_default(
    clean_scope: Scope, secrets: MemorySecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[SignInFlows, Any]] = []
    tokens = _signed_in(clean_scope)
    monkeypatch.setattr(auth_cmd, "run_sign_in", _fake_flow(seen, tokens))
    result = run(clean_scope, "auth", "add", "openai-codex", "--no-browser")
    assert result.exit_code == 0, result.output
    flows, options = seen[0]
    assert flows.provider == "openai-codex" and not options.device and options.no_browser
    doc = stored(clean_scope)
    row = doc["pool"]["openai-codex"][0]
    assert (row["auth_type"], row["source"]) == ("oauth", "pkce")
    assert TokenSet.from_secret(secrets.get(secret_name("openai-codex", row["id"])) or "") == tokens
    assert "Account plan: pro." in result.stdout
    for secret in (tokens.access_token, tokens.refresh_token or ""):
        assert secret not in result.output
        assert secret not in (clean_scope.home / "auth.json").read_text()


def test_add_with_device_runs_the_device_flow(
    clean_scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[SignInFlows, Any]] = []
    monkeypatch.setattr(
        auth_cmd, "run_sign_in", _fake_flow(seen, _signed_in(clean_scope), via_device=True)
    )
    result = run(clean_scope, "auth", "add", "chatgpt", "--device")
    assert result.exit_code == 0, result.output
    assert seen[0][1].device
    assert stored(clean_scope)["pool"]["openai-codex"][0]["source"] == "device_code"


def test_device_flow_end_to_end_against_the_fake_service(
    clean_scope: Scope, secrets: MemorySecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real ``_sign_in`` path with ``--device``: the fake service approves at once."""
    server = fake.FakeSignIn()
    flows = SignInFlows(provider="openai-codex", device=fake.DEVICE_SPEC, oauth_default=True)
    monkeypatch.setattr(auth_cmd, "flows_for", lambda _p: flows)
    monkeypatch.setattr(auth_cmd.device_code, "make_client", lambda *_a, **_k: server.client())

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(clean_scope.clock, "sleep", no_wait)
    result = run(clean_scope, "auth", "add", "openai-codex", "--device", "--no-browser")
    assert result.exit_code == 0, result.output
    assert "ABCD-1234" in result.stdout and fake.DEVICE_SPEC.verify_page in result.stdout
    row = stored(clean_scope)["pool"]["openai-codex"][0]
    kept = TokenSet.from_secret(secrets.get(secret_name("openai-codex", row["id"])) or "")
    assert kept.access_token == fake.fake_jwt(1)
    assert fake.fake_jwt(1) not in result.output and "dev-auth-1" not in result.output


def test_an_api_key_is_refused_for_a_sign_in_provider(clean_scope: Scope) -> None:
    for args in (["--api-key", "k" * 30], ["--type", "api-key"]):
        result = run(clean_scope, "auth", "add", "openai-codex", *args)
        assert result.exit_code == 1 and "takes no API key" in result.stderr


def test_device_for_a_provider_without_one_is_refused(clean_scope: Scope) -> None:
    result = run(clean_scope, "auth", "add", "openrouter", "--device")
    assert result.exit_code == 1 and "no device sign-in" in result.stderr


def _store_sign_in(scope: Scope, secrets: MemorySecretStore, tokens: TokenSet) -> PoolEntry:
    entry = PoolEntry(id="c0de0003", label="work", auth_type="oauth", source="pkce")
    secrets.set(secret_name("openai-codex", entry.id), tokens.to_secret())
    edit_pool(AuthStore(scope.home), "openai-codex", lambda p, _d: p.add(entry))
    with AuthStore(scope.home).transaction() as doc:
        doc["providers"] = {"openai-codex": {"signed_in_at": "2026-10-25T09:00:00+00:00"}}
        doc["active_provider"] = "openai-codex"
    return entry


def test_status_without_a_provider_lists_the_sign_in(
    clean_scope: Scope, secrets: MemorySecretStore
) -> None:
    empty = run(clean_scope, "auth", "status")
    assert empty.exit_code == 0 and "No credentials or sign-ins yet" in empty.stdout
    tokens = _signed_in(clean_scope, hours=2)
    _store_sign_in(clean_scope, secrets, tokens)
    result = run(clean_scope, "auth", "status")
    assert result.exit_code == 0, result.output
    assert "OpenAI (ChatGPT sign-in) (openai-codex)" in result.stdout
    assert 'sign-in "work" (pkce): token valid for 2h' in result.stdout
    assert "plan pro" in result.stdout
    assert "sign-in flows: browser (PKCE), device code" in result.stdout
    assert tokens.access_token not in result.output


def test_status_says_an_expired_token_renews_on_next_use(
    clean_scope: Scope, secrets: MemorySecretStore
) -> None:
    _store_sign_in(clean_scope, secrets, _signed_in(clean_scope, hours=-1))
    result = run(clean_scope, "auth", "status", "openai-codex")
    assert "token expired; renewed on next use" in result.stdout


def test_logout_with_the_provider_option_deletes_the_tokens(
    clean_scope: Scope, secrets: MemorySecretStore
) -> None:
    entry = _store_sign_in(clean_scope, secrets, _signed_in(clean_scope))
    result = run(clean_scope, "auth", "logout", "--provider", "openai-codex")
    assert result.exit_code == 0, result.output
    assert "Signed out of openai-codex: removed 1" in result.stdout
    assert secrets.get(secret_name("openai-codex", entry.id)) is None
    doc = stored(clean_scope)
    assert doc["pool"].get("openai-codex", []) == [] and "active_provider" not in doc
    assert AuthStore(clean_scope.home).signed_in() == []


def test_logout_needs_one_provider(clean_scope: Scope) -> None:
    assert run(clean_scope, "auth", "logout").exit_code == 2
    both = run(clean_scope, "auth", "logout", "openrouter", "--provider", "openai-codex")
    assert both.exit_code == 2


def test_doctor_reports_each_sign_in_without_its_token(
    clean_scope: Scope, secrets: MemorySecretStore
) -> None:
    tokens = _signed_in(clean_scope, hours=-1)
    _store_sign_in(clean_scope, secrets, tokens)
    findings = check_auth(DoctorContext(scope=clean_scope))
    text = "\n".join(f.message for f in findings)
    assert 'openai-codex sign-in "work": token expired; renewed on next use' in text
    assert tokens.access_token not in text

    secrets.delete(secret_name("openai-codex", "c0de0003"))
    missing = [f for f in check_auth(DoctorContext(scope=clean_scope)) if f.severity == "warn"]
    assert missing and missing[0].fix == "indus auth add openai-codex"
