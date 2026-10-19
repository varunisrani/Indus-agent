# pyright: strict
"""The one builder for every child process environment Indus starts (R02.22).

Terminal commands, background processes, the bang shell and quick commands use the ``default``
variant; MCP stdio servers use ``minimal``; code execution uses ``code``. No spawn site may build
an environment from the process env itself (``tooling/checks/no_raw_environ.py`` enforces it).

``default``:
    the scope's env view, plus the caller's ``extra``, minus every blocked name; then the
    passthrough names (values from the scope's secret store, else the scope env); then names a
    caller forced with ``_INDUS_FORCE_<NAME>``; then the pins (``INDUS_HOME`` = the scope's home,
    ``INDUS_SESSION_ID``, ``AI_AGENT``, ``INDUS_AGENT``).
``minimal``:
    only the few names a stdio server needs to start, plus its configured values as given.
``code``:
    only names with a safe prefix and no credential word, plus passthrough, forced names and pins.

Blocked names come from a static floor, the ``.env`` managed-key registry (the provider registry
puts its declared key and base-URL names there), ``key_env`` of custom providers in the scope
config, names registered with :func:`register_blocked_names` and every ``INDUS_`` name. Matching
ignores case. A blocked name can never come back through passthrough; only an internal caller's
force entry brings one through.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal, cast

from indus.foundation.envload import managed_key_names
from indus.foundation.home import HOME_ENV
from indus.foundation.scope import Scope

log = logging.getLogger(__name__)

Variant = Literal["default", "minimal", "code"]

FORCE_PREFIX = "_INDUS_FORCE_"
SESSION_ENV = "INDUS_SESSION_ID"
AGENT_MARKERS: Mapping[str, str] = {"AI_AGENT": "indus-agent", "INDUS_AGENT": "true"}
PASSTHROUGH_CONFIG_KEY = "terminal.env_passthrough"

# Product names (``INDUS_*``) a child may keep from the parent. Empty: every product setting is
# policy or session state of this process; the pins above are written after the filter.
KEPT_PRODUCT_NAMES: frozenset[str] = frozenset()
_PRODUCT_PREFIXES = ("INDUS_", "_INDUS_")

MINIMAL_NAMES: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "TERM",
    "SHELL",
    "TMPDIR",
)

CODE_SAFE_PREFIXES: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_",
    "TERM",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SHELL",
    "LOGNAME",
    "XDG_",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "CONDA",
)
CREDENTIAL_WORDS: tuple[str, ...] = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "PASSWD",
    "AUTH",
    "DSN",
    "WEBHOOK",
    "CREDS",
    "BEARER",
    "APIKEY",
    "COOKIE",
)

# Our floor of names that belong to Indus's own integrations, grouped by owner and sorted. The
# general AWS credential chain is deliberately absent: the local terminal is the operator's shell.
_FLOOR_GROUPS: dict[str, tuple[str, ...]] = {
    "model providers": (
        "AI_GATEWAY_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "CEREBRAS_API_KEY",
        "COHERE_API_KEY",
        "DASHSCOPE_API_KEY",
        "DEEPSEEK_API_KEY",
        "FIREWORKS_API_KEY",
        "GEMINI_API_KEY",
        "GLM_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GROQ_API_KEY",
        "HELICONE_API_KEY",
        "HF_TOKEN",
        "KIMI_API_KEY",
        "LM_API_KEY",
        "LM_BASE_URL",
        "MINIMAX_API_KEY",
        "MISTRAL_API_KEY",
        "MOONSHOT_API_KEY",
        "NVIDIA_API_KEY",
        "OLLAMA_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORGANIZATION",
        "OPENAI_ORG_ID",
        "OPENROUTER_API_KEY",
        "PERPLEXITY_API_KEY",
        "TOGETHER_API_KEY",
        "XAI_API_KEY",
        "ZAI_API_KEY",
    ),
    "web backends": (
        "BRAVE_API_KEY",
        "EXA_API_KEY",
        "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL",
        "JINA_API_KEY",
        "PARALLEL_API_KEY",
        "SERPAPI_API_KEY",
        "TAVILY_API_KEY",
    ),
    "messaging platforms": (
        "DISCORD_BOT_TOKEN",
        "EMAIL_PASSWORD",
        "HASS_TOKEN",
        "MATTERMOST_TOKEN",
        "SIGNAL_ACCOUNT",
        "SLACK_APP_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_SIGNING_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_WEBHOOK_SECRET",
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_APP_SECRET",
    ),
    "github": (
        "GH_TOKEN",
        "GITHUB_APP_ID",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_TOKEN",
    ),
    # Kept equal to (or a superset of) ``indus.backends.vendor_env.VENDOR_TOKEN_NAMES``; safety
    # sits below backends in the layer order, so the declarations are mirrored here (P21-11).
    "sandbox vendors": (
        "DAYTONA_API_KEY",
        "DAYTONA_JWT_TOKEN",
        "E2B_API_KEY",
        "MODAL_OAUTH_CLIENT_SECRET",
        "MODAL_OAUTH_REFRESH_TOKEN",
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "VERCEL_OIDC_TOKEN",
        "VERCEL_PROJECT_ID",
        "VERCEL_TEAM_ID",
        "VERCEL_TOKEN",
    ),
}
STATIC_BLOCKED: frozenset[str] = frozenset(n for group in _FLOOR_GROUPS.values() for n in group)

# Interpreter location variables that would point a child's tools at Indus's own runtime.
_RUNTIME_LOCATION_NAMES = ("VIRTUAL_ENV", "CONDA_PREFIX")
_ALWAYS_DROPPED = frozenset({"PYTHONHOME"})

_lock = threading.Lock()
_registered: dict[str, frozenset[str]] = {}
_passthrough: set[str] = set()
_warned: set[str] = set()


# -- registration --------------------------------------------------------------------------------


def register_blocked_names(source: str, names: Iterable[str]) -> None:
    """Declare credential names owned by ``source`` (an adapter, plugin, sandbox vendor or the
    provider registry). Calling again for the same source replaces its set."""
    cleaned = frozenset(n.strip().upper() for n in names if n and n.strip())
    with _lock:
        _registered[source] = cleaned


def unregister_blocked_names(source: str) -> None:
    with _lock:
        _registered.pop(source, None)


def register_passthrough(names: Iterable[str]) -> list[str]:
    """Allow ``names`` (a skill's declared variables) into children; returns the refused ones.

    A blocked name is refused now and also filtered again at build time, so a name that becomes
    blocked later (an adapter registered after the skill) stops being forwarded.
    """
    refused: list[str] = []
    blocked = _blocked_now(None)
    for raw in names:
        name = raw.strip()
        if not name:
            continue
        if _is_blocked(name, blocked):
            refused.append(name)
            log.warning("refused env passthrough of %s: it is an Indus-managed credential", name)
            continue
        with _lock:
            _passthrough.add(name)
    return refused


def clear_passthrough() -> None:
    with _lock:
        _passthrough.clear()


# -- the blocklist -------------------------------------------------------------------------------


def _config_value(scope: Scope | None, key: str) -> object:
    if scope is None or scope.config is None:
        return None
    try:
        return scope.config.get(key)
    except Exception as exc:  # an odd config must not open the gate or break a spawn
        log.debug("could not read %s: %s", key, type(exc).__name__)
        return None


def _custom_provider_names(scope: Scope | None) -> set[str]:
    providers = _config_value(scope, "providers")
    names: set[str] = set()
    if not isinstance(providers, Mapping):
        return names
    for entry in cast(Mapping[Any, Any], providers).values():
        if isinstance(entry, Mapping):
            key_env = cast(Mapping[str, Any], entry).get("key_env")
            if isinstance(key_env, str) and key_env.strip():
                names.add(key_env.strip().upper())
    return names


def _blocked_now(scope: Scope | None) -> frozenset[str]:
    with _lock:
        registered = [n for names in _registered.values() for n in names]
    managed = {n.upper() for n in managed_key_names()}
    return frozenset({*STATIC_BLOCKED, *managed, *registered, *_custom_provider_names(scope)})


def blocked_names(scope: Scope | None = None) -> frozenset[str]:
    """Every explicitly blocked name (upper case) for ``scope``; ``INDUS_*`` goes by prefix."""
    return _blocked_now(scope)


def _is_product_name(upper: str) -> bool:
    return upper.startswith(_PRODUCT_PREFIXES) and upper not in KEPT_PRODUCT_NAMES


def _is_blocked(name: str, blocked: frozenset[str]) -> bool:
    upper = name.upper()
    return upper in blocked or _is_product_name(upper) or upper in _ALWAYS_DROPPED


def is_blocked(name: str, scope: Scope | None = None) -> bool:
    """True when ``name`` never reaches a child unless an internal caller forces it."""
    return _is_blocked(name, _blocked_now(scope))


def is_credential_name(name: str) -> bool:
    """The ``code`` variant's test: the name contains one of the credential words."""
    upper = name.upper()
    return any(word in upper for word in CREDENTIAL_WORDS)


# -- passthrough ---------------------------------------------------------------------------------


def passthrough_names(scope: Scope | None) -> tuple[list[str], list[str]]:
    """(accepted, refused) passthrough names: ``terminal.env_passthrough`` plus registered ones."""
    raw = _config_value(scope, PASSTHROUGH_CONFIG_KEY)
    configured: list[str] = []
    if isinstance(raw, list | tuple):
        configured = [str(v).strip() for v in cast(Iterable[Any], raw) if isinstance(v, str)]
    with _lock:
        registered = sorted(_passthrough)
    blocked = _blocked_now(scope)
    accepted: list[str] = []
    refused: list[str] = []
    for name in dict.fromkeys([*configured, *registered]):
        if not name:
            continue
        (refused if _is_blocked(name, blocked) else accepted).append(name)
    for name in refused:
        with _lock:
            first = name not in _warned
            _warned.add(name)
        if first:
            log.warning("env passthrough of %s refused: Indus-managed credentials never pass", name)
    return accepted, refused


def _scope_value(scope: Scope, name: str) -> str | None:
    """A passthrough value for this scope: its secret store first, then its own env view."""
    if scope.secrets is not None:
        try:
            value = scope.secrets.get(name)
        except Exception as exc:
            log.debug("secret store lookup for %s failed: %s", name, type(exc).__name__)
            value = None
        if value is not None:
            return value
    return scope.env.get(name)


# -- building ------------------------------------------------------------------------------------


def _own_runtime_prefixes() -> set[str]:
    out: set[str] = set()
    for raw in (sys.prefix, sys.exec_prefix):
        try:
            out.add(str(Path(raw).resolve()))
        except OSError:
            out.add(raw)
    return out


def _points_at_own_runtime(value: str) -> bool:
    try:
        resolved = str(Path(value).resolve())
    except (OSError, ValueError):
        return False
    return resolved in _own_runtime_prefixes()


def _split_forced(env: dict[str, str]) -> dict[str, str]:
    """Remove ``_INDUS_FORCE_<NAME>`` entries from ``env``; return ``{NAME: value}``."""
    forced: dict[str, str] = {}
    for key in [k for k in env if k.upper().startswith(FORCE_PREFIX)]:
        value = env.pop(key)
        target = key[len(FORCE_PREFIX) :]
        if target:
            forced[target] = value
    return forced


def _pins(scope: Scope, session_id: str | None) -> dict[str, str]:
    pins = {HOME_ENV: str(scope.home), **AGENT_MARKERS}
    if session_id:
        pins[SESSION_ENV] = session_id
    return pins


def _filter_default(env: Mapping[str, str], blocked: frozenset[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in env.items():
        if _is_blocked(name, blocked):
            continue
        if name.upper() in _RUNTIME_LOCATION_NAMES and _points_at_own_runtime(value):
            continue
        out[name] = value
    return out


def _filter_code(env: Mapping[str, str], blocked: frozenset[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in env.items():
        upper = name.upper()
        if _is_blocked(name, blocked) or is_credential_name(name):
            continue
        if not upper.startswith(CODE_SAFE_PREFIXES):
            continue
        if upper in _RUNTIME_LOCATION_NAMES and _points_at_own_runtime(value):
            continue
        out[name] = value
    return out


def child_env(
    scope: Scope,
    *,
    variant: Variant = "default",
    extra: Mapping[str, str] | None = None,
    session_id: str | None = None,
) -> dict[str, str]:
    """The environment for a child process acting for ``scope``. See the module docstring."""
    if variant == "minimal":
        base = {n: scope.env[n] for n in MINIMAL_NAMES if n in scope.env}
        base.update(extra or {})
        return base

    merged = {**scope.env, **(extra or {})}
    forced = _split_forced(merged)
    blocked = _blocked_now(scope)
    env = _filter_code(merged, blocked) if variant == "code" else _filter_default(merged, blocked)

    accepted, _ = passthrough_names(scope)
    for name in accepted:
        value = _scope_value(scope, name)
        if value is not None:
            env[name] = value

    env.update(forced)
    env.update(_pins(scope, session_id))
    return env
