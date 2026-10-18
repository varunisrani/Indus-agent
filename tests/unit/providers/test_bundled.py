"""P03-03: the bundled catalogue validates and keeps keys on their own hosts (A55, D-16.42)."""

from __future__ import annotations

import dataclasses
import re

import pytest

from indus.foundation import envload
from indus.providers import bundled
from indus.providers.hosts import is_loopback, parse_base_url
from indus.providers.profile import ProviderProfile
from indus.providers.registry import LOCAL_SERVERS
from tooling.checks.banned import load_patterns, scan_text

EXPECTED_ORDER = [
    "openrouter",
    "openai",
    "anthropic",
    "deepseek",
    "xai",
    "mistral",
    "huggingface",
    "nvidia",
    "zai",
    "kimi-coding",
    "minimax",
    "alibaba",
    "ai-gateway",
    "ollama-cloud",
    "lmstudio",
    "custom",
]


def test_catalogue_names_and_order() -> None:
    assert [p.name for p in bundled.PROFILES] == EXPECTED_ORDER
    assert set(bundled.by_name()) == set(EXPECTED_ORDER)


def test_every_profile_revalidates() -> None:
    for profile in bundled.PROFILES:
        fields = {f.name: getattr(profile, f.name) for f in dataclasses.fields(profile)}
        assert ProviderProfile(**fields) == profile


def test_names_and_aliases_unique_with_local_servers() -> None:
    seen: list[str] = []
    for profile in (*bundled.PROFILES, *LOCAL_SERVERS):
        seen.extend(profile.all_names())
    assert len(seen) == len(set(seen)), sorted(n for n in seen if seen.count(n) > 1)


@pytest.mark.parametrize("profile", bundled.PROFILES, ids=lambda p: p.name)
def test_https_except_loopback(profile: ProviderProfile) -> None:
    if not profile.base_url:
        assert profile.name == "custom"
        return
    parsed = parse_base_url(profile.base_url)
    assert parsed.scheme == "https" or is_loopback(profile.base_url)
    assert profile.hostname == parsed.host


@pytest.mark.parametrize("profile", bundled.PROFILES, ids=lambda p: p.name)
def test_env_names_shape(profile: ProviderProfile) -> None:
    for name in (*profile.env_vars, profile.base_url_env_var):
        if name:
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*", name)


def test_vendor_profiles_need_keys_and_have_their_own_host() -> None:
    vendors = [p for p in bundled.PROFILES if p.family == "vendor"]
    hosts = [p.hostname for p in vendors]
    assert len(hosts) == len(set(hosts))
    for profile in vendors:
        assert profile.auth_type == "api_key" and profile.env_vars and profile.auto_detect
        assert not profile.base_url_env_var  # a vendor key never follows an env-chosen URL


def test_local_and_custom_not_auto_detected() -> None:
    by_name = bundled.by_name()
    for name in ("lmstudio", "custom"):
        assert not by_name[name].auto_detect
        assert by_name[name].auth_type == "api_key_optional"
    assert by_name["lmstudio"].base_url == "http://127.0.0.1:1234/v1"
    assert by_name["custom"].base_url == ""
    assert by_name["custom"].env_vars == ("OPENAI_API_KEY",)


def test_wire_formats() -> None:
    by_name = bundled.by_name()
    assert by_name["anthropic"].api_mode == "anthropic_messages"
    assert by_name["minimax"].api_mode == "anthropic_messages"
    assert by_name["openai"].api_mode == "chat_completions"
    assert by_name["openai"].max_tokens_param == "max_completion_tokens"
    assert by_name["openrouter"].base_url == "https://openrouter.ai/api/v1"
    assert by_name["huggingface"].env_vars == ("HF_TOKEN",)


def test_no_oauth_or_portal_routes() -> None:
    for profile in bundled.PROFILES:
        assert profile.auth_type in ("api_key", "api_key_optional")
        assert "portal" not in profile.name
        for name in profile.env_vars:
            assert "OAUTH" not in name and name != "ANTHROPIC_TOKEN"


def test_no_banned_names_in_any_field() -> None:
    patterns, _ = load_patterns()
    for profile in bundled.PROFILES:
        text = " ".join(
            str(getattr(profile, f.name))
            for f in dataclasses.fields(profile)
            if isinstance(getattr(profile, f.name), str | tuple)
        )
        assert scan_text(text, patterns) == [], profile.name


def test_default_headers_are_empty() -> None:
    assert all(not p.default_headers for p in bundled.PROFILES)


def test_env_var_names_cover_every_profile() -> None:
    names = bundled.env_var_names()
    assert len(names) == len(set(names))
    assert {"OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_BASE_URL", "LM_BASE_URL"} <= set(
        names
    )


def test_register_managed_env_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(envload, "_managed_registry", set())
    bundled.register_managed_env_names()
    assert set(bundled.env_var_names()) <= envload.managed_key_names()
