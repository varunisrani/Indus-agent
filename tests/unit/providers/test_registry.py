"""P03-02: provider lookup by name/alias, custom providers from config, plugins (A43, A56)."""

from __future__ import annotations

import logging

import pytest

from indus.foundation import config as config_mod
from indus.foundation import envload
from indus.foundation.config import loader
from indus.foundation.errors import ProviderConflict
from indus.foundation.scope import Scope
from indus.providers import bundled
from indus.providers.profile import ProviderProfile
from indus.providers.registry import LOCAL_SERVERS, Registry
from tests.unit.providers.conftest import ScopeFactory


def test_lookup_by_alias_and_case(make_scope: ScopeFactory) -> None:
    registry = Registry(make_scope())
    assert registry.lookup("openrouter") is registry.lookup("OpenRouter")
    profile = registry.lookup("OR")
    assert profile is not None and profile.name == "openrouter"
    assert registry.lookup(" Claude ") is registry.lookup("anthropic")
    assert registry.get("hf") is registry.lookup("huggingface")
    assert "kimi" in registry


@pytest.mark.parametrize("name", ["nope", "", "   ", "portal"])
def test_unknown_is_none(make_scope: ScopeFactory, name: str) -> None:
    assert Registry(make_scope()).lookup(name) is None


@pytest.mark.parametrize(
    ("name", "url"),
    [
        ("ollama", "http://127.0.0.1:11434/v1"),
        ("vllm", "http://127.0.0.1:8000/v1"),
        ("llamacpp", "http://127.0.0.1:8080/v1"),
        ("llama.cpp", "http://127.0.0.1:8080/v1"),
        ("lmstudio", "http://127.0.0.1:1234/v1"),
    ],
)
def test_local_server_aliases(make_scope: ScopeFactory, name: str, url: str) -> None:
    profile = Registry(make_scope()).lookup(name)
    assert profile is not None
    assert profile.base_url == url and profile.is_custom and not profile.auto_detect
    assert profile.auth_type == "api_key_optional"


def test_config_custom_provider_appears(make_scope: ScopeFactory) -> None:
    scope = make_scope(config={"providers": {"myllm": {"base_url": "http://127.0.0.1:8080/v1"}}})
    registry = Registry(scope)
    profile = registry.lookup("MyLLM")
    assert profile is not None
    assert profile.name == "myllm"
    assert profile.api_mode == "chat_completions"
    assert profile.family == "custom" and profile.hostname == "127.0.0.1"
    assert profile.env_vars == ("OPENAI_API_KEY",)
    assert registry.source_of("myllm") == "config"


def test_config_custom_provider_details(make_scope: ScopeFactory) -> None:
    scope = make_scope(
        config={
            "providers": {
                "gpu-box": {
                    "base_url": "https://gpu.example/anthropic/",
                    "key_env": "GPU_BOX_KEY",
                    "aliases": ["gpu"],
                    "context_length": 131072,
                    "models": {"big-1": {"context_length": 262144}, "tiny": {}},
                }
            }
        }
    )
    profile = Registry(scope).lookup("gpu")
    assert profile is not None and profile.name == "gpu-box"
    assert profile.base_url == "https://gpu.example/anthropic"
    assert profile.api_mode == "anthropic_messages"
    assert profile.env_vars == ("GPU_BOX_KEY",)
    assert profile.context_length("big-1") == 262144
    assert profile.context_length("tiny") == 131072
    assert profile.context_length_hint == 131072


def test_config_provider_on_openai_host_detects_responses(make_scope: ScopeFactory) -> None:
    scope = make_scope(config={"providers": {"direct": {"base_url": "https://api.openai.com/v1"}}})
    profile = Registry(scope).lookup("direct")
    assert profile is not None and profile.api_mode == "responses"  # P08-09


def test_bad_entries_are_skipped_with_warning(
    make_scope: ScopeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    scope = make_scope(
        config={
            "providers": {
                "nourl": {"key_env": "X_KEY"},
                "badurl": {"base_url": "my.host/v1"},
                "badkey": {"base_url": "https://ok.example/v1", "key_env": "lower-case"},
                "Bad Name": {"base_url": "https://ok.example/v1"},
                "good": {"base_url": "https://ok.example/v1", "api_key": "inline-secret-value"},
            }
        }
    )
    with caplog.at_level(logging.WARNING):
        registry = Registry(scope)
    for name in ("nourl", "badurl", "badkey", "bad name"):
        assert registry.lookup(name) is None
    assert registry.lookup("good") is not None
    text = caplog.text
    assert "badurl" in text and "badkey" in text and "api_key is ignored" in text
    assert "inline-secret-value" not in text


def test_settings_entry_for_builtin_is_not_a_profile(make_scope: ScopeFactory) -> None:
    scope = make_scope(config={"providers": {"openrouter": {"request_timeout_seconds": 60}}})
    registry = Registry(scope)
    assert registry.source_of("openrouter") == "bundled"


def test_shadowing_needs_override(
    make_scope: ScopeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    scope = make_scope(config={"providers": {"openai": {"base_url": "https://proxy.example/v1"}}})
    with caplog.at_level(logging.WARNING):
        registry = Registry(scope)
    profile = registry.lookup("openai")
    assert profile is not None and profile.base_url == "https://api.openai.com/v1"
    assert "override: true" in caplog.text


def test_shadowing_with_override(
    make_scope: ScopeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    scope = make_scope(
        config={"providers": {"openai": {"base_url": "https://proxy.example/v1", "override": True}}}
    )
    with caplog.at_level(logging.WARNING):
        registry = Registry(scope)
    profile = registry.lookup("openai-api")  # the built-in's alias no longer points anywhere
    assert profile is None
    replaced = registry.lookup("openai")
    assert replaced is not None and replaced.base_url == "https://proxy.example/v1"
    assert replaced.is_custom and registry.source_of("openai") == "config"
    assert "replaces the built-in" in caplog.text
    names = [p.name for p in registry.list_providers()]
    assert names.index("openai") == 1  # keeps its catalogue place


def test_alias_of_builtin_cannot_be_redefined(make_scope: ScopeFactory) -> None:
    scope = make_scope(config={"providers": {"or": {"base_url": "https://proxy.example/v1"}}})
    profile = Registry(scope).lookup("or")
    assert profile is not None and profile.name == "openrouter"


def test_local_server_redefined_freely(make_scope: ScopeFactory) -> None:
    scope = make_scope(
        config={"providers": {"ollama": {"base_url": "http://gpu-box.lan:11434/v1"}}}
    )
    profile = Registry(scope).lookup("ollama")
    assert profile is not None and profile.base_url == "http://gpu-box.lan:11434/v1"


def test_config_change_refreshes(make_scope: ScopeFactory, scope: Scope) -> None:
    built = make_scope(config={"providers": {"one": {"base_url": "https://one.example/v1"}}})
    registry = Registry(built)
    assert registry.lookup("one") is not None and registry.lookup("two") is None
    config_mod.save(
        scope, {"providers": {"two": {"base_url": "https://two.example/v1"}}}, replace=True
    )
    loader.invalidate()
    assert registry.lookup("two") is not None
    assert registry.lookup("one") is None
    assert registry.refresh() is False


def test_order_stable(make_scope: ScopeFactory) -> None:
    scope = make_scope(
        config={
            "providers": {
                "zeta": {"base_url": "https://z.example/v1"},
                "alpha": {"base_url": "https://a.example/v1"},
            }
        }
    )
    registry = Registry(scope)
    registry.register(
        ProviderProfile(name="plug-b", base_url="https://b.example/v1", env_vars=("B_KEY",)), "p1"
    )
    registry.register(
        ProviderProfile(name="plug-a", base_url="https://a2.example/v1", env_vars=("A_KEY",)), "p2"
    )
    names = [p.name for p in registry.list_providers()]
    builtin = [p.name for p in bundled.PROFILES] + [p.name for p in LOCAL_SERVERS]
    assert names == [*builtin, "alpha", "zeta", "plug-b", "plug-a"]
    assert [p.name for p in Registry(scope).list_providers()] == [*builtin, "alpha", "zeta"]


def test_hidden_filtering(make_scope: ScopeFactory) -> None:
    registry = Registry(make_scope())
    registry.register(
        ProviderProfile(name="secret-beta", base_url="https://beta.example/v1", hidden=True),
        "plugin-x",
    )
    assert "secret-beta" not in [p.name for p in registry.list_providers()]
    assert "secret-beta" in [p.name for p in registry.list_providers(include_hidden=True)]
    assert registry.lookup("secret-beta") is not None


def test_plugin_register_conflict(make_scope: ScopeFactory) -> None:
    registry = Registry(make_scope())
    clash = ProviderProfile(name="openrouter", base_url="https://other.example/v1")
    with pytest.raises(ProviderConflict):
        registry.register(clash, "plugin-x")
    with pytest.raises(ProviderConflict):
        registry.register(
            ProviderProfile(name="newone", aliases=("or",), base_url="https://n.example/v1"),
            "plugin-x",
        )
    with pytest.raises(ProviderConflict):
        registry.register(ProviderProfile(name="ok", base_url="https://n.example/v1"), "bundled")
    registry.register(clash, "plugin-x", override=True)
    found = registry.lookup("openrouter")
    assert found is not None and found.base_url == "https://other.example/v1"
    assert registry.source_of("openrouter") == "plugin-x"


def test_plugins_survive_config_refresh(make_scope: ScopeFactory, scope: Scope) -> None:
    registry = Registry(make_scope(config={}))
    registry.register(ProviderProfile(name="plug", base_url="https://p.example/v1"), "plugin-x")
    config_mod.save(scope, {"providers": {"c": {"base_url": "https://c.example/v1"}}}, replace=True)
    loader.invalidate()
    assert registry.lookup("c") is not None
    assert registry.lookup("plug") is not None


def test_auto_detect_candidates(make_scope: ScopeFactory) -> None:
    names = [p.name for p in Registry(make_scope()).auto_detect_candidates()]
    assert names[0] == "openrouter"
    assert "lmstudio" not in names and "custom" not in names and "ollama" not in names


def test_building_registers_managed_env_names(make_scope: ScopeFactory) -> None:
    envload._managed_registry.clear()  # pyright: ignore[reportPrivateUsage]
    Registry(make_scope())
    assert "OPENROUTER_API_KEY" in envload.managed_key_names()
