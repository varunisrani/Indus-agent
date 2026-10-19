"""Builders for the curator and journey tests: skills with usage records at given ages."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.learning import curator
from indus.learning.curator import ledger_port
from indus.learning.skills.ledger import SkillLedger
from indus.learning.skills.resolver import clear_scan_cache
from indus.learning.skills.usage import SkillUsage
from tests.unit.learning.skills.helpers import FakeConfig, make_skill, record_hub

DAY = 86400.0


@pytest.fixture(autouse=True)
def clean_registries() -> Iterator[None]:
    curator.clear_reference_sources()
    restore = ledger_port.set_ledger(None)
    clear_scan_cache()
    yield
    curator.clear_reference_sources()
    restore()
    clear_scan_cache()


def ledger_rows(scope: Scope) -> list[dict[str, Any]]:
    """The profile's skill ledger (P09-13), oldest entry first."""
    return [e.to_json() for e in reversed(SkillLedger.for_scope(scope).entries())]


def with_settings(scope: Scope, **sections: Mapping[str, Any]) -> Scope:
    return scope.with_config(FakeConfig(dict(sections)))


def skills_root(scope: Scope) -> Path:
    return scope.home / "skills"


def add_skill(
    scope: Scope,
    name: str,
    *,
    origin: str | None = "agent",
    created_days: float | None = 0.0,
    active_days: float | None = None,
    use_count: int = 0,
    pinned: bool = False,
    state: str = "active",
    rel: str | None = None,
    record: bool = True,
    body: str = "Steps go here.\n",
) -> Path:
    """A skill folder plus its usage record; ``*_days`` are ages before the clock's now."""
    folder = make_skill(skills_root(scope), rel or name, name, body=body)
    if not record:
        return folder
    now = scope.clock.now()

    def ago(days: float | None) -> str | None:
        if days is None:
            return None
        return (now - timedelta(days=days)).isoformat(timespec="seconds")

    def change(rec: dict[str, Any], _now: str) -> None:
        rec.update(
            origin=origin,
            created_at=ago(created_days),
            last_activity_at=ago(active_days),
            last_used_at=ago(active_days) if use_count else None,
            use_count=use_count,
            pinned=pinned,
            state=state,
        )

    SkillUsage(skills_root(scope), clock=scope.clock).update(name, change, force=True)
    return folder


def mark_bundled(scope: Scope, *names: str) -> None:
    path = skills_root(scope) / ".bundled_manifest"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = "".join(f"{n}:{'0' * 64}\n" for n in names)
    path.write_text(path.read_text() + rows if path.exists() else rows, encoding="utf-8")


def mark_hub(scope: Scope, *names: str) -> None:
    for name in names:
        record_hub(skills_root(scope), name)


def record(scope: Scope, name: str) -> dict[str, Any]:
    return SkillUsage(skills_root(scope), clock=scope.clock).get(name)
