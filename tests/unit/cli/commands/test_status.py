"""P07-21: `indus status` and `indus dump` — goldens with a fake environment; no secret leaks."""

from __future__ import annotations

from pathlib import Path

import pytest

from indus.foundation.scope import EnvView, Scope
from indus.foundation.secrets import open_secret_store
from tests.unit.cli.commands._helpers_d import (  # noqa: F401
    aged,
    fake_clock,
    run,
    seeded,
    store_globals,
)

OPENROUTER = "sk-or-" + "v1-" + "Q7x" * 14
ANTHROPIC = "sk-ant-" + "api03-" + "Lm4" * 14
CUSTOM = "cust" + "-" + "Rr8" * 10
STORED = "gsk" + "_" + "Hh2" * 14


@pytest.fixture
def fake_env(scope: Scope) -> Scope:
    """A home with two provider keys in the env, one in the secret store, a custom endpoint."""
    home = scope.home
    (home / "config.yaml").write_text(
        "model:\n  default: test-model-1\n  provider: openrouter\n"
        "providers:\n  myco:\n    base_url: https://llm.example.test/v1\n"
        f"    api_key: {CUSTOM}\n"
        "terminal:\n  backend: local\n"
    )
    env = EnvView(
        {
            "INDUS_HOME": str(home),
            "OPENROUTER_API_KEY": OPENROUTER,
            "ANTHROPIC_API_KEY": ANTHROPIC,
        }
    )
    store = open_secret_store(home, scope.profile_id, mode="file")
    store.set("XAI_API_KEY", STORED)
    return scope.with_env(env).with_secrets(store)


def _no_secrets(text: str) -> None:
    for value in (OPENROUTER, ANTHROPIC, CUSTOM, STORED):
        assert value not in text
        assert value[-8:] not in text


def _norm(text: str, scope: Scope) -> str:
    return text.replace(str(scope.home), "<HOME>")


def test_status_golden(fake_env: Scope) -> None:
    with seeded(fake_env) as store:
        aged(store, "s1", 1)
        aged(store, "s2", 2)
    result = run(fake_env, "status")
    assert result.code == 0, result.err
    lines = _norm(result.out, fake_env).splitlines()
    assert lines[:4] == [
        "Indus Agent status",
        "  Profile:      work (<HOME>)",
        "  Model:        test-model-1",
        "  Provider:     openrouter (from config.yaml)",
    ]
    assert lines[4] == "  Credentials:  openrouter, anthropic, xai, myco"
    assert lines[5] == "  Signed in:    none"
    assert lines[6] == "  Terminal:     local"
    assert lines[7].startswith("  Sessions:     2 session(s), ")
    assert lines[8:] == ["  Gateway:      not running", "  Scheduled:    0 jobs"]
    _no_secrets(result.out + result.err)


def test_status_full_and_deep_show_names_only(fake_env: Scope) -> None:
    with seeded(fake_env) as store:
        aged(store, "s1", 1)
    full = run(fake_env, "status", "--full")
    assert full.code == 0, full.err
    text = _norm(full.out, fake_env)
    assert "  Config:       <HOME>/config.yaml" in text
    assert "  OPENROUTER_API_KEY           set" in text
    assert "  XAI_API_KEY                  set" in text
    assert "  OPENAI_API_KEY               not set" in text
    assert "  providers.myco.api_key" in text and "  model.default" in text
    assert "Deep checks" not in text
    deep = run(fake_env, "status", "--deep")
    assert "  Store check:  ok" in deep.out
    assert "  Backend:      local commands run on this machine" in deep.out
    _no_secrets(full.out + deep.out + full.err + deep.err)


def test_status_without_any_provider(scope: Scope) -> None:
    bare = scope.with_env(EnvView({"INDUS_HOME": str(scope.home)}))
    result = run(bare, "status")
    assert result.code == 0
    assert "Provider:     none:" in result.out and "indus setup" in result.out
    assert "Credentials:  none" in result.out and "Signed in:    none" in result.out
    assert "0 sessions (nothing saved yet)" in result.out
    assert not (scope.home / "state.db").exists()


def test_dump_golden_without_values(fake_env: Scope) -> None:
    result = run(fake_env, "dump")
    assert result.code == 0, result.err
    lines = _norm(result.out, fake_env).splitlines()
    assert lines[0] == "--- indus dump ---" and lines[-1] == "--- end dump ---"
    assert lines[1].startswith("version:    Indus Agent ")
    assert "profile:    work" in lines and "home:       <HOME>" in lines
    assert "model:      test-model-1" in lines and "provider:   openrouter" in lines
    assert "providers:  openrouter, anthropic, xai, myco" in lines
    assert "  OPENROUTER_API_KEY           set" in lines
    assert "  XAI_API_KEY                  set" in lines
    assert "  OPENAI_API_KEY               not set" in lines
    assert "  providers.myco.api_key" in lines
    _no_secrets(result.out + result.err)


def test_dump_show_keys_masks(fake_env: Scope) -> None:
    result = run(fake_env, "dump", "--show-keys")
    assert result.code == 0
    assert f"  OPENROUTER_API_KEY           {OPENROUTER[:4]}…{OPENROUTER[-4:]}" in result.out
    for value in (OPENROUTER, ANTHROPIC, STORED, CUSTOM):
        assert value not in result.out


def test_short_keys_are_never_partly_shown(scope: Scope, tmp_path: Path) -> None:
    short = "abc" + "12345"
    env = EnvView({"INDUS_HOME": str(scope.home), "OPENAI_API_KEY": short})
    result = run(scope.with_env(env), "dump", "--show-keys")
    assert short not in result.out


def test_status_and_dump_count_sign_ins(scope: Scope) -> None:
    import json

    from indus.providers.auth.store import AUTH_FILE

    bare = scope.with_env(EnvView({"INDUS_HOME": str(scope.home)}))
    doc = {
        "version": 1,
        "providers": {"deepseek": {"signed_in_at": "2026-01-01T00:00:00+00:00", "flow": "pkce"}},
        "pool": {"openrouter": [{"id": "o1", "label": "sign-in-1", "auth_type": "oauth"}]},
    }
    (scope.home / AUTH_FILE).write_text(json.dumps(doc), encoding="utf-8")
    status = run(bare, "status")
    assert "  Signed in:    deepseek, openrouter" in status.out
    assert "Credentials:  " in status.out and "openrouter" in status.out.split("Credentials:")[1]
    dump = run(bare, "dump")
    assert "signed in:  deepseek, openrouter" in dump.out
