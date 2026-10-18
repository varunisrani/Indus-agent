"""The single child-environment builder (P05-05, R02.22)."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from indus.foundation import envload
from indus.foundation.scope import Scope, make_scope
from indus.safety import child_env as ce


class FakeConfig:
    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = dict(data)

    def get(self, path: str, /) -> object:
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return None
            node = node[part]
        return node


class FakeSecrets:
    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def get(self, name: str, /) -> str | None:
        return self._values.get(name)

    def known_values(self) -> list[str]:
        return list(self._values.values())

    @property
    def generation(self) -> int:
        return 1


@pytest.fixture(autouse=True)
def _fresh_registries(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(ce, "_registered", {})
    monkeypatch.setattr(ce, "_passthrough", set())
    monkeypatch.setattr(ce, "_warned", set())
    monkeypatch.setattr(envload, "_managed_registry", set())
    yield


def fake(name: str) -> str:
    return "fake-value-for-" + name.lower()


def scope_with(
    tmp_path: Path,
    env: Mapping[str, str],
    *,
    config: Mapping[str, Any] | None = None,
    secrets: Mapping[str, str] | None = None,
    name: str = "home",
) -> Scope:
    scope = make_scope(tmp_path / name, env=env)
    if config is not None:
        scope = scope.with_config(FakeConfig(config))
    if secrets is not None:
        scope = scope.with_secrets(FakeSecrets(secrets))
    return scope


# Names a fake provider registry declares: keys and base URLs, some outside our static floor.
FAKE_REGISTRY = {
    "acme": ("ACME_LLM_KEY", "ACME_LLM_BASE_URL"),
    "zeta": ("ZETA_INFERENCE_TOKEN",),
    "omni": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
}


def test_fake_registry_names_never_reach_children(tmp_path: Path) -> None:
    declared = [n for names in FAKE_REGISTRY.values() for n in names]
    ce.register_blocked_names("providers", declared)
    env = {n: fake(n) for n in declared} | {"PATH": "/usr/bin", "EDITOR": "vi"}
    out = ce.child_env(scope_with(tmp_path, env))
    assert not set(declared) & set(out)
    assert out["PATH"] == "/usr/bin" and out["EDITOR"] == "vi"


def test_managed_key_registry_is_a_source(tmp_path: Path) -> None:
    envload.register_managed_keys("ACME_LLM_KEY")
    out = ce.child_env(scope_with(tmp_path, {"ACME_LLM_KEY": fake("x")}))
    assert "ACME_LLM_KEY" not in out


def test_custom_provider_key_env_from_config_is_blocked(tmp_path: Path) -> None:
    config = {"providers": {"mine": {"base_url": "http://localhost:9", "key_env": "MY_LLM_KEY"}}}
    out = ce.child_env(scope_with(tmp_path, {"MY_LLM_KEY": fake("k")}, config=config))
    assert "MY_LLM_KEY" not in out


@pytest.mark.parametrize("name", sorted(ce.STATIC_BLOCKED))
def test_static_floor_is_dropped(tmp_path: Path, name: str) -> None:
    out = ce.child_env(scope_with(tmp_path, {name: fake(name)}))
    assert name not in out


def test_matching_ignores_case(tmp_path: Path) -> None:
    out = ce.child_env(scope_with(tmp_path, {"openai_api_key": fake("a"), "Gh_Token": fake("b")}))
    assert "openai_api_key" not in out and "Gh_Token" not in out


def test_aws_chain_kept_but_bedrock_bearer_dropped(tmp_path: Path) -> None:
    chain = [
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
    ]
    env = {n: fake(n) for n in chain} | {"AWS_BEARER_TOKEN_BEDROCK": fake("bedrock")}
    out = ce.child_env(scope_with(tmp_path, env))
    assert all(out[n] == fake(n) for n in chain)
    assert "AWS_BEARER_TOKEN_BEDROCK" not in out


def test_users_own_secret_names_stay_in_default_variant(tmp_path: Path) -> None:
    # Credentials of the user's other tools (another CLI's own login included) are not ours.
    own = [
        "MY_APP_KEY",
        "DEPLOY_WEBHOOK_SECRET",
        "SLACK_USER_TOKEN",
        "LOCAL_LLM_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ]
    out = ce.child_env(scope_with(tmp_path, {n: fake(n) for n in own}))
    assert all(out[n] == fake(n) for n in own)


def test_product_names_dropped_and_pins_set(tmp_path: Path) -> None:
    env = {
        "INDUS_HOME": "/somewhere/else",
        "INDUS_PROFILE": "other",
        "INDUS_YOLO": "1",
        "INDUS_DASHBOARD_TOKEN": fake("dash"),
        "_INDUS_INTERNAL": "x",
        "AI_AGENT": "someone-else",
    }
    scope = scope_with(tmp_path, env)
    out = ce.child_env(scope, session_id="sess-123")
    assert out["INDUS_HOME"] == str(scope.home)
    assert out["INDUS_SESSION_ID"] == "sess-123"
    assert out["AI_AGENT"] == "indus-agent" and out["INDUS_AGENT"] == "true"
    for name in ("INDUS_PROFILE", "INDUS_YOLO", "INDUS_DASHBOARD_TOKEN", "_INDUS_INTERNAL"):
        assert name not in out


def test_no_session_id_means_no_session_var(tmp_path: Path) -> None:
    out = ce.child_env(scope_with(tmp_path, {"INDUS_SESSION_ID": "stale"}))
    assert "INDUS_SESSION_ID" not in out


def test_two_scopes_pin_different_homes(tmp_path: Path) -> None:
    env = {"INDUS_HOME": str(tmp_path / "launch")}
    a = ce.child_env(scope_with(tmp_path, env, name="a"))
    b = ce.child_env(scope_with(tmp_path, env, name="b"))
    assert a["INDUS_HOME"] == str(tmp_path / "a")
    assert b["INDUS_HOME"] == str(tmp_path / "b")


def test_scope_env_is_the_base_not_the_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROCESS_ONLY_MARKER", "1")
    out = ce.child_env(scope_with(tmp_path, {"PATH": "/bin"}))
    assert "PROCESS_ONLY_MARKER" not in out


def test_extra_values_merge_and_are_filtered(tmp_path: Path) -> None:
    extra = {"MY_CUSTOM_VAR": "caller", "OPENAI_BASE_URL": "http://override"}
    out = ce.child_env(scope_with(tmp_path, {"MY_CUSTOM_VAR": "parent"}), extra=extra)
    assert out["MY_CUSTOM_VAR"] == "caller"
    assert "OPENAI_BASE_URL" not in out


def test_force_prefix_from_env_and_extra(tmp_path: Path) -> None:
    env = {"OPENAI_API_KEY": fake("parent"), ce.FORCE_PREFIX + "OPENAI_API_KEY": "forced-from-env"}
    extra = {ce.FORCE_PREFIX + "OPENAI_BASE_URL": "forced-from-extra"}
    out = ce.child_env(scope_with(tmp_path, env), extra=extra)
    assert out["OPENAI_API_KEY"] == "forced-from-env"
    assert out["OPENAI_BASE_URL"] == "forced-from-extra"
    assert not [k for k in out if k.startswith(ce.FORCE_PREFIX)]


def test_force_cannot_override_pins(tmp_path: Path) -> None:
    scope = scope_with(tmp_path, {})
    out = ce.child_env(scope, extra={ce.FORCE_PREFIX + "INDUS_HOME": "/elsewhere"})
    assert out["INDUS_HOME"] == str(scope.home)


def test_passthrough_value_comes_from_scope_secrets(tmp_path: Path) -> None:
    config = {"terminal": {"env_passthrough": ["SERVICE_TOKEN", "TENOR_API_KEY"]}}
    scope = scope_with(
        tmp_path,
        {"SERVICE_TOKEN": "launch-profile-value"},
        config=config,
        secrets={"SERVICE_TOKEN": "this-profile-value"},
    )
    out = ce.child_env(scope)
    assert out["SERVICE_TOKEN"] == "this-profile-value"
    assert "TENOR_API_KEY" not in out  # declared but no value anywhere: omitted


def test_passthrough_missing_value_never_read_from_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVICE_TOKEN", "process-value")
    config = {"terminal": {"env_passthrough": ["SERVICE_TOKEN"]}}
    out = ce.child_env(scope_with(tmp_path, {}, config=config, secrets={}))
    assert "SERVICE_TOKEN" not in out


def test_passthrough_of_blocked_name_is_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = {"terminal": {"env_passthrough": ["OPENAI_API_KEY", "openai_api_key", "OK_NAME"]}}
    scope = scope_with(
        tmp_path,
        {"OPENAI_API_KEY": fake("o")},
        config=config,
        secrets={"OPENAI_API_KEY": fake("s"), "OK_NAME": "v"},
    )
    with caplog.at_level(logging.WARNING, logger=ce.__name__):
        out = ce.child_env(scope)
        ce.child_env(scope)
    assert "OPENAI_API_KEY" not in out and "openai_api_key" not in out
    assert out["OK_NAME"] == "v"
    accepted, refused = ce.passthrough_names(scope)
    assert accepted == ["OK_NAME"] and set(refused) == {"OPENAI_API_KEY", "openai_api_key"}
    assert sum("OPENAI_API_KEY" in r.getMessage() for r in caplog.records) == 1


def test_registered_passthrough_and_late_blocking(tmp_path: Path) -> None:
    assert ce.register_passthrough(["SKILL_API_KEY", "GITHUB_TOKEN", " "]) == ["GITHUB_TOKEN"]
    scope = scope_with(tmp_path, {}, secrets={"SKILL_API_KEY": "v1"})
    assert ce.child_env(scope)["SKILL_API_KEY"] == "v1"
    ce.register_blocked_names("late-adapter", ["skill_api_key"])
    assert "SKILL_API_KEY" not in ce.child_env(scope)
    ce.unregister_blocked_names("late-adapter")
    assert ce.child_env(scope)["SKILL_API_KEY"] == "v1"
    ce.clear_passthrough()
    assert "SKILL_API_KEY" not in ce.child_env(scope)


def test_minimal_variant_is_exact(tmp_path: Path) -> None:
    env = {n: "v-" + n for n in ce.MINIMAL_NAMES} | {
        "XDG_CONFIG_HOME": "/x",
        "EDITOR": "vi",
        "GITHUB_TOKEN": fake("g"),
    }
    configured = {"GITHUB_PERSONAL_ACCESS_TOKEN": fake("pat"), "SERVER_MODE": "stdio"}
    out = ce.child_env(scope_with(tmp_path, env), variant="minimal", extra=configured)
    assert set(out) == set(ce.MINIMAL_NAMES) | set(configured)
    assert out["GITHUB_PERSONAL_ACCESS_TOKEN"] == fake("pat")


def test_code_variant_drops_credential_words_and_unsafe_prefixes(tmp_path: Path) -> None:
    env = {
        "PATH": "/bin",
        "HOME": "/home/u",
        "LANG": "C.UTF-8",
        "LC_CTYPE": "C",
        "XDG_DATA_HOME": "/d",
        "PYTHONPATH": "/p",
        "MY_DB_DSN": "postgres://u@h/db",
        "PATH_TOKEN": "x",
        "LC_SECRET": "y",
        "SERVICE_COOKIE": "z",
        "HOME_AUTH": "w",
        "EDITOR": "vi",
        "PAGER": "less",
    }
    config = {"terminal": {"env_passthrough": ["TENOR_API_KEY"]}}
    scope = scope_with(tmp_path, env, config=config, secrets={"TENOR_API_KEY": "t"})
    out = ce.child_env(scope, variant="code", session_id="s1")
    for kept in ("PATH", "HOME", "LANG", "LC_CTYPE", "XDG_DATA_HOME", "PYTHONPATH"):
        assert out[kept] == env[kept]
    for dropped in (
        "MY_DB_DSN",
        "PATH_TOKEN",
        "LC_SECRET",
        "SERVICE_COOKIE",
        "HOME_AUTH",
        "EDITOR",
    ):
        assert dropped not in out
    assert out["TENOR_API_KEY"] == "t"
    assert out["INDUS_HOME"] == str(scope.home) and out["INDUS_SESSION_ID"] == "s1"


def test_runtime_location_markers(tmp_path: Path) -> None:
    other = tmp_path / "project" / ".venv"
    other.mkdir(parents=True)
    env = {"PYTHONHOME": "/opt/py", "VIRTUAL_ENV": sys.prefix, "CONDA_PREFIX": str(other)}
    out = ce.child_env(scope_with(tmp_path, env))
    assert "PYTHONHOME" not in out and "VIRTUAL_ENV" not in out
    assert out["CONDA_PREFIX"] == str(other)


def test_is_blocked_and_credential_name_helpers(tmp_path: Path) -> None:
    assert ce.is_blocked("telegram_bot_token")
    assert ce.is_blocked("INDUS_ANYTHING")
    assert not ce.is_blocked("EDITOR")
    # Substring match on purpose: the code sandbox prefers dropping MONKEYS to leaking a key.
    assert ce.is_credential_name("my_db_dsn") and ce.is_credential_name("MONKEYS")
    assert not ce.is_credential_name("EDITOR")
    assert "GH_TOKEN" in ce.blocked_names()


def test_odd_config_never_breaks_a_spawn(tmp_path: Path) -> None:
    class Broken:
        def get(self, path: str, /) -> object:
            raise RuntimeError("bad config")

    scope = make_scope(tmp_path / "h", env={"PATH": "/bin"}).with_config(Broken())
    assert ce.child_env(scope)["PATH"] == "/bin"
    odd = scope_with(
        tmp_path, {}, config={"providers": ["x"], "terminal": {"env_passthrough": "NOT_A_LIST"}}
    )
    assert ce.passthrough_names(odd) == ([], [])
