"""P24 demo: an edit that introduces a type error is caught by the LSP stop gate (D-16.799 wiring).

Everything here is the shipped path: a ``SessionService`` with the host's ``LspUpkeep`` (the
process's language-server tier), an ``Agent`` whose tool seam is wrapped in ``LspToolHooks`` and
whose loop asks the standard stop gates, the real ``write_file`` tool and its lint step, and a
language server over stdio (``tests/fakes/lsp_server.py``: ``BAD(name)`` is an error). Only the
model is scripted.

The server publishes its verdict 0.3 s after an edit, so the edit tool (0.08 s budget) returns
without it. The model then says it is done; the LSP gate waits for the outstanding check, finds the
new error and sends the agent back with it as a durable row after the interim answer; the model
fixes the file and its next answer stands.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from indus.core.exit_reasons import ExitReason
from indus.core.stop_gates import FINISH_REASON
from indus.core.stop_gates_lsp import HEADER
from indus.foundation.clock import SystemClock
from indus.foundation.scope import EnvView, Scope, make_scope
from indus.foundation.secrets import open_secret_store
from indus.protocol.methods import SessionCreateParams
from indus.service.lsp_host import LspUpkeep
from indus.service.session_service import SessionService
from indus.tools.builtin import write_file
from indus.tools.lsp import diagnostics
from indus.tools.lsp.manager import LspSettings
from indus.tools.lsp.servers import ServerSpec
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import fake_options
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn, tool_batch
from tests.fakes.turn import check_alternation

FAKE_SERVER = Path(__file__).resolve().parents[2] / "fakes" / "lsp_server.py"
BROKEN = "total = add(1, 2)\nlabel: int = BAD(str_assigned_to_int)\n"
FIXED = "total = add(1, 2)\nlabel: int = 3\n"


def real_scope(isolated_home: Path) -> Scope:
    """A profile on the real clock: the language server is a real child process."""
    home = isolated_home / "profiles" / "work"
    home.mkdir(parents=True, exist_ok=True)
    (home / "profile.yaml").touch()
    env = EnvView.from_process().with_overlay({"INDUS_HOME": str(home)})
    scope = make_scope(home, env=env, clock=SystemClock())
    return scope.with_secrets(open_secret_store(home, scope.profile_id, mode="file"))


def server_spec(tmp_path: Path) -> ServerSpec:
    script = tmp_path / "server.json"
    script.write_text(json.dumps({"push_delay": 0.3}), encoding="utf-8")
    argv = (sys.executable, str(FAKE_SERVER), str(script))
    return ServerSpec("fake-typecheck", (".fk",), (), command=argv, project_code=False)


def bodies(server: FakeModelServer) -> list[dict[str, Any]]:
    return [dict(r.body or {}) for r in server.model_requests]


def text_of(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content or "")


def test_lsp_stop_gate_catches_a_type_error_and_the_turn_goes_on_to_a_fix(
    isolated_home: Path, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    target = repo / "calc.fk"
    scope = real_scope(isolated_home)
    model = FakeModelServer(
        script_of(
            tool_batch(("write_file", {"path": str(target), "content": BROKEN})),
            text_turn("Done: calc.fk is written."),
            tool_batch(("write_file", {"path": str(target), "content": FIXED})),
            text_turn("Fixed the type error; calc.fk is done."),
        )
    )
    registry = ToolRegistry(clock=scope.clock)
    write_file.register_tools(registry)
    upkeep = LspUpkeep(scope, settings=LspSettings(servers=(server_spec(tmp_path),)))

    async def scenario() -> tuple[Any, list[Any], int]:
        service = SessionService(
            scope,
            fake_route(model),
            toolsets=["file"],
            options=fake_options(model, registry=registry, session_id=None),
            lsp=upkeep,
        )
        try:
            created = await service.create(SessionCreateParams(cwd=str(repo)))
            assert diagnostics.active() is not None  # the host installed the tier
            ticket = await service.submit(created.session_id, "Write calc.fk.")
            result = await asyncio.wait_for(service.wait(created.session_id, ticket.turn_id), 60)
            manager = upkeep.manager
            assert manager is not None
            started = len(manager.clients)
            return result, list(result.rows), started
        finally:
            await service.aclose()

    result, rows, started = asyncio.run(scenario())

    assert started == 1  # one language server for (server, repo)
    assert diagnostics.active() is None  # the last service out stopped and uninstalled it
    assert result.exit_reason == ExitReason.COMPLETED
    assert result.final_text == "Fixed the type error; calc.fk is done."
    assert target.read_text(encoding="utf-8") == FIXED

    # The interim answer, then the gate's durable message naming the new error, then the fix.
    turn = [r for r in rows if not r.scaffold]
    interim = next(r for r in turn if r.finish_reason == FINISH_REASON)
    assert interim.role == "assistant" and interim.content == "Done: calc.fk is written."
    nudge = turn[turn.index(interim) + 1]
    assert nudge.role == "user" and nudge.content.startswith(HEADER)
    assert "bad str_assigned_to_int" in nudge.content and "calc.fk" in nudge.content
    check_alternation(turn)

    # The model saw the error before its third request and wrote the fix in it.
    requests = bodies(model)
    assert len(requests) == 4
    third = requests[2]["messages"]
    assert "bad str_assigned_to_int" in text_of(third[-1])
    assert third[-2]["role"] == "assistant" and text_of(third[-2]) == interim.content
    # The fix introduced nothing new: the last answer stood without another gate message.
    assert "bad" not in text_of(requests[3]["messages"][-1]).lower()
