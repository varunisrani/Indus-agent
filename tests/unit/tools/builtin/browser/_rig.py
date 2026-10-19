"""Shared set-up for the browser tool tests: a scope with a fake clock, a fake driver, a manager."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from indus.foundation.scope import Scope, make_scope
from indus.tools.builtin.browser import BrowserTools
from indus.tools.builtin.browser.session import SessionManager
from tests.fakes.agent import DictConfig
from tests.fakes.browser import FakeDriver, FakeProcesses, FakeSite
from tests.fakes.clock import FakeClock
from tests.fakes.resolver import FakeResolver

PUBLIC = "93.184.216.34"


def make_clock() -> FakeClock:
    return FakeClock(datetime(2026, 3, 1, tzinfo=UTC))


def make_scope_for(
    tmp_path: Path,
    clock: FakeClock,
    config: Mapping[str, Any] | None = None,
    *,
    name: str = "home",
    env: Mapping[str, str] | None = None,
) -> Scope:
    home = tmp_path / name
    home.mkdir(parents=True, exist_ok=True)
    base = {"PATH": "/usr/bin:/bin", "HOME": str(home), **(env or {})}
    return make_scope(home, env=base, clock=clock).with_config(DictConfig(config or {}))


@dataclass
class Rig:
    clock: FakeClock
    driver: FakeDriver
    processes: FakeProcesses
    manager: SessionManager
    tools: BrowserTools
    resolver: FakeResolver


def make_rig(sites: Mapping[str, FakeSite] | None = None, **manager_kw: Any) -> Rig:
    clock = make_clock()
    driver = FakeDriver(sites)
    processes = FakeProcesses()
    manager = SessionManager(
        driver_factory=lambda: driver,
        clock=clock,
        processes=processes,
        janitor=False,
        sandbox_check=lambda scope: False,
        **manager_kw,
    )
    resolver = FakeResolver({"example.com": [PUBLIC], "shop.example": [PUBLIC]})
    tools = BrowserTools(manager, resolver=resolver)
    return Rig(clock, driver, processes, manager, tools, resolver)
