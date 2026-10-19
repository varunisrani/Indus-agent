"""The platform registry: every platform the gateway can run, in one table (P15-06).

Each ``PlatformEntry`` says how to build the platform's adapter, how to tell whether its library is
installed without importing it, which env names carry its credential and its access lists, its
message cap, whether its DMs are really private (R06.24) and an optional cron target parser.
Telegram, Discord, Slack, the API server and the webhook receiver are built in; plugins add
entries through the P13 provider registry (``load_plugin_entries``).

A platform's SDK is imported in exactly one place, ``PlatformRegistry.create_adapter``, which the
gateway calls only for enabled platforms: importing this module, listing entries or probing
dependencies (``importlib.util.find_spec``) never imports an SDK.

``proxy_for`` decides which proxy an adapter uses: a per-platform proxy setting always wins;
otherwise the standard proxy env vars, unless ``gateway.trust_env`` is false.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

__all__ = [
    "BUILTIN_NAMES",
    "PlatformEntry",
    "PlatformRegistry",
    "TargetParser",
    "builtin_entries",
    "modules_installed",
    "policy_env_name",
    "proxy_for",
]

log = logging.getLogger(__name__)

type TargetParser = Callable[[str], tuple[str, str | None] | None]
type AdapterFactory = Callable[[Any], Any]

BUILTIN_NAMES: tuple[str, ...] = ("telegram", "discord", "slack", "api_server", "webhook")


def policy_env_name(platform: str, suffix: str) -> str:
    """``INDUS_<PLATFORM>_<SUFFIX>``: the env name of one of a platform's policy settings."""
    return f"INDUS_{platform.upper()}_{suffix}"


def modules_installed(*names: str) -> Callable[[], bool]:
    """A passive dependency probe: true when every top-level module can be found.

    ``find_spec`` on a top-level name locates the package without running it, so the probe is safe
    from status screens and the enablement pass.
    """

    def probe() -> bool:
        for name in names:
            try:
                if importlib.util.find_spec(name) is None:
                    return False
            except (ImportError, ValueError):
                return False
        return True

    return probe


def _lazy_factory(target: str) -> AdapterFactory:
    """A factory that imports ``module:attr`` only when an adapter is actually built."""
    module_name, _, attr = target.partition(":")

    def build(config: Any) -> Any:
        module = importlib.import_module(module_name)
        return getattr(module, attr)(config)

    build.__qualname__ = f"lazy({target})"
    return build


@dataclass(frozen=True)
class PlatformEntry:
    """One platform the gateway can run."""

    name: str
    label: str
    factory: AdapterFactory
    check_fn: Callable[[], bool]
    required_env: tuple[str, ...] = ()
    install_hint: str = ""
    allowlist_env: str = ""
    allow_all_env: str = ""
    allow_bots_env: str | None = None
    home_env: str = ""
    max_len: int = 0
    dm_is_private: bool = True
    platform_hint: str = ""
    cron_target_parser: TargetParser | None = None
    # Group allowlists read from env (empty: config only). Authorization reads them (P15-08).
    group_users_env: str = ""
    group_chats_env: str = ""
    # Inbound events are authenticated by the platform's own check (signature, key), so user
    # allowlists and pairing do not apply. Never for a platform where people send messages.
    trusted_inbound: bool = False
    source: Literal["builtin", "plugin"] = "builtin"
    plugin_name: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict[str, Any])


def _chat_entry(
    name: str,
    label: str,
    *,
    sdk: tuple[str, ...],
    extra_name: str,
    required_env: tuple[str, ...],
    max_len: int,
    hint: str,
) -> PlatformEntry:
    return PlatformEntry(
        name=name,
        label=label,
        factory=_lazy_factory(f"indus.gateway.adapters.{name}:create_adapter"),
        check_fn=modules_installed(*sdk),
        required_env=required_env,
        install_hint=f"pip install 'indus-agent[{extra_name}]'",
        allowlist_env=policy_env_name(name, "ALLOWED_USERS"),
        allow_all_env=policy_env_name(name, "ALLOW_ALL_USERS"),
        allow_bots_env=policy_env_name(name, "ALLOW_BOTS"),
        home_env=policy_env_name(name, "HOME_CHANNEL"),
        max_len=max_len,
        dm_is_private=True,
        platform_hint=hint,
    )


def builtin_entries() -> tuple[PlatformEntry, ...]:
    """The platforms that ship with Indus Agent, in a fixed order."""
    return (
        replace(
            _chat_entry(
                "telegram",
                "Telegram",
                # Our own Bot API client over httpx (a base dependency): no extra to install.
                sdk=("httpx",),
                extra_name="telegram",
                required_env=("TELEGRAM_BOT_TOKEN",),
                max_len=4096,
                hint=(
                    "You are talking through Telegram. Keep replies short enough to read on a "
                    "phone; Markdown is converted for you."
                ),
            ),
            install_hint="pip install indus-agent (Telegram needs no extra)",
        ),
        _chat_entry(
            "discord",
            "Discord",
            sdk=("discord",),
            extra_name="discord",
            required_env=("DISCORD_BOT_TOKEN",),
            max_len=2000,
            hint="You are talking through Discord. Standard Markdown renders natively.",
        ),
        _chat_entry(
            "slack",
            "Slack",
            sdk=("slack_bolt", "slack_sdk"),
            extra_name="slack",
            required_env=("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
            max_len=39000,
            hint="You are talking through Slack. Formatting is converted to Slack's markup.",
        ),
        PlatformEntry(
            name="api_server",
            label="API server",
            factory=_lazy_factory("indus.gateway.http.api_server:create_adapter"),
            check_fn=modules_installed("fastapi", "uvicorn"),
            install_hint="pip install 'indus-agent[gateway]'",
            dm_is_private=False,
            trusted_inbound=True,
        ),
        PlatformEntry(
            name="webhook",
            label="Webhook receiver",
            factory=_lazy_factory("indus.gateway.http.webhook:create_adapter"),
            check_fn=modules_installed("fastapi", "uvicorn"),
            install_hint="pip install 'indus-agent[gateway]'",
            dm_is_private=False,
            trusted_inbound=True,
        ),
    )


def _probe(entry: PlatformEntry) -> bool:
    try:
        return entry.check_fn() is True
    except Exception as exc:  # a probe must never take the gateway down
        log.warning("platform %s: dependency check failed: %s", entry.name, exc)
        return False


class PlatformRegistry:
    """Name → ``PlatformEntry``. Thread-safe; built-ins first, plugin entries on top."""

    def __init__(self, builtins: Iterable[PlatformEntry] | None = None) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, PlatformEntry] = {}
        self._builtin_names: frozenset[str] = frozenset()
        start = tuple(builtin_entries() if builtins is None else builtins)
        for entry in start:
            self._entries[entry.name] = entry
        self._builtin_names = frozenset(e.name for e in start if e.source == "builtin")

    # -- lookup ---------------------------------------------------------------------------------

    def get(self, name: str) -> PlatformEntry | None:
        with self._lock:
            return self._entries.get(name.strip().lower())

    def names(self) -> list[str]:
        with self._lock:
            return list(self._entries)

    def entries(self) -> list[PlatformEntry]:
        with self._lock:
            return list(self._entries.values())

    def is_builtin(self, name: str) -> bool:
        return name.strip().lower() in self._builtin_names

    def missing_deps(self, name: str) -> str | None:
        """The install hint when the platform's library is missing, else None."""
        entry = self.get(name)
        if entry is None:
            return f"unknown platform {name!r}"
        if _probe(entry):
            return None
        return entry.install_hint or f"the {entry.label} library is not installed"

    # -- changes --------------------------------------------------------------------------------

    def register(self, entry: PlatformEntry, *, replace_existing: bool = False) -> bool:
        """Add ``entry``; an existing name is replaced only with ``replace_existing``."""
        name = entry.name.strip().lower()
        if not name or not name.replace("_", "").isalnum():
            raise ValueError(f"platform name {entry.name!r} must be letters, digits and _")
        if entry.trusted_inbound and entry.source == "plugin" and name in self._builtin_names:
            # People send messages on a shipped chat platform; a plugin replacing its adapter must
            # not switch off allowlists and pairing for it.
            log.warning(
                "plugin %s: platform %s cannot be marked trusted_inbound; flag dropped",
                entry.plugin_name or "?",
                name,
            )
            entry = replace(entry, trusted_inbound=False)
        if name != entry.name:
            entry = replace(entry, name=name)
        with self._lock:
            if name in self._entries and not replace_existing:
                log.warning("platform %s is already registered; refused", name)
                return False
            self._entries[name] = entry
        return True

    def unregister(self, name: str) -> bool:
        with self._lock:
            return self._entries.pop(name.strip().lower(), None) is not None

    def load_plugin_entries(self, providers: Mapping[str, Mapping[str, Any]]) -> int:
        """Register the platform entries plugins offered (the P13 provider slot ``platform``).

        A plugin entry may replace a built-in adapter (user beats bundled, P13-14). Returns how
        many entries were registered.
        """
        slot = providers.get("platform") or {}
        added = 0
        for label, offered in slot.items():
            provider = getattr(offered, "provider", offered)
            # A P13 ``ProviderEntry``: the offering plugin's id is its ``plugin`` field.
            plugin_id = str(getattr(offered, "plugin", "") or "")
            if not isinstance(provider, PlatformEntry):
                log.warning(
                    "plugin %s: platform %s is not a PlatformEntry; ignored", plugin_id, label
                )
                continue
            entry = replace(
                provider, source="plugin", plugin_name=plugin_id or provider.plugin_name
            )
            if self.register(entry, replace_existing=True):
                added += 1
        return added

    # -- building -------------------------------------------------------------------------------

    def create_adapter(self, name: str, config: Any) -> Any | None:
        """Build the adapter for an enabled platform: the only place an SDK gets imported.

        Returns None (and logs why) for an unknown platform, a missing library or a factory error.
        """
        entry = self.get(name)
        if entry is None:
            log.warning("no platform named %s is registered", name)
            return None
        if not _probe(entry):
            log.warning(
                "platform %s is enabled but its library is missing: %s",
                entry.name,
                entry.install_hint or "install its extra",
            )
            return None
        try:
            return entry.factory(config)
        except Exception as exc:
            log.error("platform %s: building the adapter failed: %s", entry.name, exc)
            return None


# -- proxy ----------------------------------------------------------------------------------------

_PROXY_NAMES = {
    "https": ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"),
    "http": ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"),
}


def _bypassed(host: str, no_proxy: str) -> bool:
    host = host.strip().lower().rstrip(".")
    if not host:
        return False
    for raw in no_proxy.split(","):
        item = raw.strip().lower()
        if not item:
            continue
        if item == "*":
            return True
        item = item.split(":", 1)[0].lstrip(".").rstrip(".")
        if item and (host == item or host.endswith("." + item)):
            return True
    return False


def proxy_for(
    *,
    env: Mapping[str, str],
    trust_env: bool = True,
    platform_proxy: str | None = None,
    target_host: str | None = None,
    scheme: Literal["https", "http"] = "https",
) -> str | None:
    """The proxy URL an adapter should use, or None for a direct connection.

    A per-platform proxy setting always wins. Otherwise the standard env vars apply unless
    ``trust_env`` is false; ``NO_PROXY`` exempts ``target_host``.
    """
    explicit = (platform_proxy or "").strip()
    if explicit:
        return explicit
    if not trust_env:
        return None
    no_proxy = env.get("NO_PROXY") or env.get("no_proxy") or ""
    if target_host and _bypassed(target_host, no_proxy):
        return None
    for name in _PROXY_NAMES[scheme]:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None
