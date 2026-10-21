"""P24-15: the manager (catalogue, gates, single flight, broken set, idle reaping, child env)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from indus.foundation.clock import SystemClock
from indus.foundation.scope import EnvView, make_scope
from indus.tools.lsp import servers
from indus.tools.lsp.client import LspStartError
from indus.tools.lsp.manager import (
    DEFAULT_IDLE_S,
    MIN_IDLE_S,
    LspManager,
    LspSettings,
    describe_servers,
    idle_timeout,
)
from indus.tools.lsp.servers import (
    CATALOGUE,
    ServerSpec,
    find_git_root,
    language_id,
    match_key,
    merge_config,
    project_root,
    resolve_command,
)
from tests.conftest import FAKE_CLOCK_START
from tests.fakes.agent import DictConfig
from tests.fakes.clock import FakeClock, checkpoint
from tests.unit.tools.lsp._helpers import FAKE_SERVER, git_tree, server_argv


class FakeClient:
    """Stands in for ``LspClient`` (the manager only starts, asks and stops it)."""

    created: list[FakeClient] = []

    def __init__(self, server_id: str, root: str, argv: Sequence[str], **kw: Any) -> None:
        self.server_id, self.root, self.argv, self.kw = server_id, root, list(argv), kw
        self.started = 0
        self.stopped = 0
        self.alive = False
        self.fail: str | None = None
        self.gate: asyncio.Event | None = None
        FakeClient.created.append(self)

    @property
    def running(self) -> bool:
        return self.alive

    @property
    def state(self) -> str:
        return "running" if self.alive else "stopped"

    @property
    def pid(self) -> int | None:
        return None

    def open_paths(self) -> list[str]:
        return []

    def failure_details(self) -> str:
        return ""

    async def start(self) -> None:
        self.started += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise LspStartError(self.fail)
        self.alive = True

    async def shutdown(self) -> None:
        self.stopped += 1
        self.alive = False


@pytest.fixture(autouse=True)
def fresh_clients() -> None:
    FakeClient.created = []


SPEC = ServerSpec(
    "fake",
    (".fk",),
    ("fake-ls",),
    markers=("fake.toml",),
    install_hint="get fake-ls",
    project_code=False,
)


def settings(**changes: Any) -> LspSettings:
    base: dict[str, Any] = {"servers": (SPEC, *CATALOGUE)}
    base.update(changes)
    return LspSettings(**base)


def env_with_fake_binary(tmp_path: Path) -> Any:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    tool = bin_dir / "fake-ls"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)

    def factory(extra: Mapping[str, str]) -> dict[str, str]:
        return {"PATH": str(bin_dir), **extra}

    return factory


def manager(tmp_path: Path, clock: Any = None, **changes: Any) -> LspManager:
    return LspManager(
        settings(**changes),
        clock=clock or SystemClock(),
        env_factory=env_with_fake_binary(tmp_path),
        client_factory=cast(Any, FakeClient),
    )


# -- catalogue ---------------------------------------------------------------------------------


def test_catalogue_matching_and_language_ids() -> None:
    assert match_key("/x/Dockerfile") == "dockerfile"
    assert match_key("/x/a.PY") == ".py"
    found = {
        p: servers.server_for(p, CATALOGUE) for p in ("a.py", "b.tsx", "c.go", "d.rs", "Dockerfile")
    }
    assert {p: s.server_id if s else None for p, s in found.items()} == {
        "a.py": "pyright",
        "b.tsx": "typescript",
        "c.go": "gopls",
        "d.rs": "rust-analyzer",
        "Dockerfile": "dockerfile-language-server",
    }
    assert servers.server_for("notes.md", CATALOGUE) is None
    assert language_id("x.tsx") == "typescriptreact" and language_id("x.unknown") == "plaintext"
    assert all(spec.install_hint for spec in CATALOGUE)
    assert {s.server_id for s in CATALOGUE if not s.project_code} >= {"pyright", "clangd"}


def test_project_root_walks_markers_but_skips_python_packages(tmp_path: Path) -> None:
    repo = git_tree(tmp_path / "repo")
    (repo / "svc").mkdir()
    (repo / "svc" / "pyproject.toml").write_text("", encoding="utf-8")
    pkg = repo / "svc" / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "setup.py").write_text("", encoding="utf-8")
    markers = ("pyproject.toml", "setup.py")
    assert find_git_root(str(pkg / "m.py")) == str(repo)
    assert project_root(str(pkg / "m.py"), str(repo), markers) == str(repo / "svc")
    assert project_root(str(repo / "top.py"), str(repo), markers) == str(repo)
    assert project_root(str(pkg / "m.py"), str(repo), ()) == str(repo)


def test_config_adjusts_known_servers_and_adds_new_ones() -> None:
    merged = merge_config(
        {
            "gopls": {"disabled": True},
            "mine": {"command": ["my-ls", "--stdio"], "extensions": [".MY"], "language_id": "my"},
            "broken": {"command": "not-a-list", "extensions": [".x"]},
            "noise": "not a mapping",
        }
    )
    assert merged[0].server_id == "mine" and merged[0].handles == (".my",)
    assert merged[0].project_code and merged[0].language == "my"
    gopls = next(s for s in merged if s.server_id == "gopls")
    assert gopls.disabled
    assert all(s.server_id not in ("broken", "noise") for s in merged)
    assert len(merged) == len(CATALOGUE) + 1


def test_resolve_command_uses_the_children_path_only(tmp_path: Path) -> None:
    factory = env_with_fake_binary(tmp_path)
    path = factory({})["PATH"]
    assert resolve_command(SPEC, path) == [str(tmp_path / "bin" / "fake-ls")]
    assert resolve_command(SPEC, str(tmp_path / "empty")) is None
    configured = ServerSpec("c", (".c",), (), command=(sys.executable, "-m", "x"))
    assert resolve_command(configured, "") == [sys.executable, "-m", "x"]


def test_idle_floor_is_30_seconds(tmp_path: Path) -> None:
    assert idle_timeout(5) == MIN_IDLE_S == 30.0
    assert idle_timeout(0) == 0.0 and idle_timeout(-3) == 0.0
    assert idle_timeout(900) == 900.0
    home = tmp_path / "h"
    home.mkdir()
    base = make_scope(home, env={"PATH": "/usr/bin"})
    assert LspSettings.from_scope(base).idle_timeout == DEFAULT_IDLE_S == 600.0
    low = base.with_config(DictConfig({"lsp": {"idle_timeout": 5, "trusted_workspaces": ["~/w"]}}))
    found = LspSettings.from_scope(low)
    assert found.idle_timeout == 30.0
    assert found.trusted_workspaces == (os.path.expanduser("~/w"),)


# -- gates -------------------------------------------------------------------------------------


def test_files_outside_a_git_tree_get_no_server(tmp_path: Path) -> None:
    async def scenario() -> None:
        lsp = manager(tmp_path)
        loose = tmp_path / "loose"
        loose.mkdir()
        assert not lsp.handles(str(loose / "a.fk"))
        assert await lsp.client_for(str(loose / "a.fk")) is None
        assert FakeClient.created == []
        await lsp.close()

    asyncio.run(scenario())


def test_disabled_lsp_and_disabled_servers(tmp_path: Path) -> None:
    repo = git_tree(tmp_path / "repo")

    async def scenario() -> None:
        off = manager(tmp_path, enabled=False)
        assert await off.client_for(str(repo / "a.fk")) is None
        from dataclasses import replace

        no_fake = manager(tmp_path, servers=(replace(SPEC, disabled=True),))
        assert await no_fake.client_for(str(repo / "a.fk")) is None
        assert FakeClient.created == []

    asyncio.run(scenario())


def test_untrusted_project_code_server_is_skipped_until_trusted(tmp_path: Path) -> None:
    repo = git_tree(tmp_path / "repo")
    from dataclasses import replace

    risky = replace(SPEC, project_code=True)

    async def scenario() -> None:
        lsp = manager(tmp_path, servers=(risky,))
        assert not lsp.handles(str(repo / "a.fk"))
        assert await lsp.client_for(str(repo / "a.fk")) is None
        assert lsp.status().untrusted == [{"server": "fake", "root": str(repo)}]
        lsp.trust(str(repo))
        client = await lsp.client_for(str(repo / "a.fk"))
        assert client is not None and lsp.status().untrusted == []
        listed = manager(tmp_path, servers=(risky,), trusted_workspaces=(str(tmp_path),))
        assert await listed.client_for(str(repo / "b.fk")) is not None
        await lsp.close()
        await listed.close()

    asyncio.run(scenario())


# -- spawning ----------------------------------------------------------------------------------


def test_concurrent_callers_share_one_spawn(tmp_path: Path) -> None:
    repo = git_tree(tmp_path / "repo")
    (repo / "sub").mkdir()
    (repo / "sub" / "fake.toml").write_text("", encoding="utf-8")

    async def scenario() -> None:
        lsp = manager(tmp_path)
        gate = asyncio.Event()
        original = FakeClient.__init__

        def gated(self: FakeClient, *a: Any, **kw: Any) -> None:
            original(self, *a, **kw)
            self.gate = gate

        FakeClient.__init__ = gated  # type: ignore[method-assign]
        try:
            first = asyncio.ensure_future(lsp.client_for(str(repo / "sub" / "a.fk")))
            second = asyncio.ensure_future(lsp.client_for(str(repo / "sub" / "b.fk")))
            for _ in range(5):
                await checkpoint()
            gate.set()
            a, b = await asyncio.gather(first, second)
        finally:
            FakeClient.__init__ = original  # type: ignore[method-assign]
        assert a is b and a is not None
        assert len(FakeClient.created) == 1 and FakeClient.created[0].started == 1
        assert a.root == str(repo / "sub")  # type: ignore[attr-defined]
        other = await lsp.client_for(str(repo / "top.fk"))
        assert other is not a and len(FakeClient.created) == 2
        assert sorted(c["root"] for c in lsp.status().clients) == [str(repo), str(repo / "sub")]
        await lsp.close()
        assert all(c.stopped == 1 for c in FakeClient.created)

    asyncio.run(scenario())


def test_failed_spawn_is_not_retried_until_restart(tmp_path: Path) -> None:
    repo = git_tree(tmp_path / "repo")

    async def scenario() -> None:
        lsp = manager(tmp_path)
        original = FakeClient.start

        async def failing(self: FakeClient) -> None:
            self.fail = "fake did not start: boom"
            await original(self)

        FakeClient.start = failing  # type: ignore[method-assign]
        try:
            assert await lsp.client_for(str(repo / "a.fk")) is None
            assert await lsp.client_for(str(repo / "b.fk")) is None
            assert len(FakeClient.created) == 1
            assert not lsp.handles(str(repo / "a.fk"))
            broken = lsp.status().broken
            assert broken == [
                {"server": "fake", "root": str(repo), "reason": "fake did not start: boom"}
            ]
        finally:
            FakeClient.start = original  # type: ignore[method-assign]
        assert await lsp.restart() == 0
        assert lsp.status().broken == []
        assert await lsp.client_for(str(repo / "a.fk")) is not None
        await lsp.close()

    asyncio.run(scenario())


def test_broken_pair_retried_after_broken_retry_s(tmp_path: Path) -> None:
    repo = git_tree(tmp_path / "repo")
    clock = FakeClock(FAKE_CLOCK_START)

    async def scenario() -> None:
        lsp = LspManager(
            settings(broken_retry_s=120.0),
            clock=clock,
            env_factory=lambda extra: {"PATH": str(tmp_path / "nothing")},
            client_factory=cast(Any, FakeClient),
        )
        assert await lsp.client_for(str(repo / "a.fk")) is None
        assert "get fake-ls" in lsp.status().broken[0]["reason"]  # the install hint
        clock.advance(119)
        assert not lsp.handles(str(repo / "a.fk"))
        clock.advance(1)
        assert lsp.handles(str(repo / "a.fk"))
        await lsp.close()

    asyncio.run(scenario())


def test_dead_client_is_replaced(tmp_path: Path) -> None:
    repo = git_tree(tmp_path / "repo")

    async def scenario() -> None:
        lsp = manager(tmp_path)
        first = await lsp.client_for(str(repo / "a.fk"))
        assert first is not None
        first.alive = False  # type: ignore[attr-defined]
        second = await lsp.client_for(str(repo / "a.fk"))
        assert second is not None and second is not first
        assert first.stopped == 1  # type: ignore[attr-defined]
        await lsp.close()

    asyncio.run(scenario())


def test_idle_server_reaped_after_600_seconds(tmp_path: Path) -> None:
    repo = git_tree(tmp_path / "repo")
    clock = FakeClock(FAKE_CLOCK_START)

    async def scenario() -> None:
        lsp = manager(tmp_path, clock=clock)
        assert lsp.settings.idle_timeout == 600.0
        busy = await lsp.client_for(str(repo / "a.fk"))
        assert busy is not None
        await clock.run_until_idle()
        for _ in range(9):  # 540 s
            await clock.advance_and_settle(60)
        assert busy.stopped == 0  # type: ignore[attr-defined]
        await clock.advance_and_settle(60)  # 600 s since its last use
        await clock.run_until_idle()
        assert busy.stopped == 1  # type: ignore[attr-defined]
        assert lsp.status().clients == []
        fresh = await lsp.client_for(str(repo / "a.fk"))
        assert fresh is not None and fresh is not busy
        await lsp.close()

    asyncio.run(scenario())


def test_touch_keeps_a_server_alive(tmp_path: Path) -> None:
    repo = git_tree(tmp_path / "repo")
    clock = FakeClock(FAKE_CLOCK_START)

    async def scenario() -> None:
        lsp = manager(tmp_path, clock=clock)
        client = await lsp.client_for(str(repo / "a.fk"))
        await clock.run_until_idle()
        for _ in range(9):
            await clock.advance_and_settle(60)
        assert await lsp.client_for(str(repo / "a.fk")) is client  # used at 540 s
        for _ in range(9):
            await clock.advance_and_settle(60)
        assert client.stopped == 0  # type: ignore[union-attr]
        await lsp.close()

    asyncio.run(scenario())


# -- a real child -------------------------------------------------------------------------------


def test_server_env_comes_from_child_env_builder(tmp_path: Path) -> None:
    repo = git_tree(tmp_path / "repo")
    dump = tmp_path / "env.json"
    argv = server_argv(tmp_path, dump_env=str(dump))
    home = tmp_path / "profile"
    home.mkdir()
    provider_value = "sk" + "-" + "x" * 24
    env = EnvView.from_process().with_overlay(
        {"ANTHROPIC_API_KEY": provider_value, "INDUS_HOME": str(home)}
    )
    config = DictConfig(
        {
            "lsp": {
                "servers": {
                    "fake": {
                        "command": argv,
                        "extensions": [".fk"],
                        "env": {"FAKE_LS_FLAG": "on"},
                    }
                },
                "trusted_workspaces": [str(repo)],
            }
        }
    )
    scope = make_scope(home, env=env).with_config(config)

    async def scenario() -> None:
        lsp = LspManager(scope=scope, clock=SystemClock())
        client = await lsp.client_for(str(repo / "x.fk"))
        assert client is not None and client.running
        await lsp.close()
        assert not client.running

    asyncio.run(scenario())
    seen = json.loads(dump.read_text(encoding="utf-8"))
    assert "ANTHROPIC_API_KEY" not in seen
    assert provider_value not in json.dumps(seen)
    assert seen["FAKE_LS_FLAG"] == "on"
    assert seen["INDUS_HOME"] == str(home)  # the builder's pins


def test_describe_servers_marks_installed(tmp_path: Path) -> None:
    factory = env_with_fake_binary(tmp_path)
    rows = describe_servers((SPEC, *CATALOGUE[:2]), factory({})["PATH"])
    assert rows[0]["installed"] and rows[0]["command"].endswith("fake-ls")
    assert rows[0]["install_hint"] == "get fake-ls"
    assert FAKE_SERVER.exists()


# -- indus lsp ---------------------------------------------------------------------------------


def test_cli_status_hint_and_restart(scope: Any, tmp_path: Path) -> None:
    from indus.tools.lsp import diagnostics
    from indus.tools.lsp.diagnostics import EditDiagnostics
    from tests.unit.cli.commands._helpers_d import run

    status = run(scope, "lsp", "status", "--json")
    assert status.code == 0, status.err
    payload = json.loads(status.out)
    assert payload["enabled"] is True and payload["process"] is None
    ids = [row["server"] for row in payload["servers"]]
    assert ids[:2] == ["pyright", "typescript"] and "gopls" in ids
    human = run(scope, "lsp")
    assert human.code == 0 and "Language servers: on" in human.out
    assert "no language servers run in this process" in human.out

    hint = run(scope, "lsp", "install-hint", "pyright")
    assert hint.code == 0 and "npm install -g pyright" in hint.out
    unknown = run(scope, "lsp", "install-hint", "nope")
    assert unknown.code == 1 and "Unknown language server" in unknown.err

    quiet = run(scope, "lsp", "restart")
    assert quiet.code == 0 and "No language servers run in this process" in quiet.out

    repo = git_tree(tmp_path / "repo")
    lsp = manager(tmp_path)
    diagnostics.install_active(EditDiagnostics(lsp))
    try:
        assert asyncio.run(lsp.client_for(str(repo / "a.fk"))) is not None
        listed = json.loads(run(scope, "lsp", "status", "--json").out)
        assert listed["process"]["clients"][0]["server"] == "fake"
        restarted = run(scope, "lsp", "restart")
        assert "Stopped 1 language server(s)" in restarted.out
        assert FakeClient.created[0].stopped == 1
    finally:
        diagnostics.install_active(None)


def test_release_and_deleted_roots(tmp_path: Path) -> None:
    repo = git_tree(tmp_path / "repo")
    other = git_tree(tmp_path / "other")

    async def scenario() -> None:
        lsp = manager(tmp_path)
        mine = await lsp.client_for(str(repo / "a.fk"))
        theirs = await lsp.client_for(str(other / "a.fk"))
        assert mine is not None and theirs is not None
        assert await lsp.release(str(repo)) == 1
        assert mine.stopped == 1 and theirs.stopped == 0  # type: ignore[attr-defined]
        import shutil

        shutil.rmtree(other)
        assert await lsp.reap_idle() == [("fake", str(other))]
        assert theirs.stopped == 1  # type: ignore[attr-defined]
        await lsp.close()

    asyncio.run(scenario())
