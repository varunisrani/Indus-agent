"""Which model a cron run talks to (P14-11): the job's pin, else ``cron.model`` /
``cron.provider``, else the main model at fire time. A pinned job never walks the fallback chain
(it fails rather than switch model); credential-pool rotation applies to every job."""

from __future__ import annotations

from dataclasses import dataclass

from indus.cron import settings
from indus.cron.ports import JobSpec
from indus.foundation.scope import Scope

__all__ = ["RouteChoice", "route_choice"]


@dataclass(frozen=True)
class RouteChoice:
    model: str | None = None
    provider: str | None = None
    base_url: str | None = None
    pinned: bool = False
    reasoning_effort: str | None = None


def route_choice(job: JobSpec, scope: Scope) -> RouteChoice:
    if job.route_pinned:
        return RouteChoice(
            model=job.model,
            provider=job.provider,
            base_url=job.base_url,
            pinned=True,
            reasoning_effort=job.reasoning_effort,
        )
    return RouteChoice(
        model=settings.text_setting(scope, "cron.model") or None,
        provider=settings.text_setting(scope, "cron.provider") or None,
        pinned=False,
        reasoning_effort=job.reasoning_effort,
    )
