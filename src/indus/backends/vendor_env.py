# pyright: strict
"""Vendor credentials of the cloud backends, and what reaches a sandbox's environment (P21-11).

Token names: each cloud backend declares the vendor's standard credential names (D-16.22).
``register_vendor_names`` hands them to the P05 child-environment builder, so they are stripped
from every child process, MCP server and sandbox env (R02.22); they are also in that builder's
static floor, which keeps them out before registration. ``vendor_credentials`` reads them for the
backend itself (the profile's secret store first, then its env view); the backend passes them to
the SDK as arguments, never through ``os.environ``.

Forwarded variables (``terminal.env_passthrough`` and skill-declared names) reach a sandbox only
through the SDK's ``env`` parameter (``sdk_exec``) or, for Apptainer, the client's
``APPTAINERENV_*`` environment, never as text in the command (R02.7). Their values are read fresh
for every command; a name without a value now is unset in the command, and every passthrough name
is kept out of the shell snapshot together with the per-session names (R02.1).
"""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from indus.foundation.scope import Scope
from indus.safety.child_env import passthrough_names, register_blocked_names

__all__ = [
    "REQUIRED_SETS",
    "VENDOR_SOURCE",
    "VENDOR_TOKEN_NAMES",
    "PassthroughPlan",
    "SdkExec",
    "VendorCredentials",
    "passthrough_plan",
    "register_vendor_names",
    "sdk_exec",
    "snapshot_excluded_names",
    "unset_line",
    "vendor_credentials",
    "vendor_token_names",
]

log = logging.getLogger(__name__)

VENDOR_SOURCE = "sandbox vendors"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

VENDOR_TOKEN_NAMES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "modal": (
            "MODAL_TOKEN_ID",
            "MODAL_TOKEN_SECRET",
            "MODAL_OAUTH_CLIENT_SECRET",
            "MODAL_OAUTH_REFRESH_TOKEN",
        ),
        "daytona": ("DAYTONA_API_KEY", "DAYTONA_JWT_TOKEN"),
        "vercel_sandbox": (
            "VERCEL_TOKEN",
            "VERCEL_OIDC_TOKEN",
            "VERCEL_PROJECT_ID",
            "VERCEL_TEAM_ID",
        ),
    }
)

# Alternatives, each a set of names that together let the backend sign in.
REQUIRED_SETS: Mapping[str, tuple[tuple[str, ...], ...]] = MappingProxyType(
    {
        "modal": (("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"),),
        "daytona": (("DAYTONA_API_KEY",), ("DAYTONA_JWT_TOKEN", "DAYTONA_ORGANIZATION_ID")),
        "vercel_sandbox": (
            ("VERCEL_TOKEN", "VERCEL_PROJECT_ID", "VERCEL_TEAM_ID"),
            ("VERCEL_OIDC_TOKEN",),
        ),
    }
)
_LABELS: Mapping[str, str] = MappingProxyType(
    {"modal": "Modal", "daytona": "Daytona", "vercel_sandbox": "Vercel Sandbox"}
)


def vendor_token_names() -> frozenset[str]:
    """Every credential name a cloud backend declares."""
    return frozenset(n for names in VENDOR_TOKEN_NAMES.values() for n in names)


def register_vendor_names() -> None:
    """Block the vendor names in every child environment (idempotent; call at startup)."""
    register_blocked_names(VENDOR_SOURCE, sorted(vendor_token_names()))


# -- credentials ----------------------------------------------------------------------------------


def _value(scope: Scope, name: str) -> str | None:
    if scope.secrets is not None:
        try:
            found = scope.secrets.get(name)
        except Exception as exc:
            log.debug("secret store lookup for %s failed: %s", name, type(exc).__name__)
            found = None
        if found:
            return found
    return scope.env.get(name) or None


@dataclass(frozen=True, slots=True)
class VendorCredentials:
    """What was found for one backend; ``missing`` is empty when it can sign in."""

    values: Mapping[str, str] = field(default_factory=lambda: {})
    missing: tuple[str, ...] = ()

    def reason(self, backend: str) -> str | None:
        """A requirement reason for ``doctor`` and startup, or None when complete."""
        if not self.missing:
            return None
        label = _LABELS.get(backend, backend)
        return f"{label} credentials missing: set {' and '.join(self.missing)}"

    def __repr__(self) -> str:  # never show values
        return f"VendorCredentials(names={sorted(self.values)}, missing={list(self.missing)})"


def vendor_credentials(scope: Scope, backend: str) -> VendorCredentials:
    """The backend's credentials for ``scope`` (secret store first, then env)."""
    alternatives = REQUIRED_SETS.get(backend)
    if alternatives is None:
        raise ValueError(f"no vendor credentials are declared for {backend!r}")
    wanted = dict.fromkeys(
        [*VENDOR_TOKEN_NAMES.get(backend, ()), *(n for group in alternatives for n in group)]
    )
    values = {name: v for name in wanted if (v := _value(scope, name)) is not None}
    for group in alternatives:
        if all(name in values for name in group):
            return VendorCredentials(values, ())
    first = alternatives[0]
    return VendorCredentials(values, tuple(n for n in first if n not in values))


# -- passthrough ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PassthroughPlan:
    """``values`` to hand the sandbox, ``unset`` names with no value now, all ``names``."""

    values: Mapping[str, str] = field(default_factory=lambda: {})
    unset: tuple[str, ...] = ()
    names: tuple[str, ...] = ()


def _valid(names: Iterable[str]) -> list[str]:
    return [n for n in dict.fromkeys(n.strip() for n in names) if n and _NAME.match(n)]


def passthrough_plan(scope: Scope) -> PassthroughPlan:
    """The forwarded variables for one command (blocked names never pass)."""
    accepted, _ = passthrough_names(scope)
    names = _valid(accepted)
    blocked = vendor_token_names()
    names = [n for n in names if n.upper() not in blocked]
    values: dict[str, str] = {}
    unset: list[str] = []
    for name in names:
        found = _value(scope, name)
        if found is None:
            unset.append(name)
        else:
            values[name] = found
    return PassthroughPlan(values, tuple(sorted(unset)), tuple(sorted(names)))


def unset_line(names: Iterable[str]) -> str:
    """``unset NAME…`` as one script line ("" when there is nothing to unset)."""
    wanted = sorted({n for n in names if _NAME.match(n)})
    if not wanted:
        return ""
    return f"unset {' '.join(shlex.quote(n) for n in wanted)} 2>/dev/null || true\n"


@dataclass(frozen=True, slots=True)
class SdkExec:
    """A command for an SDK exec: ``command`` holds names only, ``env`` the values."""

    command: str
    env: Mapping[str, str]

    def __repr__(self) -> str:  # never show values
        return f"SdkExec(command={self.command!r}, env_names={sorted(self.env)})"


def sdk_exec(scope: Scope, script: str) -> SdkExec:
    """``script`` ready for a vendor exec call: values go in ``env`` (the SDK's env parameter)."""
    plan = passthrough_plan(scope)
    return SdkExec(unset_line(plan.unset) + script, dict(plan.values))


def snapshot_excluded_names(scope: Scope) -> tuple[str, ...]:
    """Names kept out of the shell snapshot besides the per-session ones (P05 wrapper)."""
    return passthrough_plan(scope).names
