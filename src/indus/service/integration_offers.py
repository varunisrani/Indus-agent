# pyright: strict
"""Suggested jobs when an integration is connected (P24-46 follow-up, wave 45).

Two kinds of integration exist: an MCP server (``mcp``, named by its server name) and a messaging
platform (``platform``, named by the platform). The surfaces where a person connects one call
``connected(scope, kind, name)`` once the connection is saved: ``indus mcp add`` and the
dashboard's MCP page, ``indus gateway setup`` and the dashboard's Channels page (a platform turned
on). Every blueprint (``cron.blueprints``: the starter catalogue and the skills') whose
``integrations`` list names the integration's kind, its bare name or ``kind:name`` and that fills
from its defaults alone is offered as a suggested job with source ``integration``
(``cron.suggestions.offer``). The dedup key is the catalogue refresh's (``blueprint:<key>``), so a
blueprint is offered once whichever way it was found, and never again after an accept or a
dismiss. Nothing is scheduled until the person accepts it.

``connected`` never raises (a connect never fails because of an offer) and returns the suggestions
it added, for the surface to mention. The cron modules load on the first call.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final, Literal

from indus.foundation.scope import Scope

if TYPE_CHECKING:
    from indus.cron.blueprints import Blueprint
    from indus.cron.suggestions import Suggestion

__all__ = ["KINDS", "IntegrationKind", "connected", "notice_lines", "offer_for", "wants"]

log = logging.getLogger(__name__)

type IntegrationKind = Literal["mcp", "platform"]
KINDS: Final[tuple[IntegrationKind, ...]] = ("mcp", "platform")


def wants(bp: Blueprint, kind: str, name: str) -> bool:
    """Whether ``bp`` waits for the integration ``kind``/``name``."""
    tags = set(bp.integrations)
    label = name.strip().lower()
    return bool(tags) and (kind in tags or label in tags or f"{kind}:{label}" in tags)


def offer_for(scope: Scope, kind: str, name: str) -> list[Suggestion]:
    """Offer the blueprints waiting for ``kind``/``name`` (blocking); the suggestions added."""
    from indus.cron.blueprints import BlueprintError, fill, list_blueprints
    from indus.cron.suggestions import offer

    if kind not in KINDS or not name.strip():
        return []
    added: list[Suggestion] = []
    for bp in list_blueprints(scope):
        if not wants(bp, kind, name) or not bp.fills_from_defaults():
            continue
        try:
            spec = fill(bp)
        except BlueprintError as exc:
            log.info("blueprint %s does not fill from its defaults: %s", bp.key, exc)
            continue
        made = offer(
            scope,
            dedup_key=f"blueprint:{bp.key}",
            title=bp.title,
            spec=spec,
            source="integration",
            description=bp.description,
        )
        if made is not None:
            added.append(made)
    return added


def connected(scope: Scope, kind: IntegrationKind, name: str) -> list[Suggestion]:
    """The integration ``kind``/``name`` was connected: offer what waits for it. Never raises."""
    try:
        added = offer_for(scope, kind, name)
    except Exception:
        log.warning("could not offer the jobs waiting for %s %s", kind, name, exc_info=True)
        return []
    if added:
        log.info("connecting %s %s suggested %d job(s)", kind, name, len(added))
    return added


def notice_lines(added: list[Suggestion]) -> list[str]:
    """One line per new suggestion, for a command-line surface."""
    return [
        f"Suggested job: {s.title} (nothing runs until you accept it: `indus cron suggestions`)."
        for s in added
    ]
