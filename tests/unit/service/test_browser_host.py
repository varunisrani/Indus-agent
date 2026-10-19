"""Browser sessions end with their owner: a finished subagent, a closed session, host exit."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from indus.core.subagents.teardown import (
    BROWSER_RELEASER,
    ChildResources,
)
from indus.core.subagents.teardown import (
    _releasers as releasers,  # pyright: ignore[reportPrivateUsage]
)
from indus.service import code_host
from indus.service.code_host import KernelUpkeep
from indus.tools.builtin import browser
from tests.fakes.browser import FakeSite
from tests.unit.tools.builtin.browser._rig import Rig, make_clock, make_rig, make_scope_for


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    made = make_rig({"https://example.com/": FakeSite(title="Home")})
    monkeypatch.setattr(browser.DEFAULT_TOOLS, "manager", made.manager)
    return made


def test_a_finished_subagent_closes_its_browser(rig: Rig, tmp_path: Path) -> None:
    assert BROWSER_RELEASER in releasers
    scope = make_scope_for(tmp_path, make_clock())

    async def scenario() -> None:
        await rig.manager.session("child", scope)
        await rig.manager.session("parent", scope)
        child = ChildResources(
            scope=scope, parent_session_id="parent", child_session_id="child", subagent_id="sa-1"
        )
        await child.close()
        assert rig.manager.get("child") is None
        assert rig.manager.get("parent") is not None  # the parent's browser stays
        await rig.manager.shutdown()

    asyncio.run(scenario())
    assert len(rig.driver.closed) == 2


def test_a_closed_session_closes_its_browser_and_host_exit_closes_the_rest(
    rig: Rig, tmp_path: Path
) -> None:
    scope = make_scope_for(tmp_path, make_clock())

    async def scenario() -> None:
        for task in ("old-id", "other"):
            await rig.manager.session(task, scope)
        await code_host.end_session("current-id", "old-id")  # an id it continued from
        assert rig.manager.get("old-id") is None and rig.manager.get("other") is not None
        await KernelUpkeep(scope, reaper=False).shutdown()
        assert rig.manager.sessions == {}
        assert rig.driver.stopped  # the Playwright driver of the loop is stopped too

    asyncio.run(scenario())
    profiles = scope.home / "cache" / "browser" / "profiles"
    assert not profiles.exists() or list(profiles.iterdir()) == []


def test_a_browser_that_will_not_close_is_reaped(rig: Rig, tmp_path: Path) -> None:
    scope = make_scope_for(tmp_path, make_clock())
    rig.driver.close_failures = 1

    async def scenario() -> None:
        await rig.manager.session("child", scope)
        await rig.manager.end("child")
        assert rig.manager.get("child") is None

    asyncio.run(scenario())
