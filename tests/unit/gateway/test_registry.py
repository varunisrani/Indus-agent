"""P15-06: the platform registry, lazy SDK import and proxy choice."""

from __future__ import annotations

import logging
import subprocess
import sys
import types
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.gateway import registry as reg
from indus.gateway.registry import (
    BUILTIN_NAMES,
    PlatformEntry,
    PlatformRegistry,
    builtin_entries,
    modules_installed,
    policy_env_name,
    proxy_for,
)
from indus.state.pairing import allowlist_env_name
from tests.fakes.clock import FakeClock


def _entry(name: str = "fakechat", **kwargs: Any) -> PlatformEntry:
    base: dict[str, Any] = {
        "name": name,
        "label": name.title(),
        "factory": lambda config: ("adapter", name, config),
        "check_fn": lambda: True,
    }
    base.update(kwargs)
    return PlatformEntry(**base)


def test_builtins_present_in_order_with_env_names() -> None:
    registry = PlatformRegistry()
    assert registry.names() == list(BUILTIN_NAMES)
    telegram = registry.get("telegram")
    assert telegram is not None
    assert telegram.required_env == ("TELEGRAM_BOT_TOKEN",)
    assert telegram.allowlist_env == "INDUS_TELEGRAM_ALLOWED_USERS"
    assert telegram.allow_all_env == "INDUS_TELEGRAM_ALLOW_ALL_USERS"
    assert telegram.allow_bots_env == "INDUS_TELEGRAM_ALLOW_BOTS"
    assert telegram.home_env == "INDUS_TELEGRAM_HOME_CHANNEL"
    assert telegram.max_len == 4096 and telegram.dm_is_private
    slack = registry.get("SLACK")
    assert slack is not None
    assert slack.required_env == ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")
    assert slack.max_len == 39000
    discord = registry.get("discord")
    assert discord is not None and discord.max_len == 2000
    for name in ("api_server", "webhook"):
        entry = registry.get(name)
        assert entry is not None
        assert entry.trusted_inbound and not entry.dm_is_private
    assert not any(e.trusted_inbound for e in builtin_entries() if e.name in {"telegram", "slack"})
    assert all(e.source == "builtin" for e in registry.entries())
    assert policy_env_name("discord", "HOME_CHANNEL") == "INDUS_DISCORD_HOME_CHANNEL"
    # the pairing store (state layer) mirrors approvals into the same allowlist name
    assert allowlist_env_name("discord") == policy_env_name("discord", "ALLOWED_USERS")


def test_register_refuses_duplicates_unless_replacing() -> None:
    registry = PlatformRegistry(builtins=())
    assert registry.register(_entry())
    assert not registry.register(_entry(label="Other"))
    got = registry.get("fakechat")
    assert got is not None and got.label == "Fakechat"
    assert registry.register(_entry(label="Other"), replace_existing=True)
    got = registry.get("fakechat")
    assert got is not None and got.label == "Other"
    assert registry.unregister("fakechat")
    assert not registry.unregister("fakechat")
    with pytest.raises(ValueError):
        registry.register(_entry(name="../evil"))


def test_names_are_normalised_to_lower_case() -> None:
    registry = PlatformRegistry(builtins=())
    registry.register(_entry(name="IrcChat"))
    assert registry.names() == ["ircchat"]
    assert registry.get(" IRCCHAT ") is not None


@dataclass
class _Offered:
    provider: object
    plugin: str


def test_plugin_entries_load_from_the_provider_slot(caplog: pytest.LogCaptureFixture) -> None:
    registry = PlatformRegistry()
    providers = {
        "platform": {
            "irc": _Offered(_entry(name="irc"), "irc-plugin"),
            "junk": _Offered(object(), "bad-plugin"),
        }
    }
    with caplog.at_level(logging.WARNING):
        assert registry.load_plugin_entries(providers) == 1
    irc = registry.get("irc")
    assert irc is not None
    assert irc.source == "plugin" and irc.plugin_name == "irc-plugin"
    assert "not a PlatformEntry" in caplog.text
    assert registry.load_plugin_entries({}) == 0


def test_a_real_plugin_host_slot_feeds_the_registry(tmp_path: Path) -> None:
    """P13's provider kind ``platform`` (integration): what a plugin registers through its
    context is what ``load_plugin_entries`` reads from ``host.providers``."""
    from indus.foundation.scope import make_scope
    from indus.plugins.context import PluginHost
    from indus.plugins.hooks import HookBus
    from indus.plugins.manifest import manifest_from_mapping
    from indus.tools.registry import ToolRegistry

    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    host = PluginHost(make_scope(tmp_path), ToolRegistry(), HookBus(clock=clock), clock=clock)
    made, _ = manifest_from_mapping({"name": "irc-plugin"}, fallback_name="irc-plugin")
    assert host.context(made).register_provider("platform", _entry(name="irc"), name="irc")
    registry = PlatformRegistry()
    assert registry.load_plugin_entries(host.providers) == 1
    irc = registry.get("irc")
    assert irc is not None and irc.source == "plugin" and irc.plugin_name == "irc-plugin"


def test_plugin_cannot_trust_builtin(caplog: pytest.LogCaptureFixture) -> None:
    registry = PlatformRegistry()
    offered = _entry(name="telegram", trusted_inbound=True)
    with caplog.at_level(logging.WARNING):
        assert registry.load_plugin_entries({"platform": {"t": _Offered(offered, "p")}}) == 1
    telegram = registry.get("telegram")
    assert telegram is not None
    assert telegram.source == "plugin" and telegram.trusted_inbound is False
    assert "trusted_inbound" in caplog.text
    # A plugin's own new platform may be trusted (an event bus, not people).
    registry.load_plugin_entries(
        {"platform": {"h": _Offered(_entry(name="homebus", trusted_inbound=True), "p")}}
    )
    homebus = registry.get("homebus")
    assert homebus is not None and homebus.trusted_inbound


def test_importing_the_registry_imports_no_sdk() -> None:
    code = (
        "import sys\n"
        "import indus.gateway.registry as r\n"
        "reg = r.PlatformRegistry()\n"
        "[r.modules_installed(n)() for n in ('telegram', 'discord', 'slack_bolt')]\n"
        "[reg.missing_deps(n) for n in reg.names()]\n"
        "bad = [m for m in ('telegram', 'discord', 'slack_bolt', 'slack_sdk', 'fastapi')"
        " if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True
    )
    assert done.stdout.strip() == ""


def test_create_adapter_imports_only_the_enabled_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    imported: list[str] = []
    real_import = reg.importlib.import_module

    def spy(name: str, package: str | None = None) -> types.ModuleType:
        imported.append(name)
        if name.startswith("fake_sdk_"):
            module = types.ModuleType(name)
            module.create_adapter = lambda config: ("built", name, config)  # type: ignore[attr-defined]
            return module
        return real_import(name, package)

    monkeypatch.setattr(reg.importlib, "import_module", spy)
    registry = PlatformRegistry(builtins=())
    for name in ("alpha", "beta"):
        registry.register(
            _entry(name=name, factory=reg._lazy_factory(f"fake_sdk_{name}:create_adapter"))  # pyright: ignore[reportPrivateUsage]
        )
    assert imported == []
    enabled = {"beta"}
    built = [
        registry.create_adapter(name, {"n": name}) for name in registry.names() if name in enabled
    ]
    assert built == [("built", "fake_sdk_beta", {"n": "beta"})]
    assert imported == ["fake_sdk_beta"]


def test_create_adapter_contains_failures(caplog: pytest.LogCaptureFixture) -> None:
    calls: list[str] = []

    def broken_factory(config: Any) -> Any:
        calls.append("factory")
        raise RuntimeError("boom")

    def broken_probe() -> bool:
        raise RuntimeError("probe boom")

    registry = PlatformRegistry(builtins=())
    registry.register(
        _entry(
            name="nodeps",
            check_fn=lambda: False,
            install_hint="pip install x",
            factory=lambda c: calls.append("nodeps"),
        )
    )
    registry.register(_entry(name="badprobe", check_fn=broken_probe))
    registry.register(_entry(name="badfactory", factory=broken_factory))
    with caplog.at_level(logging.WARNING):
        assert registry.create_adapter("nodeps", None) is None
        assert registry.create_adapter("badprobe", None) is None
        assert registry.create_adapter("badfactory", None) is None
        assert registry.create_adapter("missing", None) is None
    assert calls == ["factory"]
    assert "pip install x" in caplog.text
    assert registry.missing_deps("nodeps") == "pip install x"
    assert registry.missing_deps("badfactory") is None
    assert registry.missing_deps("missing") == "unknown platform 'missing'"


def test_modules_installed_probe() -> None:
    assert modules_installed("json", "sqlite3")() is True
    assert modules_installed("json", "no_such_module_indus_xyz")() is False
    assert modules_installed("")() is False


@pytest.mark.parametrize(
    ("env", "trust", "platform_proxy", "host", "expected"),
    [
        ({}, True, None, None, None),
        ({"HTTPS_PROXY": "http://p:1"}, True, None, None, "http://p:1"),
        ({"https_proxy": "http://lower:1"}, True, None, None, "http://lower:1"),
        ({"ALL_PROXY": "socks5://a:1"}, True, None, None, "socks5://a:1"),
        ({"HTTPS_PROXY": "http://p:1"}, False, None, None, None),
        ({"HTTPS_PROXY": "http://p:1"}, False, "http://mine:2", None, "http://mine:2"),
        ({"HTTPS_PROXY": "http://p:1"}, True, "http://mine:2", None, "http://mine:2"),
        ({"HTTPS_PROXY": "http://p:1"}, True, "  ", None, "http://p:1"),
        (
            {"HTTPS_PROXY": "http://p:1", "NO_PROXY": "telegram.org"},
            True,
            None,
            "api.telegram.org",
            None,
        ),
        ({"HTTPS_PROXY": "http://p:1", "no_proxy": ".slack.com"}, True, None, "slack.com", None),
        ({"HTTPS_PROXY": "http://p:1", "NO_PROXY": "*"}, True, None, "x.y", None),
        (
            {"HTTPS_PROXY": "http://p:1", "NO_PROXY": "other.org"},
            True,
            None,
            "api.telegram.org",
            "http://p:1",
        ),
        (
            {"HTTPS_PROXY": "http://p:1", "NO_PROXY": "gram.org"},
            True,
            None,
            "telegram.org",
            "http://p:1",
        ),
    ],
)
def test_proxy_choice(
    env: dict[str, str],
    trust: bool,
    platform_proxy: str | None,
    host: str | None,
    expected: str | None,
) -> None:
    assert (
        proxy_for(env=env, trust_env=trust, platform_proxy=platform_proxy, target_host=host)
        == expected
    )


def test_http_scheme_prefers_http_proxy() -> None:
    env = {"HTTP_PROXY": "http://h:1", "HTTPS_PROXY": "http://s:1"}
    assert proxy_for(env=env, scheme="http") == "http://h:1"
    assert proxy_for(env=env) == "http://s:1"
