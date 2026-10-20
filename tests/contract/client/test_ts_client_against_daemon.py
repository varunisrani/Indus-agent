"""P17-20: the TypeScript client (``@indus/client``) against a real daemon WebSocket endpoint.

The daemon half is the real thing on loopback: ``WsEndpoint`` (uvicorn + wsproto) over a
``DaemonSessionHost`` on the fake model, with the ticket book the control socket would mint from.
The client half is ``tests/contract/client/ts_client_script.ts`` run by Node (type stripping, no
build step). Tickets reach Node only on its stdin, one fresh ticket per dial.

The script attaches, creates a session, submits a prompt, answers the approval the turn raises,
then loses its socket while the turn is parked in a tool. The turn finishes while nobody watches;
the script reconnects with a new ticket, re-subscribes from its cursor and must see every event
of the session exactly once, in order, ending with ``turn.done``.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.ws.server import WsEndpoint
from indus.service.ws_auth import SUBPROTOCOL, TICKET_PROTOCOL_PREFIX, WS_INTERACTIVE, TicketBook
from indus.state import registry
from indus.state.registry import StoreHandle
from tests.fakes.agent import eventually
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.session_host import Holder, confirm, open_host
from tests.unit.backends._aio import aio

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tests" / "contract" / "client" / "ts_client_script.ts"
CLIENT_DEPS = ROOT / "packages" / "client" / "node_modules" / "@indus" / "protocol"


def _node() -> str | None:
    node = shutil.which("node")
    if node is None:
        return None
    try:
        version = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.match(r"v(\d+)\.(\d+)", version)
    if found is None:
        return None
    major, minor = int(found[1]), int(found[2])
    # TypeScript runs without a build from Node 22.18 (type stripping on by default).
    return node if (major, minor) >= (22, 18) else None


NODE = _node()
HAS_WS = any(importlib.util.find_spec(name) for name in ("wsproto", "websockets"))

pytestmark = [
    pytest.mark.skipif(NODE is None, reason="needs Node >= 22.18 on PATH"),
    pytest.mark.skipif(not CLIENT_DEPS.exists(), reason="pnpm install has not run"),
    pytest.mark.skipif(not HAS_WS, reason="no WebSocket implementation for the server"),
]


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


def _grant(book: TicketBook, url: str) -> tuple[dict[str, Any], str]:
    ticket = book.mint(WS_INTERACTIVE).ticket
    return {"url": url, "protocols": [SUBPROTOCOL, TICKET_PROTOCOL_PREFIX + ticket]}, ticket


class Script:
    """The Node process: JSON lines in, JSON lines out."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.lines: list[dict[str, Any]] = []
        self._unclaimed: list[dict[str, Any]] = []

    async def send(self, value: dict[str, Any]) -> None:
        stdin = self.process.stdin
        assert stdin is not None
        stdin.write((json.dumps(value) + "\n").encode())
        await stdin.drain()

    async def expect(self, step: str, timeout: float = 30.0) -> dict[str, Any]:
        """The next line of ``step`` (lines may come in another order than asked for)."""
        for index, line in enumerate(self._unclaimed):
            if line.get("step") == step:
                return self._unclaimed.pop(index)
        stdout = self.process.stdout
        assert stdout is not None
        while True:
            raw = await asyncio.wait_for(stdout.readline(), timeout)
            if not raw:
                err = b""
                if self.process.stderr is not None:
                    err = await self.process.stderr.read()
                raise AssertionError(f"the script ended before {step!r}: {err.decode()[-2000:]}")
            line = json.loads(raw)
            self.lines.append(line)
            if line.get("step") == step:
                return line
            self._unclaimed.append(line)


@aio
async def test_ts_client_attaches_answers_and_resumes_from_its_cursor(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    assert NODE is not None
    holder = Holder()
    host = await open_host(
        scope,
        store,
        tmp_path,
        tool_batch(("confirm", {})),
        tool_batch(("hold", {})),
        text_turn("all ", "done"),
        tools={"confirm": confirm, "hold": holder},
    )
    book = TicketBook.for_home(scope.home)
    endpoint = WsEndpoint(host, book, scope=scope, strict=True)
    url = await endpoint.start()
    tickets: list[str] = []
    process = await asyncio.create_subprocess_exec(
        NODE,
        str(SCRIPT),
        cwd=str(ROOT),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    script = Script(process)
    try:
        first, ticket = _grant(book, url)
        tickets.append(ticket)
        await script.send({"grant": first})
        approval = await script.expect("approval")
        assert approval["request_id"].startswith("srq-") and approval["generation"] >= 1
        submitted = await script.expect("submitted")
        sid = submitted["session_id"]
        await eventually(lambda: holder.entered, limit=20)

        await script.send({"cut": True})
        cut = await script.expect("cut")
        await eventually(lambda: host.state(sid).viewers == (), limit=10)
        holder.gate.set()
        finished = await host.wait(submitted["turn_id"])
        assert finished.outcome == "completed"

        second, ticket = _grant(book, url)
        tickets.append(ticket)
        await script.send({"grant": second})
        done = await script.expect("done", timeout=40)
        assert await asyncio.wait_for(process.wait(), 20) == 0
    finally:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        await endpoint.stop()
        await host.aclose()

    # The approval was answered once, over the wire, with its generation.
    assert done["asked"] == 1
    # Every event of the session exactly once, in order, across the reconnect.
    seqs = done["seqs"]
    assert seqs == list(range(1, len(seqs) + 1)), seqs
    assert cut["cursor"] < seqs[-1]
    assert done["last_cursor"] == seqs[-1]
    assert done["text"] == "all done"
    assert done["readies"] == 2
    assert endpoint.violations == []
    # Tickets never left the subprotocol list: not in the client's log, not in its output.
    stdout_text = json.dumps(script.lines)
    for ticket in tickets:
        assert ticket not in stdout_text
        assert ticket not in "\n".join(done["logs"])
