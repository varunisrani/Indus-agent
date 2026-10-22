"""The host side of language servers (P24-15/16 wiring) and the project a new session joins
(P24-14 wiring): what ``SessionService`` does with ``lsp`` and a create ``cwd``."""

from __future__ import annotations

import asyncio
from pathlib import Path

from indus.foundation.scope import Scope
from indus.protocol.methods import SessionCreateParams
from indus.service.lsp_host import LspUpkeep
from indus.service.session_service import SessionService
from indus.state.projects import ProjectStore
from indus.tools.lsp import diagnostics
from indus.tools.lsp.manager import LspSettings
from tests.fakes.agent import fake_options
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, fake_route, script_of

ON = LspSettings()  # the defaults: on (the test suite turns hosts' own settings off)


def test_services_of_one_process_share_one_tier(scope: Scope) -> None:
    async def main() -> None:
        first, second = LspUpkeep(scope, settings=ON), LspUpkeep(scope, settings=ON)
        first.start()
        checker = diagnostics.active()
        assert checker is not None and first.manager is checker.manager
        second.start()
        second.start()  # once per upkeep
        assert diagnostics.active() is checker and second.manager is first.manager
        await first.shutdown()
        assert diagnostics.active() is checker  # the other service still uses it
        await first.shutdown()  # a second leave changes nothing
        assert diagnostics.active() is checker
        await second.shutdown()
        assert diagnostics.active() is None and second.manager is None

    asyncio.run(main())


def test_hosts_in_tests_get_no_tier_and_lsp_off_installs_nothing(scope: Scope) -> None:
    async def main() -> None:
        LspUpkeep(scope).start()  # the suite's guard: settings from the scope are "off"
        assert diagnostics.active() is None
        upkeep = LspUpkeep(scope, settings=LspSettings(enabled=False))
        upkeep.start()
        upkeep.trust("/anywhere")
        assert diagnostics.active() is None and upkeep.manager is None
        assert await upkeep.release("/anywhere") == 0
        await upkeep.shutdown()

    asyncio.run(main())


def test_a_session_folder_is_trusted_by_its_work_tree(scope: Scope, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    loose = tmp_path / "loose"
    loose.mkdir()

    async def main() -> None:
        upkeep = LspUpkeep(scope, settings=ON)
        upkeep.start()
        manager = upkeep.manager
        assert manager is not None
        upkeep.trust(repo / "src")
        upkeep.trust(loose)
        assert manager.trusted(str(repo / "lib")) and manager.trusted(str(loose))
        assert not manager.trusted(str(tmp_path / "other"))
        await upkeep.shutdown()

    asyncio.run(main())


def test_the_service_joins_the_tier_trusts_the_folder_and_leaves_at_close(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    server = FakeModelServer(script_of(), clock=fake_clock)
    upkeep = LspUpkeep(scope, settings=ON)

    async def main() -> None:
        svc = SessionService(
            scope,
            fake_route(server),
            toolsets=[],
            options=fake_options(server, session_id=None),
            lsp=upkeep,
        )
        assert diagnostics.active() is None  # nothing until a session is attached
        await svc.create(SessionCreateParams(cwd=str(repo / "pkg")))
        manager = upkeep.manager
        assert manager is not None and diagnostics.active() is not None
        assert manager.trusted(str(repo / "other"))
        await svc.aclose()
        assert diagnostics.active() is None

    asyncio.run(main())


def test_a_session_created_in_a_project_folder_joins_it(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    app = tmp_path / "app"
    (app / "src").mkdir(parents=True)
    store = ProjectStore(scope)
    try:
        made = store.create("app", folders=[app])
    finally:
        store.close()
    server = FakeModelServer(script_of(), clock=fake_clock)

    async def main() -> tuple[str, str, str]:
        svc = SessionService(
            scope, fake_route(server), toolsets=[], options=fake_options(server, session_id=None)
        )
        inside = await svc.create(SessionCreateParams(cwd=str(app / "src")))
        outside = await svc.create(SessionCreateParams(cwd=str(tmp_path)))
        bare = await svc.create(SessionCreateParams())
        await svc.aclose()
        return inside.session_id, outside.session_id, bare.session_id

    inside, outside, bare = asyncio.run(main())
    store = ProjectStore(scope)
    try:
        joined = store.session_project(inside)
        assert joined is not None and joined.id == made.id
        assert store.session_project(outside) is None and store.session_project(bare) is None
    finally:
        store.close()


def test_a_profile_without_projects_gets_no_projects_file(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    server = FakeModelServer(script_of(), clock=fake_clock)

    async def main() -> None:
        svc = SessionService(
            scope, fake_route(server), toolsets=[], options=fake_options(server, session_id=None)
        )
        await svc.create(SessionCreateParams(cwd=str(tmp_path)))
        await svc.aclose()

    asyncio.run(main())
    assert not (scope.home / "projects.db").exists()
