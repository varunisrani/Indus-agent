# pyright: strict
"""The key and address variables our tools declare they read (W60-L2-1).

Nothing here is a new table: every name comes from a declaration the tool code itself reads. The
web providers (``secret_names`` / ``url_names`` on each provider of the default registry), the
cloud browser providers (``secret_names`` / ``url_names``), the Camofox engine's optional bearer
key, the browser's own-Chrome address (``INDUS_BROWSER_CDP_URL``) and, when a registry is given,
every registered tool's ``requires_secrets`` (plugin tools declare theirs there). Model provider
keys are not listed; they belong to the provider profiles.

``tool_key_needs()`` returns one ``ToolKeys`` per backend that reads at least one variable, in a
stable order (built-ins first, then registry tools by name). Values are never read here; whether
a variable is set is the caller's question.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from indus.foundation.scope import Scope
from indus.tools.registry import ToolRegistry

__all__ = ["KeyNeed", "ToolKeys", "tool_key_needs"]

type NeedKind = Literal["key", "url"]


@dataclass(frozen=True)
class KeyNeed:
    name: str
    kind: NeedKind


@dataclass(frozen=True)
class ToolKeys:
    """One backend of one toolset and the variables it reads."""

    toolset: str
    backend: str
    label: str
    needs: tuple[KeyNeed, ...]


def _needs(keys: tuple[str, ...], urls: tuple[str, ...]) -> tuple[KeyNeed, ...]:
    return (*(KeyNeed(n, "key") for n in keys), *(KeyNeed(n, "url") for n in urls))


def _web() -> list[ToolKeys]:
    from indus.tools.builtin.web.providers import default_registry

    rows: list[ToolKeys] = []
    for provider in default_registry().all():
        needs = _needs(provider.secret_names, provider.url_names)
        if needs:
            rows.append(ToolKeys("web", provider.name, provider.title or provider.name, needs))
    return rows


def _browser() -> list[ToolKeys]:
    from indus.tools.builtin.browser.cloud import PROVIDERS
    from indus.tools.builtin.browser.engines import Camofox
    from indus.tools.builtin.browser.session import CDP_URL_ENV

    rows = [
        ToolKeys("browser", p.name, p.label, _needs(p.secret_names, p.url_names))
        for p in PROVIDERS.values()
        if p.secret_names or p.url_names
    ]
    rows.append(ToolKeys("browser", "camofox", Camofox.label, _needs(Camofox.secret_names, ())))
    rows.append(ToolKeys("browser", "cdp", "Chrome (CDP)", _needs((), (CDP_URL_ENV,))))
    return rows


def _registered(registry: ToolRegistry, scope: Scope | None) -> list[ToolKeys]:
    return [
        ToolKeys(entry.toolset, entry.name, entry.name, _needs(entry.requires_secrets, ()))
        for entry in registry.entries(scope)
        if entry.requires_secrets
    ]


def tool_key_needs(
    registry: ToolRegistry | None = None, scope: Scope | None = None
) -> list[ToolKeys]:
    """Every declared tool variable, grouped per backend (see the module notes)."""
    rows = [*_web(), *_browser()]
    if registry is not None:
        rows.extend(_registered(registry, scope))
    return rows
