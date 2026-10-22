"""``indus lsp restart`` and the CLI's worktree removals reach the daemon's language servers
(P24-15 follow-ups, wave 45, D-16.982 / D-16.983): the owner-only control verbs ``lsp-restart`` and
``lsp-release``, their params, the tier entry points behind them, and the CLI's client side."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.control_socket import KNOWN_VERBS, OWNER_ONLY_VERBS, ControlServer
from indus.service import lsp_control, lsp_host
from indus.service.gateway_control import ControlReply
from indus.service.lsp_control import RELEASE_VERB, RESTART_VERB, LspVerbs
from indus.service.lsp_host import LspUpkeep
from indus.tools.lsp import diagnostics
from indus.tools.lsp.manager import LspManager, LspSettings
from tests.unit.tools.lsp import test_manager as fakes
from tests.unit.tools.lsp._helpers import git_tree

ON = LspSettings()


@pytest.fixture(autouse=True)
def fresh_clients() -> None:
    fakes.FakeClient.created = []


@pytest.fixture
def short_home() -> Iterator[Path]:
    """A home whose socket path fits (pytest's tmp paths can be too long for sun_path)."""
    home = Path(tempfile.mkdtemp(prefix="ilsp-", dir="/tmp"))
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _fake_tier(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LspManager:
    """The tier ``LspUpkeep.start`` builds gets the fake-client manager of the manager tests."""
    made = fakes.manager(tmp_path)

    def build(settings: LspSettings, *, scope: Scope) -> LspManager:
        return made

    monkeypatch.setattr(lsp_host, "LspManager", build)
    return made


def test_the_verbs_are_known_and_owner_only(scope: Scope) -> None:
    names = set(LspVerbs(scope).handlers())
    assert names == {RESTART_VERB, RELEASE_VERB}
    assert names <= set(KNOWN_VERBS) and names <= OWNER_ONLY_VERBS


def test_without_a_tier_nothing_runs(scope: Scope) -> None:
    verbs = LspVerbs(scope)

    async def main() -> tuple[Any, Any]:
        return await verbs.restart({}), await verbs.release({"folder": "/work/tree"})

    assert asyncio.run(main()) == ({"stopped": 0, "running": False}, {"stopped": 0})


def test_bad_params_are_refused(scope: Scope) -> None:
    verbs = LspVerbs(scope)
    bad: list[dict[str, Any]] = [
        {},
        {"folder": "relative/tree"},
        {"folder": 7},
        {"folder": "/a\0b"},
        {"folder": "/" + "x" * lsp_control.MAX_FOLDER_CHARS},
        {"folder": "/work", "extra": True},
    ]

    async def main() -> list[Any]:
        found = [await verbs.release(p) for p in bad]
        found.append(await verbs.restart({"now": True}))
        return found

    for answer in asyncio.run(main()):
        assert answer["refused"] == "bad_params"


def test_restart_and_release_reach_the_tier(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_tier(monkeypatch, tmp_path)
    tree = git_tree(tmp_path / "repo" / ".worktrees" / "w1")
    repo = git_tree(tmp_path / "repo")
    verbs = LspVerbs(scope)

    async def main() -> list[Any]:
        upkeep = LspUpkeep(scope, settings=ON)
        upkeep.start()
        manager = upkeep.manager
        assert manager is not None
        await manager.client_for(str(tree / "a.fk"))
        await manager.client_for(str(repo / "b.fk"))
        released = await verbs.release({"folder": str(tree) + "/"})
        left = sorted(key[1] for key in manager.clients)
        restarted = await verbs.restart({})
        after = len(manager.clients)
        await upkeep.shutdown()
        return [released, left, restarted, after]

    released, left, restarted, after = asyncio.run(main())
    assert released == {"stopped": 1} and left == [str(repo)]
    assert restarted == {"stopped": 1, "running": True} and after == 0
    assert [c.stopped for c in fakes.FakeClient.created] == [1, 1]


def test_restart_and_release_reach_only_their_profile_s_tier(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two profiles on one host (SEC-14 with the wave-45 verbs): a profile's ``lsp-release`` and
    ``lsp-restart`` stop that profile's servers only; a subagent's worktree removal
    (``diagnostics.release_folder``) stops the folder's servers in every tier."""
    other_home = tmp_path / "profiles" / "other"
    other_home.mkdir(parents=True)
    other = replace(scope, home=other_home, profile_id="other", routed=True)
    built: dict[str, LspManager] = {}

    def build(settings: LspSettings, *, scope: Scope) -> LspManager:
        place = tmp_path / "fakes" / str(len(built))
        place.mkdir(parents=True)
        made = built[str(scope.home)] = fakes.manager(place)
        return made

    monkeypatch.setattr(lsp_host, "LspManager", build)
    tree = git_tree(tmp_path / "repo" / ".worktrees" / "w1")
    target = str(tree / "a.fk")

    async def main() -> list[Any]:
        mine, theirs = LspUpkeep(scope, settings=ON), LspUpkeep(other, settings=ON)
        mine.start()
        theirs.start()
        a, b = mine.manager, theirs.manager
        assert a is not None and b is not None and a is not b
        assert built == {str(scope.home): a, str(other_home): b}
        await a.client_for(target)
        await b.client_for(target)
        released = await LspVerbs(other).release({"folder": str(tree)})
        after_release = (len(a.clients), len(b.clients))
        await b.client_for(target)
        restarted = await LspVerbs(scope).restart({})
        after_restart = (len(a.clients), len(b.clients))
        await a.client_for(target)
        everywhere = await diagnostics.release_folder(str(tree))
        after_all = (len(a.clients), len(b.clients))
        await theirs.shutdown()
        await mine.shutdown()
        return [released, after_release, restarted, after_restart, everywhere, after_all]

    assert asyncio.run(main()) == [
        {"stopped": 1},
        (1, 0),
        {"stopped": 1, "running": True},
        (0, 1),
        2,
        (0, 0),
    ]


def test_the_cli_reaches_the_daemon_over_the_control_socket(
    scope: Scope, tmp_path: Path, short_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_tier(monkeypatch, tmp_path)
    tree = git_tree(tmp_path / "repo" / ".worktrees" / "w1")

    async def main() -> list[Any]:
        upkeep = LspUpkeep(scope, settings=ON)
        upkeep.start()
        manager = upkeep.manager
        assert manager is not None
        server = ControlServer(short_home, status=lambda: {"state": "running"}, env={})
        for verb, (handler, seconds) in LspVerbs(scope).handlers().items():
            server.register(verb, handler, timeout=seconds)
        await server.start()
        try:
            await manager.client_for(str(tree / "a.fk"))
            release = lsp_control.gateway_releaser(short_home)
            await asyncio.to_thread(release, str(tree))
            gone = len(manager.clients)
            await manager.client_for(str(tree / "a.fk"))
            restarted = await asyncio.to_thread(lsp_control.restart_in_gateway, short_home)
        finally:
            await server.stop()
            await upkeep.shutdown()
        return [gone, restarted]

    assert asyncio.run(main()) == [0, (1, True)]


def _reply(**values: Any) -> ControlReply:
    return ControlReply(**values)


def test_the_cli_falls_back_when_no_daemon_takes_the_verb(tmp_path: Path) -> None:
    asked: list[tuple[str, Any]] = []

    def query_with(reply: ControlReply) -> Any:
        def query(home: Path, verb: str, params: Any = None, **kw: Any) -> ControlReply:
            asked.append((verb, params))
            return reply

        return query

    home = tmp_path / "home"
    missing = query_with(_reply(ok=False, error="no_socket"))
    older = query_with(_reply(ok=False, error="unknown_verb"))
    refused = query_with(_reply(ok=True, result={"refused": "bad_params", "detail": "x"}))
    for query in (missing, older, refused):
        assert lsp_control.restart_in_gateway(home, query=query) is None
        assert lsp_control.release_in_gateway(home, "/w", query=query) is None
    idle = query_with(_reply(ok=True, result={"stopped": 0, "running": False}))
    assert lsp_control.restart_in_gateway(home, query=idle) == (0, False)
    lsp_control.gateway_releaser(home, query=idle)("tree")
    assert asked[-1] == (RELEASE_VERB, {"folder": str(Path("tree").resolve())})


def test_restart_lines_say_where_the_servers_stopped(scope: Scope) -> None:
    from indus.cli.commands.lsp import restart_lines

    def daemon(home: Path, verb: str, params: Any = None, **kw: Any) -> ControlReply:
        return ControlReply(True, result={"stopped": 2, "running": True})

    def idle(home: Path, verb: str, params: Any = None, **kw: Any) -> ControlReply:
        return ControlReply(True, result={"stopped": 0, "running": False})

    assert restart_lines(scope, query=daemon) == [
        "Stopped 2 language server(s) in the gateway; the next edit starts them again."
    ]
    assert "runs no language servers for this profile" in restart_lines(scope, query=idle)[0]
    assert "No language servers run in this process" in restart_lines(scope)[0]
