"""Where a ``config set`` key belongs, whether the schema knows it, and how values are shown.

Routing (``route_for``): a bare credential-shaped name goes to the secret store, any other
UPPER_SNAKE name to ``.env``, everything else to ``config.yaml``. Type coercion and the
section/container guards are P01's (``foundation.config.keys``); this module adds the key checks
(``check_key``: known, a did-you-mean, and whether the key is a known key behind an extra prefix),
the display masking shared by ``config show`` and ``config get``, and the stale-route rule for a
provider switch (R04.9).
"""

from __future__ import annotations

import difflib
import re
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from indus.foundation import redact
from indus.foundation.config import keys, schema
from indus.foundation.scope import Scope
from indus.providers import bundled
from indus.providers.hosts import is_loopback, parse_base_url
from indus.providers.registry import Registry

__all__ = [
    "CREDENTIAL_SUFFIXES",
    "OPEN_ROOTS",
    "KeyCheck",
    "Route",
    "admin_pinned",
    "check_key",
    "credential_names",
    "drop_stale_route",
    "is_credential_name",
    "is_secret_key",
    "known_roots",
    "mask_tree",
    "mask_value",
    "route_for",
    "show_value",
]

CREDENTIAL_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET")

# Top-level keys whose children are the user's own names (any path below them is accepted).
OPEN_ROOTS = frozenset(
    {
        "providers",
        "quick_commands",
        "platform_toolsets",
        "credential_pool_strategies",
        "fallback_providers",
        "command_allowlist",
        "doctor",
    }
)

type Route = Literal["secret", "env", "config"]


def credential_names() -> frozenset[str]:
    """Key names the bundled provider profiles read (base-URL names are settings, not keys)."""
    return frozenset(name for profile in bundled.PROFILES for name in profile.env_vars)


def is_credential_name(key: str) -> bool:
    """A bare name (no dot) that holds a credential: ``*_API_KEY``, ``*_TOKEN``, ``*_SECRET`` or a
    provider key name; compared upper-case, so ``openai_api_key`` counts."""
    if "." in key or not key:
        return False
    upper = key.upper()
    return upper.endswith(CREDENTIAL_SUFFIXES) or upper in credential_names()


def route_for(key: str) -> Route:
    if is_credential_name(key) and keys.is_env_shaped(key.upper()):
        return "secret"
    if keys.is_env_shaped(key):
        return "env"
    return "config"


def admin_pinned(key: str) -> bool:
    """Keys an administrator overlay pins (I20). The overlay is not built yet, so none are."""
    return False


# -- schema checks --------------------------------------------------------------------------------


def known_roots() -> frozenset[str]:
    return frozenset(schema.DEFAULTS) | OPEN_ROOTS


@dataclass(frozen=True)
class KeyCheck:
    known: bool
    suggestion: str | None = None
    wrong_prefix: bool = False


def _walk_known(parts: list[str]) -> bool:
    """Does the schema (or an open section) cover this path?"""
    top = parts[0]
    if top.startswith("_") or top in OPEN_ROOTS:
        return True
    if top not in schema.DEFAULTS:
        return False
    node: Any = schema.DEFAULTS[top]
    for seg in parts[1:]:
        if isinstance(node, list):
            return True
        if not isinstance(node, Mapping):
            return False
        mapping = cast(Mapping[str, Any], node)
        if not mapping:
            return True  # a free-form mapping: its keys are the user's own
        if seg not in mapping:
            return False
        node = mapping[seg]
    return True


def _as_known_suffix(parts: list[str]) -> str | None:
    if parts and parts[0] in known_roots() and _walk_known(parts):
        return keys.join_key(parts)
    return None


def check_key(key: str) -> KeyCheck:
    """Whether the schema knows ``key``; else a suggestion, and whether the key is a known key
    with an extra prefix (``foo.agent.max_turns``), which ``config set`` refuses."""
    parts = keys.split_key(key)
    if _walk_known(parts):
        return KeyCheck(True)
    roots = known_roots()
    top = parts[0]
    if top not in roots:
        for start in range(1, len(parts)):
            found = _as_known_suffix(parts[start:])
            if found is not None:
                return KeyCheck(False, found, wrong_prefix=True)
        close = difflib.get_close_matches(top, sorted(roots), n=1, cutoff=0.6)
        if close:
            return KeyCheck(False, keys.join_key([close[0], *parts[1:]]))
        return KeyCheck(False)
    node: Any = schema.DEFAULTS[top]
    for index in range(1, len(parts)):
        if not isinstance(node, Mapping) or not node:
            break
        mapping = cast(Mapping[str, Any], node)
        seg = parts[index]
        if seg not in mapping:
            for start in range(index, len(parts)):
                found = _as_known_suffix(parts[start:])
                if found is not None:
                    return KeyCheck(False, found, wrong_prefix=True)
            close = difflib.get_close_matches(seg, sorted(mapping), n=1, cutoff=0.6)
            if close:
                return KeyCheck(False, keys.join_key([*parts[:index], close[0]]))
            return KeyCheck(False)
        node = mapping[seg]
    return KeyCheck(False)


# -- masking --------------------------------------------------------------------------------------

_SECRET_WORDS = frozenset(
    {"key", "apikey", "token", "secret", "password", "passwd", "credential", "credentials"}
)
# A last word that says the value names or locates a secret rather than holding one.
_NAMING_WORDS = frozenset({"env", "name", "names", "url", "id", "file", "path", "type", "mode"})
_SPLIT = re.compile(r"[_\-.\s]+")


def is_secret_key(key: str) -> bool:
    """The last segment of ``key`` names a credential (``api_key``, ``bot_token``,
    ``OPENAI_API_KEY``); ``max_tokens`` and ``key_env`` do not."""
    leaf = key.rsplit(".", 1)[-1]
    words = [w for w in _SPLIT.split(leaf.lower()) if w]
    if not words or words[-1] in _NAMING_WORDS:
        return False
    return any(w in _SECRET_WORDS for w in words)


def _reference(value: str) -> bool:
    return value.strip().startswith("$")


def mask_value(value: str) -> str:
    """``abcd…wxyz`` (or ``[redacted]`` when short); references such as ``${NAME}`` stay."""
    if not value or _reference(value):
        return value
    return redact.mask(value)


def mask_tree(value: Any, scope: Scope, *, key: str | None = None) -> Any:
    """A copy of ``value`` safe to print: strings under credential-shaped names masked whole,
    every other string run through the redactor (vendor key shapes, values the profile holds)."""
    redactor = redact.Redactor(scope)

    def walk(node: Any, name: str | None, depth: int) -> Any:
        if depth > redact.MAX_DEPTH:
            return redact.DEPTH_MARKER
        if isinstance(node, str):
            if name is not None and is_secret_key(name):
                return mask_value(node)
            return redactor.redact(node, force=True)
        if isinstance(node, Mapping):
            mapping = cast(Mapping[Any, Any], node)
            return {k: walk(v, str(k), depth + 1) for k, v in mapping.items()}
        if isinstance(node, list | tuple):
            return [walk(v, name, depth + 1) for v in cast(list[Any], node)]
        return node

    return walk(value, key, 0)


def show_value(value: Any) -> str:
    """How ``config get`` prints a value without ``--json``."""
    from indus.foundation import yamlio

    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, Mapping | list):
        return textwrap.dedent(yamlio.dump_plain(value)).rstrip("\n")
    return str(value)


# -- provider switch ------------------------------------------------------------------------------


def _same_host(url: str, other: str) -> bool:
    if not url or not other:
        return False
    try:
        return parse_base_url(url).host == parse_base_url(other).host
    except Exception:
        return False


def drop_stale_route(
    model: dict[str, Any], new_provider: str, scope: Scope
) -> tuple[dict[str, str], str | None]:
    """Remove ``base_url``/``api_mode`` from ``model`` when they belonged to the provider being
    left (R04.9). Returns ``(dropped, warning)``. Kept: a local URL, a URL on the new provider's
    own host, and anything when the new provider is a custom endpoint."""
    url = str(model.get("base_url") or "")
    mode = str(model.get("api_mode") or "")
    if not url and not mode:
        return {}, None
    if url and is_loopback(url):
        return {}, None
    profile = Registry(scope).lookup(new_provider)
    if profile is None:
        where = f"model.base_url ({url})" if url else "model.api_mode"
        return {}, f"'{new_provider}' is not a known provider, so {where} was kept."
    if profile.family == "custom":
        return {}, None
    if url and _same_host(url, profile.base_url):
        return {}, None
    dropped: dict[str, str] = {}
    for name in ("base_url", "api_mode"):
        if model.get(name):
            dropped[name] = str(model.pop(name))
    return dropped, None
