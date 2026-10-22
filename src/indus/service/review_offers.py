# pyright: strict
"""Suggested jobs from the background review (P24-46 wiring, D-16.799).

When a review (P09-15) created or changed a skill, and that skill declares a ``blueprint`` block
that fills from its defaults alone, the blueprint is offered as a suggested job with source
``usage`` (``cron.suggestions.offer``): nothing is scheduled until a person accepts it with
``/suggestions``. The dedup key is the one the catalogue refresh uses (``blueprint:<key>``), so a
blueprint is offered once whichever way it was found, and never again after a dismiss.

``listen(runner, scope)`` hangs ``offer_from_review`` on a session's review runner; the host that
attaches reviews (the CLI runtime) calls it. The store work runs off the event loop and never
raises into the review. The cron modules load on the first offer, not with the CLI runtime
(``indus -z`` cold start, D-04.1 budget).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from indus.foundation.blocking import run_blocking
from indus.foundation.scope import Scope
from indus.learning.review.fork import ReviewOutcome, ReviewRunner
from indus.learning.review.summary import written_skills

if TYPE_CHECKING:
    from indus.cron.suggestions import Suggestion

__all__ = ["listen", "offer_blueprints_of", "offer_from_review"]

log = logging.getLogger(__name__)


def offer_blueprints_of(scope: Scope, skills: Iterable[str]) -> list[Suggestion]:
    """Offer the blueprints ``skills`` declare (``usage``); the suggestions added."""
    from indus.cron.blueprints import BlueprintError, fill, skill_blueprints
    from indus.cron.suggestions import offer

    wanted = {name.strip().lower() for name in skills if name.strip()}
    if not wanted:
        return []
    added: list[Suggestion] = []
    for bp in skill_blueprints(scope):
        if bp.skill not in wanted or not bp.fills_from_defaults():
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
            source="usage",
            description=bp.description,
        )
        if made is not None:
            added.append(made)
    return added


async def offer_from_review(scope: Scope, outcome: ReviewOutcome) -> list[Suggestion]:
    """The suggestions a finished review earned (none unless it wrote a skill); never raises."""
    if outcome.status != "done":
        return []
    skills = written_skills(outcome.rows)
    if not skills:
        return []
    try:
        added = await run_blocking(offer_blueprints_of, scope, skills, label="review suggestions")
    except Exception:
        log.warning("could not offer the review's suggested jobs", exc_info=True)
        return []
    if added:
        log.info("the review suggested %d job(s): %s", len(added), [s.title for s in added])
    return added


def listen(runner: ReviewRunner, scope: Scope) -> None:
    """Offer suggested jobs after each of ``runner``'s reviews."""

    async def heard(outcome: ReviewOutcome) -> None:
        await offer_from_review(scope, outcome)

    runner.listeners.append(heard)
