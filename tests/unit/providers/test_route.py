"""P03-05: route precedence, key lookup and key-to-host scoping (R01.11, R04.9, A50)."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from indus.foundation.errors import InvalidBaseURL, KeyHostMismatch, NoUsableProvider, ProviderError
from indus.providers.profile import USER_AGENT
from indus.providers.route import RuntimeRoute, Secret, is_placeholder, resolve
from tests.unit.providers.conftest import ScopeFactory, fake_key

OR_KEY, DS_KEY, XAI_KEY, AN_KEY = fake_key("or"), fake_key("ds"), fake_key("xai"), fake_key("an")
OA_KEY = fake_key("oa")
ALL_KEYS = {
    "OPENROUTER_API_KEY": OR_KEY,
    "DEEPSEEK_API_KEY": DS_KEY,
    "XAI_API_KEY": XAI_KEY,
    "ANTHROPIC_API_KEY": AN_KEY,
}


def _key(route: RuntimeRoute) -> str | None:
    return route.api_key.reveal() if route.api_key else None


@pytest.mark.parametrize(
    ("explicit", "config", "env", "provider", "source"),
    [
        ("openrouter", "deepseek", "xai", "openrouter", "explicit"),
        (None, "deepseek", "xai", "deepseek", "config"),
        (None, None, "xai", "xai", "env"),
        (None, None, None, "openrouter", "auto"),
    ],
)
def test_precedence_table(
    make_scope: ScopeFactory,
    explicit: str | None,
    config: str | None,
    env: str | None,
    provider: str,
    source: str,
) -> None:
    cfg: dict[str, Any] = {"model": {"provider": config}} if config else {}
    env_vars = dict(ALL_KEYS)
    if env:
        env_vars["INDUS_INFERENCE_PROVIDER"] = env
    route = resolve(make_scope(config=cfg, env=env_vars), provider=explicit)
    assert (route.provider, route.source) == (provider, source)


@pytest.mark.parametrize(
    ("explicit", "config", "env", "expected"),
    [
        ("m-explicit", "m-config", "m-env", "m-explicit"),
        (None, "m-config", "m-env", "m-config"),
        (None, None, "m-env", "m-env"),
        (None, None, None, ""),
    ],
)
def test_model_precedence(
    make_scope: ScopeFactory,
    explicit: str | None,
    config: str | None,
    env: str | None,
    expected: str,
) -> None:
    cfg: dict[str, Any] = {"model": {"provider": "openrouter"}}
    if config:
        cfg["model"]["default"] = config
    env_vars = {"OPENROUTER_API_KEY": OR_KEY}
    if env:
        env_vars["INDUS_INFERENCE_MODEL"] = env
    assert resolve(make_scope(config=cfg, env=env_vars), model=explicit).model == expected


def test_config_provider_beats_env(make_scope: ScopeFactory) -> None:
    scope = make_scope(
        config={"model": {"provider": "anthropic", "default": "claude-x"}},
        env={**ALL_KEYS, "INDUS_INFERENCE_PROVIDER": "openrouter"},
    )
    route = resolve(scope)
    assert route.provider == "anthropic" and route.api_mode == "anthropic_messages"
    assert _key(route) == AN_KEY and route.key_env == "ANTHROPIC_API_KEY"


def test_config_anthropic_ignores_shell_openai_base_url(make_scope: ScopeFactory) -> None:
    scope = make_scope(
        config={"model": {"provider": "anthropic"}},
        env={"ANTHROPIC_API_KEY": AN_KEY, "OPENAI_BASE_URL": "https://old-proxy.example/v1"},
    )
    route = resolve(scope)
    assert route.base_url == "https://api.anthropic.com"


def test_saved_custom_url_beats_shell_openai_base_url(make_scope: ScopeFactory) -> None:
    scope = make_scope(
        config={"model": {"provider": "custom", "base_url": "http://127.0.0.1:8080/v1"}},
        env={"OPENAI_BASE_URL": "https://old-proxy.example/v1"},
    )
    route = resolve(scope)
    assert route.provider == "custom" and route.base_url == "http://127.0.0.1:8080/v1"


def test_custom_uses_openai_base_url_when_nothing_saved(make_scope: ScopeFactory) -> None:
    scope = make_scope(
        env={"OPENAI_BASE_URL": "https://proxy.example/v1/", "OPENAI_API_KEY": OA_KEY}
    )
    route = resolve(scope, provider="custom")
    assert route.base_url == "https://proxy.example/v1"
    assert _key(route) == OA_KEY


def test_explicit_base_url_beats_config(make_scope: ScopeFactory) -> None:
    scope = make_scope(
        config={"model": {"provider": "custom", "base_url": "http://127.0.0.1:1/v1"}}
    )
    assert resolve(scope, base_url="http://127.0.0.1:2/v1").base_url == "http://127.0.0.1:2/v1"


def test_openrouter_key_never_sent_to_custom_host(make_scope: ScopeFactory) -> None:
    env = {"OPENROUTER_API_KEY": OR_KEY}
    custom = resolve(make_scope(env=env), provider="custom", base_url="https://my.host/v1")
    assert custom.api_key is None
    named = resolve(
        make_scope(
            config={
                "model": {"provider": "mine"},
                "providers": {"mine": {"base_url": "https://my.host/v1"}},
            },
            env=env,
        )
    )
    assert named.provider == "mine" and named.api_key is None
    with pytest.raises(KeyHostMismatch, match="openrouter.ai"):
        resolve(make_scope(env=env), provider="openrouter", base_url="https://my.host/v1")


@pytest.mark.parametrize(
    "url",
    [
        "https://openrouter.ai.evil.example/api/v1",
        "https://evil.example/openrouter.ai/api/v1",
        "https://api.openrouter.ai/api/v1",
        "http://openrouter.ai/api/v1",
    ],
)
def test_vendor_key_only_on_exact_host(make_scope: ScopeFactory, url: str) -> None:
    with pytest.raises(KeyHostMismatch):
        resolve(make_scope(env={"OPENROUTER_API_KEY": OR_KEY}), provider="openrouter", base_url=url)


def test_placeholder_key_is_missing(make_scope: ScopeFactory) -> None:
    bare = "s" + "k-"
    with pytest.raises(NoUsableProvider) as caught:
        resolve(make_scope(env={"OPENAI_API_KEY": bare}), provider="openai")
    assert caught.value.env_names == ("OPENAI_API_KEY",)
    assert "OPENAI_API_KEY" in (caught.value.hint or "")


def test_store_value_preferred_over_env(make_scope: ScopeFactory) -> None:
    stored = fake_key("stored")
    scope = make_scope(
        config={"model": {"provider": "openrouter"}},
        env={"OPENROUTER_API_KEY": fake_key("dotenv")},
        secrets={"OPENROUTER_API_KEY": stored},
    )
    assert _key(resolve(scope)) == stored


def test_store_placeholder_falls_back_to_env(make_scope: ScopeFactory) -> None:
    from_env = fake_key("dotenv")
    scope = make_scope(
        env={"OPENROUTER_API_KEY": from_env},
        secrets={"OPENROUTER_API_KEY": "your-key-here"},
    )
    assert _key(resolve(scope, provider="openrouter")) == from_env


def test_key_priority_order(make_scope: ScopeFactory) -> None:
    second = fake_key("glm")
    scope = make_scope(env={"GLM_API_KEY": second})
    route = resolve(scope, provider="zai")
    assert _key(route) == second and route.key_env == "GLM_API_KEY"
    first = fake_key("zai")
    scope = make_scope(env={"GLM_API_KEY": second, "ZAI_API_KEY": first})
    assert _key(resolve(scope, provider="zai")) == first


def test_stale_vendor_base_url_dropped_with_warning(
    make_scope: ScopeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    scope = make_scope(
        config={"model": {"provider": "anthropic", "base_url": "https://openrouter.ai/api/v1"}},
        env={"ANTHROPIC_API_KEY": AN_KEY},
    )
    with caplog.at_level(logging.WARNING):
        route = resolve(scope)
    assert route.base_url == "https://api.anthropic.com"
    assert route.warnings and "OpenRouter" in route.warnings[0]
    assert "openrouter.ai" in caplog.text


def test_explicit_provider_ignores_other_providers_base_url(make_scope: ScopeFactory) -> None:
    scope = make_scope(
        config={"model": {"provider": "custom", "base_url": "http://127.0.0.1:8080/v1"}},
        env={"DEEPSEEK_API_KEY": DS_KEY},
    )
    route = resolve(scope, provider="deepseek")
    assert route.base_url == "https://api.deepseek.com/v1" and _key(route) == DS_KEY


def test_saved_api_mode_follows_its_provider(make_scope: ScopeFactory) -> None:
    scope = make_scope(
        config={
            "model": {
                "provider": "custom",
                "base_url": "https://gw.example/v1",
                "api_mode": "anthropic_messages",
            }
        },
        env={"OPENROUTER_API_KEY": OR_KEY},
    )
    assert resolve(scope).api_mode == "anthropic_messages"
    assert resolve(scope, provider="openrouter").api_mode == "chat_completions"
    assert resolve(scope, api_mode="chat_completions").api_mode == "chat_completions"


def test_auto_with_only_deepseek_key(make_scope: ScopeFactory) -> None:
    route = resolve(make_scope(env={"DEEPSEEK_API_KEY": DS_KEY}))
    assert route.provider == "deepseek" and route.source == "auto" and _key(route) == DS_KEY


def test_auto_with_nothing_lists_env_names(make_scope: ScopeFactory) -> None:
    with pytest.raises(NoUsableProvider) as caught:
        resolve(make_scope())
    names = caught.value.env_names
    assert names[0] == "OPENROUTER_API_KEY" and "DEEPSEEK_API_KEY" in names
    assert "LM_API_KEY" not in names


def test_auto_with_saved_base_url_is_custom(make_scope: ScopeFactory) -> None:
    scope = make_scope(
        config={"model": {"base_url": "http://127.0.0.1:8080/v1", "default": "local-model"}},
        env={"ANTHROPIC_API_KEY": AN_KEY, "OPENROUTER_API_KEY": OR_KEY},
    )
    route = resolve(scope)
    assert route.provider == "custom" and route.base_url == "http://127.0.0.1:8080/v1"
    assert route.api_key is None and route.model == "local-model"


def test_auto_with_saved_vendor_url_picks_that_vendor(make_scope: ScopeFactory) -> None:
    scope = make_scope(
        config={"model": {"base_url": "https://api.deepseek.com/v1"}},
        env={"OPENROUTER_API_KEY": OR_KEY, "DEEPSEEK_API_KEY": DS_KEY},
    )
    route = resolve(scope)
    assert route.provider == "deepseek" and _key(route) == DS_KEY


def test_generic_key_not_sent_to_another_vendor_host(
    make_scope: ScopeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    env = {"OPENAI_API_KEY": OA_KEY}
    with caplog.at_level(logging.WARNING):
        route = resolve(
            make_scope(env=env), provider="custom", base_url="https://api.deepseek.com/v1"
        )
    assert route.api_key is None and route.warnings
    openai_host = resolve(
        make_scope(env=env), provider="custom", base_url="https://api.openai.com/v1"
    )
    assert _key(openai_host) == OA_KEY


def test_named_custom_key_env(make_scope: ScopeFactory) -> None:
    own = fake_key("gpu")
    scope = make_scope(
        config={
            "model": {"provider": "gpu", "default": "big-1"},
            "providers": {"gpu": {"base_url": "https://gpu.example/v1", "key_env": "GPU_KEY"}},
        },
        env={"GPU_KEY": own, "OPENAI_API_KEY": OA_KEY},
    )
    route = resolve(scope)
    assert route.provider == "gpu" and _key(route) == own and route.key_env == "GPU_KEY"


def test_openai_host_routes_to_responses(make_scope: ScopeFactory) -> None:
    route = resolve(make_scope(), provider="custom", base_url="https://api.openai.com/v1")
    assert route.api_mode == "responses"  # P08-09: no more fallback to chat completions
    assert not route.warnings


def test_p08_modes_available_unknown_refused(make_scope: ScopeFactory) -> None:
    scope = make_scope(env={"OPENROUTER_API_KEY": OR_KEY})
    for mode in ("responses", "bedrock", "gemini"):
        assert resolve(scope, provider="openrouter", api_mode=mode).api_mode == mode
    with pytest.raises(ProviderError, match="not known"):
        resolve(scope, provider="openrouter", api_mode="smoke_signals")


def test_unknown_provider(make_scope: ScopeFactory) -> None:
    with pytest.raises(NoUsableProvider, match="unknown provider"):
        resolve(make_scope(), provider="nope")


def test_custom_without_url(make_scope: ScopeFactory) -> None:
    with pytest.raises(NoUsableProvider) as caught:
        resolve(make_scope(), provider="custom")
    assert caught.value.env_names == ("OPENAI_BASE_URL",)


def test_bad_url_raises(make_scope: ScopeFactory) -> None:
    with pytest.raises(InvalidBaseURL):
        resolve(make_scope(), provider="custom", base_url="my.host/v1")


def test_local_server_needs_no_key(make_scope: ScopeFactory) -> None:
    route = resolve(
        make_scope(env={"LM_BASE_URL": "http://192.168.1.9:1234/v1"}), provider="lmstudio"
    )
    assert route.base_url == "http://192.168.1.9:1234/v1" and route.api_key is None
    ollama = resolve(make_scope(env={"OPENAI_API_KEY": OA_KEY}), provider="ollama")
    assert ollama.base_url == "http://127.0.0.1:11434/v1" and ollama.api_key is None


def test_headers_are_ours(make_scope: ScopeFactory) -> None:
    route = resolve(make_scope(env={"OPENROUTER_API_KEY": OR_KEY}), provider="openrouter")
    assert dict(route.headers) == {"User-Agent": USER_AGENT}
    assert route.profile is not None and route.profile.name == "openrouter"


def test_repr_masks_the_key(make_scope: ScopeFactory) -> None:
    route = resolve(make_scope(env={"OPENROUTER_API_KEY": OR_KEY}), provider="openrouter")
    assert OR_KEY not in repr(route) and OR_KEY not in str(route)
    assert OR_KEY not in repr(route.api_key) and OR_KEY not in str(route.api_key)
    assert "redacted" in repr(route)


def test_secret_wrapper() -> None:
    a, b = Secret(OR_KEY), Secret(OR_KEY)
    assert a == b and hash(a) == hash(b) and a != Secret(DS_KEY)
    assert a.reveal() == OR_KEY and len(a.fingerprint()) == 16 and OR_KEY not in a.fingerprint()
    assert bool(a) and not bool(Secret(""))


@pytest.mark.parametrize(
    ("value", "missing"),
    [
        (None, True),
        ("", True),
        ("short", True),
        ("s" + "k-", True),
        ("s" + "k-proj-", True),
        ("g" + "hp_", True),
        ("h" + "f_", True),
        ("s" + "k-or-v1-", True),
        ("xxx", True),
        ("s" + "k-xxxxxxxxxxxxxxxx", True),
        ("your-key-here", True),
        ("YOUR_API_KEY", True),
        ("<your key>", True),
        ("<OPENAI_API_KEY>", True),
        ("${OPENAI_API_KEY}", True),
        ("changeme-please", True),
        ("********", True),
        ("00000000000", True),
        ("has a space inside", True),
        (fake_key("ok"), False),
        ("  " + fake_key("padded") + "\n", False),
        ("a1b2c3d4e5", False),
    ],
)
def test_placeholder_table(value: str | None, missing: bool) -> None:
    assert is_placeholder(value) is missing
