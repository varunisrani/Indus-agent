"""MCP servers through the real CLI session host (P13-05..12 integration).

``cli.runtime.build_service`` → ``SessionService(mcp=McpUpkeep)``: the profile's servers start
before the first agent, their tools join the platform bundle, a model's call reaches the real stdio
server (with ``MEDIA:`` directives in its text neutralised), ``/mcp`` shows them, ``/reload-mcp``
adds a server without touching the running session's stored tool array until its next compaction
(a new session gets it at once), and closing the host closes the servers.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.cli.args import HostOverrides
from indus.cli.runtime import ServiceSpec, make_host
from indus.cli.session_select import LaunchPolicy
from indus.cli.slash.dispatch import dispatch
from indus.cli.slash.port import HostPort
from indus.foundation.clock import SystemClock
from indus.foundation.scope import EnvView, Scope, make_scope
from indus.foundation.secrets import open_secret_store
from indus.mcp import discovery
from indus.protocol.methods import SessionCreateParams
from indus.service import mcp_host
from indus.state import registry
from tests.fakes.agent import any_window
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn, tool_batch
from tests.unit.cli.slash._fakes import make_ctx, with_config

WAIT = 30.0
FAKE = Path(__file__).resolve().parents[2] / "fakes" / "mcp" / "stdio_server.py"


@pytest.fixture(autouse=True)
def in_tmp(tmp_path: Path) -> Iterator[None]:
    here = os.getcwd()
    os.chdir(tmp_path)
    yield
    os.chdir(here)


def real_scope(isolated_home: Path) -> Scope:
    """A profile on the system clock: the MCP connect and call bounds run in real time."""
    home = isolated_home / "profiles" / "work"
    home.mkdir(parents=True, exist_ok=True)
    (home / "profile.yaml").touch()
    env = EnvView.from_process().with_overlay({"INDUS_HOME": str(home)})
    scope = make_scope(home, env=env, clock=SystemClock())
    return scope.with_secrets(open_secret_store(home, scope.profile_id, mode="file"))


def server_yaml(*names: str, confirm: bool = True) -> str:
    lines = [
        "approvals:",
        f"  mcp_reload_confirm: {'true' if confirm else 'false'}",
        "mcp_servers:",
    ]
    for name in names:
        lines += [
            f"  {name}:",
            f"    command: {json.dumps(sys.executable)}",
            f"    args: [{json.dumps(str(FAKE))}, '--name', '{name}']",
            "    connect_timeout: 30",
        ]
    return "\n".join(lines) + "\n"


def overrides(server: FakeModelServer) -> HostOverrides:
    return HostOverrides(
        route=fake_route(server),
        client_factory=lambda route: server.client(),
        list_models=any_window,
    )


def test_cli_sessions_get_mcp_tools_and_reload_follows_the_config(
    isolated_home: Path, tmp_path: Path
) -> None:
    scope = with_config(real_scope(isolated_home), server_yaml("files"))
    secret = "/home/me/.ssh/id_ed25519"
    model = FakeModelServer(
        script_of(
            tool_batch(("mcp__files__echo", {"text": f"see MEDIA:{secret}"})),
            *[text_turn("done")] * 4,
        )
    )
    store = registry.acquire(scope)
    spec = ServiceSpec(
        scope=scope,
        route=fake_route(model),
        policy=LaunchPolicy(cwd=str(tmp_path)),
        store=store,
        overrides=overrides(model),
    )
    host = make_host(spec, call_timeout=WAIT)
    try:
        with host:
            service = host.service
            sid = host.create(SessionCreateParams(cwd=str(tmp_path))).session_id
            agent = host.run(lambda: service.agent(sid))
            upkeep = mcp_host.upkeep_for(scope)
            assert upkeep is not None and service.mcp is upkeep
            assert upkeep.report is not None and upkeep.report.connected == ["files"]
            assert upkeep.watcher is not None  # interactive run: the config is watched
            assert discovery.host_for(scope) is upkeep.host
            # The platform bundle gained the umbrella toolset; the tools are in the first array.
            assert agent.toolset_selection == ["indus-cli", "mcp"]
            assert "mcp__files__echo" in agent.tool_names
            stored = json.dumps(agent.runtime.tools, sort_keys=True)
            assert "mcp__files__echo" in stored

            # The model calls the server; the server's MEDIA directive does not survive.
            host.submit(sid, "echo something")
            host.wait(sid, timeout=WAIT)
            rows = host.call(service.store.load(sid))
            [answer] = [r for r in rows if r.role == "tool"]
            assert secret in str(answer.content)
            assert "media:" not in str(answer.content).lower()
            assert "[MEDIA directive from the MCP server removed]" in str(answer.content)

            # /mcp: the live table.
            port = HostPort(host, scope)
            ctx = make_ctx(scope, port=port, session_id=sid)
            dispatch("/mcp", ctx)
            [row] = [line for line in ctx.lines if line.startswith("files")]
            # 5 tools and the 4 resource/prompt utilities
            assert "ready" in row and row.split()[-1] == "9"

            # A second server in the config; /reload-mcp asks first.
            scope_now = with_config(scope, server_yaml("files", "notes"))
            assert scope_now.home == scope.home
            refused = make_ctx(scope, port=port, session_id=sid, confirm=lambda q: False)
            dispatch("/reload-mcp", refused)
            assert refused.lines == ["MCP reload cancelled; nothing changed."]
            unattended = make_ctx(scope, port=port, session_id=sid, interactive=False)
            dispatch("/reload-mcp", unattended)
            assert "approvals.mcp_reload_confirm: false" in unattended.lines[0]
            asked: list[str] = []
            yes = make_ctx(
                scope, port=port, session_id=sid, confirm=lambda q: asked.append(q) or True
            )
            dispatch("/reload-mcp", yes)
            assert asked and "prompt cache" in asked[0]
            assert yes.lines[0].startswith("MCP servers reloaded: added: notes")
            # The running session keeps its stored tool array (R07.23) ...
            assert json.dumps(agent.runtime.tools, sort_keys=True) == stored
            assert "mcp__notes__echo" not in agent.tool_names
            # ... until its next compaction rebuilds it.
            host.run(lambda: [hook() for hook in agent.runtime.on_compaction])
            assert "mcp__notes__echo" in agent.tool_names
            assert "mcp__notes__echo" in json.dumps(agent.runtime.tools)
            # A new session gets the new tools at once.
            other = host.create(SessionCreateParams(cwd=str(tmp_path))).session_id
            assert "mcp__notes__echo" in host.run(lambda: service.agent(other).tool_names)
            mcp = upkeep.host
            assert mcp is not None and set(mcp.handles) == {"files", "notes"}
            managed = [h.managed for h in mcp.handles.values()]
        # Host exit: servers closed, nothing left registered for the profile.
        assert all(m.state.value == "closed" for m in managed), [m.state for m in managed]
        assert mcp_host.upkeep_for(scope) is None
        assert discovery.host_for(scope) is None
    finally:
        registry.release(store)


def test_a_profile_without_servers_starts_no_mcp_host(isolated_home: Path, tmp_path: Path) -> None:
    scope = with_config(real_scope(isolated_home), "model:\n  default: fake\n")
    model = FakeModelServer(script_of(text_turn("hi")))
    store = registry.acquire(scope)
    spec = ServiceSpec(
        scope=scope,
        route=fake_route(model),
        policy=LaunchPolicy(cwd=str(tmp_path)),
        store=store,
        overrides=overrides(model),
    )
    try:
        with make_host(spec, call_timeout=WAIT) as host:
            sid = host.create(SessionCreateParams(cwd=str(tmp_path))).session_id
            agent = host.run(lambda: host.service.agent(sid))
            upkeep = mcp_host.upkeep_for(scope)
            assert upkeep is not None and upkeep.host is None and upkeep.watcher is None
            assert agent.toolset_selection == ["indus-cli"]
            ctx = make_ctx(scope, port=HostPort(host, scope), session_id=sid)
            dispatch("/mcp", ctx)
            assert ctx.lines[0] == "No MCP servers are configured."
            ctx.lines.clear()
            dispatch("/reload-mcp", ctx)
            assert ctx.lines == ["No MCP servers are configured."]
        assert mcp_host.upkeep_for(scope) is None
    finally:
        registry.release(store)
