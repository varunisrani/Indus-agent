"""P03-01: ``ProviderProfile`` construction rules (R01.12 identity headers)."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from indus import __version__
from indus.foundation.errors import InvalidProviderProfile
from indus.providers.profile import USER_AGENT, ProviderProfile, foreign_product_tokens


def _profile(**kwargs: Any) -> ProviderProfile:
    fields: dict[str, Any] = {"name": "acme", "base_url": "https://api.acme.example/v1"}
    fields.update(kwargs)
    return ProviderProfile(**fields)


def test_minimal_profile_defaults() -> None:
    profile = _profile(env_vars=("ACME_API_KEY",))
    assert profile.api_mode == "chat_completions"
    assert profile.auth_type == "api_key"
    assert profile.max_tokens_param == "max_tokens"
    assert profile.supports_streaming and profile.supports_model_listing
    assert not profile.hidden and profile.family == "vendor" and profile.auto_detect
    assert profile.reasoning_efforts is None and profile.adaptive_thinking_models == ()
    assert profile.bearer_auth is False
    assert profile.needs_key and not profile.is_custom
    assert profile.label == "acme"
    assert profile.all_names() == ("acme",)


def test_user_agent_is_ours() -> None:
    assert f"indus/{__version__}" == USER_AGENT


@pytest.mark.parametrize(
    ("base_url", "hostname"),
    [
        ("https://api.acme.example/v1", "api.acme.example"),
        ("https://API.Acme.Example./v1", "api.acme.example"),
        ("http://127.0.0.1:1234/v1", "127.0.0.1"),
    ],
)
def test_hostname_derived_and_normalised(base_url: str, hostname: str) -> None:
    assert _profile(base_url=base_url).hostname == hostname


def test_explicit_hostname_kept_normalised() -> None:
    assert _profile(hostname="Gateway.Acme.Example.").hostname == "gateway.acme.example"


def test_api_key_profile_needs_base_url() -> None:
    with pytest.raises(InvalidProviderProfile, match="needs a base_url"):
        ProviderProfile(name="acme", env_vars=("ACME_API_KEY",))


@pytest.mark.parametrize("auth_type", ["api_key_optional", "none"])
def test_optional_auth_may_omit_base_url(auth_type: str) -> None:
    profile = ProviderProfile(name="acme", auth_type=auth_type)
    assert profile.base_url == "" and profile.hostname == ""


def test_foreign_user_agent_refused() -> None:
    with pytest.raises(InvalidProviderProfile, match="User-Agent"):
        _profile(default_headers={"User-Agent": "SomeOtherCLI/1.0"})


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Client": "SomeOtherCLI/2.3.1"},
        {"X-Title": "made by OtherAgent/0.9 (compatible)"},
        {"user-agent": "indus/1.0 OtherTool/3"},
    ],
)
def test_foreign_product_token_in_any_header_refused(headers: dict[str, str]) -> None:
    with pytest.raises(InvalidProviderProfile):
        _profile(default_headers=headers)


def test_own_headers_accepted() -> None:
    profile = _profile(
        default_headers={
            "User-Agent": USER_AGENT,
            "X-Title": "Indus Agent",
            "Accept": "application/json",
            "HTTP-Referer": "https://indus.invalid/",
        }
    )
    assert profile.default_headers["User-Agent"] == USER_AGENT


@pytest.mark.parametrize(
    "headers",
    [{"X-Note": "line\r\nInjected: yes"}, {"Bad Header": "x"}, {"X-Note": "a\nb"}],
)
def test_header_shape_refused(headers: dict[str, str]) -> None:
    with pytest.raises(InvalidProviderProfile):
        _profile(default_headers=headers)


def test_foreign_product_tokens_helper() -> None:
    assert foreign_product_tokens("indus/0.0.1") == []
    assert foreign_product_tokens("Mozilla/5.0 indus/1") == ["Mozilla"]
    assert foreign_product_tokens("application/json") == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "Acme"),
        ("name", ""),
        ("name", "acme provider"),
        ("aliases", ("Bad Alias",)),
        ("api_mode", "carrier_pigeon"),
        ("auth_type", "oauth_device_code"),
        ("max_tokens_param", "max_output"),
        ("family", "portal"),
        ("env_vars", ("acme_key",)),
        ("env_vars", ("1KEY",)),
        ("base_url_env_var", "acme-url"),
        ("base_url", "api.acme.example/v1"),
        ("base_url", "https://user@api.acme.example"),
        ("models_url", "ftp://models.acme.example"),
        ("reasoning_efforts", ("medium", "galaxy-brain")),
        ("default_max_tokens", 0),
        ("context_length_hint", -5),
    ],
)
def test_invalid_fields_refused(field: str, value: Any) -> None:
    with pytest.raises(InvalidProviderProfile):
        _profile(**{field: value})


@pytest.mark.parametrize("mode", ["chat_completions", "anthropic_messages", "responses", "gemini"])
def test_known_api_modes_accepted(mode: str) -> None:
    assert _profile(api_mode=mode).api_mode == mode


def test_frozen() -> None:
    profile = _profile(default_headers={"X-Title": "Indus Agent"}, model_aliases={"fast": "m-1"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.name = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        profile.default_headers["X-Title"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        profile.model_aliases["fast"] = "m-2"  # type: ignore[index]


def test_caller_mapping_is_copied() -> None:
    headers = {"X-Title": "Indus Agent"}
    profile = _profile(default_headers=headers)
    headers["X-Title"] = "changed"
    assert profile.default_headers["X-Title"] == "Indus Agent"


def test_hashable_and_equal() -> None:
    a, b = _profile(), _profile()
    assert a == b and hash(a) == hash(b)
    assert len({a, b}) == 1


def test_hooks_default_to_no_ops() -> None:
    profile = _profile()
    body = {"model": "m", "messages": []}
    assert profile.prepare_body(body, "m") is body
    assert profile.classify_error(object()) is None
    assert profile.context_length("m") is None


def test_custom_hooks_called() -> None:
    def add_flag(body: dict[str, Any], model: str) -> dict[str, Any]:
        return {**body, "flag": model}

    profile = _profile(
        prepare_body=add_flag,
        classify_error=lambda ctx: "verdict",
        context_length=lambda model: 131_072 if model == "big" else None,
    )
    assert profile.prepare_body({}, "x") == {"flag": "x"}
    assert profile.classify_error(None) == "verdict"
    assert profile.context_length("big") == 131_072
    assert profile.context_length("small") is None


def test_model_aliases_and_reasoning_patterns() -> None:
    profile = _profile(model_aliases={"fast": "acme-fast-2"}, reasoning_models=("think",))
    assert profile.resolve_model("fast") == "acme-fast-2"
    assert profile.resolve_model("other") == "other"
    assert profile.is_reasoning_model("acme-THINK-1")
    assert not profile.is_reasoning_model("acme-fast-2")
